# App 热更新

从 Android **0.13.0 / versionCode 14** 开始接入 Expo 官方 `expo-updates ~55.0.30`。旧版需要先覆盖安装一次基础 APK。之后 JavaScript 页面、交互逻辑和随包静态资源可通过 OTA 更新；新增原生模块、权限、SDK 或签名证书仍需新基础包。服务端功能部署本身不依赖 App 热更新。

## 用户体验

- 冷启动自动检查和下载；已下载的更新在下次冷启动生效。
- 从后台返回时再次检查，15 分钟内合并重复检查；手动检查间隔至少 30 秒。
- 设置 → **应用更新** 显示当前内容版本、下载状态，提供“检查更新”和“立即应用更新”。阅读中不强制重启。
- 网络失败继续使用已安装内容。Expo 校验签名和资源哈希，不完整的下载不会替换当前启动版本。
- 使用 Expo 自带的启动错误恢复机制。**这不意味着任何运行时错误都能自动回退**：首次内容展示前的启动错误可以回退；内容展示后的错误通常需要发布修复更新。数据结构迁移必须兼容旧版本。

## 部署与边界

分发 URL：`https://radar.yswdra.cn/updates/v1/manifest`，Expo Updates v1；客户端携带 platform、runtime 和 channel。默认 `stable`，`preview` 用于发布前的分发验证。Android 与 iOS、不同 runtime 互相隔离；未发布匹配版本时返回 204。

分发进程独立运行在 `127.0.0.1:18478`，服务名 `ai-radar-updates`，代码 `/opt/ai-radar/updates`，数据 `/var/lib/ai-radar-updates`。Caddy 只把 `/updates/*` 转发到新服务，其余 Radar 路由和其他站点继续沿用原服务。该进程只读、无登录令牌、无签名私钥、无上传 API，资源文件可公开下载。与主服务共享已锁定的 Python 依赖环境，不导入业务应用，也不运行采集或翻译任务。

首次安装使用 `scripts/install-updates.sh PACKAGE_DIR EXPECTED_CADDY_SHA256`。PACKAGE_DIR 必须包含 `server/radar/updates.py`、`scripts/publish-updates.py`、`app/updates/certificate.pem`、`deploy/ai-radar-updates.service`（按文件名平铺）。安装先核对端口、Caddy 哈希，健康验证失败会恢复原 Caddy 配置并停止新服务。后续升级分发程序应单独审查，只重启 `ai-radar-updates`。

## 签名与原生兼容性

手机内置 `app/updates/certificate.pem` 公钥证书。RSA-SHA256 私钥默认位于仓库内已忽略的 `credentials/ota/private-key.pem`，要求 0600；也可通过 `RADAR_OTA_PRIVATE_KEY` 指定。**私钥仅留在发布端，不上传服务器或 Git**。证书有效期到 2036 年；到期或密钥轮换前需要发布包含新证书的基础包。请单独加密备份私钥和 Android 签名文件。

`app/updates/native-android.json` 记录 Expo 官方 fingerprint 和测试过的基础 APK 哈希。发布工具在导出前后都检查原生指纹；发现变动就拒绝发布，不允许用已有 runtime 覆盖不同原生指纹。不要手改基线绕过检查。

新原生版本流程：更新 `app.json` 的版本、runtime、versionCode/buildNumber，同步原生配置；运行 App 测试、类型检查，构建并验证基础包；最后登记基线：

```sh
node app/scripts/ota.cjs baseline android dist/ai-radar-NEW-android.apk
```

项目固定 Expo/RN 的 NDK 27.1.12297006；Android 根 Gradle 文件也为 expo-updates 指定同一版本，避免其自动选择额外 NDK。运行 prebuild 后保留这项配置和现有 release 签名配置。

## 发布日常更新

先修改代码及 `app/src/updateRevision.ts`，测试与类型检查通过后执行。下面命令从仓库根目录运行，需项目 Node/pnpm、Python 3.9+、OpenSSL，以及已配置的官方腾讯云 CLI：

