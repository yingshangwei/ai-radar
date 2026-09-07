# 腾讯云部署

目标实例：`lhins-e5gcg722`，`ap-seoul`，公网 `43.155.203.253`，Ubuntu Server 24.04 LTS，2 核 4 GB。

## 当前部署状态 · 2026-09-08

当前 release 为 `/opt/ai-radar/releases/20260908-07015b7`，TAT `inv-m8d69c0sm9` 激活成功（SUCCESS，退出 0），实际输出为 `dist/cloud/translation-number-context-activation.txt`。完整测试 **732 passed、6 skipped**，Ruff 通过；跳过项为既有浏览器环境限制。本版修复通用数字、百分比及账号校验边界，未改动模型配置、文章原文或候选译文，也未启动模型任务。

归档 `dist/ai-radar-server-20260908-07015b7.tar.gz` 为 128,176 字节、48 份已提交文件、6 个传输分片，SHA-256 `8b15b4094c0b766d739e778799334d4d67901a5d08a1cc9296264b3e8269a5e5`，清单 `dist/cloud/translation-number-context-release.json`。逐文件比对提交 `07015b7` 与归档一致，包含新增 `translation_numbers.py`；分片重组及嵌入的源码清单一致。预检 `inv-n8d6870h30` 生成 `/var/lib/ai-radar/backups/radar-translation-number-context-20260907T215422Z.db`。

部署保留 14 张表、三份当前私有配置、Caddy、原 8080 服务及九组历史证据。新增保护包括已结束的 `fafcf476` 阶段验证操作，以及阶段配置收据、原配置和数据库备份；收据指针 SHA、备份 SHA 与当前配置 `b98e3f85…` 均核对。新部署以已启用的阶段配置作为基线，不将旧预检中的配置哈希误当作当前值。旧操作、收据和私有输出均保持；独立虚拟环境、切换前空闲检查、冻结备份与回滚机制保留。

部署前只读预览 `inv-e8d66s0kah` 于 21:52:54 UTC 完成，检查主消息及 47 份绑定网页关联的 93 份缓存、277 个段落；97 个未完成段落的数字标记由 43 减至 31，账号标记由 2 减至 0，没有新增问题。96 个当前策略已审计段落前后均无机器检查问题。该检查仅在内存比较规则，不代表剩余语义疑点通过。激活后主消息 46 ready、47 份网页全文 13 ready / 34 review_required，余额告警为空，API / 浏览器 / Caddy 正常。21:57:56.910998 UTC 公网只读 `dist/cloud/translation-number-context-public.json` 通过：默认近七天 42 篇消息及 43 个关联资源（13 ready / 30 review_required），未通过的全文中文隐藏，401/403 权限隔离有效，无运行任务或授权窗口；调度器开启，X 与官方来源 healthy，Facebook auth_required。本次没有模型调用，也没有重试此前待审内容。Android / iOS 无需重建。

## 历史：4d196af 分阶段配置与验证

当时 release 为 `/opt/ai-radar/releases/20260908-4d196af`，TAT `inv-m8d4nk0pmn` 启用成功（SUCCESS，退出 0）。完整测试 **594 passed、6 skipped**，Ruff 通过。代码部署保持 14 张表、三份私有配置、Caddy、原 8080 服务和七组历史证据；实际启用输出为 `dist/cloud/translation-stage-options-activation.txt`。

归档 `dist/ai-radar-server-20260908-4d196af.tar.gz` 为 127,211 字节、47 份文件、6 个传输分片，SHA-256 `39031635997a779dfb3f03df8aafdba211c70e6871db8506fed9a0d02ae3b588`，清单 `dist/cloud/translation-stage-options-release.json`。预检 `inv-j8d4mgg5fd` 生成 `/var/lib/ai-radar/backups/radar-translation-stage-options-20260907T210017Z.db`。配置切换与真实模型验证单独记录，不能把代码部署当作翻译通过证明；Android / iOS 无需重建。

配置随后由独立 TAT `inv-n8d4v4g0xk` 应用，严格只改七处叶字段，开启校对/审计的 high 推理与 32768 token，单次请求总时限 300 秒；初稿保持 disabled / 12000。备份与差异元数据为 `/var/lib/ai-radar/translation-stage-config/20260907T210825Z-e965fb23/metadata.json`，切换仅重启 `ai-radar.service`，14 张表、两个凭据文件、七组历史及其他服务保留。原配置 SHA-256 `605fa438519e34b37074e8c07b64cc61e2920f8a80a04430ecdc62d978bedc59` → 新配置 `b98e3f85d9d685370c77e747803c8be1fa3343a5e154dafc505925870d2f56b7`。真实验证使用新的独立操作基线，并逐项验证合法配置差异；旧操作记录未改写。

