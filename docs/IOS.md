# iOS 构建与签名

应用 ID 为 `cn.yswdra.airadar`，原生工程位于 `app/ios`。React Native 界面、云端 API 和设备令牌存储与 Android 共用。发布版本内置 JavaScript，不依赖 Metro。

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

`preview` 是内部测试配置，使用 Apple Ad Hoc 签名。需要有效的付费 Apple Developer 账号，以及目标 iPhone 的 UDID。用户完成 Apple 授权后，通过官方 EAS 流程登记设备和生成签名：

```bash
cd app
pnpm dlx eas-cli@23.2.0 device:create
pnpm dlx eas-cli@23.2.0 build --platform ios --profile preview
```

只有被加入描述文件的设备才能安装。新增设备后需要重新签名或重建。详见 [Expo 内部分发说明](https://docs.expo.dev/build/internal-distribution/)。不自动购买 Apple 会员，不自动提交 App Store。

## 当前验证

- 首次云端原生构建 [`52d8a69e-d0e7-451d-9418-5d12312c41e7`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/52d8a69e-d0e7-451d-9418-5d12312c41e7) 已成功，版本 `0.1.0` / `1`，`simulator` profile。下载后确认 Xcode 26.2 编译、最低 iOS 15.1、arm64 与 x86_64 模拟器架构及内置 Hermes bundle。
- 首次云端 Expo Doctor 提示 `expo-font` 缺少直接依赖声明，已通过官方 `expo install` 补齐，版本保持锁文件中的 `55.0.8`；iOS prebuild 复核没有原生语义变化，TypeScript 检查通过。首次 iOS Xcode 日志和已交付 Android APK 均确认字体原生模块已存在。
- 修正后的构建 [`e54ecfe1-a025-4f8d-bb96-a208411ee7ea`](https://expo.dev/accounts/yswdra/projects/ai-radar/builds/e54ecfe1-a025-4f8d-bb96-a208411ee7ea) 于 2026-09-07 02:14 北京时间成功。源码提交 `1a7e64a`；云端 Expo Doctor 为 19/20，已消除缺少直接依赖的提醒。
- 仓库主动保留两个原生工程，因此 Expo Doctor 的“app.json 不会由 EAS 自动同步到原生工程”提醒适用。修改相关配置后必须执行 prebuild 并审查原生差异；没有屏蔽这条检查。
- `eas account:usage yswdra --json` 确认 Free 套餐，本月 iOS 构建额度 15 次，提交时统计使用量 0；没有新增套餐、附加项或超额费用。
- 本机只有 Apple Command Line Tools，没有完整 Xcode；官方 `eas simulator:availability` 返回当前账号未开放远程模拟器功能，因此原生运行仍不可验证。
- Apple Developer 账号状态与真机签名尚待用户提供；未生成可安装 IPA。

## 模拟器交付产物

- `dist/ai-radar-0.1.0-ios-simulator.tar.gz`，14,215,183 字节。
- SHA-256：`d19e7e0c227dd6f9e8faa9ddcb011d4f01b62e401394cd0028af296e992c8a03`，同目录提供 `.sha256` 文件。
- 解压后的应用也保存在 `dist/ios-simulator/AIRadar.app`。
- 核验 `Info.plist`、Mach-O 两个架构的平台标记（iOS Simulator）、内置 Hermes、秘密扫描与 `codesign --verify --deep --strict` 均通过。模拟器签名校验通过不代表具备 iPhone 分发证书。
- 详细本机核验结果位于 `dist/ios-simulator-verification.json`。尚未启动 iOS 模拟器或在 iPhone 真机运行。

有完整 Xcode 和已启动的模拟器后，可从仓库根目录安装：

```bash
xcrun simctl install booted dist/ios-simulator/AIRadar.app
xcrun simctl launch booted cn.yswdra.airadar
```

在连接页填写 `https://radar.yswdra.cn` 与本机私有 `credentials/cloud-reader.env` 中的 reader token。令牌未打包进应用。
