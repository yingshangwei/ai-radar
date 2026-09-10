# Android 构建与签名

## 当前版本：0.11.0 / code 12

增加「你的雷达 → 模型用量」：时间筛选、当前模型分工、模型与功能阶段汇总、最近调用明细，Token 自动 K / M / B，未知用量独立显示。覆盖安装沿用原签名和设备连接。

交付包 `dist/ai-radar-0.11.0-android.apk`，70,306,390 字节，SHA-256 `ace447ebc44e013baf8220f986f7d1927c3b501197d0b0e08010506bc96b26b1`。Android code 12、原证书 SHA-256 `b166858aae76fd673083b3fe3860ede1a1732d03d1995e671aa751008f0c89e9`、最低 API 24 / 目标 36，签名和版本验证通过。App 45 项测试通过、1 项既有跳过，类型检查通过；iOS 共享资源导出通过，未生成新 IPA。详见 [模型用量](MODEL-USAGE.md)。

## 历史版本：0.10.0 / code 11

交付包 `dist/ai-radar-0.10.0-android.apk`，70,295,910 字节，SHA-256 `6fdf6abb5e9f4ba958e4412769f3dde06d9dfa06e9711b9d719a8dd4827cbae8`；App 源码 `da7b5268c5c576c819383a4327f49d28c1094725`。沿用应用 ID、正式签名和四 ABI，最低 API 24、目标 API 36。签名、ZIP 完整性、Hermes bundle 一致性及 6 个已知私密值扫描通过。

增加离线公式排版，覆盖论文中英文正文、标题、引用和网页解读；列表提示进入详情查看公式。无法排版时保留原始表达式。字体、CSS、KaTeX 和许可随包内置，不读取网页授权或下载远程字体。实际 Android 检查发现并修复了 WebView 阻止父级文章滚动的问题。

App 43 项测试通过、1 项既有跳过，类型检查通过；320 / 390 / 768 px 离线页面检查覆盖真实 Silver Rate 摘要与合成分数、多行公式、长公式、错误语法。专用 API 36 模拟器实测覆盖安装后保留 reader 连接、公式显示、从公式正文开始的上下滑动、断网阅读。最终真实服务器复核完成后，模拟器亦确认新中文标题、正文与公式正常显示。证据位于忽略目录 `dist/math-ui/`。iOS 工程同步为 0.10.0 / build 11，离线资源导出成功；本轮未生成 iOS 签名安装包或进行 iOS 原生运行验证。

## 历史版本：0.9.0 / code 10

交付包 `dist/ai-radar-0.9.0-android.apk`，69,644,350 字节，SHA-256 `7f57b28c92bc854886ba15618c36e4e70b7b8f58852753e0f79c9470515976f7`；源码 `327c6fa30f7f6c3c38cdfaa672d563753e40c314`。沿用原应用 ID、签名证书、四 ABI、最低 API 24 和目标 API 36。正式签名、ZIP 完整性和 APK 内 Hermes bundle 与本次生成 bundle 一致性均通过，记录 `dist/android-v0.9-verification.json`。

增加潜力预判标识、依据与不确定点、真实互动升温观察、自动试关注期限及状态；雷达新增「前瞻」「学界」筛选，后台发现完成后自动刷新关注列表。App 40 项测试通过、1 项既有跳过，类型检查通过。Expo、Android、iOS 工程版本均更新为 0.9.0 / build 10；本轮没有新 iOS 安装包。

最后一次正式构建在同一规范环境中完成，56 秒成功；此前一次 packageRelease 增量打包失败，追加 stacktrace 后重跑通过，未更改源码或清理缓存，不能臆断其根因。本轮未在用户设备或模拟器安装 0.9.0，不能声明已实测登录保留。直接在对话交付，禁止操作微信。

## 历史版本：0.8.0 / code 9

交付包 `dist/ai-radar-0.8.0-android.apk`，69,640,562 字节，SHA-256 `c5d5cb134765825aff0281b2cc7f75ede8ed06cb59a0350749f3a0683a8997c9`，源码 `f185060336a54f69730c69c85782dbe0cf4694fd`。使用仓库 `scripts/build-android.py` 完成正式签名构建，最低 API 24、目标 API 36；沿用签名证书 SHA-256 `b166858aae76fd673083b3fe3860ede1a1732d03d1995e671aa751008f0c89e9`。Expo、Android 与 iOS 工程版本均为 0.8.0 / build 9；本轮只生成 Android APK，没有新 iOS 安装包。

0.8 增加中文任务状态、执行与排队数量、最近进展及服务响应时间，区分自动重试和需要处理的任务；补译队列单独显示可执行、处理中、重试及待处理数量。离线明确显示上次状态，心跳变化不会反复刷新正文。新字段兼容旧服务；自动恢复的实际能力仍取决于服务器版本，当前发布状态见 [验证记录](VALIDATION.md)。

