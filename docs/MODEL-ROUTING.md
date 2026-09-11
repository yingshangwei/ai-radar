# Codex 三档模型路由

用户指定的 `light` 对应 Codex CLI 的 **low** 参数。新请求明确传入型号与 `model_reasoning_effort`，不依赖 CLI 默认模型或个人配置。

| 档位 | 型号 / 推理档位 | 工作 |
| --- | --- | --- |
| 常规处理 | `gpt-5.6-sol` / `medium` | 日报、网页解读、雷达标题生成，以及根据审核意见进行修正 |
| 前置确认 | `gpt-6-astra` / `low` | 上述内容的事实审计、技术翻译审计、动态关注与前瞻判断 |
| 疑难决断 | `gpt-6-astra` / `medium` | 仅在 low 明确无法决断、且给出可匹配输入材料的实质争议证据时调用 |

普通任务只调用 Sol。low 能完成判断就直接使用其结果，不再走 medium；有证据支持的否定结论也属于完成判断。low 返回的升级理由只能是 `evidence_conflict` 或 `complex_inference`，必须给出具体问题和可核对的材料引用。证据不足、网络/授权失败、超时、无效 JSON 或一般润色均不触发更贵的模型。材料不足仍保留为未完成，不编造通过结果。

更强模型完成后也必须经过原有来源 ID、逐字引文、数字、翻译和事实审核校验；没有通过标准的放宽。逻辑步骤最多包含 low 确认和一次 medium 决断；不会循环升级。原工作流的批次、重试、预算和队列边界保留，实际额外调用单独计量。

## 配置与生效

- 实现：`server/radar/model_router.py`；独立配置示例：`deploy/model-routing.toml`。
- 生产策略：`/etc/ai-radar/model-routing.toml`，通过 `RADAR_MODEL_ROUTING_CONFIG` 指定，修改后重启 API 以加载新策略。
- 三个 profile 的型号、推理档位可配置。明确配置的文件无效/缺失时停止相关调用并报告错误，不自动回到昂贵的默认模型。
- 仅接管 `kind="codex"`；百炼、其他 API/Agent 适配器保持现有配置。百炼 Qwen3.7-Flash 仍负责翻译初稿，Qwen Plus 负责普通翻译校对和审计；技术翻译的 Codex 修正改用 Sol medium、审计用 Astra low，必要时再升级。
- 不修改业务配置的模型指纹、翻译 revision、已通过文章或摘要。策略切换不会仅因换模型触发历史内容批量重新翻译/审核；新调用的实际模型记录在用量账本中。
- 无需升级 App：现有「模型用量」从服务端读取分工与阶段标签，显示 `常规处理 · medium`、`前置确认 · low` 和 `疑难决断（按需）· medium`，Token 继续自动 K → M → B。历史调用不会被重新标记为新档位。

## 会话与恢复

前瞻批次继续使用现有持久队列和 OS 锁。Astra low 在同一角色会话内逐批复用，模型/配置/输出协议发生变化时按原规则开启新会话。medium 的疑难决断使用独立、与批次绑定的持久调用，不把其他候选的历史判断当作证据。

其他事实确认按输入、Schema、配置和功能计算请求标识，确认和决断的回执各自持久化于 `/var/lib/ai-radar/model-routing`。并发相同确认等待同一回执；已完整结束的调用直接恢复，不重复扣取用量。前瞻任务在 low 完成但数据库尚未接收结果时重启，可以恢复低档回执并继续所需决断。`limit=0` 只做无模型的恢复检查，不启动 medium。

如果外部调用结果不确定，保留审计和原工作流的待检查状态，不盲目重发或声称已完成。服务器不会为了降低费用直接修改文章正文、译文或审核结论。

## 验证

- 本地服务端全套 1599 通过、18 项既有跳过；路由、CLI、发现批次及用量相关用例覆盖分档、禁止误升级、并发合并、会话复用和中断恢复。
- 使用服务器已授权 Codex 账号对隔离合成材料调用 Sol medium 与 Astra low，各一次并成功，未触发 medium；隔离账本不会混入业务内容或正式用量。
- medium 的条件分支通过本地模拟 CLI 回执测试；没有为验收强行让生产文章进入 medium。

官方依据：[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)。
