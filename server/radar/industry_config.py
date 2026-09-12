"""Personal industry research limits; source collection has no paid connectors."""

from pydantic import BaseModel, Field


class IndustryConfig(BaseModel):
    enabled: bool = False
    collect_minutes: int = Field(default=30, strict=True, ge=15, le=1440)
    lookback_days: int = Field(default=90, strict=True, ge=7, le=365)
    max_items_per_source: int = Field(default=12, strict=True, ge=1, le=30)
    analysis_enabled: bool = True
    analysis_hours: int = Field(default=24, strict=True, ge=6, le=168)
    max_evidence: int = Field(default=12, strict=True, ge=2, le=24)
    max_evidence_chars: int = Field(default=36000, strict=True, ge=4000, le=80000)
    max_calls_per_day: int = Field(default=12, strict=True, ge=1, le=40)
    max_snapshots_per_theme_per_day: int = Field(default=1, strict=True, ge=1, le=2)


THEMES = {
    "infrastructure": {
        "id": "infrastructure", "name": "算力与基础设施",
        "description": "芯片、网络、数据中心、电力与散热的需求及兑现。",
        "hypothesis": "AI 投资增加能否转为供应商订单、交付、利润和现金回款。",
        "indicators": ["客户资本开支及其 AI 口径", "订单、积压订单转收入", "交付与通电容量",
                       "毛利率、库存和现金流"],
        "risks": ["项目取消或延期", "量增价跌与过度备货", "融资成本、出口限制及客户集中"],
        "terms": [r"\b(?:gpu|semiconductor|chip|datacenter|data center|capex|hbm|inference)\b",
                  r"算力|芯片|数据中心|半导体|液冷|资本开支|推理成本"],
    },
    "software": {
        "id": "software", "name": "企业软件与 AI 商业化",
        "description": "从模型能力、产品和付费部署，观察软件收入与利润。",
        "hypothesis": "单位合格任务成本下降能否促进付费采用，覆盖推理成本并提高留存。",
        "indicators": ["付费客户、席位与实际使用", "收费单位和每客户收入", "净收入留存与续费",
                       "推理成本之后的毛利与现金流"],
        "risks": ["免费捆绑与价格竞争", "AI 新收入替代原有席位收入", "采用热度不能证明付费回报"],
        "terms": [r"\b(?:copilot|saas|enterprise|agent|agents|coding|developer|llm|model)\b",
                  r"企业软件|智能体|付费|编程|大模型|开发工具|模型发布"],
    },
    "workflows": {
        "id": "workflows", "name": "工作流与服务业变迁",
        "description": "客服、咨询、内容、教育及传统企业工作流的效率与利润分配。",
        "hypothesis": "AI 自动化改善单位任务经济性，同时改变服务计价、获客和岗位结构。",
        "indicators": ["生产部署与真实处理量", "成功解决成本、返工与满意度", "按人头或按结果计费",
                       "收入、获客成本、留存与毛利"],
        "risks": ["质量下降与人工接管", "客户压价吃掉效率收益", "裁员或业务收缩造成表面人均提升"],
        "terms": [r"\b(?:productivity|customer service|contact center|automation|workforce|healthcare|education)\b",
                  r"生产率|客服|外包|自动化|医疗|教育|工作流|招聘|裁员|广告"],
    },
}