专用 Android API 36 模拟器 `5560` 已完成 0.7 → 0.8 覆盖安装，演示首页和设置页检查通过，观察到的运行时错误为 0，随后停止该模拟器。升级前基线是登录页，**本轮没有验证登录保留**，不能沿用此前升级的结论。证据：`dist/android-v0.8-verification.json`、`dist/android-v0.8-before.xml`、`dist/android-v0.8-after.xml`、`dist/android-v0.8-demo.xml`、`dist/android-v0.8-settings.png` / `.xml`。App 测试 34 通过、1 项既有跳过，TypeScript 与 Prettier 检查通过。

安装包直接通过对话链接交付，不操作微信。

## 历史版本：0.7.0 / code 8

交付包 `dist/ai-radar-0.7.0-android.apk`，69,635,814 字节，SHA-256 `c486959d9bf19edbdd0fabce36b1d96558725aba3838cefe2488a992d5ce6060`。本地正式签名构建成功，沿用 0.6 的签名证书、四种 ABI、最低 API 24 和目标 API 36；扫描 1,165 个包内文件，五个已知私密值均未检出。核验记录为 `dist/android-v0.7-verification.json`。

0.7 调整雷达列表、文章详情及中英文正文的阅读排版。已在专用 Android 模拟器完成 0.6 → 0.7 覆盖升级并保留 reader 连接，再使用演示内容验证雷达、中文详情和英文原文切换；没有修改生产内容。原生截图及检查位于 `dist/article-layout-qa/`，另有 390 / 320 px 布局检查。此次未实测管理令牌保留，也未进行 iOS 原生运行验证。

历史上曾尝试通过微信发送 0.7 安装包，但因安全提示要求重新登录而未发送。之后用户明确禁止微信操作，后续不再尝试。

## 历史版本：0.6.0 / code 7

交付包 `dist/ai-radar-0.6.0-android.apk`，69,627,066 字节，SHA-256 `698cfcb85980487e8f0ce30983a89c5cb863bd4c22f64aa5dc87b16aac9c3c5c`，源码 `76831b0`。原签名证书、四种 ABI、最低 API 24 和目标 API 36 保持不变；扫描 1,165 个包内文件，五个已知私密值均未检出。构建核验见 `dist/android-v0.6-verification.json`。

0.6 增加任务结束和内容状态变化后的自动刷新，App 返回前台或离线恢复后会更新消息、日报与详情；收藏成功后同步详情及本地缓存。网站许可超过 30 个时分批循环检查，并按服务器保存轮转进度，短暂关闭再打开也会继续。暂停、撤销和需要重新验证的网站会退出自动读取范围；来源覆盖不完整时显示「部分覆盖」。

已在专用 Android 模拟器完成 0.5 → 0.6 覆盖升级，实测保留阅读令牌登录并可在前后台切换后恢复。自动刷新、离线恢复、收藏缓存、超过 30 个网站的轮转与取消边界另有自动化回归；本次升级实测范围不包含管理令牌保留或 iOS 运行。

## 历史版本：0.5.0 / code 6

交付包 `dist/ai-radar-0.5.0-android.apk`，69,622,350 字节，SHA-256 `142bb1f7bf3779df2f11e617fb57fff0f3f143e5677b80a3f60ad265834387ba`，源码 `98c3847`。沿用原签名与四 ABI，可覆盖安装保留阅读连接、管理令牌和网站许可。

公开页面由服务端自动读取；手机按网站许可一次后，在前台自动读取、保存、翻译和总结，无需逐篇确认。Android 16 模拟器只点一次网站许可，完成三个不同目标 URL 的正文入库；第一篇可见网页保存后自动返回，另外两篇由前台队列继续读取。详见 [网页采集说明](WEB-AUTHORIZATION.md)。

原生验收发现并修复 React Native URL 没有属性 setter，以及 Android WebMessageListener 只回传 origin 的差异。回归使用安装版本的真实 React Native URL 实现。测试 APK 只为本地隔离 API 允许 HTTP，正式包恢复生产配置；两者 Hermes 字节码逐字节相同。正式包签名、版本、无 debug/cleartext 开关及五个已知私密值扫描通过。

## 历史版本：0.4.0 / code 5

交付包 `dist/ai-radar-0.4.0-android.apk`，69,398,978 字节，SHA-256 `5351b9ac1342a9b22adef95a7ed439181a7b7f01b013d9f38338c9586bfcc1a5`。原签名证书和四 ABI 保持不变，可覆盖旧版保留登录。新增 App 内网页授权中心、受保护的远程浏览器、一次保存管理令牌与验证后自动补采，使用方法见 [WEB-AUTHORIZATION.md](WEB-AUTHORIZATION.md)。

