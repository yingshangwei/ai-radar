# 前沿 · AI Radar

一个面向个人的 AI 前沿信息应用。Android 与 iOS 共用 React Native 界面，服务端独立部署，信息采集与模型/Agent 可分别替换。

```
ai-radar/
├── server/          FastAPI、采集、排序、日报、模型适配、测试
│   └── radar/
├── app/
│   ├── android/     Android 原生 Gradle 工程
│   ├── ios/         iOS 原生 Xcode 工程
│   ├── src/         类型、接口、安全存储、主题
│   └── App.tsx      共用移动界面
├── deploy/          独立 systemd 服务、Docker、反向代理片段
├── scripts/         腾讯云盘点、备份
└── docs/            接入、部署与验证记录
```

## 已实现

- 每日中文汇报、历史日报、信号详情、原始引用；无新增内容时明确说明来源覆盖与数据空缺。
- DeepSeek 持久化中英对照：采集时翻译、修订校对、独立语义审计，未通过时由服务器自动修正并复审；中文搜索与离线阅读复用缓存。发现问题修复服务器逻辑，不人工修改文章或译文。详见 [翻译配置](docs/TRANSLATION.md)。
- [学界采集](docs/RESEARCH.md)：Hugging Face 热门论文与 arXiv 理论精选直接进入雷达，保留原日期、作者摘要和真实社区热度；按论文 ID 去重，中文及解读复用现有审核流程。
- [动态发现与潜力预判](docs/DISCOVERY.md)：从热门及已关注来源的明确关联中发现真实账号，限量试关注；低互动新思路保留为候选，服务端判断后进入雷达并标明依据与不确定性。
- 主消息和已保存的关联网页正文共用补译队列，分别报告完成状态。补译只处理未完成的当前内容，复用草稿和已通过段落；多段审计返回错误 ID 时逐段重新审核，完整保留未解决疑点，未通过不发布。
- 网页原文与直接关联文章 / 应用的持久化中文解读、关键要点和正文；不递归扩展，日报与手机复用缓存。详见 [读取范围与配置](docs/READING.md)。
- DeepSeek 余额不足时在手机首页、详情和设置持续提示；保存翻译进度，后续调用成功自动恢复。
- X/Twitter 官方搜索 API（关键词发现 + 重点账号），Facebook Graph API（授权主页）；支持分页、互动指标与显式授权/限额状态。
- OpenAI、Google AI、DeepMind、Hugging Face、Meta Newsroom 官方 RSS，以及 Anthropic 官方新闻页。
- 重点账号管理、可配置热度门槛、AI 主题过滤、最近一周的信息流、关键词检索与收藏。
- 可切换 Codex CLI、Claude Code CLI、自定义 CLI、OpenAI Responses、兼容 Chat Completions 的服务、Anthropic API。模型与服务地址均为配置项。
- 手机使用系统安全存储保存设备令牌；网页预览只使用会话存储。缓存文章和日报可离线阅读，认证失败不会被缓存掩盖。
- 独立定时采集和日报任务、幂等入库、任务互斥、原文引用校验、SQLite WAL 与一致性备份。

**尚待完成的外部接入与原生构建，请看 [验证记录](docs/VALIDATION.md)。** 示例模式有明确标识，绝不写入实际数据库。

## 当前云端服务

