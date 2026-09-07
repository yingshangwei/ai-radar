"""Conservative numeric correspondence for translation, independent of model calls."""

import re
import unicodedata
from collections import Counter
from decimal import Decimal

MONTHS = list(zip(
    "January February March April May June July August September October November December".split(),
    "一月 二月 三月 四月 五月 六月 七月 八月 九月 十月 十一月 十二月".split(),
    strict=True,
))
_MONTH_NUMBERS = {english.casefold(): index for index, (english, _) in enumerate(MONTHS, 1)}
_ABBREVIATIONS = dict(zip(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13), strict=True,
))
_ABBREVIATIONS["sept"] = 9
_MONTH_NUMBERS.update(_ABBREVIATIONS)
_MONTH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) +
    r")(?![A-Za-z0-9_])(?:\.(?![A-Za-z_]))?", re.I,
)
_URL = re.compile(r"https?://[^\s\u3400-\u9fff<>\[\]\"'`，。！？；：、（）“”‘’《》【】]+")
# Extracted prose can omit the space in ").1 item"; the period then ends a
# sentence. Opening delimiters still allow real fractions such as "(.5)".
_NUMBER = re.compile(
    r"(?<![\d,])\d{1,3}(?:,\d{3})+(?!\d|,\d)(?:\.\d+)*|"
    r"\d+(?:\.\d+)*|(?<![A-Za-z0-9_.\)\]\}])\.\d+"
)
_SCALES = {
    "billion": 10**9, "million": 10**6, "thousand": 10**3,
    "B": 10**9, "M": 10**6, "K": 10**3, "k": 10**3,
    "十亿": 10**9, "千万": 10**7, "百万": 10**6,
    "亿": 10**8, "万": 10**4, "千": 10**3,
}
_SCALE_PATTERN = re.compile(
    r"\s*(billion(?![A-Za-z0-9_])|million(?![A-Za-z0-9_])|thousand(?![A-Za-z0-9_])|"
    r"[BMKk](?![A-Za-z0-9_])|十亿|千万|百万|亿|万|千)"
)


def _month_list_line(match, value):
    """A standalone list is calendar evidence; a person's surrounding prose is not."""
    start = value.rfind('\n', 0, match.start()) + 1
    end = value.find('\n', match.end())
    line = value[start:end if end >= 0 else len(value)]
    months = list(_MONTH_PATTERN.finditer(line))
    if len(months) < 2:
        return False
    if not re.fullmatch(r"[\s(\[]*", line[:months[0].start()]):
        return False
    if not re.fullmatch(r"[\s)\]]*", line[months[-1].end():]):
        return False
    return all(re.fullmatch(r"(?:\s*[,;/|]\s*|\s+(?:and\s+)?)", line[left.end():right.start()], re.I)
               for left, right in zip(months, months[1:], strict=False))


def _date_month(match, value):
    before, after = value[:match.start()], value[match.end():]
    name = match[0].rstrip('.').casefold()
    if re.match(r"['’]s\b", after, re.I):
        return False
    if (re.search(r"\b(?:Dr|Mr|Mrs|Ms|Professor)\.?\s+$", before, re.I) or
            re.match(r"\s+(?:created|said|says|spoke|speaks)\b", after, re.I)):
        return False
    day = r"(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
    separator = r"\s*" if match[0].endswith('.') else r"\s+"
    following = re.match(separator + r"(?:[12]\d{3}|" + day + r")(?!\w)", after, re.I)
    if following and not re.match(
        r"\s+(?:miles?|steps?|kilomet(?:er|re)s?|met(?:er|re)s?|million|billion)\b",
        after[following.end():], re.I,
    ):
        return True
    if re.search(r"(?<![\w.,])" + day + r"\s+$", before, re.I):
        return True
    if name in {'jan', 'mar'} and _month_list_line(match, value):
        return True
    if match[0][0].isupper() and name not in {"may", "march", "jan", "mar"}:
        return True
    event = re.match(r"\s+(meetings?|releases?|updates?)\b", after, re.I)
    if event and (
        name not in {"may", "march", "jan", "mar"} or event[1].casefold().startswith("meeting") or
        re.search(r"\b(?:the|a|an|our|their|its|this|that|next|last)\s+$", before, re.I)
    ):
        return True
    return bool(re.search(
        r"\b(?:in|during|since|until|through|throughout|from|by|before|after|this|last|next)\s+$",
        before, re.I,
    ))


def _normal_text(value):
    # Chinese punctuation separates numbers. NFKC must not turn it into an
    # ASCII thousands separator (e.g. 0.864，129 or 0.23，0.13).
    value = _URL.sub('', value).replace('，', ' ').replace('、', ' ')
    return unicodedata.normalize('NFKC', value)


def _negative(value, start):
    if not start or value[start - 1] not in '-−':
        return False
    before = value[:start - 1]
    if before and re.search(r"[A-Za-z0-9_+\-−]$", before):
        return False  # Product/version identifiers and tightly written ranges.
    if re.search(r"(?:\d|[)\]])\s*$", before):
        return False  # 1 -3 and 1 - 3 are ranges/operators, not a unary sign.
    return True


def number_counts(value: str, counterpart: str) -> Counter[str]:
    """Extract numeric values, preserving decimal/version spelling and signs.

    The counterpart only bounds the existing isolated-month and spelled-ordinal
    allowances. It cannot grant arbitrary missing-number exemptions.
    """
    value, counterpart = _normal_text(value), _normal_text(counterpart)
    isolated = _MONTH_PATTERN.fullmatch(counterpart.strip())
    ambiguous = Counter(
        [_MONTH_NUMBERS[isolated[0].rstrip('.').casefold()]]
        if isolated and not _date_month(isolated, counterpart.strip()) else [],
    )
    chinese_months = {name: index for index, (_, name) in enumerate(MONTHS, 1)}

    def month_value(match):
        token = match[0]
        index = chinese_months[token] if token in chinese_months else int(token[:-1])
        if ambiguous[index]:
            ambiguous[index] -= 1
            return '月份'
        return str(index) + '月'

    value = re.sub(
        r"(?<![零一二三四五六七八九十\d])(?:" +
        '|'.join(reversed(list(chinese_months))) + r"|(?:0?[1-9]|1[0-2])月)", month_value, value,
    )
    dated_value = value
    value = _MONTH_PATTERN.sub(
        lambda match: str(_MONTH_NUMBERS[match[0].rstrip('.').casefold()]) + '月'
        if _date_month(match, dated_value) else match[0], value,
    )
    digits = '零一二三四五六七八九十'
    ordinal_words = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth'.split()
    spelled_ordinals = Counter(re.findall(
        r"\b(?:" + '|'.join(ordinal_words) + r")\b", counterpart.lower(),
    ))

    def ordinal(match):
        number = digits.index(match[1])
        word = ordinal_words[number]
        if spelled_ordinals[word]:
            spelled_ordinals[word] -= 1
            return '第' + word
        return '第' + str(number)

    value = re.sub(r"第([一二三四五六七八九十])(?![一二三四五六七八九十百千万])", ordinal, value)
    result = Counter()
    for match in _NUMBER.finditer(value):
        token = match[0].replace(',', '')
        if token.startswith('.'):
            token = '0' + token
        scale = _SCALE_PATTERN.match(value, match.end())
        if scale and token.count('.') <= 1:
            token = format((Decimal(token) * _SCALES[scale[1]]).normalize(), 'f')
        if _negative(value, match.start()):
            token = '-' + token
        result[token] += 1
    return result
