# iOS 构建与签名

## 当前源码：0.7.0 / build 8，未构建新 iOS 包

共享 UI 已同步雷达列表、文章详情与中英文正文的阅读排版；`app/app.json`、iOS `Info.plist` 和 Xcode 工程现有版本字段均更新为 0.7.0 / 8。共享界面类型检查通过，布局已在 Web 和 Android 验证；这些结果不代表 iOS 原生运行通过。

本次没有执行 iOS 云构建或原生构建，没有新模拟器包或 iPhone IPA；最新已构建产物仍为下文的 0.5.0 模拟器包。此前源码上传授权与 Apple 账号访问授权仍待处理，未再次访问相关授权页面。

## 历史准备：0.6.0 / build 7，云构建待授权

共享移动源码 `76831b0` 已完成内容自动刷新、详情/收藏缓存同步与超过 30 个网站的持久轮转，App 本地测试与类型检查通过。固定提交的隔离目录 `ai-radar-ios-0.6-build-76831b0`、原生 Info.plist 和官方 EAS 上传归档均为 0.6.0 / 7；归档只含 App 与必要仓库元文件，334 份文件及 Git 对象按五个已知私密值扫描无命中。

只读查询 EAS Free iOS 用量为 10 / 15，没有进行新上传或消耗额度。自动审批拒绝向私人项目 `@yswdra/ai-radar` 上传源码并使用一次免费构建额度，理由是此前 Expo 登录授权未明确涵盖该目的地的源码上传。已请求具体授权，未通过其他方式重试。记录 `dist/eas-v0.6-approval-block.json`；可审阅上传内容 `dist/eas-ios-v0.6-archive`，扫描 `dist/eas-v0.6-upload-scan.json`。

## 最新已构建版本：0.5.0 / code 6

最终 EAS 模拟器构建 `9526a508-77d8-4893-b1ff-38cac07ed0fd` 成功，源码 `98c3847`，包含一次网站许可、手机自动读取以及原生 URL / 消息桥兼容修复。交付包 `dist/ai-radar-0.5.0-ios-simulator.tar.gz`，14,496,493 字节，SHA-256 `4f3f76f3b644ab26cac3496a8d1e6c372aeb6f1f9a8730fbdb1143bd8350f833`。

`dist/ios-simulator-v0.5/AIRadar.app` 已核验 0.5.0 / 6、最低 iOS 15.1、arm64 / x86_64 模拟器平台、内置 JavaScript、严格签名及五个已知秘密扫描。0.5.0 构建时使用既有 Free 额度，用量为 iOS 10 / 15，无超额费用或附加项；0.6.0 准备时已重新只读查询额度，结果仍为 iOS 10 / 15。此前中间包只保留为基线；交付路径指向最终源码。产物仍存在，尚未实际运行 iOS，也未生成可安装到 iPhone 的 IPA。后续 `f574f87` 仅升级服务器，无需重建此包。

## 当前本机运行条件

本机为 macOS 15.6 / arm64，当前开发工具指向 Command Line Tools；没有完整 Xcode、可用 `simctl` 或已安装的模拟器运行时，有效代码签名身份与描述文件数量均为 0。旧 EAS 远程模拟器可用性结果保留在历史记录，没有重新查询。

