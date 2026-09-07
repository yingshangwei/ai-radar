"""Equivalent percent spelling and social handles must keep their original meaning."""

import pytest

from radar.translation import MENTION, quality_issues


@pytest.mark.parametrize("source,candidate", [
    ("The AI score rose 20 percent.", "AI 得分提高了 20%。"),
    ("The AI score rose 20%.", "AI 得分提高了百分之20。"),
    ("The AI score rose 20%.", "AI 得分提高了 20％。"),
    ("The AI score rose 0.1 percent.", "AI 得分提高了百分之0.1。"),
    ("The AI score rose 20 per cent.", "AI 得分提高了 20%。"),
    ("The AI score rose 20 PER CENT.", "AI 得分提高了百分之20。"),
    ("The AI score rose 20％.", "AI 得分提高了 20%。"),
])
def test_equivalent_percent_spellings_pass(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source", ["The AI score rose 20 percent.", "The AI score rose 20%."])
@pytest.mark.parametrize("amount", ["2", "200"])
@pytest.mark.parametrize("pattern", ["AI 得分提高了 {}%。", "AI 得分提高了百分之{}。", "AI 得分提高了 {}％。"])
def test_equivalent_percent_marks_never_hide_changed_numbers(source, amount, pattern):
    assert "数字或版本不一致" in quality_issues(source, pattern.format(amount))


@pytest.mark.parametrize("source,candidate", [
    ("The AI score rose 20 percent.", "AI 得分提高了 20。"),
    ("The AI score rose 20%.", "AI 得分提高了 20。"),
    ("The AI score rose 20.", "AI 得分提高了百分之20。"),
    ("The AI score rose 20.", "AI 得分提高了 20％。"),
    ("The AI score rose 20％.", "AI 得分提高了 20。"),
    ("The AI score rose 20 per cent.", "AI 得分提高了 20。"),
])
def test_percent_unit_cannot_be_added_or_dropped(source, candidate):
    assert "百分比或货币标记不一致" in quality_issues(source, candidate)


@pytest.mark.parametrize("source,candidate", [
    ("The AI score is at the 20th percentile.", "AI 得分处于第20百分位。"),
    ("The AI score rose 20 percentage points.", "AI 得分提高了20个百分点。"),
])
def test_percentile_and_percentage_points_are_distinct_units(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source,candidate", [
    ("The AI score is at the 20th percentile.", "AI 得分处于20%。"),
    ("The AI score rose 20 percentage points.", "AI 得分提高了20%。"),
    ("The AI score rose 20%.", "AI 得分提高了20个百分点。"),
])
def test_percentage_cannot_replace_a_percentile_or_percentage_point_unit(source, candidate):
    assert "百分比或货币标记不一致" in quality_issues(source, candidate)


@pytest.mark.parametrize("text,handles", [
    ("来自@alice的更新", ["@alice"]),
    ("由@alice和@bob发布", ["@alice", "@bob"]),
    ("由@alice_2发布", ["@alice_2"]),
    ("@alice.", ["@alice"]),
    ("消息来自@alice。", ["@alice"]),
    ("AI news from @alice.", ["@alice"]),
    ("消息来自「@alice」。", ["@alice"]),
])
def test_social_handles_can_touch_chinese_text(text, handles):
    assert MENTION.findall(text) == handles


@pytest.mark.parametrize("source,candidate", [
    ("AI news from @alice.", "来自@alice的 AI 消息。"),
    ("AI news from @alice and @bob.", "来自@alice和@bob的 AI 消息。"),
    ("由@alice发布 AI 更新。", "AI 更新由 @alice 发布。"),
])
def test_equivalent_chinese_handle_spacing_passes(source, candidate):
    assert quality_issues(source, candidate) == []


@pytest.mark.parametrize("source,candidate", [
    ("由@alice发布 AI 更新。", "由@bob发布 AI 更新。"),
    ("由@alice发布 AI 更新。", "发布 AI 更新。"),
    ("发布 AI 更新。", "由@alice发布 AI 更新。"),
    ("由@alice发布 AI 更新。", "由@alice和@alice发布 AI 更新。"),
    ("由@alice和@bob发布 AI 更新。", "由@alice和@carol发布 AI 更新。"),
])
def test_changed_missing_added_or_duplicate_handles_are_rejected(source, candidate):
    assert "引用账号不一致" in quality_issues(source, candidate)


@pytest.mark.parametrize("text", [
    "alice@example.org", "first.last@example.org", "word@foo", "alice+tag@example.org",
    "邮箱用户@example.org", "用户@例子.公司", "邮箱alice@example.org。",
])
def test_email_like_ascii_words_are_not_social_mentions(text):
    assert MENTION.findall(text) == []


@pytest.mark.parametrize("source", [
    "AI docs: https://example.org/@alice",
    "AI docs: https://example.org/path?account=@alice",
    "AI docs: https://alice@example.org/path",
])
def test_handles_inside_urls_use_link_integrity_not_social_mention_checks(source):
    issues = quality_issues(source, "请查看 AI 文档。")
    assert "原文链接不一致" in issues
    assert "引用账号不一致" not in issues


def test_url_filtering_keeps_real_mentions_outside_the_url():
    source = "AI docs https://example.org/@alice from @bob."
    correct = "来自@bob的 AI 文档 https://example.org/@alice"
    wrong = "来自@carol的 AI 文档 https://example.org/@alice"
    assert quality_issues(source, correct) == []
    assert "引用账号不一致" in quality_issues(source, wrong)


def test_changed_handle_inside_url_is_still_a_link_error():
    issues = quality_issues("AI docs https://example.org/@alice", "AI 文档 https://example.org/@bob")
    assert "原文链接不一致" in issues
    assert "引用账号不一致" not in issues


@pytest.mark.parametrize("candidate", ["AI 费用为 20 欧元。", "AI 费用为 20。"])
def test_mark_normalization_keeps_existing_explicit_currency_gate(candidate):
    assert "百分比或货币标记不一致" in quality_issues("The AI fee is USD 20.", candidate)
