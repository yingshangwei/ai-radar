from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from radar.api import create_app
from radar.config import Settings
from radar.models import Article, ArticleAdmission


@pytest.fixture
def client(tmp_path):
    conf = tmp_path/'config.toml'
    conf.write_text('[translation]\nenabled=false\n[reading]\nenabled=false\n')
    app = create_app(Settings(config_path=str(conf), database_url=f'sqlite:///{tmp_path}/db',
                              reader_token='reader', admin_token='admin', scheduler_enabled=False))
    now = datetime.now(UTC)
    with app.state.sessions.begin() as s:
        values = [
            ('1', 'x', 'OpenAI', 'OpenAI', ['模型'], True, False, 0),
            ('2', 'x', 'openai', 'OpenAI updated name', ['技术'], False, True, 1),
            ('3', 'x', 'different', 'OpenAI', ['模型'], True, False, 2),
            ('4', 'facebook', 'OpenAI', 'OpenAI', ['模型'], True, False, 3),
            ('5', 'rss', '', '研究员: 小明', ['学界'], False, False, 4),
            ('6', 'x', 'noise', 'Noise', ['动态'], True, False, 5),
            ('7', 'x', 'old', 'Old researcher', ['技术'], False, True, 240),
        ]
        for uid, platform, handle, name, topics, priority, saved, hours in values:
            s.add(Article(id=uid, external_id=uid, source_id='fixture', platform=platform,
                author=name, handle=handle, title='Synthetic AI evidence', text='A research result mentions OpenAI.',
                url=f'https://example.org/{uid}', canonical_url=f'https://example.org/{uid}',
                published_at=(now-timedelta(hours=hours)).isoformat(), topics=topics,
                priority=priority, saved=saved, metrics={}, score=10))
        s.flush()
        s.add(ArticleAdmission(article_id='6', policy='test', fingerprint='test', visible=False,
                               reason='reaction_only'))
    c = TestClient(app)
    c.headers['Authorization'] = 'Bearer reader'
    yield c
    c.close()


def test_exact_account_does_not_match_same_name_mention_or_other_platform(client):
    r = client.get('/v1/articles', params={'author': 'x:handle:OPENAI'}).json()
    assert r['total'] == 2
    assert [a['id'] for a in r['items']] == ['1', '2']
    assert client.get('/v1/articles', params={'author': 'facebook:handle:openai'}).json()['total'] == 1
    r = client.get('/v1/articles', params={'author': 'rss:name:研究员: 小明'}).json()
    assert r['total'] == 1 and r['items'][0]['id'] == '5'


def test_facets_cover_entire_scope_and_preserve_admission_and_recent_window(client):
    r = client.get('/v1/article-authors').json()
    assert r['total'] == 5
    authors = {a['key']: a for a in r['items']}
    assert authors['x:handle:openai']['count'] == 2
    assert authors['x:handle:openai']['name'] == 'OpenAI'
    assert 'x:handle:noise' not in authors and 'x:handle:old' not in authors
    assert len(authors) == 4
    # Every advertised author/count corresponds to exactly that API result.
    for key, option in authors.items():
        result = client.get('/v1/articles', params={'author': key}).json()
        assert result['total'] == option['count']


def test_filters_intersect_and_pagination_count_matches(client):
    params = {'author': 'x:handle:openai', 'limit': 1}
    first = client.get('/v1/articles', params=params).json()
    second = client.get('/v1/articles', params={**params, 'offset': 1}).json()
    assert first['total'] == second['total'] == 2
    assert [first['items'][0]['id'], second['items'][0]['id']] == ['1', '2']
    assert client.get('/v1/articles', params={**params, 'topic': '模型', 'priority': True}).json()['total'] == 1
    assert client.get('/v1/articles', params={**params, 'platform': 'facebook'}).json()['total'] == 0
    assert client.get('/v1/articles', params={**params, 'q': 'not-present'}).json()['total'] == 0
    for scope in ({'topic': '模型'}, {'saved': True}, {'priority': True, 'platform': 'x'}, {'q': 'research'}):
        authors = client.get('/v1/article-authors', params=scope).json()
        assert authors['total'] == client.get('/v1/articles', params=scope).json()['total']
        for option in authors['items']:
            assert client.get('/v1/articles', params={**scope, 'author': option['key']}).json()['total'] == option['count']


def test_saved_author_can_find_old_saved_posts_and_unknown_author_never_falls_back(client):
    assert client.get('/v1/articles', params={'author': 'x:handle:old'}).json()['total'] == 0
    assert client.get('/v1/articles', params={'author': 'x:handle:old', 'saved': True}).json()['total'] == 1
    assert client.get('/v1/articles', params={'author': 'x:handle:unknown'}).json()['total'] == 0
    assert client.get('/v1/articles', params={'author': 'rss:name:% OR 1=1'}).json()['total'] == 0


@pytest.mark.parametrize('key', ['OpenAI', 'x:oops:openai', 'other:handle:openai', 'x:handle:', 'x:handle:  '])
def test_malformed_identity_rejected(client, key):
    assert client.get('/v1/articles', params={'author': key}).status_code == 422


def test_author_facet_requires_authentication(client):
    client.headers.pop('Authorization')
    assert client.get('/v1/article-authors').status_code == 401