正式一次单条验证 `inv-m8d5020e3h` 对应任务 `fafcf476-8167-4070-ba94-41d7da07fa82`，21:11:36–21:18:13.821015 UTC 执行结束，error 转为待审，未新增整篇 ready。4 次校对/审计均正常返回并报告推理 token，确认当前配置实际生效。最终只读 `inv-e8d57ignvx` SUCCESS，实际输出 `dist/cloud/translation-stage-validation-final.txt` / `.json`；原文、60 份既有 ready、所有未选缓存、11 张表、配置与七组历史均保持。验证只表示结束及保护通过，不能解释为翻译质量通过。

全量主消息 46 ready，47 份网页全文 13 ready / 34 review_required / 0 error。21:19:44 UTC 公网 `dist/cloud/translation-stage-public.json` 核验默认近七天 42 篇消息及 43 个资源（13 ready / 30 review_required），未通过全文隐藏，401/403 权限隔离有效；X 与官方信息源 healthy、Facebook auth_required，余额告警为空，调度器开启，无运行任务、租约或授权窗口。没有继续重跑这次已结束的验证。

## 历史：f574f87 部署与字面量恢复

当时 release 为 `/opt/ai-radar/releases/20260908-f574f87`，TAT `inv-m8d3qqgvhd` 启用成功（SUCCESS，退出 0）。完整服务器测试 **506 passed、6 skipped**，Ruff 通过；跳过项仍为既有浏览器环境相关测试。部署前后 14 张表、三份私有配置、Caddy、原 8080 服务及六组历史证据保持不变。第六组包含已结束的 errors-only 操作之 latest、metadata、数据库备份与私有 CLI 输出，旧诊断证据未被覆盖。

当时归档 `dist/ai-radar-server-20260908-f574f87.tar.gz`，125,817 字节、47 份已提交文件、6 个传输分片，SHA-256 `1d179e7a0b14d8f6acd31379e8ef646deb7f89238b99f327b719bdc5671d8c4b`；清单 `dist/cloud/translation-calendar-context-release.json`。预检 `inv-e8d3pbg22j` 生成备份 `/var/lib/ai-radar/backups/radar-translation-calendar-context-20260907T202807Z.db`。仍采用独立虚拟环境、锁定依赖、切换前空闲检查与冻结备份，不修改模型配置或移动端源码。

该版继承 `cae1e18` 的通用恢复：初稿和校对遇到受保护字面量对应错误时，以同一原文和输入草稿请求模型重新输出；与 JSON/结构恢复共用最多两次请求预算，不手工补链接或替换译文。数字校验识别有上下文依据的月份及货币等价表示，排除明确人名、重量语境以及小数/版本末尾被错误识别为日期的情况。独立语义审计、真实数字/链接差异与未解决疑点的拒绝门槛保留。

新一次正式 errors-only 操作由 `inv-m8d3sj08ug` 成功启动，任务 `c10b0ead-2174-4239-bd61-53294353a8ce` 于 2026-09-07 20:31:38.714907–20:32:03.448959 UTC 执行完成，只选择 1 份现存 error 缓存，结果仍为 error，分类为校对阶段 `protected_literal_mismatch`。没有再次重试或发布；独立元数据与只读影响检查见 [验证记录](VALIDATION.md)。

最终验收 `inv-m8d3t8g1sa` 于 20:32:18 UTC 成功，实际输出保存于 `dist/cloud/translation-calendar-literal-final.txt` / `.json`。`verification_passed=true` 仅表示操作结束及保护核验通过，`translation_ready=false`。60 份既有 ready、34 份未选待审缓存、11 张受保护表、配置、原文、旧补译及 `bccb692e` 操作不变；单元 exited、退出 0。数据库全量仍为主消息 46 ready、47 份网页正文 13 ready / 33 review_required / 1 error，全文中文尚未补齐。

