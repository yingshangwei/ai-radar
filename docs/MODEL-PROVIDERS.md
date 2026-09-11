# GLM、Grok、Gemini 接入核对

核对日期：2026-09-12。本记录是官方接口与当前源码的兼容性检查，不代表三家账号已授权或真实模型调用已通过；没有切换生产模型。

## 当前代码能做什么

`server/radar/providers.py` 已有官方 OpenAI Python SDK 的两种协议适配器：`openai_chat` 使用 Chat Completions，`openai` 使用 Responses。`kind` 表示协议适配器，并不表示请求一定发往 OpenAI；实际厂商由 `base_url` 和对应密钥决定。现有依赖可以复用，无需额外安装聚合网关或手机插件。

| 厂商 | 建议的现有适配器 | 官方 Base URL | 需要的授权 |
| --- | --- | --- | --- |
| GLM / 智谱国内平台 | `openai_chat` | `https://open.bigmodel.cn/api/paas/v4/` | 智谱开放平台 API Key |
| GLM / Z.AI 国际平台 | `openai_chat` | `https://api.z.ai/api/paas/v4/` | 对应 Z.AI 平台 API Key |
| Grok / xAI | `openai`，即 Responses | `https://api.x.ai/v1` | xAI API Key 与 API 额度 |
| Gemini / Google | `openai_chat` | `https://generativelanguage.googleapis.com/v1beta/openai/` | Google AI Studio 项目下的 Gemini API Key |

地址和认证依据：[智谱兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)、[Z.AI API](https://docs.z.ai/api-reference/introduction)、[xAI 快速开始](https://docs.x.ai/developers/quickstart)、[Gemini 兼容接口](https://ai.google.dev/gemini-api/docs/openai)。国内智谱与国际 Z.AI 使用各自平台颁发的密钥和地址，不假设账号、余额或 key 通用。GLM Coding Plan 有另外的端点与额度规则，本应用的后台内容处理按通用 API 接入。

## 配置片段

以下是互斥的基础连接示例，`model` 必须改成该账号实际开放的型号。仅将需要的 provider 配置合并到目标功能，不用片段覆盖整份生产配置；这些示例也不读取或包含真实密钥。

### GLM 国内平台

```toml
[provider]
kind = "openai_chat"
base_url = "https://open.bigmodel.cn/api/paas/v4/"
api_key_env = "GLM_API_KEY"
model = "YOUR_ENABLED_GLM_MODEL"
structured_outputs = false
timeout_seconds = 240
```

当前智谱[对话补全文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)明确描述 `json_object`。因此基础接入保守选择 JSON 模式，不假设 OpenAI strict JSON Schema 全部兼容。`false` 只关闭厂商侧强约束 Schema；服务端仍执行 Pydantic、来源 ID、事实及翻译审核，JSON 格式正确不等于内容审核通过。

### Grok

```toml
[provider]
kind = "openai"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
model = "YOUR_ENABLED_GROK_MODEL"
timeout_seconds = 240
```

xAI 官方提供 OpenAI SDK `responses.parse` 的[结构化输出示例](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)，与现有 Responses 适配器相符。这里需要 xAI 平台的密钥；项目用于采集推文的 X Bearer Token 不是这个凭据。

Grok 的 [X Search](https://docs.x.ai/developers/tools/x-search) 是另一个需要明确配置的工具能力。仅把摘要模型换成 Grok，不会自动联网搜索或接管 X 采集；当前适配器没有传入该工具。若后续接入，应保留检索引用、时间范围和独立工具费用记录。

### Gemini

```toml
[provider]
kind = "openai_chat"
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
api_key_env = "GEMINI_API_KEY"
model = "YOUR_ENABLED_GEMINI_MODEL"
structured_outputs = true
timeout_seconds = 240
```

Google 官方兼容接口支持 Pydantic 结构化输出。新授权建议直接从 [Google AI Studio](https://aistudio.google.com/) 创建 key；[当前密钥文档](https://ai.google.dev/gemini-api/docs/api-key)正在将 Standard key 迁移到 Auth key，新建 key 默认采用后者。上线实测还需核对项目模型权限、额度和服务器出口的[支持地区](https://ai.google.dev/gemini-api/docs/available-regions)。密钥只保存在服务端，不随 APK 或 OTA 分发。

## 接入三档路由还需要补齐什么

当前 `model_router.active()` 明确只接管 `kind="codex"`，三个 profile 也只支持 Codex 型号和 effort。**不能直接把三档配置中的型号改为 GLM、Grok 或 Gemini**，否则它仍会调用 Codex CLI。上述 API 配置可以用于既有的按功能 provider 配置，但不会自动继承 low → medium 的条件升级或 Codex 持久 session。

要实现跨厂商三档选择，应扩展路由 profile 为独立的 provider 配置，并沿用相同的证据升级规则、调用身份和恢复记录。常规生成、审核、发现的 provider 可以分别选择；API 会话上下文、缓存计费和断线后处理必须按厂商协议实现，不能把 Codex session ID 传给其他 API。

启用前还需要补齐这些能力：

- **成本参数映射**：当前 API ProviderConfig 尚未暴露推理预算和输出 Token 上限。GLM 的 `thinking` / `reasoning_effort`、Grok Responses 的 `reasoning.effort`、Gemini 的 `reasoning_effort` / thinking 配置不同，不能统一透传 Codex 的 `medium`。例如 xAI 当前部分型号默认 high，GLM 文档部分型号将 low/medium 映射为 high，不能仅换 URL 就认为成本已经受控。[GLM 参数](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[Grok 推理](https://docs.x.ai/developers/model-capabilities/text/reasoning)、[Gemini 映射](https://ai.google.dev/gemini-api/docs/openai#thinking)。
- **用量与错误归属**：已有 HTTP hooks 可记录标准输入、输出 Token 与功能；目前这三个域名会归为 `openai_compatible`。应增加准确厂商标识，验证缓存/推理 Token 字段，并将各家的额度不足、限流和授权错误映射为手机可理解的状态。既有余额模块也不会因模型 API 可用就自动支持新厂商余额。
- **隔离实测**：使用合成材料验证连接、真实业务 Schema、来源引用、数字/否定、中文和公式，以及超时/重试和回执。对比质量、延迟、输入/输出/推理 Token 后再指定用途；相同任务的总成本包含失败重试和二次审核，不能只按型号名称判断便宜。

此次没有新的模型调用、生产配置修改或 App 更新。原 Sol medium / Astra low / 按需 Astra medium 与百炼翻译继续运行。
