# 持久化中文版本

翻译在服务端采集、导入和日报整理时完成。手机端默认中文，可切换原文；阅读、搜索、刷新、收藏、离线缓存均不触发模型调用。原始 `title` / `text` 完整保留，API 另提供 `title_zh` / `text_zh` 和 `translation.status`。

## 配置

将 `DEEPSEEK_API_KEY` 保存在 `/etc/ai-radar/server.env`（0600），不要放入仓库、移动端或日志。在 `/etc/ai-radar/config.toml` 中启用：

```toml
[translation]
enabled = true
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-flash"
review_model = "deepseek-v4-pro"
revision = "zh-v1"
concurrency = 2
max_documents = 100
max_attempts = 3
```

摘要的 `[provider]` 独立配置，仍可使用 Codex CLI、Claude CLI 或模型 API。翻译使用现有 OpenAI SDK 的兼容接口，可通过 endpoint、密钥环境变量、模型和 `request_options` 替换。其他服务不支持 DeepSeek 的 `thinking` 参数时，配置 `request_options = {}`。

## 缓存与恢复

- 缓存键包含原始标题、正文、目标语言、术语表和 `revision`。互动数字、收藏、来源记录 ID 不参与；同内容跨记录共享译文。更改模型不会使全部缓存失效，明确更改 `revision` 才重新生成。
- 原文变更会排队新译文，接口不会展示旧内容的译文。历史收藏和日报引用也会补齐中文。
- 长文分段处理，不截断正文；已生成草稿、已通过校对的段落分别持久化。失败后只恢复未完成步骤。
- 数据库租约防止不同任务同时翻译同一内容。故障退避 30 分钟，自动最多 3 次；超时租约可恢复。每轮最多处理配置数量的新文档。
- X 等来源的标题是截断正文预览时，中文标题取已校对中文正文的前 120 字；避免单独翻译半句话时补写内容。
- 日报已存在时不重新生成。需要更新日报时，模型接收缓存中文与原文证据，整合而非逐篇重新翻译。补充抓取的完整官方正文按自身内容缓存。

管理员可运行 `radar translate`，或 `POST /v1/admin/jobs?kind=translate` 补齐历史中文。`force=true` 仅重新尝试未完成项，不覆盖已完成缓存。API `/v1/status` 返回已完成、等待、失败和待复核数量；不公开密钥或模型原始错误。

## 余额不足提示

DeepSeek 返回 HTTP 402 时，服务端持久保存账户告警，手机首页、文章详情和设置显示“DeepSeek 余额不足”。已有译文与原文继续可读。识别依据为官方 [错误码说明](https://api-docs.deepseek.com/zh-cn/quick_start/error_codes/)，401 授权错误、429 限流和服务器错误不会误报为余额不足。

同一批剩余翻译停止发起新调用，已经在途的调用可能完成；保存的翻译草稿不会丢失，余额错误也不占用内容的最大重试次数。充值后，下次定时或管理员触发的翻译调用成功会清除告警，未完成翻译从已有进度恢复。服务重启不会清除未解决的告警，较早开始的成功请求也不能覆盖后来发生的余额错误。

App 在前台每 30 秒更新服务状态，重新进入前台也会刷新；离线时明确标注缓存告警为“上次记录”。这是 App 内持续提示，尚未接入系统推送。阅读和状态刷新不调用翻译模型，也不主动轮询账户余额。

## 质量门槛

先生成忠实译文，再由单独的模型调用逐句对照原文修订。重点检查否定、推测、引用归属、开放权重与开源的区别、研究结果与证明的区别。原始内容和辅助译文均视为不可信数据，不执行其中的命令。

程序检查数字/版本、百分比、货币符号、链接、@账号、中文覆盖、明显删漏以及输出段落是否完整对应。允许千分位、中英文月份和已知数量单位的等价转换，例如 `13 million` 与 `1300 万`；引用帖的账号和时间单独保护，模型不能重写。无法通过检查或模型仍有疑点的内容标记 `review_required`，不对外发布候选译文。模型校对不能保证零错误，产品保留原文与一手链接，便于核验。

首次真实数据验收还做了编辑复核：纠正 Pixel 品牌、风险等级译法，以及一条把参赛系统误作竞赛名称的主客体错误。后者经 Codex 对照原文定稿，缓存保留先前译文与修正原因，review_model 如实记录额外复核来源。未来新内容仍执行自动翻译与模型校对，不能把这次人工式抽查当作所有未来内容都经过额外复核。

## 可选免费辅助

默认使用 DeepSeek 加本地免费规则校验，不额外占用服务器运行另一个翻译模型。若已部署官方 [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate)，可配置 `auxiliary_url`；如需鉴权，设置 `LIBRETRANSLATE_API_KEY`。该服务基于 [Argos Translate](https://github.com/argosopentech/argos-translate)，有额外内存、模型下载与许可证要求（LibreTranslate 为 AGPL-3.0），本项目未捆绑其运行时或模型。

辅助输出只作为 DeepSeek 的候选参考，不能跳过模型校对直接发布；辅助不可用时继续由 DeepSeek 完成。没有配置时不会把内容发送到未知公共翻译服务。
