# 腾讯云部署

目标实例：`lhins-e5gcg722`，`ap-seoul`，公网 `43.155.203.253`，Ubuntu Server 24.04 LTS，2 核 4 GB。

## 当前部署状态 · 2026-09-07

- 已通过用户配置的腾讯云官方 CLI + TAT 部署，当前 API release 为 `/opt/ai-radar/releases/20260906-c8f7568`。
- HTTPS 地址：`https://radar.yswdra.cn`；`/healthz` 已从本机及服务器验证为 200。无令牌读取返回 401，reader 读取返回 200，reader 调用管理任务返回 403。
- 官方 Caddy 2.11.4 已安装并运行，使用独立 `radar` 主机规则；DNSPod 新增 `radar` A 记录，防火墙只追加 TCP 443。原有根域名、`www`、22/80/8080 规则未更改。
- 原有 8080 进程 PID `303258` 保持运行，部署前后访问根路径均返回 404；新 API 仅监听 `127.0.0.1:18473`。
- 首次 5 个官方来源采集 23 条有效信息。9 月 7 日 00:52 加入 Meta Newsroom 后有 6 个健康官方来源、25 条有效信息；之后导入 8 条已授权浏览器采集的 X 公开帖，总计 33 条，并实际生成 9 月 5 日与 6 日中文日报。X 定时采集仍等待付费 API 选择与凭据接入，Facebook 账号正在审核。
- Codex CLI 0.153.3 完整原生包已安装，包括 code-mode host、bwrap、rg 和包清单。9 月 7 日 00:03（北京时间）服务用户完成设备码登录，独立 `codex login status` 返回 `Logged in using ChatGPT`。
- 云端实际调用 Codex 生成 9 月 4 日历史日报，6 条中文总结使用 9 个来源，所有引用 ID 均匹配原文。任务完成后开启调度器：每天北京时间 08:00 汇报，每两小时采集。
- reader 连接信息通过临时 RSA 公钥加密传回本机，仅保存在仓库忽略的 `credentials/cloud-reader.env`（0600）中，未输出到日志。腾讯云密钥和模型凭据没有打进 App。
- Meta RSS 仅通过外部配置 `/etc/ai-radar/config.toml` 加入，代码 release 未改变。修改前确认无运行中任务并备份配置，重启本项目服务后验证调度器、HTTPS、鉴权和原有 8080 进程均正常。

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

使用 SQLite backup API，避免单独复制主库遗漏 WAL 数据。升级前备份并保留旧 release。当前为初始数据库结构；未来涉及结构修改时须提供明确迁移，不能仅依靠 `create_all` 升级旧库。不要将旧版代码和不兼容的新库混用。

Docker Compose 是另一个可选方案，配置在 `deploy/compose.yaml`。目标主机尚未装 Docker，当前不为部署强制安装它。不要同时启动 systemd 方案与 Compose 方案。