20:33:24.632861 UTC 的公网只读验证 `dist/cloud/translation-integrity-public.json` 成功：状态总消息数 46，默认近七天可见 42 篇消息、43 个关联资源，资源为 13 ready / 29 review_required / 1 error；可见范围与数据库全量分开统计。未通过的全文中文不暴露，无令牌读取 401、reader 管理调用 403，无运行中任务或授权窗口，余额告警为空，调度器开启，X 与官方信息源 healthy，Facebook auth_required。本次服务器升级无需重建 Android / iOS；iOS 本机运行与 Apple 访问授权状态见 [IOS.md](IOS.md)。

## 历史：37e24fc 部署与 errors-only 恢复

当时 release 为 `/opt/ai-radar/releases/20260908-37e24fc`，TAT `inv-e8d1wbgit4` 启用成功（SUCCESS，退出 0）。部署前后 14 张表的数据与三份私有配置保持不变，Caddy、原 8080 服务及历史诊断基线保留。服务器完整测试 **386 passed、6 skipped**，Ruff 通过；跳过项为既有浏览器环境相关测试。此次仅修改服务器，Android / iOS 源码未变，不需要重新构建移动端。

当时源码归档为 `dist/ai-radar-server-20260908-37e24fc.tar.gz`，123,815 字节，SHA-256 `d2993b78cec1fb3338eca0cd37a34254ba092741494fec292d458ac2cc2458e0`，包含 47 份已提交文件与校验清单；本机清单为 `dist/cloud/translation-stage-recovery-release.json`。每版使用独立虚拟环境与锁定依赖；依赖准备期间旧 API 继续服务，切换前再次检查任务与授权窗口为空并冻结备份。旧 release、各次诊断脚本、输出和数据库基线保留，未用新结果覆盖历史证据。

关联网页现在按当前正文进入统一翻译队列：主消息优先、共享内容去重，排除无关联、空正文和旧缓存；`force` 只补未完成内容，保留已通过段落和整篇 ready 缓存。状态中的主消息 `counts` 与网页 `resource_counts` 分别汇报。正式 `radar translate --limit 1 --force` 可在服务空闲时限制单批处理量，不改变配置文件、原文、模型或缓存键，也不触发网页读取与摘要。

连续真实诊断定位并修复了多段审计 ID 未对应、响应结构不合格，以及疑点数组超过旧版 12 条上限的问题。当前保留完整校对和审计疑点；任一未解决疑点仍阻止发布，两轮修订上限、输出大小限制及原文/候选指纹校验保留。单次诊断采用独立私有基线与日志，重复观察不重新提交；检查原文等 11 张受保护表、58 份既有 ready 缓存、未选缓存、三份私有配置及旧操作记录。

此前单条任务 `157a3e92`（ID 前缀）于 2026-09-07 18:37:01–18:37:35 UTC 结束时仍未通过；验收 `inv-98d0gi0qh2` 确认两个已通过段落复用，原文、未选缓存和 58 份既有 ready 缓存不变。随后 `34a57f1` 通过 `inv-n8d0tcgh6c` 启动单条任务 `02be5af3-107d-42f0-ab3c-e1b1946d5b1d`，已 completed；验收 `inv-e8d0uigww3` 成功，本轮未记录协议失败、两个通过段落复用，但整篇仍为 review_required。网页统计为 11 ready、33 error、3 review_required，主消息 46 ready；不能声称全部回填。

已部署的 `7ab2ed6` 进一步明确 `issues` 只列有原文依据的未解决差异，不能混入通过的检查笔记；修订模型须核实 `checks`，不能据未经验证的意见增写原文没有的内容。正式补译任务 `61afa513-2046-4730-9200-9f9d9e209144`（提交 `inv-j8d18t05tr`）于 2026-09-07 19:04:23–19:24:50.928733 UTC 执行完成；这是原补译操作第 2 轮，选择原 36 份缓存，没有再运行单条诊断。19:25:54 UTC 只读验收 `inv-j8d1v0g2tu` 成功：所选 2 ready、26 review_required、8 error，全部网页 13 ready、26 review_required、8 error，主消息 46 ready。11 张受保护表、58 份既有 ready 缓存、未选缓存、配置与原文不变，部署基线 9 份缓存中的 14 个已通过段落保留。

本次 `37e24fc` 预检 `inv-e8d1vs0h3p` 成功，备份 `/var/lib/ai-radar/backups/radar-translation-stage-recovery-20260907T192644Z.db`；随后启用 `inv-e8d1wbgit4` 已成功，原数据、配置、服务及历史基线均保留。

