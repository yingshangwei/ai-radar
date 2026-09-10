# 模型分工与 Token 用量

App 0.12 的「你的雷达 → 模型用量」先展示厂商账户余额与额度，下方提供今天、7 天、30 天、累计统计，按模型、业务功能、阶段展开，并显示最近 50 次调用。读取使用现有设备访问令牌；手机不持有模型密钥。

## 账户余额与额度

| 账户 | 官方查询能力 | 展示范围与授权 |
| --- | --- | --- |
| DeepSeek | [`GET /user/balance`](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/) | 使用现有模型 API key，展示各币种总余额、充值余额和赠金，保留负数；当前配置为备用账户 |
| 阿里百炼 | [阿里云 BSS `QueryAccountBalance`](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance) | 额外 RAM 只读凭据；展示阿里云账户可用额度与现金，不代表百炼资源包或免费 Token 余量 |
| Codex / ChatGPT | [官方 app-server](https://learn.chatgpt.com/zh-Hans/docs/app-server) `account/rateLimits/read` | 复用服务器现有 ChatGPT 登录，优先显示 Codex 主额度；其他模型额度可展开，按实际周期展示剩余百分比、重置时间和可选 credits |

余额和额度属于厂商账户，可能包含其他应用的消耗。Codex credits 保留厂商单位，不解释为现金；不同币种、不同额度窗口不合计。API 用量/成本查询不能代替余额查询，普通 OpenAI API 和 Anthropic 账户目前明确为不支持已接入的官方余额查询。

服务每 5 分钟查询一次，手机每 30 秒读服务缓存；点击刷新无需管理员令牌，同一账户最多每 60 秒实际刷新一次。查询不生成模型回答，不创建推理会话，也不兑换 credits、充值或切换模型。余额不足、低余额、授权缺失、响应异常、查询失败和历史数据分别展示。查询失败保留上次成功数值并明确标记；到达额度重置时间后等待厂商确认，不能自行恢复为 100%。

账户查询配置、缓存与业务模型路由独立，默认未配置时关闭。安装百炼查询 SDK 时，在 `server/billing` 运行 `sh install.sh`：官方 SDK 和全部依赖使用版本与哈希锁定，安装到 `/opt/ai-radar/billing/venv`，不修改模型调用环境。

复制 `server/accounts.example.toml` 为 `/etc/ai-radar/accounts.toml`，按环境调整 CLI、SDK Python 和凭据路径，再设置主服务环境：

```dotenv
RADAR_ACCOUNTS_CONFIG_PATH=/etc/ai-radar/accounts.toml
RADAR_ACCOUNTS_DATABASE_PATH=/var/lib/ai-radar/accounts/status.db
```

独立 SQLite 仅保存账户查询快照、重试时间和查询租约；不迁移业务数据库。失败采用退避，租约超时可恢复，查询有超时和进程清理；重启后保留上次状态和刷新限频。授权更新使缓存失效并自动重新查询。手机只在内存缓存账户财务信息，不写入文章离线缓存。

百炼查询需要单独 RAM 身份，最小权限策略如下（`DescribeAcccount` 拼写遵循官方文档）：

```json
{
  "Version": "1",
  "Statement": [{"Effect": "Allow", "Action": ["bss:DescribeAcccount"], "Resource": ["*"]}]
}
```

凭据保存为 `/etc/ai-radar/billing.env`，所有者 `ai-radar`、权限 `0600`：

```dotenv
ALIBABA_CLOUD_ACCESS_KEY_ID=
ALIBABA_CLOUD_ACCESS_KEY_SECRET=
ALIBABA_CLOUD_SECURITY_TOKEN=
```

长期 AccessKey 的 SecurityToken 留空，STS 临时凭据应提供该字段并在过期前更新。此文件按查询重新读取，填好后不需要再次重启服务。已有百炼模型 API key 不能代替 RAM 财务查询凭据；缺少授权时不影响翻译和模型业务。

2026-09-11 已在生产配置专用 RAM 用户及上述单一余额查询权限，服务器真实查询和公网 reader 接口验证通过。凭据更新自动生效，既有服务进程和模型配置保持不变；App 0.12 刷新账户卡片即可查看。

接口为 `GET /v1/accounts`、`POST /v1/accounts/refresh`，均接受现有 reader 或 admin 令牌；返回归一化数值、查询时间和状态，不返回密钥、厂商原始错误或模型内容。默认低余额阈值为 CNY 10 / USD 2，低额度阈值为剩余 10%，可在账户配置中调整。告警在 App 账户卡片中展示，本模块不新增系统推送。

当前生产分工（2026-09-10 实测）：

| 功能 | 生成 / 初稿 | 校对及独立审计 |
| --- | --- | --- |
| 普通中文翻译 | 百炼 Qwen3.7-Flash | 百炼 Qwen Plus |
| 技术论文 / 技术内容翻译 | 百炼 Qwen3.7-Flash | Codex CLI / gpt-6-astra |
| 每日汇报、网页解读、雷达标题 | Codex CLI / gpt-6-astra | Codex CLI / gpt-6-astra |
| 动态关注与前瞻预判 | Codex CLI / gpt-6-astra，前瞻批次复用会话 | 同一批判断，不按候选数量倍增用量 |

配置分工与历史实际用量分别展示。API 返回的模型版本优先于请求中的别名。Codex 的 JSON 回执有 Token 数，未保证报告型号；部署通过 `RADAR_CODEX_DEFAULT_MODEL=gpt-6-astra` 固定服务器探针已验证的现用默认值，并实际传入 `--model`。显式 `ProviderConfig.model` 优先。此默认值的固定不改变业务配置指纹、现有译文、审计结果或预算，也不会引发历史内容重新审核。以后换模型应使用现有各 Provider 的 `model` 配置，使会话和配置变更遵循原状态管理规则。

## 开源组件选择

采用 [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) 和 [Prometheus](https://github.com/prometheus/prometheus)，均属于 CNCF 生态，使用 Apache 2.0 许可。依照 [官方 Python exporter 集成](https://opentelemetry.io/docs/languages/python/exporters/#prometheus)，用官方 SDK 与 Prometheus exporter 输出指标，Prometheus 保存趋势。业务功能标签由 AI Radar 明确传递，不让插件通过提示词猜测。

- `ccusage` 适合本地 coding agent 日志，不覆盖百炼 API 或应用功能归属，且临时 Codex 调用未必有持久日志。
- Langfuse 自托管依赖较多，[官方 Docker Compose 建议](https://langfuse.com/self-hosting/deployment/docker-compose)超过当前共享服务器的资源条件。
- Phoenix 支持 SQLite，但主平台使用 Elastic License 2.0，依赖和运行面更大；本次选择开源许可明确、组件少的指标方案。

Prometheus 3.14.0 从[官方发布](https://prometheus.io/download/)下载并验证 SHA-256。Python exporter 独立虚拟环境固定版本、全部依赖带哈希；不修改主 API 的依赖。OpenTelemetry exporter 当前包版本为 `0.65b0`，属于官方发行的 beta 版本；它独立运行，失败不会影响采集或模型业务，累计值可从账本重新读取。

## 统计规则

- 只统计厂商回执的原始整数，不根据文本长度估算。总量 = 输入 + 输出。缓存读取、缓存写入、推理分别为输入或输出的子集，不能再次加进总量；Anthropic 输入字段不包含缓存，接入时先归一化。
- HTTPX 官方 hooks 覆盖 SDK 的每次物理请求，包括 SDK 内重试。回执在结构校验前保存；无效 JSON 或业务审计未通过仍可能消耗 Token。
- 每个 Codex CLI turn 记录一次。会话复用时包括模型实际报告的历史上下文消耗，不能只统计新增提示词。已有终态回执恢复不重复收费计数；进程中断后的已验证持久回执可补齐已有预约。
- 无回执、超时或断连显示「用量未知」；缺失值不写零。请求预约保留，超过 20 分钟无回执从「进行中」显示为未知。不重发模型调用来补统计。
- 空时段总量为 0；有调用但全部未报告时总量为未知。有部分缺失时显示已报告小计和未知调用数。开始记录之前的历史不虚构、不追算。
- K=1,000，M=1,000,000，B=1,000,000,000；最多两位小数，进位时升级单位，最近调用另保留原始输入、输出整数。
- Token 消耗不等于实际金额；Codex 账号订阅不套用 API 单价生成虚假账单。

## 部署与运维

主服务环境增加：

```dotenv
RADAR_USAGE_DATABASE_PATH=/var/lib/ai-radar/usage/usage.db
RADAR_CODEX_DEFAULT_MODEL=gpt-6-astra
```

独立 SQLite 保存只含用量的预约和回执，WAL、主键去重、事务落盘；不包含提示词、正文、凭据、原始错误或模型回答。主业务数据库没有结构或数据迁移。写入失败保留可见的统计错误计数，并继续业务；极端磁盘/权限故障可能导致统计缺口，不承诺任意系统故障下零丢失。

在服务器 `server/observability` 运行 `sh install.sh` 安装独立组件。脚本不重启 API、不修改应用密钥，已有不同配置会停止要求检查。

| 组件 | 监听 | 内存上限 | 数据 |
| --- | --- | --- | --- |
| OpenTelemetry exporter | 127.0.0.1:18476 | 128 MiB | 只读账本 |
| Prometheus | 127.0.0.1:18477 | 384 MiB | 90 天 / 512 MB 上限 |

两服务启用 systemd 故障重启、进程和 CPU 限制；没有额外开放公网端口。Exporter 没有业务 SDK 或模型密钥。Prometheus 不可用不阻塞 App；恢复后直接读取持久累计数，进程重启不会归零。短暂停采期间累计总数能恢复，但监控曲线的采样时间缺口不能精确重建。需要查看内置网页时用 SSH 转发 18477，不公开无登录的监控页面。

Exporter 使用 SQLite `mode=ro` 查询账本；systemd 允许在独立 usage 目录创建 SQLite WAL 共享内存/锁文件，否则数据库暂时没有这些辅助文件时会读失败。不会给业务数据库或模型凭据目录增加权限。

认证接口：`GET /v1/usage?period=today|7d|30d|all`。精确业务时间统计以此接口为准；Prometheus 用于趋势和运维。示例 PromQL：

```promql
sum by (provider, model, feature, stage) (
  radar_model_tokens_total{token_type=~"input|output"}
)
```

请勿对所有 `token_type` 求和，否则会把缓存、推理再次累加。只读指标 `radar_usage_ledger_available` 为 1 表示账本可读取；没有用量值不能解释成业务没有消耗。账本应纳入服务器常规备份，复制时使用 SQLite backup API。
