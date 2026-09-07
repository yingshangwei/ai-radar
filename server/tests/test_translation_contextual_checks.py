import pytest

from radar.translation import quality_issues


@pytest.mark.parametrize("source,candidate", [
    ("May improve AI results.", "可能改善 AI 结果。"),
    ("May I use this AI model?", "我可以使用这个 AI 模型吗？"),
    ("May created an AI model.", "梅创建了一个 AI 模型。"),
    ("June created an AI model.", "琼创建了一个 AI 模型。"),
    ("Dr. June leads AI research.", "琼博士领导 AI 研究。"),
    ("June said AI could help.", "琼说 AI 可以提供帮助。"),
    ("In May's opinion, AI can help.", "在梅看来，AI 能提供帮助。"),
    ("March forward with AI tools.", "借助 AI 工具向前进。"),
    ("March 5 miles with AI support.", "在 AI 支持下行进 5 英里。"),
    ("May", "五月"),
    ("March", "三月"),
    ("May speaks in June about AI.", "梅在六月谈论 AI。"),
    ("AI tools may release updates.", "AI 工具可能发布更新。"),
    ("May update AI tools.", "可能更新 AI 工具。"),
    ("A temperature of 0.1 may improve results.", "0.1 的温度可能改善结果。"),
    ("Version 2.1 may improve results.", "2.1 版本可能改善结果。"),
    ("Model v3.0.5 may improve results.", "模型 v3.0.5 可能改善结果。"),
    ("A count of 1,005 may improve results.", "1005 的数量可能改善结果。"),
])
def test_names_modals_and_verbs_do_not_invent_calendar_numbers(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source,candidate", [
    ("The AI release is May 5.", "AI 将于五月 5 日发布。"),
    ("The AI release is May 5.", "AI 将于 5月 5 日发布。"),
    ("The AI release is in May.", "AI 将在五月发布。"),
    ("The AI release is May 2026.", "AI 将于 2026 年五月发布。"),
    ("The AI release is 5 May 2026.", "AI 将于 2026 年五月 5 日发布。"),
    ("The AI release is during March.", "AI 将在三月期间发布。"),
    ("The AI release is March 5, 2026.", "AI 将于 2026 年三月 5 日发布。"),
    ("In June, 13 million lines of AI code.", "六月，1300 万行 AI 代码。"),
    ("The April meeting may include 5 models.", "四月会议可能包括 5 个模型。"),
    ("The May meeting covers AI.", "五月会议讨论 AI。"),
    ("June release", "六月发布"),
    ("The May update improves AI tools.", "五月更新改进了 AI 工具。"),
    ("The July incidents prompted AI changes.", "七月事件促使 AI 发生变化。"),
    ("In early April, AI tools improved.", "四月初，AI 工具有所改进。"),
    ("April alignment risk update", "四月对齐风险更新"),
    ("August Risk Report", "八月风险报告"),
    ("January, February, July, August", "一月、二月、七月、八月"),
    ("April", "四月"),
    ("The AI release is 5 May.", "AI 将于五月 5 日发布。"),
])
def test_explicit_dates_keep_equivalent_month_representations(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source,candidate", [
    ("The AI release is May 5.", "AI 将于六月 5 日发布。"),
    ("The AI release is in May.", "AI 将在六月发布。"),
    ("The AI release is May 2026.", "AI 将于 2026 年六月发布。"),
    ("The AI release is May 2026.", "AI 将于 2025 年五月发布。"),
    ("The AI release is in May.", "AI 将发布。"),
    ("The AI release is March 5.", "AI 将于三月 6 日发布。"),
    ("The AI release is during March.", "AI 将在四月期间发布。"),
    ("The AI release is coming.", "AI 将在五月发布。"),
    ("May discusses 5 AI tools.", "梅讨论 6 个 AI 工具。"),
    ("May", "五月和五月"),
    ("The April meeting may include 5 models.", "五月会议可能包括 5 个模型。"),
    ("The AI model may improve.", "AI 模型在五月可能改进。"),
    ("The May meeting covers AI.", "六月会议讨论 AI。"),
    ("June release", "七月发布"),
    ("The May update improves AI tools.", "六月更新改进了 AI 工具。"),
    ("The July incidents prompted AI changes.", "八月事件促使 AI 发生变化。"),
    ("The July incidents prompted AI changes.", "事件促使 AI 发生变化。"),
    ("In early April, AI tools improved.", "五月初，AI 工具有所改进。"),
    ("April alignment risk update", "五月对齐风险更新"),
    ("August Risk Report", "风险报告"),
    ("January, February, July, August", "一月、二月、七月、九月"),
    ("The AI release is 5 May.", "AI 将于六月 5 日发布。"),
    ("A temperature of 0.1 may improve results.", "0.2 的温度可能改善结果。"),
    ("Version 2.1 may improve results.", "2.2 版本可能改善结果。"),
])
def test_date_and_number_changes_are_still_rejected(source, candidate):
    assert "数字或版本不一致" in quality_issues(source, candidate)


