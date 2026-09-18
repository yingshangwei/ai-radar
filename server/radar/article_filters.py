"""One article scope for feed pagination and its author facet."""
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_, select

from .admission import visible_clause
from .models import Article, ArticleTranslation, Translation

ArticleTopic = Literal['模型', '产品', '技术', '开源', '观点', '产业', '学界', '前瞻', '动态']


def author_identity(platform, handle, name):
    handle = (handle or '').strip().lower()
    return f'{platform}:handle:{handle}' if handle else f'{platform}:name:{name}'


def article_query(*, q='', platform=None, topic=None, saved=False, priority=False, author=''):
    query = select(Article)
    if not saved:
        query = query.where(visible_clause())
    if not saved and not q:
        query = query.where(Article.published_at >= (datetime.now(UTC) - timedelta(days=7)).isoformat())
    if q:
        chinese_matches = select(ArticleTranslation.article_id).join(
            Translation, ArticleTranslation.translation_id == Translation.id
        ).where(Translation.status == 'ready', or_(
            Translation.title_zh.contains(q, autoescape=True), Translation.text_zh.contains(q, autoescape=True),
        ))
        query = query.where(or_(
            Article.title.contains(q, autoescape=True), Article.text.contains(q, autoescape=True),
            Article.author.contains(q, autoescape=True), Article.id.in_(chinese_matches),
        ))
    if platform:
        query = query.where(Article.platform == platform)
    if topic:
        query = query.where(Article.topics.contains(topic))
    if saved:
        query = query.where(Article.saved.is_(True))
    if priority:
        query = query.where(Article.priority.is_(True))
    if author:
        parts = author.split(':', 2)
        if len(parts) != 3 or parts[0] not in {'x', 'facebook', 'rss', 'web'} or not parts[2].strip() \
                or parts[1] not in {'handle', 'name'}:
            raise ValueError('发言人筛选无效，请重新选择。')
        source, kind, value = parts
        query = query.where(Article.platform == source)
        handle = func.lower(func.trim(Article.handle))
        query = query.where(handle == value.strip().lower()) if kind == 'handle' else query.where(
            handle == '', Article.author == value,
        )
    return query


def author_options(session, query):
    # Latest saved display name wins; account identity includes platform. Read
    # the whole matching scope, not only authors on the first mobile page.
    rows = session.execute(query.with_only_columns(
        Article.platform, Article.handle, Article.author,
    ).order_by(Article.published_at.desc(), Article.id))
    grouped = {}
    for platform, handle, name in rows:
        key = author_identity(platform, handle, name)
        if key not in grouped:
            grouped[key] = dict(key=key, platform=platform, handle=handle.strip(), name=name, count=0)
        grouped[key]['count'] += 1
    return sorted(grouped.values(), key=lambda a: (-a['count'], a['name'].casefold(), a['key']))
