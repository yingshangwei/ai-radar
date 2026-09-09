# 国内模型团队与重点作者

2026-09-09 核验。这里的关注是雷达的采集名单，不会替用户在 X 上点击 Follow；X 个人 Following 列表需要另行确认账号，不能从应用 Bearer Token 推断用户身份。

## 官方渠道

| 团队 | X 采集账号 | 可独立读取的官方渠道 | 接入方式 |
| --- | --- | --- | --- |
| ByteDance Seed | 暂不添加未核实账号 | [官方博客](https://seed.bytedance.com/en/blog)、[开源组织](https://github.com/ByteDance-Seed) | 自动读博客首页；只保存公告原日期、标题和原始摘要 |
| Qwen / 通义千问 | [@Alibaba_Qwen](https://x.com/Alibaba_Qwen) | [官方研究](https://qwen.ai/research)、[开源组织](https://github.com/QwenLM) | 官方 X；帖子明确链接交给网页读取队列 |
| DeepSeek | [@deepseek_ai](https://x.com/deepseek_ai) | [研究与公告](https://www.deepseek.com/en/news/)、[API 更新](https://api-docs.deepseek.com/updates)、[开源组织](https://github.com/deepseek-ai) | 官方 X + 公告首页；API 更新通过直接引用链接读取 |
| Kimi / 月之暗面 | [@Kimi_Moonshot](https://x.com/Kimi_Moonshot) | [研究博客](https://www.kimi.com/en/blog/)、[开放平台](https://platform.kimi.com/blog)、[开源组织](https://github.com/MoonshotAI) | 官方 X + 研究首页，包括首页明确列出的团队开源项目 |
| MiniMax | [@MiniMax_AI](https://x.com/MiniMax_AI) | [公告](https://www.minimax.io/news)、[官方公告归档](https://ir.minimax.io/news-events/new-releases)、[开源组织](https://github.com/MiniMax-AI) | 官方 X + 官方归档，原文链接指向 minimax.io |
| GLM / 智谱 / Z.ai | [@Zai_org](https://x.com/Zai_org) | [开发文档](https://docs.z.ai/)、[开源组织](https://github.com/zai-org) | 官方 X；帖子明确链接交给网页读取队列 |

账号以官方站点/开源组织反链和 X 官方 API 返回的真实身份交叉核对，不以蓝标或同名判断。MiniMax 官网部分页面仍链接旧的 `MiniMax__AI`；X 查询显示旧名不存在，当前同一官方主体使用 `MiniMax_AI`。`bytedanceSeed` 查到的是无简介、无发言、9 个关注者的账号，没有当作官方来源。Qwen 旧 RSS `qwenlm.github.io/blog/index.xml` 可访问，但最新条目停在 2025-09-23，不能作为当下新公告的完整来源。

独立公告采集每次仅访问四个固定官方首页，各最多 100 条卡片、5 MB、30 秒；不递归、不扫描仓库活动、不使用微信、不调用模型获取列表。没有发布日期的卡片不入库，更新日期和置顶顺序不冒充发布日期。首页只有旧消息时正常记录“读取成功、近期新增 0”，不回填成今日消息。正文由现有网页队列读取，标题/摘要属于部分证据；DeepSeek 翻译与独立审核在服务器流程内完成，缓存复用。

## 新增重点作者

- [Simon Willison](https://simonwillison.net/) / `simonw`：LLM 工具、Agent、工程实践。
- [Sebastian Raschka](https://sebastianraschka.com/) / `rasbt`：模型训练、推理和论文解读。
- [Jim Fan](https://jimfan.me/) / `DrJimFan`：NVIDIA 具身智能和机器人研究。
- [Binyuan Hui](https://huybery.github.io/) / `huybery`：代码模型研究；当前 X 简介明确写 Formerly Qwen，不标记为在职 Qwen 员工。
- [Shunyu Yao](https://ysymyth.github.io/) / `ShunyuYao12`：语言智能体、ReAct、评测。个人主页更新较早，名单只记录研究方向，不据此推断当前雇主。

上述账号与五个团队账号加入默认名单；已有同 ID 记录（包括用户关闭的账号）不会被启动逻辑覆盖。生产名单保留现有人工选择与自动试关注，新增后共 28 个启用账号。

## 关注发言的覆盖

关注账号查询为 `from:handle -is:retweet`，保留本人发言、回复和引用，不把纯转发伪装成本人观点。仍筛选 AI 相关内容；重点账号不受普通帖子最低互动量限制。模型识别补充 Qwen、Kimi、MiniMax、GLM、Seedance、Seedream 和中文模型品牌，避免仅包含这些名字的新发言被漏掉。

新增 `x_watch_freshness_first = true`：当前关注账号的最新窗口过期时，可以占用本轮原先预留给广泛搜索的请求；所有最新窗口处理后恢复广泛搜索。服务器每两小时、每轮总计最多 3 次 X 请求、每页最多 10 条保持不变。28 个账号的测试在 10 轮、18 小时覆盖各账号最新窗口，并恢复广泛搜索；这不是全量帖文或实时覆盖保证。繁忙账号的后续页仍通过持久分页补采，历史缺口如实保留。

公告通过配置启用，可随时移除某一个来源：

```toml
official_news_sources = ["seed", "deepseek", "kimi", "minimax"]
x_watch_freshness_first = true
```

这两项是 TOML 根级字段，放在 `[provider]` 等表之前。无需新安装包或新增平台授权。