该版将一次安全 JSON/结构反馈恢复共用于初稿、校对和审计；缺少 `approved` 不会被自动补成批准。正式 `radar translate --limit N --errors-only --force` 仅选择当前绑定的既有 error 缓存，不创建缺失缓存，不选择待审或 ready 内容；ID、原文占位符和语义门槛仍保留。启动 `inv-n8d1xi0t6e` 成功，任务 `bccb692e-9199-4316-a347-c567e223fd68` 于 2026-09-07 19:28:41–19:40:29.942517 UTC 执行完成，选中 8 份错误缓存，结果 7 review_required、1 error。独立元数据位于 `/var/lib/ai-radar/translation-errors-recovery/20260907T192839351351Z-7c824b11/metadata.json`。

19:29:20 UTC 的只读进展 `inv-e8d207gpge` 保留为历史；最终验收 `inv-m8d2bx03js` 于 19:41:51 UTC 成功，确认 60 份既有 ready 缓存、27 份未选待审缓存（26 份网页及 1 份其他历史记录）、11 张受保护表、配置、原文和原补译基线不变。`verification_passed=true` 仅表示操作已结束且保护核验通过，`translation_ready=false`；无运行中的任务、活动租约或授权窗口，systemd 单元 exited、退出 0。本轮最终未留下 Schema 或 ID 协议错误，唯一失败为校对阶段 `protected_literal_mismatch`，仍按原门槛拦截，没有再次重试。

最终数据库全量为主消息 46 ready、47 份网页正文 13 ready / 33 review_required / 1 error。19:41:58 UTC 公网只读验证 `dist/cloud/web-translation-public.json` 成功：`status` 总消息数 46，默认近七天返回 42 篇消息及 43 个关联资源，资源为 13 ready / 29 review_required / 1 error；可见范围不等于数据库全量。未授权读取 401、reader 管理调用 403，未审核全文 `text_zh` 不暴露，余额告警为空，X healthy、Facebook auth_required。此次服务器升级无需重建移动端；全文未全部完成，详见 [验证记录](VALIDATION.md)。

## 历史部署记录

`20260908-cae1e18` 由 TAT `inv-e8d3b6g244` 成功启用，完整测试 484 passed、6 skipped，Ruff 通过。随后只读影响检查发现三个新数字标记，未启动模型任务；通用月份上下文与小数边界修复后才前向升级至当时的 `f574f87`。该版归档与 `translation-integrity-recovery` 证据保留。

`20260908-7ab2ed6` 由 TAT `inv-n8d17a0xk5` 成功启用。归档 `dist/ai-radar-server-20260908-7ab2ed6.tar.gz`，123,303 字节，SHA-256 `b9248fd2b6f1b766b82900216a94caf7695e74f72aebcacd4d10b4859de5967e`，47 份文件；原 36 份缓存的恢复结果见上述 `61afa513` 任务。

`20260908-34a57f1` 由 TAT `inv-88d0s50xtm` 成功启用。归档 `dist/ai-radar-server-20260908-34a57f1.tar.gz`，123,032 字节，SHA-256 `54cf7fe0b13457df1b8f97529be5fa0aff235be74e265fe33b706b9964394730`，47 份文件；该版单条诊断的协议恢复与待审结果保留在上述 `02be5af3` 任务记录中。

`20260908-5672e2d` 首次增加服务器独立语义审计、自动修订与正式翻译重审 CLI。TAT `inv-m8cvw7g5d9` 成功：当时 46 条消息、七张表既有数据、三份私有配置保留，Caddy 和原 8080 进程不变；本项目 API 与浏览器均正常。18 个完整采集网站显示完成，手机队列的 reader/admin 权限隔离与只读行为通过。

该版源码归档为 `dist/ai-radar-server-20260908-5672e2d.tar.gz`，119,635 字节，SHA-256 `3d56855f19cb190872556b62654889021a7bc3afeaedf2dd3e57187fe6644fd5`，包内 47 份文件与清单一致。该版最终切换备份为 `/var/lib/ai-radar/backups/translation-automation-final-20260907T171108626588Z.db`。部署后通过正式 CLI 对 4 份历史编辑记录按原文重审，最终均通过；原文及不相关译文逐项不变，完整历史保留，重复执行为零条且全部翻译缓存不变。重审前备份 `/var/lib/ai-radar/translation-recheck/20260907T171541Z-bcfad5ae/before.db`。

