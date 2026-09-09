"""Find explicit TeX spans without interpreting formulas or changing source text."""

import hashlib
import re
from collections import Counter

TOKEN = re.compile('|'.join([
    r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`',
    *[re.escape(left) + r'[\s\S]*?' + re.escape(right)
      for left, right in [(r'\(', r'\)'), (r'\[', r'\]'), ('$$', '$$')]],
    r'(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)',
    re.escape(r'\begin{') + r'(?P<env>equation\*?|align\*?|aligned|gather\*?|multline\*?)\}'
    + r'[\s\S]*?' + re.escape(r'\end{') + r'(?P=env)\}',
]))


def math_spans(text: str):
    for match in TOKEN.finditer(text):
        value = match[0]
        if value.startswith(('`', '~~~')):
            continue
        if value.startswith('$') and not value.startswith('$$'):
            inner = value[1:-1]
            # Prices such as "$5 and $10" are not a delimited formula.
            if inner != inner.strip():
                continue
        yield match.start(), match.end(), value


def without_math(text: str) -> str:
    for start, end, _ in reversed(list(math_spans(text))):
        text = text[:start] + ' ' + text[end:]
    return text


def formula_issues(source: str, candidate: str) -> list[str]:
    original = Counter(value for _, _, value in math_spans(source))
    translated = Counter(value for _, _, value in math_spans(candidate))
    return [] if original == translated else ['数学公式或其定界符与原文不一致']


def technical_document(title: str, text: str) -> bool:
    return text.startswith('arXiv preprint · Author abstract') or bool(list(math_spans(title + '\n' + text)))


def protect_html_math(data: bytes) -> tuple[str, dict[str, str]]:
    """Retain publisher-supplied TeX through HTML/Markdown text extraction.

    Never infer TeX from flattened PDF text or invent missing mathematical structure.
    """
    from lxml import etree, html

    tree = html.fromstring(data, parser=html.HTMLParser(no_network=True))
    original, protected = html.tostring(tree, encoding="unicode"), {}
    for node in tree.xpath('//math | //script[starts-with(@type,"math/tex")]'):
        tex = node.get('alttext', '')
        annotations = node.xpath('.//annotation[@encoding="application/x-tex"]')
        if not tex and annotations:
            tex = ''.join(annotations[0].itertext())
        if node.tag == 'script':
            tex = node.text or ''
        if not tex.strip():
            continue
        display = node.get('display') == 'block' or 'mode=display' in node.get('type', '')
        value = tex.strip()
        if not (list(math_spans(value)) and next(math_spans(value))[2] == value):
            value = (r'\[' if display else r'\(') + value + (r'\]' if display else r'\)')
        marker = 'RADARMATH' + hashlib.sha256(value.encode()).hexdigest()[:20] + 'X' + str(len(protected)) + 'END'
        while marker in original:
            marker += 'X'
        protected[marker] = value
        # KaTeX publishes parallel MathML and visual HTML. Keep one source copy.
        ancestors = node.xpath('ancestor::*[contains(concat(" ",normalize-space(@class)," ")," katex ")]')
        target = ancestors[-1] if ancestors else node
        replacement = etree.Element('span')
        replacement.text, replacement.tail = marker, target.tail
        if target.getparent() is not None:
            target.getparent().replace(target, replacement)
    for node in tree.iter():
        if not isinstance(node.tag, str) or node.tag in {'pre', 'code', 'script', 'style'}:
            continue
        if node.xpath('ancestor::pre|ancestor::code|ancestor::script|ancestor::style'):
            continue
        for attribute in ('text', 'tail'):
            value = getattr(node, attribute) or ''
            for start, end, formula in reversed(list(math_spans(value))):
                marker = 'RADARMATH' + hashlib.sha256(formula.encode()).hexdigest()[:20] + 'X' + str(len(protected)) + 'END'
                while marker in original:
                    marker += 'X'
                protected[marker] = formula
                value = value[:start] + marker + value[end:]
            setattr(node, attribute, value or None)
    return html.tostring(tree, encoding="unicode"), protected


def restore_html_math(text: str, protected: dict[str, str]) -> str:
    for marker, value in protected.items():
        text = text.replace(marker, value)
    return text


def truncate_math(text: str, limit: int) -> str:
    for start, end, _ in math_spans(text):
        if start < limit < end:
            return text[:start]
    return text[:limit]
