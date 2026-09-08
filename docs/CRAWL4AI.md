# Crawl4AI 网页读取

服务器公开 HTTP 阅读仍优先使用既有解析器。需要浏览器的页面继续由隔离的 Playwright 打开，在既有公网代理、robots、目标文章身份与登录检查后，将已渲染 HTML 交给 Crawl4AI 0.9.3 官方 LXML 和 Markdown API 提取。保留段落、标题层级、代码、表格及直接链接，之后由既有服务器翻译、事实审核、解读和哈希缓存流程处理。

这次没有启用 Crawl4AI 的独立浏览器、Docker API、LLM 提取、代理轮换或深度爬取。网站访问依旧使用腾讯云服务器网络，Crawl4AI 无法保证通过 Cloudflare 或网站自己的登录验证。只处理原页面和明确的直接关联资源，不继续递归扩展。

## 自动处理

- 动态页面最多等待 12 秒，以正文稳定为依据，并进行最多三次有限滚动；未稳定时标记正文可能不完整。没有自动点击、验证码操作或无限滚动。
- 后台同域浏览器最多复用 8 次，空闲 90 秒后释放。交互授权窗口单独开启，网站资料沿用已有专用 profile。
- 网站返回临时访问限制时，按域名持久化六小时冷却，后续正式采集任务到期自动复查。并发请求先占用该次检查，失败或取消不导致其他文章连续重试。实际登录要求及用户暂停保持暂停；robots 禁止、私网和体积限制仍保留原状态。
- Crawl4AI 子进程最多 20 秒，失败后既有解析器最多再运行 10 秒。响应记录实际提取引擎和回退原因，不能将回退称作 Crawl4AI 成功。

## 部署和隔离

`RADAR_BROWSER_EXTRACTOR=crawl4ai` 只配置到浏览器 systemd unit，可切回 `trafilatura`。主 `.venv` 沿用原 `server/requirements.lock`；浏览器 `.browser-venv` 使用 Python 3.12+ 和独立 `server/requirements-browser.lock`（完整版本与哈希），固定 Playwright 1.62.0 和 Crawl4AI 0.9.3。不运行 `crawl4ai-setup`，不下载第二套浏览器或模型权重。

提取子进程仅接收当前 HTML 和最终 URL，采用临时 HOME、清理后的环境、无网络及进程创建的审计限制，Linux 限制 768 MB 地址空间和 15 秒 CPU；不接收模型密钥、worker token、登录 profile。依赖虽然包含上游 LiteLLM 分支和数值计算组件，这条链路不会启动模型调用。API 的模型提供者、译文、原文及数据库结构均不因接入而人工修改。

使用 0.9.3 避开上游已公开的旧版本 PDF SSRF 修复前版本，且本接入只对已经取得的 HTML 离线提取，不将任意 URL 交给 Crawl4AI 下载。参见[上游安全公告](https://github.com/unclecode/crawl4ai/security/advisories/GHSA-q5rj-45vw-vp2g)、[Markdown 文档](https://docs.crawl4ai.com/core/markdown-generation/)。

## 验证

本地真实依赖回归涵盖标题、代码缩进、表格数字、合法 HTML base、30 个直接链接、60,000 字符截断，以及恶意图片/iframe/PDF 引用不下载。独立 Chromium 测试在 3.3 秒后插入正文，验证能等到内容出现，不沿用原先固定 1.8 秒的读取时机。服务器上线和实际网页结果完成后记录于此。

## 致谢

This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).
