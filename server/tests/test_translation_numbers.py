from collections import Counter

import pytest

from radar.translation_numbers import number_counts


@pytest.mark.parametrize('source,candidate', [
    ('Aug and Sep releases', '8月和9月发布'),
    ('Aug. 5, 2026; Sept. 7, 2026', '2026年8月5日；2026年9月7日'),
    ('Jan. Feb. Mar. Apr. Jun. Jul. Oct. Nov. Dec.', '1月2月3月4月6月7月10月11月12月'),
    ('The release is in sep.', '将于9月发布'),
    ('In Jan, the release is ready.', '一月，版本已就绪。'),
    ('Jan.5 and 3 Mar', '1月5日和3月3日'),
    ('Jan, Feb, Mar', '1月、2月、3月'),
    ('Notes\nJan / Mar\nEnd of notes', '说明\n1月、3月\n说明结束'),
    ('Jan developed an AI model.', '扬开发了一个AI模型。'),
    ('Jan releases an AI model.', '扬发布了一个AI模型。'),
    ('Mar the surface.', '损伤表面。'),
    ('A 7B preference model and 350M parameters.', '7B偏好模型和350M参数。'),
    ('A 7B model.', '一个70亿参数模型。'),
    ('13 million lines and 2 thousand teams.', '1300万行和2千支团队。'),
    ('0.864, 129', '0.864，129'),
    ('0.23, 0.13', '0.23，0.13'),
    ('Dimensions 1,2,3.', '维度1、2、3。'),
    ('Values 12,3456 and 1,234,56.', '数值12、3456和1、234、56。'),
    ('Version v1.2.3 and v2.10.', '版本v1.2.3及v2.10。'),
    ('1,234,567 users and 1,234.50 credits.', '1234567名用户和1234.50额度。'),
    ('Measured at -5 C and −3.2 units.', '在-5 C和-3.2单位测量。'),
    ('Measured at +5 C.', '测得5 C。'),
    ('Range 1-3; range 5 - 9; range 10–12.', '范围1至3；范围5至9；范围10到12。'),
    ('Model GPT-5; version v1.2-3.', 'GPT-5模型；v1.2-3版本。'),
    ('From -5 to -3.', '从-5到-3。'),
    ('A value of .5.', '值为0.5。'),
    ('A value of .5.', '值为.5。'),
    ('See https://example.org/7B?q=-5 for 2 models.', '参见 https://example.org/7B?q=-5，涵盖2个模型。'),
    ('My first AI model costs $1.', '我的第一个AI模型价格为$1。'),
    ('May', '五月'),
    ('March forward with AI tools.', '借助AI工具向前进。'),
    ("In May's opinion, AI can help.", '在梅看来，AI能提供帮助。'),
    ('Dr. June leads research.', '琼博士领导研究。'),
    ('Dr. Aug. said hello.', '奥格博士打了招呼。'),
    ('Version v3.0.5 may improve results.', '版本v3.0.5可能改善结果。'),
])
def test_equivalent_numeric_forms(source, candidate):
    assert number_counts(source, candidate) == number_counts(candidate, source)