@pytest.mark.parametrize("source,candidate", [
    ("The AI fee is EUR 5.", "AI 费用为 5 欧元。"),
    ("The AI fee is 5 EUR.", "AI 费用为 5 欧元。"),
    ("The AI fee is 5 euros.", "AI 费用为 5 欧元。"),
    ("The AI fee is one euro.", "AI 费用为一欧元。"),
    ("The AI fee is GBP 5.", "AI 费用为 5 英镑。"),
    ("The AI fee is 5 GBP.", "AI 费用为 5 英镑。"),
    ("The AI subscription costs 5 pounds.", "AI 订阅费用为 5 英镑。"),
    ("I paid 5.50 pounds for AI tools.", "我为 AI 工具支付了 5.50 英镑。"),
    ("I paid five pounds for AI tools.", "我为 AI 工具支付了五英镑。"),
    ("The AI fee is 5 pounds sterling.", "AI 费用为 5 英镑。"),
    ("The AI fee is 5 British pounds.", "AI 费用为 5 英镑。"),
    ("The AI subscription costs €5 and £6.", "AI 订阅费用为 5 欧元和 6 英镑。"),
    ("The robot weighs 5 pounds and costs 6 pounds.", "机器人重 5 磅，售价 6 英镑。"),
])
def test_explicit_currency_words_codes_and_symbols_are_equivalent(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source,candidate", [
    ("The AI fee is EUR 5.", "AI 费用为 5 美元。"),
    ("The AI fee is 5 euros.", "AI 费用为 5 英镑。"),
    ("The AI fee is 5 euros.", "AI 费用为 5。"),
    ("The AI fee is GBP 5.", "AI 费用为 5 欧元。"),
    ("The AI subscription costs 5 pounds.", "AI 订阅费用为 5 欧元。"),
    ("The AI subscription costs 5 pounds.", "AI 订阅费用为 5 磅。"),
    ("The robot weighs 5 pounds.", "机器人重 5 英镑。"),
    ("The robot weighs 5.5 pounds.", "机器人重 5.5 英镑。"),
    ("The robot lifts 5 pounds of rice.", "机器人举起 5 英镑的大米。"),
    ("We added 5 pounds (weight).", "我们增加了 5 英镑。"),
    ("We carry 5 pounds by weight.", "我们携带 5 英镑。"),
    ("It costs £5.", "它重 5 pounds。"),
    ("It costs GBP 5.", "它重 5 pounds。"),
])
def test_wrong_or_missing_currencies_and_weight_mistranslations_still_fail(source, candidate):
    assert "百分比或货币标记不一致" in quality_issues(source, candidate)


@pytest.mark.parametrize("source,candidate", [
    ("The robot weighs 5 pounds.", "机器人重 5 磅。"),
    ("The robot weighs 5.5 pounds.", "机器人重 5.5 磅。"),
    ("The robot lifts 5 pounds of rice.", "机器人举起 5 磅大米。"),
    ("We added 5 pounds (weight).", "我们增加了 5 磅（重量）。"),
    ("We carry 5 pounds by weight.", "我们携带的重量为 5 磅。"),
    ("AI at Euro 2024.", "2024 欧洲杯上的 AI。"),
    ("The text says 5 pounds.", "文本写着 5 磅。"),
    ("The text says 5 pounds.", "文本写着 5 英镑。"),
])
def test_weight_and_unresolved_unit_context_do_not_invent_currency(source, candidate):
    # Passing deterministic checks is not publication: unresolved semantics still
    # require the existing independent model audit and correction gates.
    assert quality_issues(source, candidate) == []


def test_ambiguity_allowance_does_not_hide_more_currency_markers_or_wrong_amounts():
    assert "百分比或货币标记不一致" in quality_issues(
        "The text says pounds.", "文本写着英镑和英镑。"
    )
    assert "数字或版本不一致" in quality_issues(
        "The AI subscription costs 5 pounds.", "AI 订阅费用为 6 英镑。"
    )
    assert "百分比或货币标记不一致" in quality_issues(
        "The AI subscription costs EUR 5 at 20%.", "AI 订阅费用为 5 欧元。"
    )
