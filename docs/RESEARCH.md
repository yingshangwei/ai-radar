# 学界信息采集

学界内容与社交动态共用雷达、中文翻译、网页解读和日报流程，标记「学界」。当前 App 的全部来源列表直接兼容，无需新建栏目。

## 来源与筛选

- **Hugging Face 热门论文**：官方 Daily Papers API，读取热门和最新各一页（每页最多 100 条），按真实社区赞同数与评论数筛选。默认至少 10 个赞同、每轮最多 8 篇。保留原作者摘要，忽略平台生成的 AI 摘要。赞同数代表社区关注，不能推断同行评审、引用量、学术共识或实验已复现。
- **arXiv 前沿理论**：官方 API 的 `cs.LG`、`stat.ML` 分类候选，按泛化、样本复杂度、收敛、缩放定律等理论关键词筛选。默认读取最多 60 个候选、入选最多 4 篇；不制造点赞或「热门」标签。
- 原论文时间必须在现有雷达最近 7 天的窗口内。HF 推荐时间、arXiv 修订时间和采集时间不能替代初版发表时间；旧论文重新被推荐不会伪装成当天新论文。
- 两来源按无版本 arXiv ID 合并，同一论文只保留一条雷达记录。版本变化更新对应摘要证据并重新进入既有翻译审核流程；单纯票数变化不重复翻译，理论来源不能覆盖已保存的 HF 关注指标，未标版本的摘要不能覆盖较新版本。

## 摘要与中文

服务器直接保存官方 API 提供的论文题目、作者、原摘要，并标明「作者摘要」。中文由 DeepSeek 的现有翻译、纠错和独立审核流程生成，Codex 复用已保存的原文与中文生成解读、标题和日报。主消息和论文摘要资源使用相同翻译缓存。

当前默认解读的是**作者摘要**，不声称已阅读完整论文或验证证明。研究结论须保留成立条件，区分理论证明、经验规律与实验结果；摘要生成和独立审核均包含这些约束。已保存的真正全文不会被摘要覆盖。

只处理来源明确提供的项目、代码、讨论链接。论文 PDF、参考文献与 arXiv 导航不会被扩展成需要逐项授权的采集任务。不会递归追踪引用论文。

## 配置与恢复

在服务器配置中启用：

```toml
[research]
hf_enabled = true
arxiv_enabled = true
refresh_hours = 6
hf_limit = 8
hf_min_upvotes = 10
arxiv_limit = 4
arxiv_candidates = 60
```

两来源独立记录状态、时间和结果，正常每 6 小时刷新，由现有采集任务触发。源错误不会阻断 X 或其他来源；限流、访问受限、畸形响应与部分成功会明确反映在来源状态中，失败最早 1 小时后自动重试。每轮最多一个 arXiv API 请求，远低于官方单连接、至少间隔 3 秒的要求。结果仍受既有后台队列与模型预算控制，不为学界内容创建第二套翻译执行器。

这两个公开接口无需账号或密钥，复用现有 `httpx` 与 `feedparser`，没有引入新的抓取依赖。

官方资料：[HF OpenAPI](https://huggingface.co/.well-known/openapi.json)、[HF SDK](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/hf_api.py)、[HF 限流](https://huggingface.co/docs/hub/en/rate-limits)、[arXiv API](https://info.arxiv.org/help/api/user-manual.html)、[arXiv API 使用条款](https://info.arxiv.org/help/api/tou.html)。
