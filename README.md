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
- DeepSeek 持久化中英对照：采集时翻译、单独校对，中文搜索与离线阅读复用缓存；数字、单位、链接和引用信息校验，原文完整保留。详见 [翻译配置](docs/TRANSLATION.md)。
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

截至 2026-09-07 03:23，云端有 40 条真实信息，其中 26 条来自 6 个官方来源、14 条来自 X。用户已提供 X Bearer Token 并确认充值；首轮官方 API 读取 30 条、筛选新增 6 条，X 状态为已连接。当前每查询最多 10 条、1 页，每两小时采集一次；首次自动触发和长期覆盖仍待观察。

服务器 Codex 设备授权和真实模型调用已通过，每天北京时间 08:00 汇报。补齐材料后的 9 月 4 日日报包含 6 条总结、10 个引用；最新回放的 9 月 6 日日报已将 Meta 的 4 条 X 原文归并为一条中文报道，引用与时间窗口校验通过。

现有 40 条内容已补齐中文，日报引用也可中英切换。真实日报重建验证了 40 份翻译缓存的调用次数与时间戳均未改变。DeepSeek 密钥仅配置在服务端，摘要引擎仍为独立的 Codex CLI。

Android 0.3.0 签名安装包位于本机 `dist/ai-radar-0.3.0-android.apk`，可覆盖旧版保留登录；包含网页与文章解读、中文阅读、原文切换、离线缓存和余额不足提示。iOS 模拟器原生构建与产物见 [IOS.md](docs/IOS.md)；尚无实际 iOS 运行验证，真机签名仍待 Apple Developer 账号。Facebook 账号正在审核；Meta 官方新闻和 Meta 的 X 帖子不代表 Facebook 社交帖已接通。

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
- iOS：可在完整 Xcode 26.2+、CocoaPods 环境运行 `cd app && pnpm ios`，或使用已关联的 EAS 云构建。构建与签名说明见 [IOS.md](docs/IOS.md)；真机包需要 Apple 签名，模拟器包不等同于可安装 IPA。
- EAS 官方云构建：`cd app && pnpm dlx eas-cli login`，授权后运行 `pnpm dlx eas-cli build --platform android --profile preview` 或 `--platform ios --profile preview`。配置见 `eas.json`，不会自动提交应用商店。

## 配置和运行边界

编辑 `server/config.toml` 后重启服务。默认每天北京时间 08:00 汇报，以该时间为界统计前 24 小时；每 2 小时采集，收录近 7 天内容。`RADAR_SCHEDULER_ENABLED=true` 才开启自动任务，**只能启动一个带调度器的 worker**。历史消息不会被改为今日消息。

热度评分综合互动量、重点关注、来源权重、主题与时效。官方来源没有社交热度时保留“未知”，不捏造点赞数。重点账号的 AI 动态可低于普通热度门槛；与 AI 无关的帖子仍会过滤。关键词筛选是可解释的初筛，可能漏掉未使用这些词的前沿内容，可在 `ranking.py` 扩展。

本版为单用户私有服务，收藏和关注由同一个 reader token 对应的设备共享。不是多租户产品。Token 与模型凭证只通过部署环境配置；修改模型配置、执行采集、导入内容需要管理员权限。

部署前阅读 [腾讯云部署说明](docs/DEPLOYMENT.md)，账号与数据源接入见 [接入说明](docs/CONNECTORS.md)。
