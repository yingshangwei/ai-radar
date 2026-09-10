# 模型分工与 Token 用量

App 0.11 的「你的雷达 → 模型用量」提供今天、7 天、30 天、累计统计，按模型、业务功能、阶段展开，并显示最近 50 次调用。读取使用现有设备访问令牌；手机不持有模型密钥。

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
