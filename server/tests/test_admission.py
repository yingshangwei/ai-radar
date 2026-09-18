"""Information admission must preserve originals, fresh facts and queue accounting."""
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.admission import evaluate, reconcile, status, visible_clause
from radar.api import create_app
from radar.article_presentation import ArticlePresentationService
from radar.config import RadarConfig, Settings
from radar.db import database
from radar.digest_selection import select_digest_articles
from radar.discovery import queue_candidate
from radar.models import Article, ArticleAdmission, ArticleReading, ArticleTranslation, Translation
from radar.pipeline import ingest
from radar.ranking import article_id
from radar.schemas import IncomingArticle
from radar.translation import TranslationService, cache_key, queue_article

REPLY = [{'kind': 'reply', 'url': 'https://x.com/i/status/123'}]


@pytest.mark.parametrize('text', [
    '@OpenAI Hmm', '@someone ❤️', '@someone 👏👏', '@someone Cool', '@someone Agree',
    '@someone Yes', '@someone Extremely cute', '@someone Weird, looking',
    '@someone That’s pretty much exactly how I roll 😂', '@someone Good guide',
    '@someone Coming', '@someone Thanks for having me and nice hoodie Rodrigo 👀',
    '@someone It is one of the critical pieces', '@someone yep, just tell Claude',
    '@someone We are big fans of Steve’s work!', '@someone 同意！', '@someone 哈哈哈',
    'Hmm\n\n[引用帖不可用，未取得原文]', '@someone 🤔 https://t.co/media',
])
def test_low_information_not_rescued_by_parent_url_or_ai_addressee(text):
    assert evaluate('x', text, REPLY)[0] is False


@pytest.mark.parametrize('text,refs', [
    ('@someone Qwen supports 1M context.', REPLY),
    ('@someone Released today.', REPLY),
    ('@someone Fixed in v2.3.', REPLY),
    ('@someone Use --resume.', REPLY),
    ('@someone Set temperature=0.', REPLY),
    ('AI tools can help research.', REPLY),
    ('@someone Claude can run on this device.', REPLY),
    ('@someone AI is the highest ELO competition ever', REPLY),
    ('@someone and mobile. cli soon', REPLY),
    ('https://t.co/resource', []),
    ('@someone Latency fell to 20 ms.', REPLY),
    ('@someone 模型已开源，支持本地推理。', REPLY),
    ('@someone It costs $2 per million tokens.', REPLY),
    ('@someone $O(n)$ instead of $O(n^2)$.', REPLY),
    ('MiniMax Code CLI is open now: https://t.co/abc', []),
    ('Cases and more details: https://t.co/abc', REPLY),
    ('Code: https://github.com/example/project', REPLY),
    ('👍 https://t.co/abc', [{'url': 'https://example.org/paper', 'kind': 'link'}]),
    ('Nice\n\n[引用帖：@OpenAI，2026-09-18]\nWe released a new model with a 1M token context.', []),
    ('@someone We found that this implementation does not behave the way the documentation describes in production.', REPLY),
    ('Now available. Details shortly.', []),
])
def test_useful_short_posts_replies_and_quotes_remain_visible(text, refs):
    assert evaluate('x', text, refs)[0] is True


def test_quote_header_and_missing_quote_are_not_substance():
    assert evaluate('x', 'Hmm\n\n[引用帖：@AIModel，2026-09-18]\n👏')[0] is False
    assert evaluate('web', 'Hmm')[0] is True
    assert evaluate('facebook', '👍')[0] is False


def item(uid, text, **changes):
    return IncomingArticle(**dict(platform='x', source_id='x', external_id=uid,
        url=f'https://x.com/OpenAI/status/{uid}', title=text[:180], text=text,
        author='OpenAI', handle='OpenAI', published_at=datetime.now(UTC),
        metrics={'like_count': 99999}, references=REPLY, **changes))


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f'sqlite:///{tmp_path}/admission.db')
    yield sessions
    engine.dispose()


def config():
    return RadarConfig(publish_priority_raw=True, translation={'enabled': True},
                       reading={'enabled': False}, discovery={'enabled': True})


def test_priority_noise_is_archived_but_never_queued_or_counted_as_new_information(store):
    cfg = config()
    noise = item('1', '@someone Hmm')
    useful = item('2', '@someone Model latency is now 20 ms.')
    with store.begin() as s:
        assert ingest(s, [noise, useful], cfg) == 1
        assert queue_candidate(s, noise, cfg, source_priority=True) is None
    with store() as s:
        assert len(list(s.scalars(select(Article)))) == 2
        assert [a.external_id for a in s.scalars(select(Article).where(visible_clause()))] == ['2']
        assert s.get(Article, article_id(noise)).text == noise.text
        assert s.get(ArticleReading, article_id(noise)).references[0]['kind'] == 'reply'
        assert s.get(ArticleTranslation, article_id(noise)) is None
        assert s.get(ArticleTranslation, article_id(useful)) is not None
        assert status(s)['filtered'] == 1