此前在 `622e107` 上部署过 `338dbb9` 的两份源码补丁：Facebook 中断保留已读数据、美元/序数词校验等价处理；均包含于当前完整版本。TAT `inv-e8cuvr0qej` 成功，历史备份 `/var/lib/ai-radar/backups/collection-reliability-20260907T163602Z` 和归档 `dist/ai-radar-server-20260908-338dbb9.tar.gz` 保留。

0.5.0 历史升级：自动公开网页读取、准确的采集状态、手机正文导入与自动队列。当时 release `/opt/ai-radar/releases/20260907-622e107`，每版独立 `.venv`，前版 `bebe45b` 及更早版本保留。新增 `document_captures` 表，既有表列不改动；服务器配置与模型/社交密钥不变。升级前备份 `/var/lib/ai-radar/backups/radar-device-queue-20260907T143032Z.db`，44 条消息及七张表旧数据逐项一致，Caddy 与原 8080 服务保持运行。noVNC 的 `android-compat-v1` 灰屏修复继续生效。

归档 `dist/ai-radar-server-20260907-622e107.tar.gz`，114,607 字节，SHA-256 `f651d42652db0e6fc05e6dd84ac07e3e27ebb045c4f80ebf372379cc68c49599`。预检 `inv-e8cr83g9gv`，启用 `inv-e8cr9a0w58`，均 SUCCESS；公开 HTTPS 健康、鉴权隔离、浏览器空闲、17 个完整采集网站及手机队列跳过冷却文章均通过。具体逻辑见 [WEB-AUTHORIZATION.md](WEB-AUTHORIZATION.md)。

0.2.1 历史升级：release `20260907-52fd8e5` 增加 DeepSeek 余额不足持久告警，相关版本与备份均保留。

此前 04:05 已启用 DeepSeek 翻译和独立校对，补齐 40 条中文。密钥以一次性 RSA OAEP 加密传输、0600 保存；真实日报重建证明 40 份翻译缓存没有再次调用翻译。配置与复核说明见 [TRANSLATION.md](TRANSLATION.md)。

03:20 用户提供 X Token 并确认充值，凭据已通过一次性 RSA 4096 OAEP/SHA-256 加密传到服务器，仅写入私有 `server.env`（0600）。进程加载校验通过，其他环境变量和服务均保留，临时传输私钥已删除。备份位于 root 私有 `/opt/ai-radar/incoming/x-token-20260907`。服务器配置每查询 10 条、1 页，每两小时采集。

首轮官方 API 任务 `c700474d-e3a8-4b38-967a-6f6a2c65c3de` 完成，读取 30 条、新增 6 条、更新 1 条旧帖，总计 40 条，X 为 healthy。Codex 任务 `b5d910c6-6ecc-4575-bfbc-117ae6cef67f` 已更新 9 月 6 日日报，5 条输入形成 1 条报道、4 个引用，原文和日期窗口校验通过。下面的部署记录保留各阶段的历史计数。

