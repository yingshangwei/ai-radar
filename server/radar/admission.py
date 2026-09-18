"""Cheap, source-only social information gate, independent of factual review.

Keep originals and review receipts. A watched author or high engagement cannot
turn an acknowledgement into a useful standalone item. This deliberately does
not reject a post just for being short, a reply, or unpopular.
"""
import hashlib
import json
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

from sqlalchemy import select

from .models import Article, ArticleAdmission, ArticleReading, now_iso

POLICY = "social-information-v1"
URL = re.compile(r"https?://[^\s<>]+", re.I)
MENTION = re.compile(r"(?<!\w)@[A-Za-z0-9_]+")
QUOTE = re.compile(r"\n*\[引用帖：[^\]\n]+\]\s*\n")
UNAVAILABLE = re.compile(r"\[引用帖不可用，未取得原文\]")
# Whole reactions only; never search for these words inside technical statements.
REACTION = re.compile(
    r"(?:(?:very|so|really|extremely|absolutely|totally|fully|strongly|quite|pretty|super)\s+)*"
    r"(?:h+m+|huh|wow|oh|ooh|aha|lol|lmao|haha(?:ha)*|heh|yes|yep|yeah|yup|no|nope|nah|"
    r"ok(?:ay)?|sure|indeed|exactly|correct|true|false|right|agree(?:d)?|same|this|"
    r"nice|cool|great|awesome|amazing|incredible|interesting|cute|beautiful|based|"
    r"congrats|congratulations|thanks|thank you|well said|well done|good point|good guide|"
    r"good stuff|good job|love (?:it|this|that)|let s go|you re welcome|i agree|so true|"
    r"嗯+|哦+|哈哈+|是的|对的|确实|同意|赞同|没错|谢谢|感谢|赞|厉害|恭喜|不错|好的|好看|牛逼|牛)"
    r"(?:\s+(?:indeed|exactly|absolutely|thanks|thank you|lol|haha))?", re.I,
)
# In a short reply, these carry an actual claim/action/technical operand. No
# keyword from the addressee, fabricated title, timestamp or parent URL counts.
CONCRETE = re.compile(
    r"\b(?:releas(?:e[sd]?|ing)|launch(?:ed|es|ing)?|ship(?:s|ped|ping)?|"
    r"support(?:s|ed|ing)?|available|open.?sourc(?:e[sd]?|ing)|"
    r"fix(?:ed|es|ing)?|remov(?:e[sd]?|ing)|reduc(?:e[sd]?|ing)|increas(?:e[sd]?|ing)|"
    r"costs?|latency|tokens?|weights?|parameters?|context|quantiz\w*|"
    r"benchmark\w*|dataset\w*|outage|regression|deprecated|vulnerab\w*|"
    r"fails?|crash(?:es)?|training|inference|memory|gradient\w*|"
    r"(?:price|pricing)|license|licensed)\b|"
    r"发布|上线|开源|支持|修复|延迟|上下文|权重|参数|成本|价格|显存|量化|推理|训练|漏洞|停机|回滚|基准|数据集|许可证|"
    r"(?:--[a-z][a-z-]+)|(?:\$\$?.+?\$)|(?:\b[a-z_][\w.]*\s*[=<>]\s*\S+)|"
    r"\b\d+(?:\.\d+)?\s*(?:ms|gb|tb|mb|tokens|percent|hours|days|bps)\b", re.I,
)
SUBJECT = re.compile(r"\b(?:ai|agi|gpt[\w.-]*|claude|codex|qwen[\w.-]*|deepseek|gemini|"
                     r"kimi|minimax|glm[\w.-]*|model|agent|api|gpu)\b|模型|智能体|人工智能", re.I)


def clean(text):
    return MENTION.sub("", URL.sub("", UNAVAILABLE.sub("", unicodedata.normalize("NFKC", text))))


def words(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold(), re.UNICODE))


def substantive(text):
    normalized = words(clean(text))
    return bool(normalized and not REACTION.fullmatch(normalized))