```sh
node app/scripts/ota.cjs prepare android dist/ota/RELEASE preview
python3 scripts/publish-updates.py dist/ota/RELEASE --certificate app/updates/certificate.pem
python3 scripts/upload-updates.py dist/ota/RELEASE --channel preview
```

Expo CLI 负责导出 Hermes bundle；发布工具生成并离线签署 manifest。只允许清单和其直接引用的静态资源；不上传源码映射、环境文件或导出临时文件。`EXPO_NO_DOTENV=1` 禁止导出时自动载入 dotenv；不得把秘密放入 `EXPO_PUBLIC_*` 或 App 代码。

验证 preview 的清单签名、所有资源哈希、runtime，再发布 stable，并用安装了匹配基础包的 Android 验证实际下载、应用、重启和离线启动。**向 stable 发布后，兼容设备可能立即下载**，应先完成代码审查和测试：

```sh
python3 scripts/upload-updates.py dist/ota/RELEASE --channel stable
```

上传通过腾讯 TAT 分块传输，服务器验证压缩包哈希、公钥证书一致性、清单签名、每个资源哈希与文件白名单后，原子切换单个 channel/runtime 指针。数据目录中不可变资源不覆盖、旧指针保存在 `history`。发布记录位于本地 `dist/ota/upload-*`，每个远程调用都有 invocation id；失败时检查已有 invocation，工具不会自动重新提交不确定的写操作。

也可在服务器对已上传且验证过的 stage 执行 `publish-updates.py --channel stable`，避免重复传输资源。**先验证，再激活**；不要直接编辑 channel 指针。旧时间戳不能覆盖新发布；回退需要签署新的指令。

## 回到基础包

```sh
node app/scripts/ota.cjs rollback android dist/ota/ROLLBACK preview
python3 scripts/upload-updates.py dist/ota/ROLLBACK --channel stable
```

这会发布有签名的 `rollBackToEmbedded` 指令，设备下次成功检查后下载回退指令，重启应用基础包。它需要设备能够联网检查，并不是对所有崩溃的即时远程控制。已提交的历史清单和资源应保留，不要删除可能仍被设备使用的文件。

## iOS

iOS 原生配置、公钥证书和共享更新界面已接入，同样按 platform/runtime 隔离。当前只验证了 iOS JavaScript 导出；本机缺少完整 Xcode 和 iOS 签名条件，尚无经过原生验证的新 IPA，所以没有登记 iOS 基线或发布 iOS OTA。完成签名构建及真机验证后，使用 `baseline ios TESTED.ipa` 登记，再按同样流程发布。

依据：[Expo Updates SDK](https://docs.expo.dev/versions/v55.0.0/sdk/updates/)、[更新协议 v1](https://docs.expo.dev/technical-specs/expo-updates-1/)、[签名说明](https://docs.expo.dev/eas-update/code-signing/)、[错误恢复边界](https://docs.expo.dev/eas-update/error-recovery/)。参考了 Expo 官方自建服务示例的协议处理，没有直接将其演示服务作为生产依赖。

当前 stable 内容版：**0.13.0.12**（雷达与收藏按发言人筛选），沿用 Android 0.13.0 / code 14 基础包。原文优先、手动拉取和翻译归档继续保留；本次交付见 [AUTHOR-FILTER.md](AUTHOR-FILTER.md)。

2026-09-17：本地上传回执目录现在同时保存权限 0600 的 `archive.tar.gz`，发生分块传输中断时可用原始字节核验及补传缺失块。不要重新打包后混用旧分块：gzip 时间戳会使整包哈希变化。没有拿到提交回执不等于远端未写入，须先核对远端块哈希；本次 0.13.0.10 已按此方式完成恢复与 stable 验证。

2026-09-18：stable 更新为 **0.13.0.11**，增加正文/Chat 的 Markdown 与公式排版、管理员通用 Chat、Mac 补采状态。仍匹配 Android 0.13.0 基础包，不需要重装 APK。交付记录见 [RICH-CONTENT-VALIDATION.md](RICH-CONTENT-VALIDATION.md)。