- 以下为腾讯云官方 CLI + TAT 初始部署及后续接入记录，各阶段版本和计数均为历史快照；当前 API release 见本文开头。
- HTTPS 地址：`https://radar.yswdra.cn`；`/healthz` 已从本机及服务器验证为 200。无令牌读取返回 401，reader 读取返回 200，reader 调用管理任务返回 403。
- 官方 Caddy 2.11.4 已安装并运行，使用独立 `radar` 主机规则；DNSPod 新增 `radar` A 记录，防火墙只追加 TCP 443。原有根域名、`www`、22/80/8080 规则未更改。
- 原有 8080 进程 PID `303258` 保持运行，部署前后访问根路径均返回 404；新 API 仅监听 `127.0.0.1:18473`。
- 首次 5 个官方来源采集 23 条有效信息。9 月 7 日 00:52 加入 Meta Newsroom 后有 6 个健康官方来源、25 条有效信息；之后导入 8 条已授权浏览器采集的 X 公开帖，总计 33 条，并实际生成 9 月 5 日与 6 日中文日报。当时 X 尚待 API 凭据，现已接通并观察到自然定时采集；Facebook 账号仍在审核。
- Codex CLI 0.153.3 完整原生包已安装，包括 code-mode host、bwrap、rg 和包清单。9 月 7 日 00:03（北京时间）服务用户完成设备码登录，独立 `codex login status` 返回 `Logged in using ChatGPT`。
- 云端实际调用 Codex 生成 9 月 4 日历史日报，6 条中文总结使用 9 个来源，所有引用 ID 均匹配原文。任务完成后开启调度器：每天北京时间 08:00 汇报，每两小时采集。
- reader 连接信息通过临时 RSA 公钥加密传回本机，仅保存在仓库忽略的 `credentials/cloud-reader.env`（0600）中，未输出到日志。腾讯云密钥和模型凭据没有打进 App。
- Meta RSS 仅通过外部配置 `/etc/ai-radar/config.toml` 加入，代码 release 未改变。修改前确认无运行中任务并备份配置，重启本项目服务后验证调度器、HTTPS、鉴权和原有 8080 进程均正常。
- 随后发布了 X 长帖、引用上下文和分页中断保留数据的修复。43 项测试通过；部署包校验 SHA-256 后解压到新目录，新建独立 `.venv`，复验来源文件与仓库一致、33 条数据保留、调度器仍开启、私有配置文件哈希未变、Codex 仍已登录。升级前使用 SQLite backup API 生成一致性备份。
- 新版本再次真实采集成功，新增 1 条官方信息，总计 34 条；6 个官方来源健康。HTTPS、读写权限隔离与原有 8080 进程再次通过复验。

网页解读历史升级包为 `dist/ai-radar-server-20260907-5a980ec.tar.gz`，SHA-256 `ac02ca1fccf67efa53b1543f08b740c3f5f7e22c12434f0e717fbb1524a3354a`。包内以 `requirements.lock` 安装服务端依赖，不需要开发用 `uv.lock`；不含数据库、令牌和移动端签名材料。

原 CLI 授权和浏览器上传阻碍已解决；以下盘点记录与安装步骤用于解释部署过程和后续维护。

2026-09-06 已通过腾讯云自动化助手执行只读盘点：已有进程 `server` 使用 `8080`；SSH 使用 `22`；没有发现 Docker、Nginx、Caddy 或 Node。系统盘约 50 GB 可用。盘点脚本末尾的不存在目录使 TAT 返回 ExitCode 2，前面的服务、端口和资源输出已成功获取，不能将其误读为整个盘点没有运行。

最初轻量云关联的解析列表为空，但随后通过 DNSPod 完整查询发现根域名和 `www` 已指向本机，已保留这两条记录并为本项目单独添加 `radar`。

## 隔离布局

| 资源 | AI Radar 使用位置 |
| --- | --- |
| API 监听 | `127.0.0.1:18473` |
| 系统用户 | `ai-radar`，无交互登录 shell |
| 代码发布 | `/opt/ai-radar/releases/<release>` |
| 当前版本 | `/opt/ai-radar/current` |
| Python 环境 | 每个 release 下独立的 `.venv`，通过 `/opt/ai-radar/current/.venv` 使用 |
| Codex 二进制 | `/opt/ai-radar/tools/bin/codex` |
| 数据和独立模型授权 | `/var/lib/ai-radar` |
| 配置 | `/etc/ai-radar/config.toml` |
| 私有环境文件 | `/etc/ai-radar/server.env`，权限 0600 |
| 服务 | `ai-radar.service` |
| 域名 | `radar.yswdra.cn` |

现有 8080 业务不修改、不停止。新服务限制为约 1.2 GB 内存和 1 个 CPU。API 默认只绑定回环地址，发布前需要单独配置 HTTPS 反向代理，不直接开放 18473。

## 访问腾讯云