def direct_link(ref):
    if ref.get("kind", "link") != "link":
        return False
    p = urlsplit(ref.get("url", ""))
    # Parent/quote/media URLs are context pointers, not article evidence.
    host = (p.hostname or "").lower()
    return bool(host and host not in {"t.co", "x.com", "twitter.com", "pic.x.com", "pic.twitter.com"}
                and not host.endswith(".twimg.com") and p.path not in {"", "/"})


def evaluate(platform, text, references=()):
    """Return (visible, reason); uncertain substantial content remains timely."""
    if platform not in {"x", "facebook"}:
        return True, "non_social"
    parts = QUOTE.split(text)
    own = clean(parts[0]).strip()
    normalized = words(own)
    if any(substantive(part) for part in parts[1:]):
        return True, "quoted_information"
    if any(direct_link(ref) for ref in references) or any(
            direct_link({"url": url}) for url in URL.findall(text)):
        return True, "direct_resource"
    if URL.search(text) and not MENTION.sub("", URL.sub("", text)).strip():
        # A link-only announcement needs the existing web extraction lane. An
        # unresolved short URL is not proof of noise; no extra source API call.
        return True, "unresolved_resource"
    if not normalized:
        return False, "reaction_only"
    if REACTION.fullmatch(normalized):
        return False, "acknowledgement_only"
    is_reply = any(ref.get("kind") == "reply" for ref in references) or bool(
        re.match(r"\s*@[A-Za-z0-9_]+\s", text))
    # Compact replies often assume an unavailable parent. Keep concrete details
    # even when only a few words; do not use author prestige or likes as evidence.
    short = len(normalized.split()) <= 12 and len(normalized) <= 100
    if is_reply and short and not CONCRETE.search(own):
        subject = SUBJECT.search(own)
        if subject and re.search(r"\b(?:can|cannot|will|won t|should|must|enables?)\b|能够|可以|将会|必须|不能",
                                 own[subject.end():], re.I):
            return True, "explicit_claim"
        if subject and len(normalized.split()) >= 6 and re.search(
                r"\b(?:is|are|isn t|aren t)\b", own[subject.end():], re.I):
            return True, "explicit_claim"
        if re.search(r"\b(?:cli|api|sdk|ios|android|linux|windows|macos)\b", own, re.I) and re.search(
                r"\b(?:soon|tomorrow|today|coming|next week)\b", own, re.I):
            return True, "product_timing"
        # A product link announcement may only have an unresolved t.co URL.
        if URL.search(text) and (SUBJECT.search(own) or re.search(
                r"\b(?:details|results|code|paper|report|docs|guide|demo|cases)\b|详情|代码|报告|论文|文档", own, re.I)):
            return True, "linked_information"
        return False, "insufficient_reply_context"
    return True, "substantive_text"


def visible_clause():
    return ~select(ArticleAdmission.article_id).where(
        ArticleAdmission.article_id == Article.id, ArticleAdmission.visible.is_(False),
    ).exists()


def record(session, article, references):
    fingerprint = hashlib.sha256(json.dumps(
        [article.platform, article.text, references], ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    row = session.get(ArticleAdmission, article.id)
    if row and row.policy == POLICY and row.fingerprint == fingerprint:
        return row.visible
    visible, reason = evaluate(article.platform, article.text, references)
    if row is None:
        row = ArticleAdmission(article_id=article.id)
        session.add(row)
    row.policy, row.fingerprint, row.visible, row.reason = POLICY, fingerprint, visible, reason
    row.updated_at = now_iso()
    return visible


def reconcile(session):
    """Normal startup migration: re-evaluate originals, never edit their content."""
    references = {r.article_id: r.references for r in session.scalars(select(ArticleReading))}
    counts = Counter()
    for article in session.scalars(select(Article).where(Article.platform.in_(("x", "facebook")))):
        counts["visible" if record(session, article, references.get(article.id, [])) else "filtered"] += 1
    session.flush()
    return dict(counts)


def status(session):
    from sqlalchemy import func
    reasons = dict(session.execute(select(ArticleAdmission.reason, func.count()).where(
        ArticleAdmission.visible.is_(False)).group_by(ArticleAdmission.reason)).all())
    return {"policy": POLICY, "filtered": sum(reasons.values()), "reasons": reasons}
