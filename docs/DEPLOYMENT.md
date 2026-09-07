# 腾讯云部署

目标实例：`lhins-e5gcg722`，`ap-seoul`，公网 `43.155.203.253`，Ubuntu Server 24.04 LTS，2 核 4 GB。

## 当前部署状态 · 2026-09-08

当前 release 为 `/opt/ai-radar/releases/20260908-5672e2d`，增加服务器独立语义审计、自动修订与正式翻译重审 CLI。TAT `inv-m8cvw7g5d9` 成功：46 条消息、七张表既有数据、三份私有配置保留，Caddy 和原 8080 进程不变；本项目 API 与浏览器均正常。18 个完整采集网站显示完成，手机队列的 reader/admin 权限隔离与只读行为通过。此次升级不改移动端，不需要重新安装 App。

完整可重新部署源码归档为 `dist/ai-radar-server-20260908-5672e2d.tar.gz`，119,635 字节，SHA-256 `3d56855f19cb190872556b62654889021a7bc3afeaedf2dd3e57187fe6644fd5`。包内 47 份文件的已提交内容与 `source-manifest.json` 哈希一致，每版使用独立虚拟环境与锁定依赖；依赖准备期间旧 API 继续服务，切换前再次检查任务与授权窗口为空并冻结备份。旧 release 保留以便回滚。

最终切换备份为 `/var/lib/ai-radar/backups/translation-automation-final-20260907T171108626588Z.db`。部署后通过正式 CLI 对 4 份历史编辑记录按原文重审，最终均通过；原文及不相关译文逐项不变，完整历史保留，重复执行为零条且全部翻译缓存不变。重审前备份 `/var/lib/ai-radar/translation-recheck/20260907T171541Z-bcfad5ae/before.db`。实际任务与验证见 [验证记录](VALIDATION.md)。

此前在 `622e107` 上部署过 `338dbb9` 的两份源码补丁：Facebook 中断保留已读数据、美元/序数词校验等价处理；均包含于当前完整版本。TAT `inv-e8cuvr0qej` 成功，历史备份 `/var/lib/ai-radar/backups/collection-reliability-20260907T163602Z` 和归档 `dist/ai-radar-server-20260908-338dbb9.tar.gz` 保留。

0.5.0 历史升级：自动公开网页读取、准确的采集状态、手机正文导入与自动队列。当时 release `/opt/ai-radar/releases/20260907-622e107`，每版独立 `.venv`，前版 `bebe45b` 及更早版本保留。新增 `document_captures` 表，既有表列不改动；服务器配置与模型/社交密钥不变。升级前备份 `/var/lib/ai-radar/backups/radar-device-queue-20260907T143032Z.db`，44 条消息及七张表旧数据逐项一致，Caddy 与原 8080 服务保持运行。noVNC 的 `android-compat-v1` 灰屏修复继续生效。

归档 `dist/ai-radar-server-20260907-622e107.tar.gz`，114,607 字节，SHA-256 `f651d42652db0e6fc05e6dd84ac07e3e27ebb045c4f80ebf372379cc68c49599`。预检 `inv-e8cr83g9gv`，启用 `inv-e8cr9a0w58`，均 SUCCESS；公开 HTTPS 健康、鉴权隔离、浏览器空闲、17 个完整采集网站及手机队列跳过冷却文章均通过。具体逻辑见 [WEB-AUTHORIZATION.md](WEB-AUTHORIZATION.md)。

0.2.1 历史升级：release `20260907-52fd8e5` 增加 DeepSeek 余额不足持久告警，相关版本与备份均保留。

此前 04:05 已启用 DeepSeek 翻译和独立校对，补齐 40 条中文。密钥以一次性 RSA OAEP 加密传输、0600 保存；真实日报重建证明 40 份翻译缓存没有再次调用翻译。配置与复核说明见 [TRANSLATION.md](TRANSLATION.md)。

03:20 用户提供 X Token 并确认充值，凭据已通过一次性 RSA 4096 OAEP/SHA-256 加密传到服务器，仅写入私有 `server.env`（0600）。进程加载校验通过，其他环境变量和服务均保留，临时传输私钥已删除。备份位于 root 私有 `/opt/ai-radar/incoming/x-token-20260907`。服务器配置每查询 10 条、1 页，每两小时采集。

