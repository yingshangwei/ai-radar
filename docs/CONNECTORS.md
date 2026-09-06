# 数据源与模型接入

## X / Twitter

在服务器私有环境文件配置 `X_BEARER_TOKEN`，其值来自 X Developer 应用的官方授权。使用 `/2/tweets/search/recent`，读取 `created_at`、`public_metrics`、`note_tweet`、`referenced_tweets` 和作者/引用扩展。搜索查询、页数和每页条数在 `config.toml` 配置。

默认同时搜索 AI 关键词和重点名单。每个查询最多 `x_max_pages=2` 页，每页 `x_page_size=100` 条（可设为 10–100）；重点名单每 12 个账号组成一个查询。初次联调可配置每页 10 条、每查询 1 页。页数和每页条数限制结果规模，不能保证完整覆盖，也不是金额上限。X 的具体价格、配额、搜索权限由你的账户决定。没有 Token、权限失效或额度用尽时保留明确状态，不自动改用未经授权的抓取工具。

长帖优先读取 `note_tweet.text`；引用帖原文单独标明作者及原始时间，使“现已向所有用户开放”这类依赖上下文的更新能正确进入 AI 筛选。主帖始终保留自己的发布时间与互动数，引用帖不会被重复当成新发布。引用原文不可用时明确标注；API 返回有效主帖和引用资源错误时保留可读主帖，没有有效数据的错误仍报错。不会借用被回复帖的 AI 关键词，把无关问候误收为新闻。

字段实现依据 [X 数据字典](https://docs.x.com/x-api/fundamentals/data-dictionary) 与 [扩展对象说明](https://docs.x.com/x-api/fundamentals/expansions)，已通过接口契约测试，真实付费 API 调用仍待凭据接入。

[X 官方搜索接入文档](https://docs.x.com/x-api/posts/search/integrate/overview)

2026-09-07 接入状态：用户已登录 X，AI Radar 开发者账号与应用已创建；余额为 0，最低充值选项为 5 美元。未付款，等待用户决定是否使用付费 API；尚未提取或配置 Bearer Token，定时任务不会因此产生 X API 费用。随后通过用户已登录的浏览器读取并导入了 8 条真实公开帖，完成模型日报验证；这不是定时 X 采集已开通。

## Facebook

配置 `FACEBOOK_ACCESS_TOKEN` 与 `facebook_page_ids`（数字 Page ID）；Graph API 版本通过 `facebook_version` 替换。请求已授权 Page 的 `/posts`，读取原文、发布时间、原始链接、反应数、评论数和分享数。

读取自己管理的 Page 和读取第三方公开 Page 所需权限不同。普通 Facebook 登录不自动带来 Graph API 读取权限；第三方 Page 可能需要 Meta 审核通过的 Page Public Content Access。不能把没有权限返回的空内容当作“没有新闻”。

[Meta Page Public Content Access](https://developers.facebook.com/docs/features-reference/page-public-content-access/)

2026-09-07 用户反馈 Facebook 账号需要审核，暂时无法继续授权。保留未接通状态；已另外接入 Meta Newsroom 官方 RSS，补充公司的一手 AI 新闻。这不覆盖 Facebook 用户、Page 或社交互动数据。

## 官方网站与 RSS

直接使用发布方的公开内容，作为独立的一手渠道。Anthropic 新闻页通过 Beautiful Soup 解析可见的标题、日期和简介，不使用未公开的内部 API。仅公布日期的来源会标记 `published_precision=date`，归一化为 UTC 当日零点；这不声称知道准确发布时刻。页面结构改变后会报告解析错误。

Meta 使用 [官方 Newsroom RSS](https://about.fb.com/feed/)，沿用相同日期、主题与去重规则；全站新闻中的非 AI 内容会被过滤。目前 Meta 输入使用 RSS 原始摘录，正文提取白名单尚未包含 Meta，摘要模型会收到明确的摘录证据标记。

生成日报时使用成熟的 [Trafilatura](https://trafilatura.readthedocs.io/en/latest/quickstart.html) 提取官方文章正文，避免只依据 RSS 标题分析。仅允许明确列出的官方发布方 HTTPS 域名，逐次验证重定向；用户导入的任意 URL 不会触发服务端抓取。正文读取失败时保留原始摘录，并向摘要模型标明证据类型。可用 `enrich_official_articles=false` 关闭。

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
