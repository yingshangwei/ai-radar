# X 数据供应商与费用保护

2026-09-17：AI Radar 默认配置选择 TwitterAPI.io，官方 X API 保留兼容代码，但不会自动回退。无需新增数据库、消息队列、Agent 或爬虫运行时依赖。本文描述代码能力；真实覆盖率必须以授权后的运行结果为准。

## 配置与授权

`server/config.toml` 的 `[x_data]` 独立于模型路由：

- `provider = "twitterapi_io"`；`api_key_env = "TWITTERAPI_IO_KEY"`。
- `monthly_usd = 8`、`daily_usd = 0.4`。按部署时区的自然日/月统计。
- `tweet_price_usd = 0.00015`，依据 2026-09-17 官方公开费率配置。
- `request_interval_seconds = 5.1`，兼容免费账号公开的 5 秒请求间隔。
- `overlap_minutes = 5`，相邻新窗口回看 5 分钟以容忍短暂收录延迟；并非无限延迟覆盖保证。
- `discovery_interval_minutes = 360`，广泛关键词搜索每 6 小时最多跑现有两页；关注账号仍每 30 分钟。若两页每次都返回 40 条，单关键词链路约 0.72 美元/月，而每 30 分钟搜索会约 8.64 美元/月。
- `shadow_enabled = false`，Apify 授权及账户消费限制验证后才启用；月预算 2 美元、日预算 0.1 美元。
- `credentials_file` 可指定受保护的服务端 dotenv 文件；缺少环境变量时每次调用读取，凭据补齐/轮换无需重启进程。生产使用 `/var/lib/ai-radar/x-providers.env`，仅服务账户可读写。

在 TwitterAPI.io 控制台取得 API Key，在 Apify 设置中取得 Token；分别配置 `TWITTERAPI_IO_KEY`、`APIFY_API_TOKEN`。不需要交出 X 登录 Cookie，也不自动购买套餐、开启充值或创建供应商的持续监控规则。Apify 对照优先使用已有免费额度。

本地 `credentials/x-providers.env` 被 Git 忽略且权限为 0600；禁止将它或任一凭据打入 App/OTA。服务器只向已认证设备返回供应商名称、公开控制台入口与用量汇总，不返回密钥或游标。

## 采集与费用账本

TwitterAPI.io 使用 Advanced Search、`Latest`、Unix 时间范围和每页最多 20 条；不会反复取完整时间线。固定窗口与逐页入库保留，供应商拥有独立进度空间，旧官方进度不被覆盖或复用。原文、摘要、翻译仍走原有服务端流程。

`x_data_calls` 在网络请求前事务性预留单页最高预计费用；预算不足时不发请求。正常响应按返回条数、最低一条的费用估算结算，空响应也收费。超时/异常退出保留预留，不因重启、重试或文章入库失败而退款。保存文章及采集游标仍在同一事务，网络费用在独立事务，不会随入库回滚消失。并发预留受数据库锁保护。

若响应超过约定计费页大小或实测费用超预留，将冻结该供应商后续付费请求，等待价格/接口核验。未知费用和供应商账单要区分：TwitterAPI.io 为费率估算，Apify 包含平台返回的运行费用和少量结果读取预留。预算只涵盖本服务发起的 X 数据请求，不含已有 LLM、服务器、其他客户端或供应商订阅消费；供应商改价可能改变账单，应同时使用平台侧消费限制/有限预付余额。

App 显示 X 数据月度合计、供应商和授权/余额/预算暂停原因。预算耗尽不会引导用户误充值官方 X，也不影响其他免费来源。自然日/月切换后按当前预算自动恢复；如果正常新增量持续超过预算，需要明确调整预算，不能承诺费用封顶下仍无限量采集。

## Apify 对照

使用 `xquik/x-tweet-scraper`，不是禁止频繁监控的 API Dojo Tweet Scraper V2。每天最多一次，轮换最多 3 个关注账号，查询滞后 10 分钟的一小时窗口，最多 60 条、每个账号最多 20 条；Actor 时间上限 120 秒、内存 256 MB、事件费用上限 0.02 美元，并预留 0.10 美元覆盖平台资源。

启动前写入账本，返回的 run ID 持久保存。后续采集轮次只检查该 run；启动超时且未取得 ID 时暂停新任务，不能猜测失败后重复 POST。完成后读取拥有者的 `usageTotalUsd`，另保留 0.002 美元用于结果读取/存储。平台资源可能独立收费，事件上限不是全平台账单的绝对上限，启用前应核实账户消费设置。

对照只存数量、ID 和交集指标，不触发额外翻译/模型调用，不将抽样结果当成完整漏帖率，也不自动把全部样本补入资讯流。优先检查 `sample_only` 的原帖、长帖完整度、回复/引用和收录延迟，再决定是否需要补采。原帖引用仅展开已返回的直接关系，不递归抓取对话、关注图或付费查询全部提及用户资料。

## 验证与参考

自动化回归覆盖：费用并发预留、跨日/月与重启、空结果收费、未知扣费保留、入库失败、分页续采和供应商隔离、时间/作者校验、长帖与引用/回复、重叠窗口去重、凭据热加载、Apify 不确定启动不重试及任务复用/账单统计。

- https://twitterapi.io/pricing
- https://twitterapi.io/qps-limits
- https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search
- https://apify.com/xquik/x-tweet-scraper
- https://docs.apify.com/api/v2/actors-runs-post
- https://apify.com/pricing
