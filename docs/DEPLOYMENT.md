# 腾讯云部署

目标实例：`lhins-e5gcg722`，`ap-seoul`，公网 `43.155.203.253`，Ubuntu Server 24.04 LTS，2 核 4 GB。

2026-09-06 已通过腾讯云自动化助手执行只读盘点：已有进程 `server` 使用 `8080`；SSH 使用 `22`；没有发现 Docker、Nginx、Caddy 或 Node。系统盘约 50 GB 可用。盘点脚本末尾的不存在目录使 TAT 返回 ExitCode 2，前面的服务、端口和资源输出已成功获取，不能将其误读为整个盘点没有运行。

已在轻量云控制台确认域名 `yswdra.cn` 存在，当前轻量云关联的解析列表为空。这不是 DNSPod 全量记录的查询结果；正式添加 `radar` 记录前仍需检查完整解析表。

## 隔离布局

| 资源 | AI Radar 使用位置 |
| --- | --- |
| API 监听 | `127.0.0.1:18473` |
| 系统用户 | `ai-radar`，无交互登录 shell |
| 代码发布 | `/opt/ai-radar/releases/<release>` |
| 当前版本 | `/opt/ai-radar/current` |
| Python 环境 | `/opt/ai-radar/venv` |
| Codex 二进制 | `/opt/ai-radar/tools/bin/codex` |
| 数据和独立模型授权 | `/var/lib/ai-radar` |
| 配置 | `/etc/ai-radar/config.toml` |
| 私有环境文件 | `/etc/ai-radar/server.env`，权限 0600 |
| 服务 | `ai-radar.service` |
| 拟用域名 | `radar.yswdra.cn`，需核实 DNS 后配置 |

现有 8080 业务不修改、不停止。新服务限制为约 1.2 GB 内存和 1 个 CPU。API 默认只绑定回环地址，发布前需要单独配置 HTTPS 反向代理，不直接开放 18473。

## 访问腾讯云

优先官方 [TencentCloud CLI](https://github.com/TencentCloud/tencentcloud-cli)：

```bash
server/.venv/bin/tccli configure
# 用户自己填写 SecretId、SecretKey，region 为 ap-seoul，不把密钥发到聊天。
bash scripts/tencent-audit.sh
```

也可以使用控制台的官方自动化助手和文件管理。当前已登录控制台，但本机公网 SSH 连接超时。Chrome 上传部署包需要扩展启用本地文件访问；如果该权限尚未启用，不能声称上传成功。

## 安装已审核的部署包

部署包只包含 server 源码、依赖锁文件和安装脚本，不包含本地 `.env`、数据库、社交平台凭证或个人 Codex 授权。

先上传 `dist/ai-radar-server-20260906.tar.gz` 到该实例。通过 TAT 或 SSH 运行：

```bash
set -e
mkdir -p /opt/ai-radar/releases/20260906
tar -xzf /root/ai-radar-server-20260906.tar.gz -C /opt/ai-radar/releases/20260906
bash /opt/ai-radar/releases/20260906/scripts/install-server.sh
```

安装脚本检查端口冲突，创建独立用户、虚拟环境和 service，通过锁文件校验依赖。初次自动生成服务令牌但不打印。只更新 AI Radar 自己的文件；如果原来已有 AI Radar 版本，健康检查失败会恢复原来的代码链接。

该脚本会安装 Python venv 所需的系统包。不会修改现有域名、SSH 授权、防火墙、反向代理或其他业务端口。完成后先验证 `curl http://127.0.0.1:18473/healthz`。

## Codex 授权

官方 npm 原生发行包可避免额外安装 Node。安装脚本校验 npm 的 SHA-512 integrity 后，只提取固定路径的 Codex 可执行文件，不展开不受控的归档路径：

```bash
python3 /opt/ai-radar/current/scripts/install-codex.py
sudo -u ai-radar env HOME=/var/lib/ai-radar CODEX_HOME=/var/lib/ai-radar/codex \
  /opt/ai-radar/tools/bin/codex login --device-auth
```

由用户完成设备码登录，再执行相同环境下的 `codex login status`。不上传本机整个 Codex 目录。若设备码方式在账号中未开启，按官方文档使用浏览器登录/SSH 转发，不绕过授权。

来源凭证写入 `/etc/ai-radar/server.env`；模型参数写入 `config.toml`。授权验证后将环境文件中的 `RADAR_SCHEDULER_ENABLED=false` 改为 `true`，重启 **AI Radar 自己的服务**。

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