应用 ID 为 `cn.yswdra.airadar`，最低 Android 7（API 24）。Release 会内置 JavaScript 和 Hermes 字节码，启动时不需要 Metro；服务地址默认使用已部署的 `https://radar.yswdra.cn`，设备令牌由用户在连接页填写。

## 历史版本：0.3.0 / code 4

交付包 `dist/ai-radar-0.3.0-android.apk`，68,523,485 字节，SHA-256 `f4564dc8ab42718cee969edfa748e24ef999d276981bc495cc08355b5bf8d1f2`。四 ABI，原签名证书保持一致；可覆盖安装并保留连接。新增网页解读、关键要点、关注价值与保存的中英正文，详见 [READING.md](READING.md)。下文保留早期构建过程与历史产物信息。

## 环境

- JDK 17、Node.js 24、pnpm 10。
- Android SDK Platform 36、Build Tools 36.0.0、NDK 27.1.12297006、CMake 3.22.1。
- 仓库 Gradle Wrapper 使用 Gradle 9.0.0。

正常网络下使用官方 SDK Manager：

```bash
sdkmanager 'platforms;android-36' 'build-tools;36.0.0' \
  'ndk;27.1.12297006' 'cmake;3.22.1'
```

本机的 `dl.google.com` 持续超时，但 Google 的 `dl-ssl.google.com` 可用。2026-09-07 已从该官方域名获取 SDK 清单与安装包，逐包验证清单中的大小和 SHA-1 后安装。没有修改全局代理、Gradle 用户配置或其他项目。

`scripts/google-maven.init.gradle` 仅在显式传入时将 Google Maven 的下载域名切换为 `dl-ssl.google.com`；路径、版本和 TLS 校验保持原样。这个选项只解决特定网络下的下载问题。

## 私有签名

本机已创建独立 RSA 3072 签名密钥，位于 Git 忽略的 `credentials/android-release.keystore`。`app/credentials.json` 使用 [EAS 官方本地凭据格式](https://docs.expo.dev/app-signing/local-credentials/)，两个文件均为 0600。它们需要作为同一份签名材料备份；后续更新继续使用同一密钥。

`scripts/build-android.py` 从该文件读取签名信息，仅通过子进程环境传给 Gradle。Release 构建要求有效的私有签名；密码不会作为命令行参数输出。EAS 的 Android preview/production 也使用这份本地凭据，以保持更新签名一致。iOS 签名另行配置。

凭据格式示例（所有值均为占位符）：

```json
{
  "android": {
    "keystore": {
      "keystorePath": "../credentials/android-release.keystore",
      "keystorePassword": "本机私有密码",
      "keyAlias": "ai-radar",
      "keyPassword": "本机私有密码"
    }
  }
}
```

## 构建

设置 `JAVA_HOME`、`ANDROID_HOME` 和可用 Node 的 `PATH` 后，在仓库根目录运行：

```bash
python3 scripts/build-android.py
# 本机当前网络可使用：
python3 scripts/build-android.py --google-alternate-host --proxy http://127.0.0.1:7897
```

脚本调用官方 Gradle Wrapper；正常输出位于 `app/android/app/build/outputs/apk/release/app-release.apk`，成功时打印 SHA-256。保留默认四种 ABI：arm64-v8a、armeabi-v7a、x86、x86_64。

Debug 构建可用 `--variant Debug`，不读取 Release 凭据，需要开发服务器。重新运行 Expo prebuild 后应审查原生工程差异，并保留 Release 签名与校验配置。

## 安装检查

```bash
apksigner verify --verbose --print-certs app/android/app/build/outputs/apk/release/app-release.apk
adb install -r app/android/app/build/outputs/apk/release/app-release.apk
adb shell am start -n cn.yswdra.airadar/.MainActivity
```

早期版本已在 Android 16/API 36 ARM64 模拟器完成安装、云端读取、历史日报、文章详情、收藏和取消、覆盖更新保留登录，以及断网冷启动缓存阅读。当前交付包为本文开头的 `dist/ai-radar-0.9.0-android.apk`，本次升级验收范围见当前版本段落；这些历史验证不构成本轮登录保留证明。此前 0.2.1 / code 3 包及其余额告警验收保留为历史记录；0.2.0 的中文正文、原文切换与离线中英阅读验收同样保留。

余额告警在独立本地 API 中用真实 SDK 模拟 402，验证首页、详情和设置的提示及调用恢复后自动清除。为访问本地 HTTP，测试包临时允许明文连接，Hermes bundle 与最终交付包完全一致；该测试包不对外交付。交付包使用原来的生产网络配置，只连接 HTTPS 云端。详细校验记录见 [VALIDATION.md](VALIDATION.md)。尚未在用户手机上安装，也没有提交应用商店。

用户于 2026-09-08 明确要求今后不再操作微信。此要求覆盖此前通过微信给自己发包的授权，后续安装包直接在对话交付链接。
