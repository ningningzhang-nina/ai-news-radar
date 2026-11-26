"""Manual, auditable AI news collection and ranking. Python standard library only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
UTC = timezone.utc
NOW = datetime.now(UTC)
UA = "AI-News-Radar/1.0 (public news monitoring)"
MONTH_DATE = r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.? \d{1,2}, \d{4}\b"


def date(value):
    if not isinstance(value, str) or not value:
        return None
    for parse in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        parsedate_to_datetime,
        lambda x: datetime.strptime(x.replace(".", ""), "%b %d, %Y"),
        lambda x: datetime.strptime(x, "%B %d, %Y"),
    ):
        try:
            parsed = parse(value.strip())
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def clean(value):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def canonical(url):
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return ""
    host = parts.netloc.lower().removeprefix("www.")
    if host in {"twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        host = "x.com"
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")
             and k not in {"ref", "ref_src", "s", "t", "si", "feature"}]
    if host == "youtu.be":
        return "https://youtube.com/watch?" + urlencode({"v": parts.path.strip("/")})
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def domain(url):
    return urlsplit(url).hostname or ""


def trusted_domain(url):
    host = domain(url)
    return any(host == d or host.endswith("." + d) for d in CONFIG["evidence_domains"])


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not canonical(newurl):
            raise ValueError("non-HTTPS redirect")
        if req.headers.get("Authorization"):
            raise ValueError("credentialed request redirected")
        if getattr(req, "evidence_fetch", False) and not trusted_domain(newurl):
            raise ValueError("redirect outside evidence allowlist")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and getattr(req, "evidence_fetch", False):
            redirected.evidence_fetch = True
        return redirected


def fetch(url, *, params=None, token=None, payload=None, evidence=False):
    if evidence and not trusted_domain(url):
        raise ValueError("outside evidence allowlist")
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    request.evidence_fetch = evidence
    with build_opener(SafeRedirect()).open(request, timeout=12 if payload is None else 110) as response:
        raw = response.read(12_000_001)
        if len(raw) > 12_000_000:
            raise ValueError("response too large")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def fetch_json(url, **kwargs):
    return json.loads(fetch(url, **kwargs))


def error_label(exc):
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


@dataclass
class Item:
    title: str
    url: str
    published: str
    source: str
    role: str
    excerpt: str = ""
    metrics: dict = field(default_factory=dict)
    references: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    id: str = ""
    verification: str = "unchecked"


def item(title, url, published, source, role, excerpt="", metrics=None, refs=None):
    timestamp = date(published)
    link = canonical(url)
    if not link or not clean(title) or not timestamp:
        return None
    return Item(clean(title), link, timestamp.isoformat(), source, role, clean(excerpt)[:1600],
                metrics or {}, [u for ref in refs or [] if (u := canonical(ref))], [source])


def local(tag):
    return tag.rsplit("}", 1)[-1]


def xml_field(node, *names):
    for name in names:
        for child in node:
            if local(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def feed(spec):
    root = ET.fromstring(fetch(spec["url"]))
    nodes = [node for node in root.iter() if local(node.tag) in {"entry", "item"}]
    results = []
    for node in nodes[:180]:
        link = next((c.get("href") or c.text for c in node
                     if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate"), "")
        description = xml_field(node, "description", "summary", "content")
        metrics = {}
        if spec["role"] == "video":
            group = next((c for c in node if local(c.tag) == "group"), None)
            if group is not None:
                description = xml_field(group, "description")
                stats = next((c for c in group.iter() if local(c.tag) == "statistics"), None)
                if stats is not None and str(stats.get("views", "")).isdigit():
                    metrics["youtube_views"] = int(stats.get("views"))
        results.append(item(xml_field(node, "title"), urljoin(spec["url"], link or ""),
                            xml_field(node, "published", "pubDate", "updated"),
                            spec["name"], spec["role"], description, metrics))
    return results


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.links = []
        self.chunks = []
        self.skip = 0
        self.main = 0
        self.times = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag in {"article", "main"}:
            self.main += 1
        if tag == "meta" and attrs.get("content"):
            self.meta[attrs.get("property") or attrs.get("name") or attrs.get("itemprop")] = attrs["content"]
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"article", "main"}:
            self.main = max(0, self.main - 1)

    def handle_data(self, data):
        if self.main and not self.skip:
            self.chunks.append(data)


def page_info(url):
    page = Page(fetch(url, evidence=True))
    body = clean(" ".join(page.chunks))
    stamp = next((page.meta[k] for k in ("article:published_time", "datePublished", "date", "pubdate")
                  if k in page.meta), "")
    if not stamp:
        found = re.search(MONTH_DATE, body[:1600])
        stamp = next(iter(page.times), "") or (found.group() if found else "")
    description = page.meta.get("og:description") or page.meta.get("description") or ""
    title = page.meta.get("og:title") or page.meta.get("twitter:title") or ""
    return page, title, stamp, clean(description + " " + body)[:2600]


def index(spec):
    page = Page(fetch(spec["url"], evidence=True))
    links = list(dict.fromkeys(
        url for link in page.links
        if (url := canonical(urljoin(spec["url"], link))) and domain(url) == domain(canonical(spec["url"]))
        and urlsplit(url).path.startswith(spec["prefix"])
        and len(urlsplit(url).path) > len(spec["prefix"]) + 5
        and urlsplit(url).path.count("/") == spec["prefix"].count("/")
    ))[:10]
    if not links:
        raise ValueError("no article links on index")

    def read(link):
        try:
            _, title, stamp, excerpt = page_info(link)
            result = item(title, link, stamp, spec["name"],
                          "research" if spec["name"] in {"Stanford Digital Economy Lab", "Anthropic Economics Research"} else "official", excerpt)
            if result:
                result.verification = "opened"
            return result, None if result else {"url": link, "status": "undated_or_missing_title"}
        except Exception as exc:
            return None, {"url": link, "status": error_label(exc)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(read, links))
    return [entry for entry, _ in results], [failure for _, failure in results if failure]


def github_releases(repo):
    url = f"https://api.github.com/repos/{repo}/releases"
    try:
        data = fetch_json(url, params={"per_page": 12}, token=os.getenv("GH_TOKEN"))
    except Exception as exc:
        fallback = f"https://github.com/{repo}/releases.atom"
        entries = feed({"name": repo, "url": fallback, "role": "release"})
        releases = []
        for entry in entries:
            if not entry or re.search(r"[-.](alpha|beta|rc|nightly|dev)", entry.title, re.I):
                continue
            entry.title = f"{repo} {entry.title}"
            entry.verification = "release_atom_updated"
            releases.append(entry)
        return releases, [{"url": url, "status": error_label(exc), "fallback": fallback,
                           "date_type": "official feed updated time"}]
    results = []
    for row in data:
        if row.get("draft") or row.get("prerelease"):
            continue
        entry = item(f"{repo} {row.get('name') or row['tag_name']}", row.get("html_url"),
                     row.get("published_at"), repo, "release", row.get("body"))
        if entry:
            entry.verification = "release_api"
            results.append(entry)
    return results


def hn(query, cutoff):
    data = fetch_json(CONFIG["hacker_news"]["url"], params={
        "query": query, "tags": "story", "numericFilters": f"created_at_i>={int(cutoff.timestamp())}",
        "hitsPerPage": 12,
    })
    results = []
    for row in data["hits"]:
        discussion = f"https://news.ycombinator.com/item?id={row['objectID']}"
        if not row.get("url"):
            continue
        metrics = {"hn_points": row.get("points") or 0, "hn_comments": row.get("num_comments") or 0}
        results.append(item(row.get("title"), row["url"], row.get("created_at"), "Hacker News",
                            "discussion", "", metrics, [discussion]))
    return results


def inbrief(hours):
    data = fetch_json(CONFIG["inbrief"], params={"hours": hours, "lang": "en"})
    results, unknown_date = [], 0
    for row in data["articles"]:
        published = next((row.get(key) for key in
                          ("published_at", "publishedAt", "created_at", "date", "published", "createdAt")
                          if date(row.get(key))), None)
        # An aggregator's timestamp cannot establish when the original was published.
        stamp = published or NOW.isoformat()
        entry = item(row.get("title"), row.get("url"), stamp, "InBrief", "aggregated", row.get("summary"))
        if entry:
            entry.verification = "discovery_timestamp" if published else "undated_discovery"
            unknown_date += not bool(published)
            results.append(entry)
    return results, [{"guest_tier": data.get("tier"), "reported_cap": data.get("tier_limit"),
                      "undated": unknown_date}]


def x_recent(query, cutoff):
    data = fetch_json("https://api.x.com/2/tweets/search/recent", token=os.environ["X_BEARER_TOKEN"],
                      params={"query": query, "max_results": 50,
                              "start_time": max(cutoff + timedelta(seconds=5),
                                                NOW - timedelta(days=7) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "tweet.fields": "created_at,public_metrics,author_id,entities",
                              "expansions": "author_id", "user.fields": "username"})
    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
    results = []
    for row in data.get("data", []):
        account = users.get(row.get("author_id"), "i/web")
        metrics = {name: row["public_metrics"][field] for name, field in
                   (("x_impressions", "impression_count"), ("x_likes", "like_count"))
                   if field in row.get("public_metrics", {})}
        links = [link.get("expanded_url") for link in row.get("entities", {}).get("urls", [])]
        entry = item(row["text"][:150], f"https://x.com/{account}/status/{row['id']}",
                     row["created_at"], f"X @{account}", "social", row["text"], metrics, links)
        if entry:
            entry.verification = "post_api"
            results.append(entry)
    details = [{"note": "first 50 matches", "has_next_page": bool(data.get("meta", {}).get("next_token"))}]
    return results, details


def youtube_stats(items):
    videos = {}
    for entry in items:
        video_id = dict(parse_qsl(urlsplit(entry.url).query)).get("v")
        if domain(entry.url) == "youtube.com" and video_id:
            videos.setdefault(video_id, []).append(entry)
    if not videos:
        return 0
    covered = 0
    for start in range(0, len(videos), 50):
        ids = list(videos)[start:start + 50]
        data = fetch_json("https://www.googleapis.com/youtube/v3/videos", params={
            "part": "statistics", "id": ",".join(ids), "key": os.environ["YOUTUBE_API_KEY"],
        })
        for row in data["items"]:
            views = row["statistics"].get("viewCount")
            if views is not None:
                for entry in videos[row["id"]]:
                    entry.metrics["youtube_views"] = int(views)
                covered += 1
    return covered


def collect(cutoff, hours):
    tasks = []

    def add(name, target, action):
        tasks.append((name, target, action))

    for spec in CONFIG["feeds"]:
        add(spec["name"], spec["url"], lambda spec=spec: feed(spec))
    for spec in CONFIG["indexes"]:
        add(spec["name"], spec["url"], lambda spec=spec: index(spec))
    for repo in CONFIG["github_releases"]:
        add(repo, f"https://api.github.com/repos/{repo}/releases?per_page=12", lambda repo=repo: github_releases(repo))
    for query in CONFIG["hacker_news"]["queries"]:
        target = CONFIG["hacker_news"]["url"] + "?" + urlencode({"query": query, "tags": "story",
                    "numericFilters": f"created_at_i>={int(cutoff.timestamp())}", "hitsPerPage": 12})
        add("Hacker News", target, lambda query=query: hn(query, cutoff))
    add("InBrief", CONFIG["inbrief"] + "?" + urlencode({"hours": hours, "lang": "en"}),
        lambda: inbrief(hours))
    query = "(" + " OR ".join("from:" + name for name in CONFIG["x_accounts"]) + ") -is:retweet"
    logs = []
    if os.getenv("X_BEARER_TOKEN"):
        add("X API", query, lambda: x_recent(query, cutoff))
    else:
        logs.append({"source": "X API", "query": query, "status": "未配置 X_BEARER_TOKEN，使用 InBrief 发现原帖"})

    def run(task):
        name, target, action = task
        record = {"source": name, "query": target}
        try:
            value = action()
            rows, details = value if isinstance(value, tuple) else (value, [])
            entries = [i for i in rows if i and cutoff <= date(i.published) <= NOW + timedelta(minutes=5)]
            record.update(status="partial" if name in {s["name"] for s in CONFIG["indexes"]} and details
                          else "ok", fetched=len(rows), within_window=len(entries), details=details)
            return entries, record
        except Exception as exc:
            record.update(status="error", error=error_label(exc), within_window=0)
            return [], record

    entries = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for rows, record in pool.map(run, tasks):
            entries.extend(rows)
            logs.append(record)

    if os.getenv("YOUTUBE_API_KEY"):
        try:
            logs.append({"source": "YouTube Data API", "status": "ok", "videos_with_views": youtube_stats(entries)})
        except Exception as exc:
            logs.append({"source": "YouTube Data API", "status": "error", "error": error_label(exc)})
    else:
        logs.append({"source": "YouTube Data API", "status": "未配置 YOUTUBE_API_KEY，保留频道 Feed 可见指标"})
    return entries, logs


def topic_matches(text):
    return [label for label, pattern in CONFIG["topic_patterns"].items()
            if re.search(pattern, text, re.I)]


def heat(entry):
    levels = []
    age = max(12, (NOW - date(entry.published)).total_seconds() / 3600)
    factor = max(0.5, min(2, math.sqrt(age / 72)))
    for metric, value in entry.metrics.items():
        if metric in CONFIG["heat_thresholds_72h"]:
            levels.append(sum(value >= threshold * factor
                              for threshold in CONFIG["heat_thresholds_72h"][metric]))
    return max(levels, default=None)


ROLE_PRIORITY = {"release": 7, "official": 6, "research": 5, "independent": 4,
                 "video": 3, "social": 2, "discussion": 1, "aggregated": 0}


def dedupe(items):
    grouped = {}
    for entry in sorted(items, key=lambda i: -ROLE_PRIORITY[i.role]):
        previous = grouped.setdefault(entry.url, entry)
        if previous is entry:
            continue
        previous.sources = list(dict.fromkeys(previous.sources + entry.sources))
        previous.references = list(dict.fromkeys(previous.references + entry.references))
        for metric, value in entry.metrics.items():
            previous.metrics[metric] = max(value, previous.metrics.get(metric, 0))
    for index, entry in enumerate(grouped.values(), 1):
        entry.id = f"N{index:04d}"
    return list(grouped.values())


def heuristic(entry):
    text = entry.title + " " + entry.excerpt[:470]
    topics = topic_matches(text)
    relevant = bool(topics and (entry.role in {"official", "release", "video"}
                    or re.search(r"\b(?:" + "|".join(re.escape(s) for s in CONFIG["organization"]) + r")\b",
                                 text, re.I)))
    substantial = bool(re.search(CONFIG["impact_patterns"]["substantive"], text, re.I))
    minor = bool(re.search(CONFIG["impact_patterns"]["minor"], entry.title, re.I))
    impact = 3 if relevant and substantial and not minor else 2 if relevant else 0
    # Rules can screen a specific release or provenance. Other claims need editorial assessment.
    direct = bool(entry.excerpt and (entry.role == "release" or (
        entry.role == "official" and entry.verification == "opened"
        and re.search(CONFIG["impact_patterns"]["substantive"], entry.title, re.I)
    )))
    evidence = 2 if direct else 1 if entry.role in {
        "release", "official", "research", "video", "social", "independent"
    } else 0
    reason = "主题匹配：" + "、".join(topics) if topics else "未匹配重点主题"
    if substantial:
        reason += "。含发布、开放或新证据关键词"
    if minor:
        reason += "。标题呈现小改动"
    excerpt = ""
    if direct and entry.role == "release":
        match = re.search(CONFIG["impact_patterns"]["substantive"], entry.excerpt[:470], re.I)
        if match:
            excerpt = entry.excerpt[match.start():match.start() + 115].strip()
    caveat = ("直接证据仅支持发布方已公布此消息，宣传效果与实际影响待独立核验。"
              if direct else "规则初筛，影响和关键说法待编辑核验。")
    if entry.verification == "release_atom_updated":
        caveat += "日期为官方订阅的更新时间。"
    return {
        "id": entry.id, "event_key": entry.id, "title": entry.title,
        "relevant": relevant, "topic": topics[0] if topics else "其他",
        "evidence": evidence, "impact": impact, "summary": excerpt,
        "reason": reason, "caveat": caveat,
        "evidence_ids": [entry.id] if evidence else [],
        "mode": "rules",
    }


def verify(entry):
    if entry.verification in {"opened", "release_api", "release_atom_updated", "post_api"}:
        return
    if domain(entry.url) == "youtube.com" and entry.role == "video":
        entry.verification = "official_channel_feed"
        return
    if not trusted_domain(entry.url):
        entry.verification = "outside_allowlist"
        return
    try:
        _, title, stamp, excerpt = page_info(entry.url)
        if not title or not excerpt:
            entry.verification = "page_opened_no_extract"
            return
        entry.verification = "opened"
        original_date = date(stamp)
        if original_date and entry.role in {"aggregated", "discussion"}:
            entry.published = original_date.isoformat()
        entry.excerpt = excerpt
    except Exception as exc:
        entry.verification = error_label(exc)


def schema():
    fields = {
        "id": {"type": "string"},
        "event_key": {"type": "string"},
        "title": {"type": "string"},
        "relevant": {"type": "boolean"},
        "topic": {"type": "string", "enum": ["新功能", "新概念", "工作影响", "行业大事", "其他"]},
        "evidence": {"type": "integer", "enum": list(range(4))},
        "impact": {"type": "integer", "enum": list(range(6))},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "caveat": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    }
    row = {"type": "object", "properties": fields, "required": list(fields),
           "additionalProperties": False}
    return {"type": "object", "properties": {"reviews": {"type": "array", "items": row}},
            "required": ["reviews"], "additionalProperties": False}


DIRECT_CHECKS = {"opened", "release_api", "release_atom_updated", "official_channel_feed", "post_api"}


def model_review(entries):
    if not entries:
        return {}
    model = os.getenv("OPENAI_MODEL") or "gpt-5-mini"
    result = fetch_json("https://api.openai.com/v1/responses", token=os.environ["OPENAI_API_KEY"],
                        payload={
                            "model": model, "store": False, "max_output_tokens": 13500,
                            "reasoning": {"effort": "low"},
                            "instructions": (ROOT / "editorial.md").read_text(encoding="utf-8"),
                            "input": json.dumps([asdict(i) for i in entries], ensure_ascii=False),
                            "text": {"format": {"type": "json_schema", "name": "news_reviews",
                                                "strict": True, "schema": schema()}},
                        })
    if result.get("status") != "completed":
        raise ValueError("model response incomplete")
    outputs = [c["text"] for part in result["output"] if part.get("type") == "message"
               for c in part.get("content", []) if c.get("type") == "output_text"]
    data = json.loads("".join(outputs))
    by_id = {entry.id: entry for entry in entries}
    reviews = {}
    for row in data["reviews"]:
        entry = by_id.get(row["id"])
        if not entry or entry.id in reviews or not row["event_key"].strip():
            raise ValueError("invalid candidate ID or event key")
        references = {ref: by_id[ref] for ref in row["evidence_ids"] if ref in by_id}
        row["evidence_ids"] = list(references)
        supported = [source for source in references.values()
                     if source.verification in DIRECT_CHECKS and source.excerpt
                     and source.role not in {"social", "aggregated", "discussion"}]
        independent = any(s.role == "independent" for s in supported)
        publishers = {domain(s.url) if domain(s.url) != "github.com" else s.source
                      for s in supported}
        maximum = 3 if independent and len(publishers) >= 2 else 2 if supported else 1 if references else 0
        row["evidence"] = min(row["evidence"], maximum)
        row["mode"] = "model"
        reviews[entry.id] = row
    if set(reviews) != set(by_id):
        raise ValueError("model omitted candidates")
    return reviews


def grade(entries, limit, model_enabled=None):
    initial = {i.id: heuristic(i) for i in entries}
    ordered = sorted(entries, key=lambda i: (
        -int(initial[i.id]["relevant"]), -initial[i.id]["impact"],
        -(heat(i) or 0), -ROLE_PRIORITY[i.role], -date(i.published).timestamp()
    ))
    # Reserve space for employment and methods even when product launches dominate.
    priority = []
    for topic in ("工作影响", "新概念", "新功能", "行业大事"):
        priority.extend([i for i in ordered if initial[i.id]["topic"] == topic][:3])
    priority_ids = {i.id for i in priority}
    priority = (priority + [i for i in ordered if i.id not in priority_ids])[:limit]
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(verify, priority))
    reviews = {i.id: heuristic(i) for i in entries}
    use_model = bool(os.getenv("OPENAI_API_KEY")) if model_enabled is None else model_enabled
    if not use_model:
        return reviews, "规则初筛。未配置 OPENAI_API_KEY。"
    try:
        reviews.update(model_review(priority))
        return reviews, f"模型评估 {len(priority)} 条，其他候选保留规则初筛。模型 " + (
            os.getenv("OPENAI_MODEL") or "gpt-5-mini")
    except Exception as exc:
        return reviews, f"模型请求失败（{error_label(exc)}），已回退到规则初筛。"


def rank(entries, reviews, settings, fresh, oldest):
    rows = []
    for entry in entries:
        review = reviews[entry.id]
        row = {**asdict(entry), **review, "original_title": entry.title,
               "heat": heat(entry), "window": "recent" if date(entry.published) >= fresh else "lookback"}
        rows.append(row)

    def order(row):
        heat_score = row["heat"] if row["heat"] is not None else -1
        return (-row["impact"], -heat_score, -row["evidence"], -date(row["published"]).timestamp())

    rows.sort(key=order)
    by_url = {row["url"]: row for row in rows}
    for row in rows:
        if row["role"] == "social":
            paired = next((by_url[url] for url in row["references"] if url in by_url
                           and by_url[url]["role"] in {"release", "official", "research"}), None)
            if paired and (row["mode"] == "rules" or paired["mode"] == "rules"):
                row["event_key"] = paired["event_key"]
    # Use the hottest reported copy for signals and the best eligible copy for evidence.
    groups = {}
    for row in rows:
        group = groups.setdefault(row["event_key"].casefold().strip(), [])
        group.append(row)
    for group in groups.values():
        scores = [row["heat"] for row in group if row["heat"] is not None]
        for row in group:
            row["event_heat"] = max(scores, default=None)
            row["event_signal_ids"] = [signal["id"] for signal in group if signal["metrics"]]

    for row in rows:
        if not oldest <= date(row["published"]) <= NOW + timedelta(minutes=5):
            decision, reason = "skip", "核验后原始发布日期在查询窗口之外"
        elif not row["relevant"] or row["impact"] < settings["min_impact"]:
            decision, reason = "skip", "未达到主题或影响门槛"
        elif row["evidence"] < settings["min_evidence"]:
            decision, reason = "watch", "原始证据不足，待核实"
        elif row["window"] == "lookback" and (row["event_heat"] or 0) < settings["older_min_heat"]:
            decision, reason = "skip", "回看窗口内尚未达到热度门槛"
        else:
            decision, reason = "eligible", "达到证据和影响门槛"
        row["decision"], row["decision_reason"] = decision, reason

    eligible = [r for r in rows if r["decision"] == "eligible"]
    eligible.sort(key=lambda r: order({**r, "heat": r["event_heat"]}))
    selected, seen = [], set()
    for row in eligible:
        key = row["event_key"].casefold().strip()
        if key in seen:
            row["decision"], row["decision_reason"] = "duplicate", "同一事件已收录"
        elif len(selected) >= settings["limit"]:
            row["decision"], row["decision_reason"] = "overflow", "达到快报条数上限"
        else:
            row["decision"] = "selected"
            row["rank"] = len(selected) + 1
            seen.add(key)
            selected.append(row)
    watch = []
    for row in sorted(rows, key=lambda row: (-row["evidence"], *order(row))):
        key = row["event_key"].casefold().strip()
        if row["decision"] == "watch" and row["window"] == "recent" and key not in seen:
            watch.append(row)
            seen.add(key)
    return selected, watch[:3], rows


def md(text):
    return re.sub(r"([\\`*\[\]_!|])", r"\\\1", escape(" ".join(str(text).split()), quote=False))


def link(url):
    return "<" + url.replace(">", "%3E").replace("<", "%3C").replace(" ", "%20") + ">"


def write_report(directory, selected, watch, rows, run):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 美国 AI 一手快报",
        f"生成时间 {run['collected_at']} UTC",
        f"首发窗口 {run['fresh_hours']} 小时，回看 {run['lookback_hours']} 小时。{md(run['grading'])}",
        "分数顺序为证据 / 影响 / 热度。热度按来源公开指标估算，未知表示没有可用指标。",
    ]
    by_id = {row["id"]: row for row in rows}
    if not selected:
        lines.append("本轮没有候选达到正式入选门槛。")
    for row in selected:
        h = row["event_heat"] if row["event_heat"] is not None else "未知"
        lines.append(f"## {row['rank']}. {md(row['title'])}")
        lines.append(f"**{row['evidence']} / {row['impact']} / {h}**　{row['published'][:10]}　{md(row['topic'])}")
        if row["summary"]:
            lines.append(md(row["summary"]))
        lines.append(f"**入选理由**　{md(row['reason'])}")
        lines.append(f"**证据边界**　{md(row['caveat'])}")
        urls = [by_id[ref]["url"] for ref in row["evidence_ids"] if ref in by_id]
        lines.append("**证据链接**　" + "　".join(link(u) for u in dict.fromkeys([row["url"]] + urls)))
        signals = []
        for signal_id in row["event_signal_ids"]:
            signal = by_id[signal_id]
            values = "，".join(f"{name} {value:,}" for name, value in signal["metrics"].items())
            origin = next((url for url in signal["references"] if domain(url) == "news.ycombinator.com"), signal["url"]) if any(
                k.startswith("hn_") for k in signal["metrics"]) else signal["url"]
            signals.append(md(values) + " " + link(origin))
        if signals:
            lines.append("**反响**　" + "；".join(signals))
    if watch:
        lines.append("## 待观察")
        lines.extend(f"- {md(row['title'])} · {md(row['caveat'])} · {link(row['url'])}" for row in watch)
    lines.append("完整候选、评分、查询和失败记录见 `candidates.json` 与 `run.json`。")
    (directory / "brief.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    candidates = [{k: v for k, v in row.items() if k != "excerpt"} for row in rows]
    for name, content in (("candidates.json", candidates), ("run.json", run)):
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test():
    from tempfile import TemporaryDirectory
    stamp = (NOW - timedelta(hours=5)).isoformat()
    url = "https://github.com/openai/codex/releases/tag/v1"
    a = item("OpenAI releases Agent Skills", url, stamp, "openai/codex", "release", "Agent Skills are now available")
    b = item("AI Agent Skills", url + "?utm_source=example", stamp, "Hacker News", "discussion",
             metrics={"hn_points": 170}, refs=["https://news.ycombinator.com/item?id=123"])
    c = item("OpenAI introduces a new agent API", "https://openai.com/news/example", stamp, "OpenAI News", "official")
    d = item("OpenAI Codex fixes", url + "2", stamp, "openai/codex", "release", "A bugfix")
    assert canonical("https://youtu.be/example?si=test") == "https://youtube.com/watch?v=example"
    assert not canonical("http://openai.com/news")
    entries = dedupe([a, b, c, d])
    assert len(entries) == 3 and a.metrics["hn_points"] == 170 and "Hacker News" in a.sources
    assert heat(c) is None
    assessments = {i.id: heuristic(i) for i in entries}
    selected, watch, rows = rank(entries, assessments, CONFIG["report"], NOW - timedelta(days=3),
                                 NOW - timedelta(days=7))
    assert [r["id"] for r in selected] == [a.id]
    assert [r["id"] for r in watch] == [c.id]
    assert next(r for r in rows if r["id"] == d.id)["decision"] == "skip"
    older = NOW - timedelta(days=8)
    a.published = older.isoformat()
    old_selected, _, _ = rank(entries, assessments, CONFIG["report"], NOW - timedelta(days=3),
                              NOW - timedelta(days=7))
    assert not old_selected
    with TemporaryDirectory() as directory:
        target = Path(directory)
        write_report(target, selected, watch, rows, {"collected_at": stamp, "fresh_hours": 72,
                     "lookback_hours": 168, "grading": "test"})
        assert url in (target / "brief.md").read_text()
        assert "excerpt" not in json.loads((target / "candidates.json").read_text())[0]
    print("Scoring, deduplication, evidence gate, and export tests passed.")


def main():
    settings = CONFIG["report"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh-hours", type=int, default=settings["fresh_hours"])
    parser.add_argument("--lookback-hours", type=int, default=settings["lookback_hours"])
    parser.add_argument("--output", type=Path, default=Path("out"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not 1 <= args.fresh_hours <= args.lookback_hours <= 336:
        parser.error("expected 1 <= fresh-hours <= lookback-hours <= 336")
    oldest = NOW - timedelta(hours=args.lookback_hours)
    fresh = NOW - timedelta(hours=args.fresh_hours)
    entries, logs = collect(oldest, args.lookback_hours)
    entries = dedupe(entries)
    reviews, mode = grade(entries, settings["model_limit"])
    selected, watch, rows = rank(entries, reviews, settings, fresh, oldest)
    run = {
        "collected_at": NOW.isoformat(timespec="seconds"),
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": os.getenv("GITHUB_SHA"),
        "fresh_hours": args.fresh_hours, "lookback_hours": args.lookback_hours,
        "candidate_count": len(entries), "selected_count": len(selected), "grading": mode,
        "sources": logs,
    }
    write_report(args.output, selected, watch, rows, run)
    print(f"{len(entries)} candidates, {len(selected)} selected, "
          f"{sum(log['status'] == 'error' for log in logs)} source errors. Details in run.json.")
    if not any(log["status"] in {"ok", "partial"} and "within_window" in log for log in logs):
        raise SystemExit("No collection source completed. See run.json.")


if __name__ == "__main__":
    main()
