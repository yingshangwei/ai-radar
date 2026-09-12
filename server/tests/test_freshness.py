"""Fresh input remains visible without translation, review or a fast web reader."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.config import ProviderConfig, RadarConfig, ReadingConfig, Settings, TranslationConfig
from radar.db import database
from radar.discovery import DiscoveryService, queue_candidate
from radar.discovery_priority import deadline, ordered
from radar.freshness import freshness_status, translation_updates
from radar.jobs import JobSupervisor
from radar.models import Article, DiscoveryCall, DiscoveryCandidate, Job, Translation, Watch, XCollectionState
from radar.pipeline import ingest
from radar.reading import analysis_key, terminology_ready
from radar.schemas import IncomingArticle
from radar.translation import TranslationService, ensure_translation, quality_issues
from radar.x_collection import XCollector


def incoming(uid='1', **changes):
    values = dict(platform='x', source_id='x', external_id=uid, url=f'https://x.com/OpenAI/status/{uid}',
                  author='OpenAI', handle='OpenAI', published_at=datetime.now(UTC),
                  title='We have news', text='Now available. Details shortly.', metrics={})
    values.update(changes)
    return IncomingArticle(**values)


@pytest.fixture
def db(tmp_path):
    engine, sessions = database(f'sqlite:///{tmp_path}/fresh.db')
    yield sessions
    engine.dispose()


def test_watched_raw_statement_and_author_visible_before_models(db):
    config = RadarConfig(publish_priority_raw=True, translation=TranslationConfig(enabled=True),
                         reading=ReadingConfig(enabled=False))
    with db.begin() as session:
        assert ingest(session, [incoming()], config) == 1
    with db() as session:
        article = session.scalar(select(Article))
        assert article.author == 'OpenAI' and article.text == 'Now available. Details shortly.'
        assert article.topics == ['动态']
        assert session.scalar(select(Translation)).status != 'ready'


def test_translation_archive_only_current_approved_source_and_shared_cache_once(db):
    config = RadarConfig(publish_priority_raw=True, translation=TranslationConfig(enabled=True),
                         reading=ReadingConfig(enabled=False))
    with db.begin() as session:
        ingest(session, [incoming('1'), incoming('2', text='A distinct AI model update.')], config)
        article = session.scalar(select(Article).where(Article.external_id == '1'))
        row = ensure_translation(session, article.title, article.text, config.translation)
        row.status, row.title_zh, row.text_zh = 'ready', '已发布', '现已可用，详情稍后公布。'
    with db() as session:
        assert translation_updates(session, config)['total'] == 1
        assert translation_updates(session, config)['items'][0]['author'] == 'OpenAI'
    with db.begin() as session:
        session.scalar(select(Article).where(Article.external_id == '1')).text = 'Updated source.'
    with db() as session:
        assert translation_updates(session, config)['total'] == 0


def test_technical_summary_key_and_eligibility_independent_of_translation(db):
    from radar.models import WebDocument
    config = RadarConfig(translation=TranslationConfig(enabled=True))
    doc = WebDocument(id='doc', title='AI gradient theorem', text=r'Proof with $n^{-2}$.',
                      url='https://example.org/paper', content_hash='original', final_url='')
    with db.begin() as session:
        row = ensure_translation(session, doc.title, doc.text, config.translation)
        row.status = 'error'
        key = analysis_key(session, doc, config)
        assert terminology_ready(session, doc, config)
        row.status, row.title_zh, row.text_zh = 'ready', '新中文', '稍后完成的翻译'
        assert analysis_key(session, doc, config) == key


def test_minute_head_windows_and_manual_refresh(db):
    from collections import Counter
    now = datetime.now(UTC)
    config = RadarConfig(x_head_refresh_minutes=25)
    with db.begin() as session:
        session.add(XCollectionState(id='watch:openai', data={'head_end': (now-timedelta(minutes=26)).isoformat()}))
    assert XCollector(db, config, clock=lambda: now).choose_priority(['watch:openai'], Counter()) == ('watch:openai', True)
    with db.begin() as session:
        session.get(XCollectionState, 'watch:openai').data = {'head_end': now.isoformat()}
    assert XCollector(db, config, clock=lambda: now).choose_priority(['watch:openai'], Counter()) is None
    assert XCollector(db, config, clock=lambda: now, force_fresh=True).choose_priority(['watch:openai'], Counter()) == ('watch:openai', True)


def test_status_reports_stale_accounts_instead_of_claiming_success(db):
    with db.begin() as session:
        for watch in session.scalars(select(Watch)):
            watch.enabled = watch.handle == 'OpenAI'
        session.add(XCollectionState(id='watch:openai', data={'head_end': (datetime.now(UTC)-timedelta(hours=3)).isoformat()}))
    with db() as session:
        status = freshness_status(session, RadarConfig(collect_minutes=30))
        assert status['overdue_accounts'] == 1 and status['oldest_window_minutes'] >= 180


def discovery_config(**changes):
    config = RadarConfig(provider=ProviderConfig(kind='command'))
    config.discovery = config.discovery.model_copy(update={
        'enabled': True, 'freshness_enabled': True, 'max_calls_per_day': 200,
        'max_candidates_per_day': 200, **changes})
    return config


def candidate(session, config, uid='1', **changes):
    item = incoming(uid, text='We release a new AI research benchmark with reproducible data and evaluation results.', **changes)
    return queue_candidate(session, item, config, source_priority=item.handle=='OpenAI')


def test_priority_prefers_fresh_important_evidence_and_deadline_does_not_roll_over(db):
    config = discovery_config()
    with db.begin() as session:
        a = candidate(session, config, '1', published_at=datetime.now(UTC)-timedelta(hours=8), handle='other')
        b = candidate(session, config, '2')
        assert ordered([a,b], config)[0].id == b.id
        a.created_at = (datetime.now(UTC)-timedelta(hours=2)).isoformat()
        assert deadline(a, config) < datetime.now(UTC)
    svc = DiscoveryService(db, config, lambda s,r: None)
    assert svc.has_pending()
    svc.recover()
    with db() as session:
        assert session.get(DiscoveryCandidate, a.id).status == 'expired'
        assert session.get(DiscoveryCandidate, b.id).status == 'pending'


def test_freshness_budget_shares_capacity_and_protects_unknown_calls(db, monkeypatch):
    config = discovery_config(max_calls_per_day=3)
    with db.begin() as session:
        a = candidate(session, config)
        b = candidate(session, config, '2')
        session.add(DiscoveryCall(candidate_id=a.id, fingerprint=a.fingerprint, owner='test', provider={}, status='completed'))
        session.add(DiscoveryCall(candidate_id=a.id, fingerprint=a.fingerprint, owner='test', provider={}, status='completed'))
    svc = DiscoveryService(db, config, lambda s,r: None)
    monkeypatch.setattr(svc, 'token_budget', lambda: {'available':True,'tokens':0,'unknown_calls':0})
    with db() as session:
        assert svc._eligible(session, session.get(DiscoveryCandidate,b.id), datetime.now(UTC).isoformat())
    with db.begin() as session:
        session.add(DiscoveryCall(candidate_id=b.id, fingerprint=b.fingerprint, owner='test', provider={}, status='unknown'))
    with db() as session:
        assert not svc._eligible(session,session.get(DiscoveryCandidate,b.id),datetime.now(UTC).isoformat())


def test_capacity_replaces_only_uncalled_lower_priority_candidate(db):
    config=discovery_config(max_pending=1)
    with db.begin() as session:
        a=candidate(session,config,'1',handle='other',published_at=datetime.now(UTC)-timedelta(hours=12))
        b=candidate(session,config,'2')
        assert b is not None and a.status=='not_selected' and a.error_code=='priority_replaced'
        assert session.get(DiscoveryCandidate,a.id) is not None


def test_factual_mode_preserves_numeric_and_formula_checks():
    service=TranslationService(None,TranslationConfig(factual_review_only=True,individual_parts=True))
    assert list(service.stage_batches([{'id':'a'},{'id':'b'}]))==[[{'id':'a'}],[{'id':'b'}]]
    assert quality_issues('Uses 15% and $n^{-2}$.', '使用 50% 和 $n^{-3}$。')
    # The relaxed policy changes model guidance only, never turns old denials into approvals.
    assert '否定' in service.review_standard() and '风格偏好不构成拒绝' in service.review_standard()


def test_reader_can_refresh_only_collection_and_duplicate_requests_coalesce(tmp_path,monkeypatch):
    conf=tmp_path/'config.toml'
    conf.write_text('[provider]\nkind="extractive"\n[reading]\nenabled=false\n')
    app=create_app(Settings(database_url=f'sqlite:///{tmp_path}/api.db',config_path=str(conf),reader_token='reader',admin_token='admin'))
    calls=[]
    async def collect(*,fresh=False):
        calls.append(fresh)
        return 0
    monkeypatch.setattr(app.state.pipeline,'collect',collect)
    with TestClient(app) as client:
        assert client.post('/v1/refresh').status_code==401
        headers={'Authorization':'Bearer reader'}
        one=client.post('/v1/refresh',headers=headers,json={'kind':'translate','force':True})
        two=client.post('/v1/refresh',headers=headers)
        assert one.status_code==two.status_code==200
        assert one.json()['job_id']==two.json()['job_id'] and two.json()['coalesced']
        assert client.post('/v1/admin/jobs',headers=headers,json={'kind':'translate'}).status_code==403
        assert client.get('/v1/translation-updates',headers=headers).status_code==200
    with app.state.sessions() as session:
        jobs=list(session.scalars(select(Job)))
        assert len(jobs)==1 and jobs[0].kind=='collect' and jobs[0].force


@pytest.mark.asyncio
async def test_slow_web_read_does_not_block_card_summary_or_collection(db):
    entered=asyncio.Event()
    release=asyncio.Event()
    done=[]
    async def run(**kw):
        if kw['kind']=='read':
            entered.set()
            await release.wait()
        done.append(kw['kind'])
    pipeline=SimpleNamespace(sessions=db,config=RadarConfig(),run=run,has_pending=lambda k:False)
    supervisor=JobSupervisor(pipeline,automatic=False,poll_seconds=.01)
    await supervisor.start()
    try:
        supervisor.submit('read')
        await asyncio.wait_for(entered.wait(),1)
        supervisor.submit('present')
        supervisor.submit('collect')
        async with asyncio.timeout(1):
            while not {'present','collect'}.issubset(done):
                await asyncio.sleep(.01)
        assert 'read' not in done
    finally:
        release.set()
        await supervisor.stop()


def test_deadline_uses_local_midnight_instead_of_letting_yesterday_roll_over():
    from radar.discovery_priority import day_start
    config = discovery_config(max_wait_minutes=60)
    config.timezone = 'Asia/Shanghai'
    now = datetime(2026, 9, 12, 15, 45, tzinfo=UTC)
    row = SimpleNamespace(created_at=now.isoformat(), payload={'published_at': now.isoformat()})
    assert day_start(config, now) == datetime(2026, 9, 11, 16, tzinfo=UTC)
    assert deadline(row, config) == datetime(2026, 9, 12, 16, tzinfo=UTC)


@pytest.mark.parametrize('tokens,unknown,allowed', [(999,0,True),(1000,0,False),(0,1,False)])
def test_fresh_candidate_token_admission_accounts_for_unknown_usage(db, monkeypatch, tokens, unknown, allowed):
    config = discovery_config(max_tokens_per_day=1000)
    with db.begin() as session:
        row = candidate(session, config)
    svc = DiscoveryService(db, config, lambda s,r: None)
    monkeypatch.setattr(svc, 'token_budget', lambda: {'available':True,'tokens':tokens,'unknown_calls':unknown})
    with db() as session:
        assert svc._eligible(session, session.get(DiscoveryCandidate,row.id), datetime.now(UTC).isoformat()) is allowed


def test_rolling_hour_limit_resets_without_rolling_candidate_into_next_day(db, monkeypatch):
    config = discovery_config(max_calls_per_hour=1)
    with db.begin() as session:
        first = candidate(session, config)
        second = candidate(session, config, '2')
        session.add(DiscoveryCall(candidate_id=first.id,fingerprint=first.fingerprint,owner='test',provider={},status='completed'))
    svc = DiscoveryService(db, config, lambda s,r: None)
    monkeypatch.setattr(svc, 'token_budget', lambda: {'available':False,'tokens':0,'unknown_calls':0})
    with db() as session:
        assert not svc._eligible(session,session.get(DiscoveryCandidate,second.id),datetime.now(UTC).isoformat())
    with db.begin() as session:
        session.scalar(select(DiscoveryCall)).created_at = (datetime.now(UTC)-timedelta(minutes=61)).isoformat()
    with db() as session:
        assert svc._eligible(session,session.get(DiscoveryCandidate,second.id),datetime.now(UTC).isoformat())


def test_usage_budget_reads_only_target_feature_today_and_never_creates_missing_ledger(tmp_path, monkeypatch):
    from radar.usage import UsageStore, feature_usage
    path = tmp_path/'usage.db'
    monkeypatch.setenv('RADAR_USAGE_DATABASE_PATH',str(path))
    assert not feature_usage('discovery_foresight','2026-09-12')['available']
    assert not path.exists()
    ledger = UsageStore(path)
    with ledger.connect() as sql:
        for uid, feature, started, inp, out in [
            ('a','discovery_foresight','2026-09-12T01:00:00Z',100,20),
            ('b','discovery_foresight','2026-09-12T02:00:00Z',None,None),
            ('c','translation','2026-09-12T02:00:00Z',1000,1000),
            ('d','discovery_foresight','2026-09-11T02:00:00Z',1000,1000)]:
            sql.execute('INSERT INTO usage_calls(id,feature,started_at,input_tokens,output_tokens,provider,model,model_basis,stage,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (uid,feature,started,inp,out,'test','test','reported','generation','completed'))
    assert feature_usage('discovery_foresight','2026-09-12') == {'available':True,'tokens':120,'unknown_calls':1}
