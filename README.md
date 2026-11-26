# 美国 AI 一手快报

手动搜索美国 AI 行业的重要发布、新概念，以及有数据支撑的工作影响。每次快报最多 5 条，附英文原始链接、评分理由和证据边界。

## 手动运行

打开 [Actions → 美国 AI 一手快报](https://github.com/ningningzhang-nina/ai-news-radar/actions/workflows/radar.yml)，点击 **Run workflow**。默认扫最近 72 小时，另回看 7 天里反响较大的消息。点击本次运行底部的 **Artifacts** 下载：

| 文件 | 内容 |
|---|---|
| `brief.md` | 入选与少量待观察条目 |
| `candidates.json` | 所有去重后的候选、证据来源、分数、热度原始值、筛选决定及理由 |
| `run.json` | 时间窗口、提交 SHA、评分模式、实际查询、各来源返回条数和访问问题 |

GitHub 保留快报附件 30 天。提交代码时只跑本地逻辑自测，采集由手动触发。

## 取数与评分

[来源和阈值](sources.json)可直接修改。[编辑规则](editorial.md)用于有模型的评分。来源包括官方公告、GitHub Releases、YouTube 官方频道、Hacker News、arXiv、斯坦福 Digital Economy Lab、Anthropic 经济学研究及 InBrief 的聚合候选。专注美国相关，规则模式以配置里的机构和主题关键词初筛。来源的接口分页、Feed 长度和聚合访客上限会限制实际覆盖；运行记录显示查询及错误。独立证据网页只读取配置中允许的域名。

- **证据 0–3** 按具体主张区分找不到原始依据、原始声明、直接证据及独立复现。规则模式对带说明的正式版本、以及原文可读取且标题明确的官方公布事实给 2 分。功能上线与性能宣称可以有不同证据结论。
- **影响 0–5** 按可能改变的工作流程、范围和行业意义判断。默认规则模式按公开关键词给初筛分，最高 3 分。模型模式输出理由和证据所支持的边界。
- **热度 0–5** 按各平台指标分别用公开阈值和内容年龄粗分，事件采用最高可见分。分数不代表统计百分位。原始指标见候选表，采集时间见运行记录；缺失指标记为空值。GitHub Stars 和媒体转载数量不计入。
- 正式入选默认要求证据 ≥ 2、影响 ≥ 3，回看窗口另要求热度 ≥ 3。先比影响，再比热度、证据和时间。低证据的新候选可列为待观察。代码合并相同原始 URL，模型还会在评估过的候选间归并同一事件。

## 可选配置

可直接运行规则模式。要启用语义评分和中文摘要，在仓库 **Settings → Secrets and variables → Actions** 配置下列 Secrets。使用自己的 API 账号，费用遵循服务商计费。密钥保存在 Actions Secrets，运行记录只保留查询及错误类型。

| Secret | 用途 |
|---|---|
| `OPENAI_API_KEY` | 对有限候选进行结构化评估、跨链接事件合并和摘要，默认模型为 `gpt-5-mini`，可通过 Actions variable `OPENAI_MODEL` 修改。请求失败时报告记录回退。 |
| `X_BEARER_TOKEN` | 通过 X Recent Search 查询配置里的账号及公开指标。无凭据时从 InBrief 发现可索引的 X 原帖，覆盖范围随聚合结果变化。 |
| `YOUTUBE_API_KEY` | 用官方接口补充候选视频的观看量。否则保留频道 Feed 中可读的指标。 |

程序从 YouTube 读取标题、发布时间及可见的描述和指标。视频内容的逐段审读和字幕目前需人工核对。

本地使用 Python 3.10+，无需安装依赖。

```bash
python3 radar.py --self-test
python3 radar.py --fresh-hours 72 --lookback-hours 168
```
