# Android 构建与签名

## 0.4.0 / code 5

交付包 `dist/ai-radar-0.4.0-android.apk`，69,398,978 字节，SHA-256 `5351b9ac1342a9b22adef95a7ed439181a7b7f01b013d9f38338c9586bfcc1a5`。原签名证书和四 ABI 保持不变，可覆盖旧版保留登录。新增 App 内网页授权中心、受保护的远程浏览器、一次保存管理令牌与验证后自动补采，使用方法见 [WEB-AUTHORIZATION.md](WEB-AUTHORIZATION.md)。

应用 ID 为 `cn.yswdra.airadar`，最低 Android 7（API 24）。Release 会内置 JavaScript 和 Hermes 字节码，启动时不需要 Metro；服务地址默认使用已部署的 `https://radar.yswdra.cn`，设备令牌由用户在连接页填写。

## 0.3.0 / code 4

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

2026-09-07 已在 Android 16/API 36 ARM64 模拟器完成安装、云端读取、历史日报、文章详情、收藏和取消、覆盖更新保留登录，以及断网冷启动缓存阅读。最新 0.2.1 / code 3 包位于 `dist/ai-radar-0.2.1-android.apk`，继续使用相同私有签名；0.2.0 的中文正文、原文切换与离线中英阅读验收保留，0.2.1 增加了 DeepSeek 余额不足提示。

余额告警在独立本地 API 中用真实 SDK 模拟 402，验证首页、详情和设置的提示及调用恢复后自动清除。为访问本地 HTTP，测试包临时允许明文连接，Hermes bundle 与最终交付包完全一致；该测试包不对外交付。交付包使用原来的生产网络配置，只连接 HTTPS 云端。详细校验记录见 [VALIDATION.md](VALIDATION.md)。尚未在用户手机上安装，也没有提交应用商店。