首轮官方 API 任务 `c700474d-e3a8-4b38-967a-6f6a2c65c3de` 完成，读取 30 条、新增 6 条、更新 1 条旧帖，总计 40 条，X 为 healthy。Codex 任务 `b5d910c6-6ecc-4575-bfbc-117ae6cef67f` 已更新 9 月 6 日日报，5 条输入形成 1 条报道、4 个引用，原文和日期窗口校验通过。下面的部署记录保留各阶段的历史计数。

- 已通过用户配置的腾讯云官方 CLI + TAT 部署，当前 API release 为 `/opt/ai-radar/releases/20260908-5672e2d`；历史版本均已保留。
- HTTPS 地址：`https://radar.yswdra.cn`；`/healthz` 已从本机及服务器验证为 200。无令牌读取返回 401，reader 读取返回 200，reader 调用管理任务返回 403。
- 官方 Caddy 2.11.4 已安装并运行，使用独立 `radar` 主机规则；DNSPod 新增 `radar` A 记录，防火墙只追加 TCP 443。原有根域名、`www`、22/80/8080 规则未更改。
- 原有 8080 进程 PID `303258` 保持运行，部署前后访问根路径均返回 404；新 API 仅监听 `127.0.0.1:18473`。
- 首次 5 个官方来源采集 23 条有效信息。9 月 7 日 00:52 加入 Meta Newsroom 后有 6 个健康官方来源、25 条有效信息；之后导入 8 条已授权浏览器采集的 X 公开帖，总计 33 条，并实际生成 9 月 5 日与 6 日中文日报。当时 X 尚待 API 凭据，现已接通并观察到自然定时采集；Facebook 账号仍在审核。
- Codex CLI 0.153.3 完整原生包已安装，包括 code-mode host、bwrap、rg 和包清单。9 月 7 日 00:03（北京时间）服务用户完成设备码登录，独立 `codex login status` 返回 `Logged in using ChatGPT`。
- 云端实际调用 Codex 生成 9 月 4 日历史日报，6 条中文总结使用 9 个来源，所有引用 ID 均匹配原文。任务完成后开启调度器：每天北京时间 08:00 汇报，每两小时采集。
- reader 连接信息通过临时 RSA 公钥加密传回本机，仅保存在仓库忽略的 `credentials/cloud-reader.env`（0600）中，未输出到日志。腾讯云密钥和模型凭据没有打进 App。
- Meta RSS 仅通过外部配置 `/etc/ai-radar/config.toml` 加入，代码 release 未改变。修改前确认无运行中任务并备份配置，重启本项目服务后验证调度器、HTTPS、鉴权和原有 8080 进程均正常。
- 随后发布了 X 长帖、引用上下文和分页中断保留数据的修复。43 项测试通过；部署包校验 SHA-256 后解压到新目录，新建独立 `.venv`，复验来源文件与仓库一致、33 条数据保留、调度器仍开启、私有配置文件哈希未变、Codex 仍已登录。升级前使用 SQLite backup API 生成一致性备份。
- 新版本再次真实采集成功，新增 1 条官方信息，总计 34 条；6 个官方来源健康。HTTPS、读写权限隔离与原有 8080 进程再次通过复验。

网页解读历史升级包为 `dist/ai-radar-server-20260907-5a980ec.tar.gz`，SHA-256 `ac02ca1fccf67efa53b1543f08b740c3f5f7e22c12434f0e717fbb1524a3354a`。包内以 `requirements.lock` 安装服务端依赖，不需要开发用 `uv.lock`；不含数据库、令牌和移动端签名材料。

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

使用 SQLite backup API，避免单独复制主库遗漏 WAL 数据。升级前备份并保留旧 release。本次采用新增独立表，不改既有列；未来涉及既有结构修改时须提供明确迁移，不能仅依靠 `create_all` 升级旧库。不要将旧版代码和不兼容的新库混用。

Docker Compose 是另一个可选方案，配置在 `deploy/compose.yaml`。目标主机尚未装 Docker，当前不为部署强制安装它。不要同时启动 systemd 方案与 Compose 方案。