完整 Xcode 可免费获取：Apple 明确旧版下载只需 Apple Account，不要求付费 Developer Program；Xcode 26.2 支持本机 macOS 15.6，可用现有 0.5.0 包继续运行验证，无需重复 EAS 构建或升级系统。[Apple 下载说明](https://developer.apple.com/xcode/resources/)、[兼容矩阵](https://developer.apple.com/xcode/system-requirements/)。本次 Apple 下载登录跳转被自动审批审查拒绝，已向用户请求 Apple 账号访问授权，尚待回复；未绕过授权、下载或安装大包，也未读取密码或账号令牌。App Store 登录状态未确认，不能据此认定账号已退出。

## 历史版本：0.4.0 / code 5

EAS 模拟器构建 `46d82cca-0c07-4491-ba0a-1d40b77dbd1b` 成功，移动端源码 `73e6dbd`，新增原生 WebView 模块。实际产物 `dist/ios-simulator-v0.4-final/AIRadar.app`，交付包 `dist/ai-radar-0.4.0-ios-simulator.tar.gz`，14,734,614 字节，SHA-256 `fe35ea0c70d113a424f771c6030bd1ba53e61f8d8472af7e8e2c06fd614ccf65`。签名与凭据扫描通过；仍是模拟器包，没有实际 iOS 运行验证或真机分发签名。

应用 ID 为 `cn.yswdra.airadar`，原生工程位于 `app/ios`。React Native 界面、云端 API 和设备令牌存储与 Android 共用。发布版本内置 JavaScript，不依赖 Metro。

## 历史版本：0.3.0 / code 4

EAS 模拟器构建 `5227dc71-de1b-4b34-b112-5a96b9f7c5b9` 成功，源码 `3781b62`。实际产物为 `dist/ios-simulator-v0.3/AIRadar.app`，压缩交付包 `dist/ai-radar-0.3.0-ios-simulator.tar.gz`，14,222,673 字节，SHA-256 `84df5c685e6c810ac43cc821423c30a6c038794a3cc53a272afa470b0f21c129`。

包标识、版本、arm64 / x86_64 模拟器架构、最低 iOS 15.1、签名完整性与秘密扫描均通过。新增网页解读与保存的中英正文。仍未进行实际 iOS 运行测试；此文件不能安装到实体 iPhone，真机签名仍需 Apple Developer 授权。使用既有免费构建额度，没有购买或升级。

## EAS 项目

2026-09-07 已确认 Expo CLI 浏览器授权成功，并通过独立 `eas whoami` 核验账号。项目 [@yswdra/ai-radar](https://expo.dev/accounts/yswdra/projects/ai-radar) 已关联到 `app/app.json`，ID 为 `f6697808-ced9-4ea1-9f2c-27a19cdbe41a`。

本次使用官方 EAS CLI 23.2.0。从 `app` 目录执行所有 EAS 命令，符合 [Expo 多目录仓库说明](https://docs.expo.dev/build-reference/build-with-monorepos/)。已有原生工程会直接参加构建；不要执行 `prebuild --clean` 丢失原生修改。

```bash
cd app
pnpm dlx eas-cli@23.2.0 whoami
pnpm dlx eas-cli@23.2.0 build --platform ios --profile simulator
```

`simulator` 配置不需要 Apple 签名，输出供 iOS Simulator 使用的 `.app` 压缩包。[模拟器构建](https://docs.expo.dev/build-reference/simulators/)不能安装到实体 iPhone，也不等同于 IPA。

## 上传范围

仓库根目录 `.easignore` 汇总 Git 与两个原生工程的忽略项，排除本机环境、私有凭据、缓存、产物，以及构建工作目录不需要的服务端文件。保留 `app/ios`、`app/android`、资源、源代码及 `app/pnpm-lock.yaml`。

提交前可用官方命令检查实际上传工作目录：

```bash
cd app
pnpm dlx eas-cli@23.2.0 build:inspect --platform ios --profile simulator \
  --stage archive --output ../dist/eas-ios-archive
```

EAS Git 客户端还会保留浅克隆的 `.git` 元数据；`.easignore` 不能删除已提交内容在 Git 对象中的副本。因此秘密必须始终留在 Git 之外。本次最终上传检查了 72 个工作文件及 158 个浅克隆 Git 对象，未发现设备令牌或 Android 签名密码，私有凭据文件未上传。EAS 项目没有配置任何构建环境秘密。

## 真机安装

以下为现有 EAS 内部分发路径；付费资格不是 Xcode 下载或模拟器运行的前置条件。Apple 也支持使用个人 Apple Account 在 Xcode 中配置本人设备的开发签名，该路径仍需完整 Xcode、账号授权与连接设备后的验证。[Apple 设备运行说明](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices)

`preview` 是内部测试配置，使用 Apple Ad Hoc 签名。需要有效的付费 Apple Developer 账号，以及目标 iPhone 的 UDID。用户完成 Apple 授权后，通过官方 EAS 流程登记设备和生成签名：

```bash
cd app
pnpm dlx eas-cli@23.2.0 device:create
pnpm dlx eas-cli@23.2.0 build --platform ios --profile preview
```

只有被加入描述文件的设备才能安装。新增设备后需要重新签名或重建。详见 [Expo 内部分发说明](https://docs.expo.dev/build/internal-distribution/)。不自动购买 Apple 会员，不自动提交 App Store。

## 历史构建与验证（0.1.0–0.2.1）

- 0.2.1 / 3 构建 [`bab56d1f-a0a9-4df2-b8f4-a2e39e11821f`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/bab56d1f-a0a9-4df2-b8f4-a2e39e11821f) 于 2026-09-07 04:40 北京时间成功，源码 `52fd8e5`，加入 DeepSeek 余额不足提示及前台状态刷新。下载后的 bundle、版本、iOS 15.1+、arm64/x86_64 模拟器架构、Hermes、秘密扫描及 codesign 校验通过；仍未实际运行 iOS 模拟器。
- 0.2.0 / 2 构建 [`bc0c6444-eeea-42e7-95c1-ccce0b6e358e`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/bc0c6444-eeea-42e7-95c1-ccce0b6e358e) 于 2026-09-07 04:13 北京时间成功，源码 `d9004ec`，包含持久化中文阅读、原文切换和中文日期。
- 首次云端原生构建 [`52d8a69e-d0e7-451d-9418-5d12312c41e7`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/52d8a69e-d0e7-451d-9418-5d12312c41e7) 已成功，版本 `0.1.0` / `1`，`simulator` profile。下载后确认 Xcode 26.2 编译、最低 iOS 15.1、arm64 与 x86_64 模拟器架构及内置 Hermes bundle。
- 首次云端 Expo Doctor 提示 `expo-font` 缺少直接依赖声明，已通过官方 `expo install` 补齐，版本保持锁文件中的 `55.0.8`；iOS prebuild 复核没有原生语义变化，TypeScript 检查通过。首次 iOS Xcode 日志和已交付 Android APK 均确认字体原生模块已存在。
- 修正后的构建 [`e54ecfe1-a025-4f8d-bb96-a208411ee7ea`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/e54ecfe1-a025-4f8d-bb96-a208411ee7ea) 于 2026-09-07 02:14 北京时间成功。源码提交 `1a7e64a`；云端 Expo Doctor 为 19/20，已消除缺少直接依赖的提醒。
- 仓库主动保留两个原生工程，因此 Expo Doctor 的“app.json 不会由 EAS 自动同步到原生工程”提醒适用。修改相关配置后必须执行 prebuild 并审查原生差异；没有屏蔽这条检查。
- 首次构建前，`eas account:usage yswdra --json` 确认 Free 套餐，本月 iOS 构建额度 15 次，当时统计使用量 0；最终 0.5.0 构建后的用量见本文当前版本记录。没有新增套餐、附加项或超额费用。
- 本机只有 Apple Command Line Tools，没有完整 Xcode；官方 `eas simulator:availability` 返回当前账号未开放远程模拟器功能，因此原生运行仍不可验证。
- Apple Developer 账号状态与真机签名尚待用户提供；未生成可安装 IPA。

## 当前模拟器交付与安装

- 交付包：`dist/ai-radar-0.5.0-ios-simulator.tar.gz`，14,496,493 字节，SHA-256 `4f3f76f3b644ab26cac3496a8d1e6c372aeb6f1f9a8730fbdb1143bd8350f833`；同目录提供 `.sha256` 文件。
- 解压目录：`dist/ios-simulator-v0.5/AIRadar.app`；核验结果 `dist/ios-v0.5-verification.json`。仍未实际运行 iOS，也不能安装到实体 iPhone。

有完整 Xcode 和已启动的模拟器后，可从仓库根目录安装：

```bash
xcrun simctl install booted dist/ios-simulator-v0.5/AIRadar.app
xcrun simctl launch booted cn.yswdra.airadar
```

在连接页填写 `https://radar.yswdra.cn` 与本机私有 `credentials/cloud-reader.env` 中的 reader token。令牌未打包进应用。

## 历史模拟器产物

- 0.2.1：`dist/ai-radar-0.2.1-ios-simulator.tar.gz`，14,218,220 字节，SHA-256 `83593f91e627387db6c5eca0a2445a9d045961e597c370f82d0ffd4bfc620077`。
- 0.2.1 解压目录：`dist/ios-simulator-v0.2.1/AIRadar.app`；核验结果 `dist/ios-v0.2.1-verification.json`。此前版本继续保留作为历史产物。
- `dist/ai-radar-0.1.0-ios-simulator.tar.gz`，14,215,183 字节。
- SHA-256：`d19e7e0c227dd6f9e8faa9ddcb011d4f01b62e401394cd0028af296e992c8a03`，同目录提供 `.sha256` 文件。
- 解压后的应用也保存在 `dist/ios-simulator/AIRadar.app`。
- 核验 `Info.plist`、Mach-O 两个架构的平台标记（iOS Simulator）、内置 Hermes、秘密扫描与 `codesign --verify --deep --strict` 均通过。模拟器签名校验通过不代表具备 iPhone 分发证书。
- 详细本机核验结果位于 `dist/ios-simulator-verification.json`。尚未启动 iOS 模拟器或在 iPhone 真机运行。