服务地址为 `https://radar.yswdra.cn`，已部署到指定腾讯云实例；[健康检查](https://radar.yswdra.cn/healthz)可公开访问，内容接口需要设备令牌。本机连接信息保存在未纳入 Git 的 `credentials/cloud-reader.env`，只允许当前用户读取。App 连接页填写其中的服务地址和 reader token。

当前服务器版本为 `20260908-327c6fa`，已上线[动态发现与潜力预判](docs/DISCOVERY.md)，保留学界采集及[持久任务恢复](docs/TASK_RECOVERY.md)。采集、翻译、发现和网页/日报处理分别执行；自动试关注每天最多 2 个、同时最多 6 个，7 天到期，用户的关闭和保留选择优先。X 每轮总请求预算固定为 3，不能据此承诺全量覆盖。实际验证见 [部署记录](docs/DEPLOYMENT.md)。

此前上线的[摘要事实审核](docs/SUMMARY-REVIEW.md)继续以保存的原始来源审核日报和网页解读，候选修订后重新审核通过才发布；私有候选、调用预算和进度跨任务保存。

真实 Codex 合成验收已通过：错误数字候选被拒绝，由服务器自动修订并重新审核，再次处理同一缓存时没有新增模型调用。测试数据库隔离，生产文章、译文和候选未编辑。模型审核仍不能保证绝对无误；真实结果及首次测试权限问题的零调用证据见 [验证记录](docs/VALIDATION.md)。

补充选稿和日报启动补偿继续运行：每份日报从回看期内补入最多 5 条此前未报道的消息，当前窗口优先，按实际引用显示“补充｜”或“含补充｜”；关闭 reading 时不临时抓取未保存网页。2026-09-08 的自然日报于 00:24:58 UTC 完成，10 条输入候选生成 6 条报道，实际引用 7 个不同来源，其中 3 条报道引用 3 个补充来源。来源成员、时间窗口、补充标签与历史去重检查通过；这些结构检查不等于事实准确性审核。

正式机器复检于 2026-09-08 00:34:25 UTC 完成，实际恢复 2 份缓存的可复用段落，其中 1 份整篇 ready、另 1 份仍待审，原文、候选与已有模型审核记录保持不变，未调用模型。00:36:41 UTC 公网只读复验：主消息 46 ready；47 份网页全文 14 ready、33 review_required，全文中文尚未全部完成。健康、401/403 权限隔离正常，无运行任务，余额告警为空、调度器开启；X 显示部分覆盖（partial），官方来源正常，Facebook 仍待授权。AI 筛选规则与中文输出门禁保持未发布。

修订和独立审计沿用 DeepSeek `thinking=enabled`、`reasoning_effort=high`、32768 token，整体请求时限为 300 秒；初稿保持非推理模式及 12000 token。分阶段配置和此前真实模型验证记录见 [翻译机制](docs/TRANSLATION.md)及[验证历史](docs/VALIDATION.md)。

已观察到 9 月 8 日 00:32（北京时间）自然定时采集，X 读取 30 条并新增 2 条，另处理 14 个直接来源。当前每两小时运行，默认总预算每轮 3 个请求（1 次发现、2 次重点账号），每账号每轮最多 1 页、每页最多 10 条；持久化轮转覆盖 17 个重点账号，未完成分页后续继续，不保证全面覆盖。4 份历史编辑记录已由服务器自动重审，原始证据与历史记录保留。

服务器 Codex 设备授权和真实模型调用已通过，每天北京时间 08:00 汇报。补齐材料后的 9 月 4 日日报包含 6 条总结、10 个引用；最新回放的 9 月 6 日日报已将 Meta 的 4 条 X 原文归并为一条中文报道，引用与时间窗口校验通过。

日报引用可中英切换。此前真实日报重建验证了 40 份翻译缓存的调用次数与时间戳均未改变。DeepSeek 密钥仅配置在服务端，摘要引擎仍为独立的 Codex CLI。

Android 0.9.0 签名安装包位于本机 `dist/ai-radar-0.9.0-android.apk`，沿用原应用 ID 与签名，可覆盖安装；[网页采集中心](docs/WEB-AUTHORIZATION.md)支持公开网页自动读取、按网站一次手机许可及 App 前台自动补采；包含网页与文章解读、中文阅读、原文切换、离线缓存和余额不足提示。iOS 模拟器原生构建与产物见 [IOS.md](docs/IOS.md)；本机缺完整 Xcode 和运行时，Apple 下载账号访问授权待处理，尚未实际运行 iOS 或生成真机 IPA。免费 Apple Account 可获取 Xcode，真机分发签名另行处理。Facebook 账号正在审核；Meta 官方新闻和 Meta 的 X 帖子不代表 Facebook 社交帖已接通。

手机 0.7.0 优化[雷达发言布局](docs/ARTICLE-LAYOUT.md)：作者和时间、服务端 AI 标题、三行正文预览、独立引用卡片与回复身份，详情完整阅读和中英切换。服务端新增标题缓存的发布状态见该说明。

手机 0.6.0 增加恢复网络/回到前台后的内容刷新、详情与收藏缓存同步，以及超过 30 个授权网站的持久轮转。安卓已实际覆盖安装验证 reader 登录保留；iOS 0.6.0 上传归档已准备，EAS 源码上传授权待确认，尚未构建。

## 本地启动

需要 Python 3.11+、Node.js 22.13+（推荐 24）、pnpm 10。

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/radar init
# 创建只允许当前用户读写的 .env；不会把令牌打印到日志。
# 默认 Codex：先执行 codex login，并确认 codex login status。
.venv/bin/radar daily
.venv/bin/uvicorn radar.api:create_app --factory --host 127.0.0.1 --port 18473 --workers 1
```

另一终端：

```bash
cd app
pnpm install --frozen-lockfile
pnpm web
# 连接页填写 http://127.0.0.1:18473 和 server/.env 中的 RADAR_READER_TOKEN。
# Android 模拟器使用 http://10.0.2.2:18473；真机使用部署后的 HTTPS 地址。
```

用编辑器查看本机 `.env` 完成配对，不要将令牌粘贴到聊天、提交到 Git，或写进 `EXPO_PUBLIC_*`。`RADAR_ADMIN_TOKEN` 仅用于管理接口；日常阅读使用 reader token。

## 构建和验证

```bash
cd server
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check radar tests

cd ../app
pnpm typecheck
pnpm exec expo install --check
pnpm exec expo export --platform all
```

`app/android` 和 `app/ios` 是已生成的原生工程。修改 Expo 配置后运行 `pnpm prebuild`，不要使用会删除原生更改的 `--clean`。

- Android：独立签名 Release 已完成；使用 `python3 scripts/build-android.py` 构建，环境、签名备份和本机 Google 下载兼容选项见 [Android 构建说明](docs/ANDROID.md)。Debug 版本需要 Metro。
- iOS：可在与 macOS 兼容的完整 Xcode、CocoaPods 环境运行 `cd app && pnpm ios`；本机 macOS 15.6 可使用 Xcode 26.2。已有 0.5.0 模拟器包无需重复 EAS 构建。构建与签名说明见 [IOS.md](docs/IOS.md)；真机包需要 Apple 签名，模拟器包不等同于可安装 IPA。
- EAS 官方云构建：`cd app && pnpm dlx eas-cli login`，授权后运行 `pnpm dlx eas-cli build --platform android --profile preview` 或 `--platform ios --profile preview`。配置见 `eas.json`，不会自动提交应用商店。

## 配置和运行边界

编辑 `server/config.toml` 后重启服务。默认每天北京时间 08:00 汇报，以该时间为界统计前 24 小时；每 2 小时采集，收录近 7 天内容。`RADAR_SCHEDULER_ENABLED=true` 才开启自动任务，**只能启动一个带调度器的 worker**。历史消息不会被改为今日消息。

热度评分综合互动量、重点关注、来源权重、主题与时效。官方来源没有社交热度时保留“未知”，不捏造点赞数。重点账号的 AI 动态可低于普通热度门槛；与 AI 无关的帖子仍会过滤。关键词筛选是可解释的初筛，可能漏掉未使用这些词的前沿内容，可在 `ranking.py` 扩展。

本版为单用户私有服务，收藏和关注由同一个 reader token 对应的设备共享。不是多租户产品。Token 与模型凭证只通过部署环境配置；修改模型配置、执行采集、导入内容需要管理员权限。

部署前阅读 [腾讯云部署说明](docs/DEPLOYMENT.md)，账号与数据源接入见 [接入说明](docs/CONNECTORS.md)。
