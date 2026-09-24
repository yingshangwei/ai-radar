# 独立 Surge Mac 接入

2026-09-24 为现有服务器增加 Surge 原生 Trojan/TLS 入口。此目录只包含无凭据的部署生成器；密码、订阅令牌、实际配置、安装脚本、验证客户端和下载的 App 均在仓库外的私有运维目录。不要把生成文件提交到 Git。

## 隔离边界

- `private-xray` 的 VLESS/REALITY TCP 24443 入口不改配置、凭据、二进制或进程。
- 新 `private-surge` 使用专用用户、独立复制的官方 Xray 26.9.9 二进制和 TCP 25443；新密码不复用旧凭据。
- 订阅 HTTPS 与 Trojan 共用新端口。普通 HTTP 请求经 Xray fallback 到仅监听 127.0.0.1:25444 的独立 Caddy 实例 `private-surge-profile`，采用随机 256 位令牌精确匹配路径。错误路径返回 404，响应 no-store，不开启访问日志；不修改现有 Caddy 配置，也不占用其管理接口。
- 仅增加云防火墙 TCP 25443；原六条规则保留。两种客户端共用原服务器带宽与流量额度。
- TLS 保持标准证书和域名校验。只读复用现有 Caddy 管理的 `radar.yswdra.cn` 证书，通过 root 定时任务每 30 分钟检查续期，验证域名、有效期与密钥匹配，复制到新服务专属目录，切换原子符号链接，仅重启新服务；证书未变化时不重启。现有 Caddy 仍负责 ACME 续期。
- 新代理拒绝回环、私网、元数据和本机公网地址。服务有内存/CPU/任务数限制，崩溃 5 秒后恢复，开机自动运行。

## 生成与部署

这是该私有服务器专用的运维代码，并非通用安装器。部署前必须重新核对域名、证书位置、端口、云防火墙和系统服务；`prepare.py` 内保留此部署的非秘密地址。系统基线 `/var/lib/private-surge-ops/baseline.json` 必须提前记录 `services`（MainPID/ActiveState/NRestarts）及 `hashes`，参考部署审计。

```sh
python3 ops/private-surge/prepare.py --private-dir "$SURGE_PRIVATE_DIR"
python3 ops/private-surge/build-install.py --private-dir "$SURGE_PRIVATE_DIR"
```

必须指定仓库外私有目录。首次生成随机密码与令牌，后续保留已有凭据。生成的 `install.sh` 含凭据，只能经授权的私有服务器管理通道执行；不要打印、上传网站或提交 Git。它在已有 `/etc/private-surge` 时拒绝覆盖，禁止把它当升级脚本反复执行。官方工具 `scripts/tencent-command.py` 使用 TAT `SaveCommand=False`；状态不确定时查询已有 invocation，不重复部署。

服务配置先以 Xray/Caddy 验证，再启动新服务。执行后必须比对原服务 PID、重启次数及受保护文件哈希。需要回滚时只停止并禁用新服务及新证书同步定时器，再精确删除本次新增的防火墙规则；不得覆盖整套防火墙或修改旧节点。

## 客户端

私有目录中的 `subscription-url.txt` 是实际 HTTPS 配置地址；`surge.conf` 是离线副本。Surge 从 URL 下载后使用规则模式：国内 IP 和局域网直连，其余走 Seoul；托管配置在 App 运行时按不短于一天的间隔检查更新，更新失败可继续使用旧配置。链接相当于访问凭据，不应发送给公开订阅转换服务。

## 本次验证

- 官方 Surge Mac 6.9.1 CLI 对最终 profile `--check` 返回 `OK`。测试工具由官方 v6 下载地址取得，macOS codesign 完整签名验证通过；只运行校验工具，没有安装 App、启用系统代理或变更本机路由。
- 真实公网 HTTPS 下载与本地 profile 字节一致，GET/HEAD 成功；缺失令牌、错误令牌、路径穿越请求返回 404。
- 真实 Trojan 客户端：服务器出口正确，Google 204、GitHub 200、UDP DNS 成功，错误密码及服务端内网/元数据访问被拒绝。
- 原 VLESS/REALITY 节点 Google 204、服务器出口检查再次通过。
- 新服务崩溃恢复、证书未变化不重启、权限隔离与原服务基线由独立审计脚本检查。实际用户 Surge App 导入和其所在网络体验仍需客户端使用确认。

参考：[Surge Trojan](https://manual.nssurge.com/policies/trojan.html)、[托管配置](https://manual.nssurge.com/profile/managed-profile.html)、[Surge CLI](https://manual.nssurge.com/tools/cli.html)。

首次下载如受本机已有 VPN 的虚拟 DNS/路由影响，应先退出该 VPN，或从私有目录导入 `surge.conf` 离线副本。配置已带服务器域名的固定 Host 映射，后续托管更新使用同一私有 URL。本次只让临时测试进程绑定物理网卡，没有修改用户电脑全局 DNS、路由或原 VPN。
