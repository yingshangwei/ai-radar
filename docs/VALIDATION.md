# 验证记录 · 2026-09-06

## 已验证

| 验证 | 结果 |
| --- | --- |
| Python 服务端测试 | 32 passed；覆盖鉴权、导入、去重、筛选、收藏、时区/DST、无数据日报、授权缺失、限流、分页、原文域名限制、恶意重定向、引用校验、模型适配，以及部署切换和回滚 |
| Ruff | 通过 |
| TypeScript | `tsc --noEmit` 通过 |
| Expo 依赖匹配 | `expo install --check` 通过 |
| 原生工程生成 | Android / iOS prebuild 通过 |
| 三平台 JS 产物 | Android Hermes、iOS Hermes、Web export 全部通过 |
| 依赖审计 | Python 锁文件 `pip-audit` 与 App `pnpm audit --prod` 均未发现已知漏洞 |
| 真实官方采集 | 5 个官方来源已读取，收录近一周 23 条有效信息 |
| 真实 Codex 账号调用 | 已用本机 ChatGPT 登录执行 `codex exec`，完成结构化中文摘要与引用校验 |
| 真实日报 | 已生成 2026-09-04 历史日报，后续用官方正文补充证据后重新生成；2026-09-06 为明确标识的无新增/来源覆盖报告 |
| 浏览器端到端 | 本机服务连接、真实信息流、详情、收藏写入和列表、刷新保留登录、历史日报选择已通过 |
| 视觉检查 | 已检查手机尺寸下连接页、日报和导航，以及实际历史日报；纸张白/墨黑/朱橙配色，原生轨道图形与 App 图标 |
| 部署脚本 | bash 语法检查通过；在临时目录用模拟系统命令验证正常升级、依赖安装失败、启动失败、健康检查失败、首次安装失败以及拒绝原地升级；已生成不含秘密的 server 部署包。这些测试不代替 Linux 主机实际部署 |
| 腾讯云盘点 | 已通过官方 TAT 读取目标主机端口、服务和资源；现有业务监听 8080 |

模型 API 的 OpenAI、Chat Completions、Anthropic 分支通过模拟响应测试；**只有 Codex 分支已用真实账号调用**。Claude Code CLI 与 Generic CLI 均实现适配，Generic CLI 已用真实子进程契约测试，Claude CLI 尚未完成真实授权联调。

审计曾发现 Expo 构建链 `xcode → uuid@7` 的中度公告 [GHSA-w5hq-g745-h8pq](https://github.com/advisories/GHSA-w5hq-g745-h8pq)。检查调用点仅使用 v4；仍将该依赖覆写到保留 CommonJS 的修复版 `11.1.1`，重新生成原生工程成功，复查为零已知漏洞。没有为消除提示而任意跨代升级全部依赖。

## 尚未完成，不能视为已交付

1. **腾讯云安装与 HTTPS 发布**：部署包上传被 Chrome 扩展缺少本地文件访问权限阻止；本机 TCCLI 还没有凭证，公网 SSH 超时。没有上传成功，没有更改目标服务器服务、端口、DNS 或现有业务。需要用户启用扩展文件 URL 访问，或完成 `tccli configure`。
2. **服务器上的 Codex 授权**：本机登录有效不等于服务器已登录；需在独立服务用户下进行设备码授权。没有将个人凭证复制到服务器。
3. **X 与 Facebook 实际采集**：官方适配与模拟接口测试已完成，实际账户授权仍待用户。浏览器 X 未登录，登录页面已保留。Facebook 的 Graph API 读取还需要具体应用权限与 Page ID。
4. **可安装 APK**：原生 Gradle 构建实际尝试过；Gradle 9 下载成功，但原生插件依赖解析失败；SDK 36、Build Tools 36 和 NDK 27.1 下载持续超时/SSL 握手失败。当前没有 APK。不能将 Hermes bundle 当作 APK。
5. **可安装 IPA/模拟器原生构建**：本机只有 Command Line Tools，没有完整 Xcode；等待安装 Xcode 或授权 Expo EAS。iOS 工程和 Hermes bundle 不是经过签名的 IPA。
6. **账号身份细节**：Tibo 默认设为 `tibo_maker`，仍待用户确认。关注名单可以在 App 中调整。

## 本地查看

- App 预览：`http://localhost:8082`，提供明确标识的设计示例入口。
- 开发 API：`http://127.0.0.1:18473`，使用 `server/.env` 中的令牌。
- 当前 Chrome 预览另接 `127.0.0.1:18474` 的本机测试实例，使用非生产的临时 reader token，管理操作禁用。生产部署不会使用该测试令牌。
- 这些本地进程只在本机运行期间可用；启动命令见 README。

下一步以授权和网络条件解决后的真实部署、原生构建、端到端复验为准，不重复把已经完成的代码当成整项任务完成。