优先官方 [TencentCloud CLI](https://github.com/TencentCloud/tencentcloud-cli)：

```bash
server/.venv/bin/tccli configure
# 用户自己填写 SecretId、SecretKey，region 为 ap-seoul，不把密钥发到聊天。
bash scripts/tencent-audit.sh
```

也可以使用控制台的官方自动化助手和文件管理。本次公网 SSH 超时、Chrome 文件上传缺少权限，后续在用户授权官方 CLI 后使用 TAT 分块传输部署包，并在服务器上校验完整 SHA-256 后解包。

仓库的 `scripts/tencent-command.py` 是对官方 CLI 的薄封装：`run <script> --name <name>` 提交一次临时命令并返回 invocation；`status <invocation>` 查看同一任务结果。不会自动重复提交失败或超时的命令，不读取、打印或嵌入腾讯云凭据。

## 安装已审核的部署包

部署包只包含 server 源码、依赖锁文件和安装脚本，不包含本地 `.env`、数据库、社交平台凭证或个人 Codex 授权。

先上传 `dist/ai-radar-server-20260906.tar.gz` 到该实例。通过 TAT 或 SSH 运行：

```bash
set -e
mkdir -p /opt/ai-radar/releases/20260906
tar -xzf /root/ai-radar-server-20260906.tar.gz -C /opt/ai-radar/releases/20260906
bash /opt/ai-radar/releases/20260906/scripts/install-server.sh
```

安装脚本检查端口冲突，创建独立用户、虚拟环境和 service，通过锁文件校验依赖。初次自动生成服务令牌但不打印。每次升级必须使用新的 release 目录，脚本拒绝改写当前运行版本；旧版本的 Python 依赖不会被覆盖。切换后的任何启动或健康检查失败都会恢复旧代码链接、旧 service 文件及启用/运行状态。首次安装失败会停止并禁用新服务，保留文件供诊断。

该脚本会安装 Python venv 所需的系统包。不会修改现有域名、SSH 授权、防火墙、反向代理或其他业务端口。完成后先验证 `curl http://127.0.0.1:18473/healthz`。

## Codex 授权

官方 npm 原生发行包可避免额外安装 Node。安装脚本校验 npm 的 SHA-512 integrity 与 `codex-package.json` 后，保留完整原生 bundle，并拒绝目录穿越和符号链接条目。0.153.3 的入口实际为 `bin/codex`；最初脚本预期旧目录而安装失败，已修正并在真实发行包和目标 Linux 主机验证。当前机器修正后的安装工具位于 `/opt/ai-radar/tools/install-codex.py`：

```bash
python3 /opt/ai-radar/tools/install-codex.py
sudo -u ai-radar env HOME=/var/lib/ai-radar CODEX_HOME=/var/lib/ai-radar/codex \
  /opt/ai-radar/tools/bin/codex login --device-auth
```

由用户完成设备码登录，再执行相同环境下的 `codex login status`。浏览器须选择开启设备码授权的同一账号；通过账号授权后，还需在设备码页输入本次 CLI 生成的九位代码。当前这次登录已完整验证成功。不上传本机整个 Codex 目录。若设备码方式在账号中未开启，按官方文档使用浏览器登录/SSH 转发，不绕过授权。

来源凭证写入 `/etc/ai-radar/server.env`；模型参数写入 `config.toml`。当前 `RADAR_SCHEDULER_ENABLED=true` 已生效；后续变更只重启 **AI Radar 自己的服务**。首次开启前已保留权限为 0600 的环境文件备份。

## HTTPS 与域名

先通过官方 CLI 或 DNS 控制台核实 `yswdra.cn` 的现有记录；本机使用代理的 DNS 结果可能是合成 IP，不能据此修改解析。仅对确认可用的 `radar` 子域名添加记录。

若已有反向代理，将 `deploy/Caddyfile.fragment` 的规则等价合并进去，验证配置后平滑重载。若没有，使用官方 Caddy 安装方式配置新代理；必须先检查 80/443 的占用和轻量云防火墙。不覆盖现有网站配置。

发布后验证：HTTPS 证书有效；`/healthz` 正常；无令牌的 `/v1/articles` 返回 401；reader token 不能触发管理任务；已有 8080 业务仍可访问。手机端只填写 HTTPS 服务地址及设备令牌，不分发模型或社交平台凭证。

## 备份与升级

```bash
sudo -u ai-radar python3 /opt/ai-radar/current/scripts/backup.py \
  /var/lib/ai-radar/radar.db /var/lib/ai-radar/backups
```

使用 SQLite backup API，避免单独复制主库遗漏 WAL 数据。升级前备份并保留旧 release。本次采用新增独立表，不改既有列；未来涉及既有结构修改时须提供明确迁移，不能仅依靠 `create_all` 升级旧库。不要将旧版代码和不兼容的新库混用。

Docker Compose 是另一个可选方案，配置在 `deploy/compose.yaml`。目标主机尚未装 Docker，当前不为部署强制安装它。不要同时启动 systemd 方案与 Compose 方案。
