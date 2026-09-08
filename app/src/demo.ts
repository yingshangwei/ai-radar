import type { Article, Digest, Source, Status, Watch } from "./types";

// Fictional content, explicitly marked in the UI. Never sent to the server or presented as live news.
const stamp = "2026-09-06T00:00:00+00:00";
export const demoArticles: Article[] = [
  {
    id: "demo-1",
    platform: "x",
    source_id: "x",
    title: "Agent 的下一步：从回答问题，到完成工作",
    text: "Design preview: the next step for agents is not just answering questions. It is completing useful work, checking the outcome, and knowing when to ask for help. This is fictional content.\n\n[引用帖：@builder_demo，2026-09-06T00:00:00+00:00]\nDesign preview: a reliable agent needs clear goals and a way to verify its work. This is not a real post.",
    title_zh: "Agent 的下一步：从回答问题，到完成工作",
    text_zh:
      "设计示例：当模型开始调用工具、管理上下文和验证结果，产品的核心问题也在变化。可靠的 Agent 不只是给出答案，还要完成任务、核对结果，并知道什么时候需要人的帮助。以上内容为虚构示例。\n\n[引用帖：@builder_demo，2026-09-06T00:00:00+00:00]\n设计示例：可靠的智能体需要清晰的目标，也需要验证自己工作的能力。这不是真实发言。",
    translation: { status: "ready" },
    presentation: {
      status: "ready",
      title_zh: "Agent 的下一步：从回答问题，到完成工作",
    },
    author: "林遥 · 设计示例",
    handle: "design_preview",
    url: "https://openai.com",
    published_at: stamp,
    metrics: {},
    topics: ["产品", "技术"],
    score: 96,
    priority: true,
    saved: false,
  },
  {
    id: "demo-2",
    platform: "rss",
    source_id: "demo",
    title: "开源模型，正在成为开发者的新基础设施",
    text: "设计示例：展示开源生态的文章阅读体验。正式连接后，这里会呈现可追溯到原始来源的实际内容。",
    author: "开源生态 · 示例",
    handle: "",
    url: "https://huggingface.co/blog",
    published_at: stamp,
    metrics: {},
    topics: ["开源", "模型"],
    score: 88,
    priority: false,
    saved: true,
    presentation: {
      status: "ready",
      title_zh: "开源模型，正在成为开发者的新基础设施",
    },
  },
  {
    id: "demo-3",
    platform: "facebook",
    source_id: "facebook",
    title: "多模态交互：让 AI 看见更大的世界",
    text: "设计示例：不同平台的来源使用一致的阅读界面，同时保留原始平台与链接。此内容仅供设计预览。",
    author: "研究动态 · 示例",
    handle: "",
    url: "https://ai.meta.com",
    published_at: stamp,
    metrics: {},
    topics: ["模型", "观点"],
    score: 81,
    priority: true,
    saved: false,
    presentation: {
      status: "ready",
      title_zh: "多模态交互，让 AI 理解更丰富的上下文",
    },
    social: {
      reply_to: {
        author: "研究员 · 示例",
        handle: "research_demo",
        url: "https://example.org/design-preview",
        published_at: stamp,
      },
    },
  },
];
export const demoWatches: Watch[] = [
  ["OpenAI", "OpenAI", "OpenAI", "官方动态"],
  ["Anthropic", "AnthropicAI", "Anthropic", "Claude · 官方动态"],
  ["Google DeepMind", "GoogleDeepMind", "Google", "研究与产品"],
  ["Sam Altman", "sama", "OpenAI", "重点人物"],
  ["Boris Cherny", "bcherny", "Anthropic", "Claude Code"],
  ["Demis Hassabis", "demishassabis", "Google", "重点人物"],
  ["Tibo", "tibo_maker", "Independent", "AI 产品与独立开发"],
].map(([name, handle, organization, role]) => ({
  id: handle!,
  name: name!,
  handle: handle!,
  organization: organization!,
  role: role!,
  enabled: true,
}));
export const demoSources: Source[] = [
  {
    id: "x",
    name: "X / Twitter",
    platform: "x",
    status: "preview",
    message: "设计预览，尚未连接真实数据",
  },
  {
    id: "facebook",
    name: "Facebook",
    platform: "facebook",
    status: "auth_required",
    message: "连接服务器后配置 Meta 读取授权",
  },
];
export const demoDigest: Digest = {
  date: "2026-09-06",
  title: "能力的边界，正在向外延伸",
  overview:
    "今天的设计示例围绕三个方向：Agent 从对话走向行动，开源生态持续演进，多模态带来新的交互可能。这些内容仅展示阅读体验。",
  stories: demoArticles.map((a, i) => ({
    title: a.title,
    summary: a.text,
    why_it_matters: [
      "值得关注的，是模型能力如何转化为真实工作流。",
      "开发成本与可控性，将影响下一代产品的选择。",
      "新的交互方式，往往先于新的产品形态出现。",
    ][i]!,
    category: a.topics[0]!,
    source_ids: [a.id],
  })),
  sources: demoArticles,
  provider: "design_preview",
  generated_at: stamp,
  window_start: stamp,
  window_end: stamp,
  source_count: 3,
  coverage: demoSources,
};
export const demoStatus: Status = {
  timezone: "Asia/Shanghai",
  daily_time: "08:00",
  provider: "design_preview",
  scheduler_enabled: false,
  article_count: 3,
  sources: demoSources,
  jobs: [],
};
