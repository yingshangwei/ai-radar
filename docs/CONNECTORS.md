# 数据源与模型接入

## X / Twitter

当前默认配置使用 TwitterAPI.io，按 30 分钟轮询关注账号、6 小时运行广泛发现；另有独立金额预算和可选 Apify 小样本对照。授权、费用、供应商隔离与边界见 [X 数据供应商](X-DATA-PROVIDERS.md)。下方官方接口说明为兼容路径，只有显式选择 `[x_data].provider = "official"` 才使用；不会自动回退。TwitterAPI.io 每页固定最多 20 条，忽略仅供官方接口使用的 `x_page_size`。

在服务器私有环境文件配置 `X_BEARER_TOKEN`，其值来自 X Developer 应用的官方授权。使用 `/2/tweets/search/recent`，读取 `created_at`、`public_metrics`、`note_tweet`、`referenced_tweets` 和作者/引用扩展。搜索查询、请求预算、页数和每页条数在 `config.toml` 顶层配置。

### 请求预算与重点账号轮转

持久采集器将已启用的 X 关注账号分开查询，每个账号使用 `from:账号 -is:retweet`，另以 `x_query` 做关键词发现。重点账号的查询不附加 AI 关键词，以免在平台搜索阶段漏掉依赖上下文的帖子；入库仍执行既有日期、主题与热度规则。本次采集器改动只涉及搜索调度和覆盖记录，不代表启用了新的 AI 相关性判定规则。

| 配置项 | 默认行为 | 范围与含义 |
| --- | --- | --- |
| `x_max_pages` | `2` | 每个关注账号每轮最多请求 1–10 页，也是未显式设置预算时的计算基数。 |
| `x_page_size` | `100` | 每页请求 10–100 条；已有分页窗口继续使用创建时的页大小。 |
| `x_request_budget` | 省略时为 `x_max_pages × (1 + ceil(有效关注账号数 / 12))` | 可显式设为 1–1000；限制一轮 X 请求总次数，不是每个账号各自的预算。 |
| `x_discovery_requests` | 省略时为 `x_max_pages` | 可设为 0–1000；从总预算中分给关键词发现，设为 `0` 可关闭。存在关注账号时至少保留一次重点账号请求，实际发现预算会相应截断。 |
| `x_initial_lookback_hours` | `24` | 首个查询窗口回看 1–168 小时；还受 `lookback_hours` 和服务器 167 小时安全边界约束。 |
| `x_head_refresh_hours` | `24` | 关注账号最新窗口的刷新间隔，允许 1–168 小时；不改变整个采集任务的 `collect_minutes` 间隔。 |

例如，17 个有效关注账号、`x_max_pages=2` 且省略两个预算字段时，一轮最多 6 次 X 请求，其中 2 次留给关键词发现、4 次留给重点账号。若沿用初始联调配置 `x_max_pages=1`，则最多 3 次，其中发现 1 次、重点账号 2 次。账号会跨轮推进，不会每轮都从名单开头重新开始；尚未拉取或已到刷新时间的账号优先于旧窗口补页，避免活跃账号的长分页占用所有后续轮次。预算是上限，不一定用满，失败请求也占用本轮次数。

以下是固定每轮 6 次请求的配置示例；如果希望预算随名单数量自动计算，可省略两个预算字段。必须放在 `[provider]`、`[translation]` 等表之前：

```toml
x_max_pages = 2
x_page_size = 100
x_request_budget = 6
x_discovery_requests = 2
x_initial_lookback_hours = 24
x_head_refresh_hours = 24
```

这些限制控制请求规模，不能保证完整覆盖，也不是金额上限。X 的具体价格、配额和搜索权限由你的账户决定。没有 Token、权限失效或额度用尽时保留明确状态，不自动改用未经授权的抓取工具。

### 续采、覆盖状态与故障处理

每个分页窗口固定起止时间、查询和页大小；正文入库与下一页进度在同一数据库事务内提交。服务重启后读取 `x_collection_states` 中的进度，继续未完成的关注账号窗口；保存失败不会先推进游标。分页令牌被拒绝时清除该窗口的令牌，保留原时间范围，下轮重新读取并通过既有文章 ID 去重。关键词发现每轮优先拉取新窗口，剩余发现预算只补当前窗口，不承诺补齐所有旧关键词搜索结果。

来源信息会显示“已用请求数/预算”“最近刷新间隔内已拉取最新窗口的账号数”“待补页窗口数”和“历史覆盖缺口数”。拉到最新窗口的第一页不等于该账号的整个窗口已读完。`partial` 表示仍有账号待刷新、窗口待补页或已记录的覆盖缺口，即使 HTTP 请求成功也不会报完整采集成功；已有正文照常保留。`healthy` 只表示当前启用范围内没有这些已知未完成项，不代表 X 全网或账号全部历史已覆盖。

