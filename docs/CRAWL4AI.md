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

本地真实依赖回归涵盖标题、代码缩进、表格数字、合法 HTML base、30 个直接链接、60,000 字符截断，以及恶意图片/iframe/PDF 引用不下载。独立 Chromium 测试在 3.3 秒后插入正文，验证能等到内容出现，不沿用原先固定 1.8 秒的读取时机。最终实现的全量服务端、部署和真实 Chromium 回归为 1,468 passed / 14 skipped；真实 Crawl4AI 独立环境另外执行 29 passed / 1 skipped，两环境互补执行依赖相关用例。

## 腾讯云真实网页测试

2026-09-09 03:22（北京时间），从生产浏览器 worker 串行读取以下页面。测试仅产生私有诊断报告，不写文章数据库，不调用翻译或摘要模型。

| 页面 | 结果 | 提取字符 | 耗时 |
| --- | --- | ---: | ---: |
| [Anthropic 安全评估文章](https://www.anthropic.com/news/investigating-incidents-cybersecurity-evals) | Crawl4AI 成功 | 22,037 | 9.90 秒 |
| [Google 视频理解开发文档](https://ai.google.dev/gemini-api/docs/video-understanding) | Crawl4AI 成功 | 36,702 | 9.37 秒 |
| [arXiv 2609.02750 论文摘要页](https://arxiv.org/abs/2609.02750) | Crawl4AI 成功 | 4,536 | 6.34 秒 |
| [Hugging Face BenchMIRT](https://huggingface.co/blog/allenai/benchmirt) | Crawl4AI 成功 | 10,236 | 7.04 秒 |
| [OpenAI ChatGPT 介绍](https://openai.com/chatgpt/overview/) | 网站限制服务器访问 | 0 | 2.79 秒 |
| [AI Alignment 文章](https://ai-alignment.com/clarifying-ai-alignment-cec47cd69dd6) | 网站限制服务器访问 | 0 | 5.83 秒 |

四个成功结果均明确返回 `extraction_engine=crawl4ai`、`extraction_version=0.9.3`，未回退且未触发正文稳定超时。Google 样本保留 10 行 Markdown 表格和 423 行代码样式内容。成功读取并不等于已证明所有网页元素完整或论文 PDF 全文已读取；arXiv 本次测试的是摘要页。

两个访问限制结果没有保存验证页面或伪造正文。App 的公开授权状态接口已实际返回 OpenAI / AI Alignment `action=retry`、`automatic=true` 和下一次重试时间。公开 API 健康 200、无令牌访问 401、reader 网站列表 200；浏览器在这批请求后无重启，cgroup 内存峰值约 649 MiB，低于现有 1,100 MiB 限额。

初次正文复查发现部分推荐/反馈组件混入，尤其 Google 的惰性模板内容和 Hugging Face 评论、推荐区域；已经通过解析器结构选择修复（`7f9c736`），未人工改写文章。对于无法确定边界的嵌套文章，优先保留脚注和证据，不能按“Related content”等文字直接截断。arXiv 页仍可能包含出版元数据和工具区，不能把通用提取等同于针对所有网站的完美正文清理。

可复用诊断入口是 `scripts/test-browser-reader.py`，固定连接本机 worker，使用环境中的私有 worker token，输出文件为 0600；报告包含正文首尾片段，不应公开发布。初次云报告位于 `/var/lib/ai-radar/crawl-diagnostics/20260908T192208Z-914d7807/report.json`，TAT `inv-n8ebxg0vv1` SUCCESS / 0。

03:36 的生产复测确认 Google 为 35,906 字符、8 个直接链接、10 行表格、423 行代码样式内容，耗时 8.89 秒；Hugging Face 为 9,711 字符、6 个直接链接，耗时 7.18 秒。二者均为真实 Crawl4AI，无回退；Google 的反馈 JSON 和 HF 推荐/评论区域消失，正文末段保留。少量 UI 标签（例如 Google 的反馈提问文字）仍可能出现，不宣称正文完全没有杂项。

修正后的真实依赖回归 32 passed / 1 skipped；四份实际 HTML 离线对比确认所有标题不变、Google 代码和表格保留、Anthropic 脚注未丢、arXiv 原结构未变。部署前后分别在候选包、实际生产路径运行三个独立 fixture，均无网络/模型调用。补丁仅原子更新正文解析子进程模块，API/浏览器/Caddy PID 不变。

复测 TAT `inv-e8eccig7xv`、最终源码/保护文件/正文复验 `inv-n8ece6023s` 均 SUCCESS / 0。最终浏览器无重启，内存峰值约 699 MiB。复测报告：`/var/lib/ai-radar/crawl-diagnostics/20260908T193605Z-a77914c7/report.json`。

## 致谢

This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).
