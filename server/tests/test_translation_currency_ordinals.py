"""Regression for the real Tibo translation withheld by equivalent unit spelling."""

import pytest

from radar.translation import quality_issues

SOURCE = """Squad could be my first BILLION dollar product

the launch went well, revenue has spiked to $8k MRR

it's really the most ambitious product I've ever worked on

because it's incredibly useful:
- it has everything Grok Bot does
- on top, you run it on any model you want
- and on your squad's own cloud computer

what's not there yet is a story simple enough to explain all this

I also feel the product is best suited for established companies and teams that already make money

the further you are in your business, the more obvious Squad becomes

a great problem to solve for us as indie makers, in an extremely crowded market, and going against one of the biggest AI labs in the world  🙌"""

REVIEWED = """Squad 可能是我第一个十亿美元的产品

发布进展顺利，收入已飙升至 $8k MRR

这确实是我做过的最具雄心的产品

因为它非常实用：
- 它拥有 Grok Bot 的所有功能
- 此外，你可以在任何你想要的模型上运行它
- 并且可以在你自己团队的云端计算机上运行

目前还缺少的是一个足够简单的故事来解释这一切

我还觉得这个产品最适合那些已经盈利的成熟公司和团队

你的业务越深入，Squad 的优势就越明显

作为独立开发者，在一个极度拥挤的市场中，并与世界上最大的 AI 实验室之一竞争，这对我们来说是一个很好的挑战 🙌"""


def test_actual_reviewed_tibo_translation_accepts_equivalent_spellings():
    assert quality_issues(SOURCE, REVIEWED) == []


@pytest.mark.parametrize(
    ("original", "replacement", "issue"),
    [
        ("$8k", "$9k", "数字或版本不一致"),
        ("$8k", "$8", "数字或版本不一致"),
        ("$8k", "$", "数字或版本不一致"),
        ("$8k", "8k", "百分比或货币标记不一致"),
        ("$8k", "€8k", "百分比或货币标记不一致"),
        ("第一个", "第二个", "数字或版本不一致"),
    ],
)
def test_actual_translation_still_rejects_changed_amount_unit_or_order(original, replacement, issue):
    assert issue in quality_issues(SOURCE, REVIEWED.replace(original, replacement))


@pytest.mark.parametrize("currency", ["dollar", "dollars", "DOLLARS", "USD", "US dollars", "U.S. dollars"])
def test_dollar_words_and_codes_match_chinese_currency(currency):
    assert quality_issues(f"The AI API costs 20 {currency}.", "AI API 的费用为 20 美元。") == []


@pytest.mark.parametrize("translation", ["AI API 的费用为 20。", "AI API 的费用为 20 欧元。"])
def test_dollar_word_currency_cannot_be_omitted_or_changed(translation):
    assert "百分比或货币标记不一致" in quality_issues("The AI API costs 20 dollars.", translation)


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("My first AI product.", "我的第一个 AI 产品。"),
        ("My FIRST AI product.", "我的第一个 AI 产品。"),
        ("The AI ranked 8th.", "该 AI 排名第八。"),
        ("The first AI model reached 8th place.", "第一个 AI 模型达到了第八名。"),
        ("The first name of the AI engineer.", "AI 工程师的名字。"),
        ("First, run the AI model.", "首先，运行 AI 模型。"),
    ],
)
def test_spelled_ordinals_do_not_manufacture_arabic_numbers(source, translation):
    assert quality_issues(source, translation) == []


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("My first AI product.", "我的第二个 AI 产品。"),
        ("My first AI product costs $1.", "我的第一个 AI 产品价格为 $。"),
        ("My first AI product.", "我的第一个 AI 产品，以及第一个 AI 平台。"),
        ("The AI ranked 8th.", "该 AI 排名第九。"),
    ],
)
def test_equivalent_spelled_ordinal_does_not_hide_other_numbers(source, translation):
    assert "数字或版本不一致" in quality_issues(source, translation)
