# 网页与直接来源解读

采集任务把来源正文、中文译文和内容解读分别持久化。Android 和 iOS 的消息详情展示中文摘要、关键要点、关注价值，以及可展开的中英正文；手机刷新只读取缓存，不触发模型调用。

## 读取范围

- 网页 / RSS 消息先读取原文网页，再读取原文正文或订阅摘要直接给出的链接。
- X 使用官方 API 的展开链接、长推文链接和一层引用推文中的链接；文本导入及 Facebook 消息也识别正文中的显式 URL。
- 每条消息最多读取原文之外 4 个直接资源。仅原文网页可以补充关联链接；关联页面即便含其他链接，也不会继续扩展。
- 明确提到的应用名称只能通过管理员维护的 `reading.mention_catalog` 解析。默认包含 ChatGPT、Claude Code、Gemini CLI、Cursor 的官方页面；不猜测未知名称对应的网址，不做搜索扩展。
- 导航、页脚、登录、隐私、图片、安装包等链接被过滤。模型只能分析给定材料，不能浏览、下载应用、登录或执行网页指令。

## 缓存与中文

URL 归一化后共享正文，默认 24 小时内不重复下载；支持 ETag / Last-Modified。正文内容哈希与解读规则版本共同决定摘要缓存；正文不变时不会重复总结。全文翻译复用已有 DeepSeek 分段翻译、校对及持久化机制。相同正文被不同消息引用、修改热度、再次生成日报都不会重复翻译。

日报复用已保存的中文解读，并将引用归到所属原消息，区分资源作者与转发者；抓取时间不能作为文章发布时间。历史日报不会自动改写，管理员可按需指定日期强制重新生成。

中文正文只有通过校对才显示，失败保留原文及明确状态。DeepSeek 真实 HTTP 402 沿用手机余额不足提示。翻译或校对不能保证绝对无误，原文和来源链接始终保留以便核对。

## 限制与访问安全

使用现有 Trafilatura 提取 HTML，pypdf 提取有文字层的 PDF。每个文档最多 8 MB、60,000 字符、40 页 PDF；被截断时明确标注部分正文。静态解析在独立进程中运行，Linux 限制内存 512 MB / CPU 20 秒，另有 30 秒超时。扫描件不做 OCR。需要动态渲染的页面通过隔离浏览器或已许可的手机读取流程补读，见 [网页采集说明](WEB-AUTHORIZATION.md)；登录、付费和网站访问限制仍需满足，失败状态会显示在手机端。

每次请求及跳转都校验公开 IP 并固定连接地址，通过 HTTPX 官方 SNI 扩展保留 HTTPS 证书校验；不携带账户 Cookie、认证头或环境代理。遵守 robots.txt，拒绝私网 / 本机 / 云元数据地址，限制跳转次数、响应大小和耗时。

## 配置与补处理

生产外部配置 `/etc/ai-radar/config.toml` 增加：

```toml
[reading]
enabled = true
max_links_per_article = 4
max_documents = 24
concurrency = 2
refresh_hours = 24
revision = "reading-zh-v1"
```

`max_documents` 限制每轮下载及待处理文档数，积压在后续任务继续。普通采集 / 日报任务自动处理；管理员可调用 `POST /v1/admin/jobs?kind=read` 只补读已有消息，避免再次请求 X。`force=true` 允许失败读取、解读和未通过校对的全文译文立即重试；已成功且仍在缓存期的正文、解读和译文不会重做。普通任务遵循重试时间及次数。

如果正文已经保存，只需补齐中文，可使用 `radar translate --force` 或 `POST /v1/admin/jobs?kind=translate&force=true`。统一翻译队列覆盖仍绑定消息的已保存网页，复用草稿及已审段落，不重新抓网页或生成摘要。`translation.resource_counts` 与任务消息单独报告网页中文进度，主消息全部有中文不等于网页全文全部完成。

摘要使用现有可替换 Provider 配置（Codex / Claude / 自定义 CLI / SDK），翻译独立使用 Translation 配置。原消息、原文和收藏保持不变；开发者仅改进服务器逻辑，由正式服务器流程负责生成与纠错，详见 [翻译职责与重审说明](TRANSLATION.md)。

X 展开链接依据：[官方 Post Lookup](https://docs.x.com/x-api/posts/lookup/introduction) 与 [数据字典](https://docs.x.com/x-api/fundamentals/data-dictionary)。

实现依据：[Trafilatura 官方文档](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)、[HTTPX 官方 SNI 扩展](https://www.python-httpx.org/advanced/extensions/)、[pypdf 文字提取与资源限制说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。