def test_reconciliation_is_idempotent_and_preserves_content_and_old_reviews(store):
    cfg = config()
    noise = item('1', '@someone Cool')
    with store.begin() as s:
        ingest(s, [noise], cfg)
        # Simulate a historical article translated before information admission.
        s.delete(s.get(ArticleAdmission, article_id(noise)))
        s.flush()
        a = s.get(Article, article_id(noise))
        queue_article(s, a, cfg.translation)
        t = s.get(Translation, cache_key(a.title, a.text, cfg.translation))
        t.status, t.title_zh, t.text_zh = 'ready', '很酷', '很酷'
        before = (a.text, a.title, a.metrics.copy(), t.text_zh, t.status)
    with store.begin() as s:
        assert reconcile(s) == {'filtered': 1}
        stamp = s.get(ArticleAdmission, article_id(noise)).updated_at
        reconcile(s)
        assert s.get(ArticleAdmission, article_id(noise)).updated_at == stamp
        a = s.get(Article, article_id(noise))
        t = s.scalar(select(Translation))
        assert before == (a.text, a.title, a.metrics, t.text_zh, t.status)
    # A real source update gains substance and automatically becomes eligible.
    with store.begin() as s:
        revised = noise.model_copy(update={'text': '@someone Fixed in v2.3.', 'title': 'Fixed in v2.3.'})
        ingest(s, [revised], cfg)
        assert s.get(ArticleAdmission, article_id(noise)).visible


def test_noise_source_revision_invalidates_older_unapplied_discovery(store):
    cfg = config()
    original = item('1', 'We release a new AI benchmark with reproducible evaluation data and clear experimental results.')
    with store.begin() as s:
        candidate = queue_candidate(s, original, cfg, source_priority=True)
        assert candidate is not None
        revised = original.model_copy(update={'text': '@someone Hmm', 'title': '@someone Hmm'})
        ingest(s, [revised], cfg)
        assert candidate.status == 'superseded' and candidate.error_code == 'source_changed'
        assert not s.get(ArticleAdmission, article_id(original)).visible


async def test_filtered_old_records_do_not_use_model_or_digest_capacity(store, monkeypatch):
    cfg = config()
    noise, useful = item('1', '@someone Hmm'), item('2', '@someone Fixed in v2.3.')
    with store.begin() as s:
        ingest(s, [noise, useful], cfg)
        # Keep a historical pending translation, without destroying its receipt.
        a = s.get(Article, article_id(noise))
        gate = s.get(ArticleAdmission, a.id)
        gate.visible = True
        queue_article(s, a, cfg.translation)
        gate.visible = False
    service = TranslationService(store, cfg.translation)
    monkeypatch.setattr('radar.translation.secret', lambda _: 'synthetic-key')
    calls = []
    async def translate(key, *args, **kwargs):
        calls.append(key)
    monkeypatch.setattr(service, 'translate_one', translate)
    await service.pending()
    assert calls == [cache_key(useful.title, useful.text, cfg.translation)]
    cfg.provider.kind = 'command'
    presentation = ArticlePresentationService(store, cfg, lambda _: None)
    monkeypatch.setattr(presentation.reviews, 'can_analyze_documents', lambda *a, **k: True)
    assert [x[0] for x in presentation._pending_items()] == [article_id(useful)]
    with store() as s:
        now = datetime.now(UTC)
        selected = select_digest_articles(s, cfg, date.today(), now-timedelta(days=1), now+timedelta(minutes=1))
        assert [x['id'] for x in selected.articles] == [article_id(useful)]


def test_api_filters_before_count_pagination_search_and_preserves_bookmarks(tmp_path):
    conf = tmp_path/'radar.toml'
    conf.write_text('publish_priority_raw = true\n[translation]\nenabled = false\n[reading]\nenabled = false\n')
    app = create_app(Settings(config_path=str(conf), database_url=f'sqlite:///{tmp_path}/api.db',
                              admin_token='test-admin', reader_token='test-reader', scheduler_enabled=False))
    cfg = app.state.pipeline.config
    with app.state.sessions.begin() as s:
        ingest(s, [item('1', '@someone Hmm'), item('2', '@someone Fixed in v2.3.')], cfg)
        s.get(Article, article_id(item('1', '@someone Hmm'))).saved = True
    with TestClient(app) as c:
        headers = {'Authorization': 'Bearer test-reader'}
        r = c.get('/v1/articles?limit=1', headers=headers).json()
        assert r['total'] == 1 and r['items'][0]['external_id'] == '2'
        assert c.get('/v1/articles?q=Hmm', headers=headers).json()['total'] == 0
        assert c.get('/v1/articles?saved=true', headers=headers).json()['total'] == 1
        uid = article_id(item('1', '@someone Hmm'))
        assert c.get('/v1/articles/'+uid, headers=headers).json()['text'] == '@someone Hmm'
        assert c.get('/v1/status', headers=headers).json()['admission']['filtered'] == 1