@pytest.mark.parametrize('source,candidate', [
    ('Aug and Sep releases', '8月和10月发布'),
    ('Aug. 5, 2026', '2026年8月6日'),
    ('Jan.5 and 3 Mar', '1月6日和3月3日'),
    ('Jan, Feb, Mar', '1月、2月、4月'),
    ('Jan developed an AI model.', '扬在一月开发了一个AI模型。'),
    ('Mar the surface.', '三月损伤表面。'),
    ('Jan. Feb. Mar.', '1月2月'),
    ('A 7B model.', '一个7M模型。'),
    ('A 7B model.', '一个7偏好模型。'),
    ('A 7B model.', '一个7B2模型。'),
    ('13 million lines.', '130万行。'),
    ('0.864, 129', '0.864，128'),
    ('0.23, 0.13', '0.23，0.14'),
    ('Dimensions 1,2,3.', '维度1、2。'),
    ('Values 12,3456.', '数值12345、6。'),
    ('Values 1,234,56.', '数值1234、56。'),
    ('Version v1.2.3.', '版本v1.2.4。'),
    ('Version v1.10.', '版本v1.1。'),
    ('1,234,567 users.', '123456名用户。'),
    ('At -5 C.', '在5 C。'),
    ('Change -3.2 points.', '变化+3.2分。'),
    ('Change −3.2 points.', '变化3.2分。'),
    ('At 5 C.', '在-5 C。'),
    ('From -5 to -3.', '从-5到3。'),
    ('Range 1-3.', '范围1到4。'),
    ('A value of .5.', '值为5。'),
    ('My first AI model costs $1.', '我的第一个AI模型价格为$。'),
    ('My first AI model.', '我的第二个AI模型。'),
    ('My first AI model.', '我的第一个AI模型及第一个平台。'),
    ('The AI release is in May.', 'AI将在六月发布。'),
    ('The AI model may improve.', 'AI模型在五月可能改进。'),
])
def test_numeric_differences_remain_visible(source, candidate):
    assert number_counts(source, candidate) != number_counts(candidate, source)


def test_comma_grammar_preserves_decimal_version_and_thousands_boundaries():
    assert number_counts('0.864,129; 0.23，0.13; 1,2,3; v1.2.3; 1,234.50', '') == Counter({
        '0.864': 1, '129': 1, '0.23': 1, '0.13': 1, '1': 1, '2': 1, '3': 1,
        '1.2.3': 1, '1234.50': 1,
    })


def test_invalid_grouping_is_not_partially_consumed_as_thousands():
    assert number_counts('12,3456; 1,234,56; 1,234,567; 1,234.50; 1,234.50,129; 1,234, 56', '') == Counter({
        '12': 1, '3456': 1, '1': 1, '234': 1, '56': 2, '1234567': 1,
        '1234.50': 2, '129': 1, '1234': 1,
    })


def test_leading_decimal_uses_ascii_boundary_without_reinterpreting_product_versions():
    assert number_counts('value .5; 值为.5; version v.5', '') == Counter({'0.5': 2, '5': 1})


@pytest.mark.parametrize('opening,closing', [('(', ')'), ('[', ']'), ('{', '}')])
def test_sentence_period_after_closing_delimiter_is_not_a_leading_decimal(opening, closing):
    source = 'A note ' + opening + 'stable' + closing + '.1 item follows; default 0.3.'
    equivalent = '说明（稳定）。后续1项；默认0.3。'
    assert number_counts(source, equivalent) == number_counts(equivalent, source)
    for changed in ['说明（稳定）。后续0.1项；默认0.3。', '说明（稳定）。后续项目；默认0.3。']:
        assert number_counts(source, changed) != number_counts(changed, source)


@pytest.mark.parametrize('value', ['(.5)', '[.5]', '{.5}', '值为.5', '.5'])
def test_leading_decimal_after_opening_delimiter_remains_fractional(value):
    assert number_counts(value, '0.5') == Counter({'0.5': 1})
    assert number_counts(value, '0.5') == number_counts('0.5', value)
    assert number_counts(value, '5') != number_counts('5', value)


def test_scale_requires_ascii_token_end_and_does_not_multiply_versions():
    assert number_counts('7B偏好 350M参数 7Beta 7B2 7B_model v1.2.3M', '') == Counter({
        '7000000000': 1, '350000000': 1, '7': 3, '2': 1, '1.2.3': 1,
    })


def test_signs_distinguish_unary_values_from_ranges_and_model_hyphens():
    assert number_counts('-5; +5; −5; (-5); 1-3; 1 -3; GPT-5; 中文-5', '') == Counter({
        '-5': 4, '5': 2, '1': 2, '3': 2,
    })