每个查询最多保留 8 个未完成窗口。窗口超出可回看范围、积压超限、查询改变，或平台返回不完整信息时，会记录覆盖缺口而不是把跳过内容视为已完成。部分返回也可能只缺少引用或附加信息，不应据此断言主帖全部缺失。历史缺口会影响状态，增加下一轮预算不能保证追回已过期内容。首次默认只回看 24 小时，不会自动重建完整账号历史。

正常运维可在现有采集入口触发下一轮，或等待定时采集；它会沿用已保存进度。不要为清除 `partial` 而删除采集状态或手工修改完成时间。请求预算用完时停止本轮，授权、限流或网络错误时保留故障状态与已成功提交的页；不会在同一轮立即反复重试。

长帖优先读取 `note_tweet.text`；引用帖原文单独标明作者及原始时间，使“现已向所有用户开放”这类依赖上下文的更新能正确进入 AI 筛选。主帖始终保留自己的发布时间与互动数，引用帖不会被重复当成新发布。引用原文不可用时明确标注；API 返回有效主帖和引用资源错误时保留可读主帖，没有有效数据的错误仍报错。不会借用被回复帖的 AI 关键词，把无关问候误收为新闻。

字段实现依据 [X 数据字典](https://docs.x.com/x-api/fundamentals/data-dictionary) 与 [扩展对象说明](https://docs.x.com/x-api/fundamentals/expansions)，已通过接口契约测试。2026-09-07 已用用户提供的真实 Token 完成官方 API 联调，包含长帖与引用上下文。

如果后续分页发生额度耗尽、限流或网络/HTTP 错误，已经取得的有效帖子仍会入库；来源保持“额度受限”或“采集异常”，并明确本轮未完成。缺少可靠分页元数据的响应不会推进完成时间；这种情况与可读主帖附带引用资源错误分别处理。

[X 官方搜索接入文档](https://docs.x.com/x-api/posts/search/integrate/overview)

### 历史接入记录

以下是旧分组搜索实现的联调记录，不代表上述持久采集器已完成部署验收。

2026-09-07 03:20 接入状态：用户已提供 Bearer Token 并确认充值。Token 从本机私有 `credentials/x-api.env` 加密传输到服务器 `/etc/ai-radar/server.env`（0600），未写入命令明文、Git 或 App。独立检查确认服务进程已加载凭据；X 用量接口返回 200。官方文档中的 `/2/usage/credits` 本次实际返回 404，因此没有通过 API 确认美元余额；用量接口的帖子上限不代表余额。

首次正式采集完成：3 个查询共读取 30 条，筛选新增 6 条，并更新 1 条已存在的 Tibo 帖子；X 来源变为 `healthy`，云端共 40 条，其中 X 14 条（7 条现由官方 API 更新、7 条保留此前浏览器来源）。原文链接、时间、互动数、长帖及 Sam Altman 引用 Jakub 的上下文已回读核验。

服务器初始配置采用 `x_page_size=10`、`x_max_pages=1`，现有每 120 分钟采集任务继续运行；不会自动充值。该批量上限可能漏掉活跃账号和广泛搜索的内容，不代表完整覆盖或美元预算上限。2026-09-08 已观察到自然定时任务 `5aa266ff-6126-450f-b65c-8e3aa288b8ca`：00:32:19 开始、00:33:23 完成，X 读取 30 条、新增 2 条，同时处理关联网页；本次没有手动触发采集。

## Facebook

配置 `FACEBOOK_ACCESS_TOKEN` 与 `facebook_page_ids`（数字 Page ID）；Graph API 版本通过 `facebook_version` 替换。请求已授权 Page 的 `/posts`，读取原文、发布时间、原始链接、反应数、评论数和分享数。

读取自己管理的 Page 和读取第三方公开 Page 所需权限不同。普通 Facebook 登录不自动带来 Graph API 读取权限；第三方 Page 可能需要 Meta 审核通过的 Page Public Content Access。不能把没有权限返回的空内容当作“没有新闻”。

[Meta Page Public Content Access](https://developers.facebook.com/docs/features-reference/page-public-content-access/)

2026-09-07 用户反馈 Facebook 账号需要审核，暂时无法继续授权。保留未接通状态；已另外接入 Meta Newsroom 官方 RSS，补充公司的一手 AI 新闻。这不覆盖 Facebook 用户、Page 或社交互动数据。

后续分页或另一个 Page 请求遇到授权、限流、HTTP 或网络错误时，已读取的有效帖子仍会入库，来源保留失败状态并明确标注本轮未完成；不会重新从头请求。该逻辑已通过 13 项隔离回归验证，不代表 Facebook 的真实授权已经完成。

## 官方网站与 RSS

直接使用发布方的公开内容，作为独立的一手渠道。Anthropic 新闻页通过 Beautiful Soup 解析可见的标题、日期和简介，不使用未公开的内部 API。仅公布日期的来源会标记 `published_precision=date`，归一化为 UTC 当日零点；这不声称知道准确发布时刻。页面结构改变后会报告解析错误。

Meta 使用 [官方 Newsroom RSS](https://about.fb.com/feed/)，沿用相同日期、主题与去重规则；全站新闻中的非 AI 内容会被过滤。RSS 摘录与统一读取流程保存的网页正文是不同证据，摘要模型只能使用实际取得的内容，不能把摘录当作全文。

网页正文由统一 reading 流程使用 [Trafilatura](https://trafilatura.readthedocs.io/en/latest/quickstart.html) 提取并持久化，再供日报复用；直接链接的边界、网络校验与缓存规则见 [网页解读](READING.md)。正文读取失败时保留原始摘录及明确状态，不能将未取得的网页当作证据。

当前线上 `c95c8d4` 已包含 `08b19a1` 对旧临时正文抓取分支的移除。`reading.enabled=false` 时仅使用已保存消息，不会因历史顶层配置 `enrich_official_articles=true` 而再次抓网页；该历史字段仍可解析，但不再控制另一条读取通道。日报回看期内的未报道补充队列、配额和“补充／含补充”标签也已上线，语义见 [日报补偿与补充选稿](READING.md#日报补偿与补充选稿)。本次发布没有触发模型任务，实际新日报结果与代码上线分开验收。

## 脚本与 Agent 导入

已经授权的脚本、程序或 AI 操作可以产出统一 JSON，通过本地 CLI 或管理接口导入。导入仍执行日期、AI 主题与热度过滤；不会因为来自脚本就绕过筛选。

```json
{
  "articles": [{
    "platform": "x",
    "external_id": "平台原始帖子ID",
    "url": "https://x.com/账号/status/帖子ID",
    "title": "实际标题或原文开头",
    "text": "实际原文内容",
    "author": "作者名称",
    "handle": "账号",
    "published_at": "2026-09-06T02:00:00+00:00",
    "metrics": {"like_count": 300},
    "source_id": "authorized-browser"
  }]
}
```

```bash
cd server
.venv/bin/radar import --file /private/path/collected.json
```

HTTP：`POST /v1/admin/import`，以管理令牌作 Bearer。不要把 Cookie 或访问 Token 填到文章字段。缺少可靠日期、作者、原文链接的内容不要作为最新新闻导入。已完成一批 X 浏览器真实采集与云端导入、日报联调；操作边界与结果见 [浏览器采集说明](BROWSER-COLLECTION.md)。持续运行环境仍待接通。

## 模型与 Agent 可替换

只需修改 `[provider]`，无需改变 API 或 App。所有输出最终都通过统一 schema 和引用校验，引用 ID 必须存在于输入材料中。来源内容按不可信数据处理。引用校验保证引用存在，不能自动证明摘要的每个语义判断正确。

Codex（默认，已通过本机和腾讯云服务用户的真实调用验证）：

```toml
[provider]
kind = "codex"
command = ["codex"]
# model = "你账号可用的模型标识"
timeout_seconds = 240
max_items = 35
```

在服务账户下运行 `codex login --device-auth`，由你完成浏览器授权。不要复制整个个人 `~/.codex` 目录到服务器；服务器使用独立的 `CODEX_HOME`。`codex exec` 以只读沙箱、临时工作目录、禁用 shell/web search、忽略用户工具配置的方式调用。建议服务器始终使用独立、无其他业务权限的系统用户。

[OpenAI 非交互模式](https://developers.openai.com/codex/noninteractive/) · [授权](https://developers.openai.com/codex/auth/)

Claude Code CLI：

```toml
[provider]
kind = "claude_cli"
command = ["claude"]
# model = "你账号可用的模型标识"
```

通过官方 CLI 完成登录后使用；调用时禁用工具、禁止加载其他 MCP 配置和全局设置。服务业务逻辑不依赖 Claude 的事件格式。[Claude 官方非交互与结构化输出](https://code.claude.com/docs/en/headless)

OpenAI Responses 或 Anthropic API：

```toml
[provider]
kind = "openai" # 或 anthropic
model = "你选择的模型标识"
api_key_env = "OPENAI_API_KEY" # Anthropic 使用 ANTHROPIC_API_KEY
# base_url = "https://api.openai.com/v1"
```

兼容 OpenAI Chat Completions 的其他模型服务：

```toml
[provider]
kind = "openai_chat"
model = "服务中实际存在的模型"
base_url = "https://你的模型服务/v1"
api_key_env = "MODEL_API_KEY"
structured_outputs = true
# 若服务支持 JSON mode 而不支持 JSON Schema，可设为 false。
```

自定义 CLI：

```toml
[provider]
kind = "command"
command = ["/opt/tools/agent-wrapper", "--option"]
env_allowlist = ["MY_MODEL_KEY"]
```

stdin 接收完整提示和来源 JSON，stdout 只输出一个符合 `DigestOutput` 的 JSON 文档。程序退出码非零、超时、非法 JSON 或虚构引用均判为失败。使用参数数组启动进程，绝不通过 shell 拼接执行。仅信任管理员维护的命令及参数，Generic CLI 自身的工具权限需要由其配置控制。

`kind="extractive"` 为明确标识的原文摘录模式，适用于无模型开发环境。模型失败不会自动降级后谎称完成 AI 汇报。
