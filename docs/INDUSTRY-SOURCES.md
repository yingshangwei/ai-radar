# 免费行业研究原始来源

实现：`server/radar/industry_sources.py`。它提供 `SOURCES`、`ENTITIES`、`SourceSpec`、`EvidenceInput` 与 `fetch_source`；调度、持久化及行业判断由服务器统一流程完成。采集器不调用模型、不开通账号、不读取 X 或其他提供商凭据。

## 已实现范围

| 来源 ID | 内容 | 获取方式与窗口 | 刷新建议 |
|---|---|---|---|
| `fed-monetary` | 美联储货币政策声明、纪要标题与摘要 | 官方 RSS 当前窗口；不抓全文 | 30 分钟 |
| `federal-register-ai` | AI、芯片、数据中心政策标题、日期、摘要与部门 | 官方 JSON，最多 2 页 × 100 项，本地再次按主题与日期过滤 | 60 分钟 |
| `nvidia-news` | NVIDIA 官方产品、合作和业务公告 | 官方 RSS，最多补最新 3 篇固定新闻路径正文 | 30 分钟 |
| `amd-ir` | AMD 财报、产品、合作公告 | 官方 IR RSS，最多补最新 3 篇固定公告路径正文 | 30 分钟 |
| `broadcom-ir` | Broadcom 财报、芯片、网络和软件公告 | 官方 IR RSS，最多补最新 3 篇固定公告路径正文 | 30 分钟 |
| `sec-*`，17 家 | NVIDIA、AMD、Broadcom、Micron、Microsoft、Amazon、Alphabet、Meta、Oracle、Salesforce、ServiceNow、Adobe、Palantir、Intuit、ADP、Accenture、IBM | 每个已知 CIK 的 submissions 最近窗口；最多补最新 1 份 8-K / 10-Q / 10-K / 20-F / 6-K 主文档 | 60 分钟 |

实体目录共有 25 家上市公司，用 `infrastructure` / `software` / `workflows` 三种主题；目录不是已接通 25 家全文源的声明。交易代码标识所选上市证券，CIK 标识发行主体；例如 TSM、SAP 对应美国 ADR。Schneider 使用巴黎上市证券，未配 SEC 采集源。

默认回看 90 天、每源最多 12 项；可接受 1–365 天、1–30 项。RSS 本身可能只包含 10–20 条，SEC 此版本只读最近 submissions，不读取其历史分片，因此回看天数不等于历史完整性。每项明确 `history_complete=false`。

## 证据契约

`EvidenceInput` 字段：`source_id, external_id, url, title, text, published_at, published_precision, kind, entity_ids, metadata`。`published_at` 是 UTC ISO 时间字符串；日期级政策保留 `published_precision=date`，UTC 零点仅作存储锚点。缺少发布日期、时区不明、未来或超窗口的条目被拒绝；不以采集时间、更新日期、财年结束日补发布日期。

`metadata.evidence_scope` 明确区分：

- `headline_only`：只有原始标题，不足以支持经营判断。
- `feed_summary` / `feed_content`：RSS 摘要或内容字段，不推断其完整性。
- `publisher_article`：已从该公司固定正文容器读取的公开公告正文。仍是发行人表述，需保留与独立证据的区别。
- `policy_abstract`：Federal Register 摘要，不等于全部法规条文。
- `filing_metadata`：SEC 申报通知，`kind=filing_notice`；只包含主体、表单和 accession 等元数据。
- `filing_primary_document`：SEC 主文档，`kind=filing_document`；不包含所有附件，`includes_exhibits=false`。识别到仅引用业绩附件的简短 8-K 封面时，保留 notice，标记 `document_fetch=cover_only`。

`is_full_text`、`truncated`、`partial` 描述范围与截断。全文单项最多 60,000 字符；HTTP 正文最多 2MB；文章正文提取输入最多 400,000 字符。若摘要接口成功而补正文失败，保留原始摘要并标记 `article_fetch=unavailable, partial=true`。SEC 主文档失败不删除 notice；模型不能根据 notice 猜业绩、利润或资本开支。

成功返回空列表表示本次无符合条件条目。来源访问受限、限流、格式错误、网络失败分别抛出 `IndustrySourceError`；其 `code/status/message` 均为固定、安全错误，不包含上游响应正文或凭据。

## 网络与范围限制

只接受与静态 `SOURCES` 完全相同的来源。使用直接构造的 `httpx.Request`，不继承调用者 client 的默认 Authorization、API key 头或 cookie jar，显式 `auth=None`、`follow_redirects=False`。所有 URL 必须为固定主机的 HTTPS，无用户信息、异常端口或反斜杠。只有列出的公司新闻路径允许补正文，正文链接不会继续被爬取。

Federal Register 分页 URL 由固定 API 地址与参数构造，绝不追随返回的 `next_page_url`。搜索参数可能被上游忽略，因此标题及摘要还须通过本地 AI、半导体、数据中心明确关键词复核；单独的 `chip` 或 `ai` 不足以通过。

SEC 只使用已知 CIK 与严格校验的 accession / 文档文件名生成 `www.sec.gov/Archives/edgar/data/...` 地址。进程内请求串行，每次完成后至少间隔 250ms。外部多进程部署仍需统一总限流；SEC 官方上限是每用户所有机器合计 10 请求/秒。

每次网络读取上限 25 秒，拒绝重定向、非预期内容类型、超大响应及 XML DTD/entity 声明；RSS 最多解析 200 项，政策最多 2 页。调度器须尊重来源刷新间隔与失败退避；本模块本身不建立定时任务。

## 本地真实验收及限制

无凭据探针证据保存在 Git 忽略目录 `dist/industry-research/source-probe.json` 和 `issuer-probe.json`。前者记录各源本次返回证据、实际正文长度与范围；后者对已启用 SEC CIK 的返回发行人、ticker 进行核对。实际数量以探针文件为准，不是未来持续可用性保证。

本次 Fed、Federal Register、NVIDIA、AMD、Broadcom 以及 `data.sec.gov` 无凭据 HTTP 请求可用。SEC Archives 与 submissions 域名的可达性不同，已观察到 Archives 403，因此降级为 notice，不能声称已拿到所有 SEC 全文。Vertiv / Micron / ServiceNow / Salesforce 的候选 IR 页面本次直接读取 403，未将这些页面登记成已接通的 RSS；其部分公司仍可通过 SEC 元数据采集。

免费意味着当前不需要付费账号，不代表无限转载授权。NVIDIA RSS 官方条款限个人非商业信息用途；当前实现面向用户个人研究。企业公告与政策的引用应保持原链接和范围标识。此模块不接付费实时新闻、市场一致预期或完整商业历史；不能计算缺少对照数据的“超预期概率”。

## 官方依据

- [Fed RSS](https://www.federalreserve.gov/feeds/feeds.htm)
- [Federal Register API](https://www.federalregister.gov/reader-aids/developer-resources/rest-api)
- [NVIDIA RSS 与使用条件](https://www.nvidia.com/en-us/about-nvidia/rss/)
- [NVIDIA 新闻源目录](https://nvidianews.nvidia.com/rss)
- [AMD IR 页面上的官方 RSS 链接](https://ir.amd.com/)
- [Broadcom RSS 目录](https://investors.broadcom.com/rss-feeds)
- [SEC submissions 与 XBRL API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [SEC 公平访问规则](https://www.sec.gov/about/developer-resources)

SEC 官方所谓 submissions 典型处理小于一秒，是披露分发后的 API 更新处理时间；本系统当前按分钟轮询，不能据此宣称秒级端到端到达。
