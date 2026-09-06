import asyncio
import io
import json
import socket
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import select

from radar.api import create_app
from radar.config import RadarConfig, ReadingConfig, Settings, TranslationConfig
from radar.db import database
from radar.links import html_references, mentioned_references, normalize_link, text_references
from radar.models import Article, Digest, DocumentAnalysis, Translation, WebDocument
from radar.page_parser import extract_page, parse_page
from radar.pipeline import as_dict, ingest
from radar.providers import StructuredProvider
from radar.reading import ReadingService, fingerprint, resource_views
from radar.schemas import IncomingArticle, ReadingOutput
from radar.sources import x_references
from radar.translation import TranslationService, ensure_translation
from radar.web_reader import PageFetcher, PageUnavailable, public_addresses, public_ip


def test_explicit_references_and_normalization():
    assert normalize_link('../paper?utm_source=x&v=2#results', 'https://EXAMPLE.org/news/a') == 'https://example.org/paper?v=2'
    for bad in ['javascript:alert(1)', 'https://u:p@example.org/', 'https://example.org:8080', '#footnote']:
        assert normalize_link(bad) is None
    assert text_references('See https://example.org/paper_(AI). 和 https://example.org/app。') == [
        {'url': 'https://example.org/paper_(AI)', 'label': ''}, {'url': 'https://example.org/app', 'label': ''}]
    assert html_references('<nav><a href="/noise">Noise</a></nav><p><a href="/paper">论文</a></p>',
                           'https://example.org/') == [{'url': 'https://example.org/paper', 'label': '论文'}]
    assert mentioned_references('Use Claude Code, not Claude Coder.', {'Claude Code': 'https://example.org/'}) == [
        {'url': 'https://example.org/', 'label': 'Claude Code', 'relation': 'mention'}]
    assert mentioned_references('使用Cursor开发', {'Cursor': 'https://cursor.com/'})
    assert normalize_link('https://example.org/?key=a%20b&sig=c%2fd') == 'https://example.org/?key=a%20b&sig=c%2fd'
    post = {'entities': {'urls': [{'url': 'https://t.co/x', 'expanded_url': 'https://example.org/a',
                                  'unwound_url': 'https://example.org/final'}]},
            'referenced_tweets': [{'type': 'quoted', 'id': 'q'}]}
    quote = {'note_tweet': {'entities': {'urls': [{'expanded_url': 'https://example.org/paper'}]}},
             'referenced_tweets': [{'type': 'quoted', 'id': 'deeper'}]}
    assert {r['url'] for r in x_references(post, {'q': quote, 'deeper': post})} == {
        'https://example.org/final', 'https://example.org/paper'}
    assert x_references(post, {})[0]['short_url'] == 'https://t.co/x'


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '198.18.0.1',
                                    '::1', '::ffff:127.0.0.1', 'fc00::1', 'fe80::1', '224.0.0.1'])
def test_private_networks_not_readable(address):
    assert not public_ip(address)


@pytest.mark.asyncio
async def test_mixed_dns_rejected_and_address_pinned(monkeypatch, respx_mock):
    resolved = []

    async def dns(host, port, **kwargs):
        resolved.append(host)
        ips = ['93.184.216.34', '127.0.0.1'] if host == 'mixed.example' else ['93.184.216.34']
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ips]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    with pytest.raises(PageUnavailable, match='公开网页'):
        await public_addresses('mixed.example', 443)
    route = respx_mock.get('https://93.184.216.34/paper').respond(200, text='readable text')
    await PageFetcher().bytes('https://safe.example/paper', check_robots=False)
    request = route.calls[0].request
    assert request.headers['host'] == 'safe.example'
    assert request.extensions['sni_hostname'] == 'safe.example'
    assert 'authorization' not in request.headers and 'cookie' not in request.headers
    assert resolved == ['mixed.example', 'safe.example']


@pytest.mark.asyncio
async def test_redirect_revalidates_and_drops_headers(monkeypatch, respx_mock):
    async def addresses(host, port):
        if host == '127.0.0.1':
            raise PageUnavailable('blocked', 'private address')
        return ['93.184.216.34']

    monkeypatch.setattr('radar.web_reader.public_addresses', addresses)
    respx_mock.get('https://93.184.216.34/a').respond(302, headers={
        'location': 'https://other.example/b', 'set-cookie': 'private=secret'})
    second = respx_mock.get('https://93.184.216.34/b').respond(302, headers={'location': 'http://127.0.0.1/private'})
    with pytest.raises(PageUnavailable, match='private'):
        await PageFetcher().bytes('https://safe.example/a', headers={'If-None-Match': 'version'}, check_robots=False)
    assert second.calls[0].request.headers['host'] == 'other.example'
    assert 'if-none-match' not in second.calls[0].request.headers
    assert 'cookie' not in second.calls[0].request.headers
    assert len(respx_mock.calls) == 2


@pytest.mark.asyncio
async def test_robots_and_size_limits(monkeypatch, respx_mock):
    async def addresses(host, port):
        return ['93.184.216.34']

    monkeypatch.setattr('radar.web_reader.public_addresses', addresses)
    respx_mock.get('https://93.184.216.34/robots.txt').respond(200, text='User-agent: *\nDisallow: /private')
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes('https://safe.example/private')
    assert error.value.status == 'restricted' and len(respx_mock.calls) == 1
    respx_mock.get('https://93.184.216.34/large').respond(200, content=b'x' * 100)
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes('https://safe.example/large', limit=50, check_robots=False)
    assert error.value.status == 'too_large'


@pytest.mark.asyncio
async def test_bounded_real_parser():
    paragraph = 'AI researchers describe a reproducible experiment and explain its limitations. ' * 8
    html = f'<html><head><title>AI paper</title></head><body><nav><a href="/noise">Noise</a></nav><article><h1>AI paper</h1><p>{paragraph}<a href="/direct">Read paper</a></p></article></body></html>'
    parsed = await extract_page(html.encode(), 'text/html', 'https://example.org/news')
    assert 'reproducible experiment' in parsed['text']
    assert [r['url'] for r in parsed['links']] == ['https://example.org/direct']
    assert not parsed['partial']
    partial = parse_page(b'a' * 61000, 'text/plain', 'https://example.org/')
    assert partial['partial'] and len(partial['text']) == 60000
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.encrypt('password')
    output = io.BytesIO()
    pdf.write(output)
    with pytest.raises(ValueError, match='encrypted'):
        parse_page(output.getvalue(), 'application/pdf', 'https://example.org/paper.pdf')
    with pytest.raises(ValueError):
        parse_page(b'\x00' * 100, 'application/octet-stream', 'https://example.org/binary')


def incoming(external_id='root', **kwargs):
    return IncomingArticle(**(dict(platform='web', external_id=external_id, author='Researcher',
        title='AI experiment', text='An AI experiment with reproducible measurements.',
        url='https://example.org/' + external_id, published_at=datetime.now(UTC)) | kwargs))


def summary(uid):
    return dict(source_id=uid, title_zh='人工智能实验解读', summary_zh='作者介绍了实验方法，并说明现有证据的限制。',
                key_points_zh=['目前结果仅适用于指定实验条件。'], why_it_matters_zh='为进一步复现实验提供依据。')


@pytest.mark.asyncio
async def test_one_hop_cache_restart_backlog_and_changed_content(tmp_path, monkeypatch):
    engine, sessions = database(f'sqlite:///{tmp_path}/reading.db')
    config = RadarConfig(reading=ReadingConfig(enabled=True, mention_catalog={}, max_documents=2))
    calls, analyses = [], []
    changed = False

    async def fetch(self, url, **kwargs):
        calls.append((url, kwargs.get('headers')))
        if url.endswith('/root'):
            text = 'AI source experiment. ' * 8
            return url, 200, {'content-type': 'text/plain', 'etag': 'v1'}, (text + ' https://example.org/child').encode()
        if kwargs.get('headers', {}).get('If-None-Match') and not changed:
            return url, 304, {}, b''
        text = (('Backlog AI experiment. ' if url.endswith('/backlog') else 'Changed AI results. ') if changed else 'AI child results. ') * 8
        return url, 200, {'content-type': 'text/plain', 'etag': 'v2'}, (text + ' https://example.org/grandchild').encode()

    class Provider:
        async def analyze(self, docs):
            analyses.extend(docs)
            return ReadingOutput(documents=[summary(d['id']) for d in docs])

    monkeypatch.setattr(PageFetcher, 'bytes', fetch)
    monkeypatch.setattr('radar.reading.make_provider', lambda _: Provider())
    with sessions.begin() as s:
        ingest(s, [incoming()], config)
        uid = s.scalar(select(Article.id))
    service = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    result = await service.pending()
    assert result == {'enabled': True, 'fetched': 2, 'summarized': 2}
    assert {c[0] for c in calls} == {'https://example.org/root', 'https://example.org/child'}
    with sessions() as s:
        views = resource_views(s, [uid], config.translation, full=True)[uid]
        assert len(views) == 2 and all(r['status'] == 'ready' and r['text'] for r in views)
        assert len(service.evidence([as_dict(s.get(Article, uid))])[0]['resources']) == 2
    # New service, new metrics and another post sharing the URL do not refetch/reanalyze.
    with sessions.begin() as s:
        ingest(s, [incoming(metrics={'like_count': 900}), incoming('post', platform='x', handle='OpenAI',
               text='AI results https://example.org/child', metrics={'like_count': 500})], config)
    service = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    await service.pending(force=True)
    assert len(calls) == len(analyses) == 2
    with sessions.begin() as s:
        child = s.scalar(select(WebDocument).where(WebDocument.url.endswith('/child')))
        child.retry_at = ''
    await service.pending()
    assert len(calls) == 3 and len(analyses) == 2  # 304 reuses analysis
    assert calls[-1][1] == {'If-None-Match': 'v2'}
    changed = True
    with sessions.begin() as s:
        s.scalar(select(WebDocument).where(WebDocument.url.endswith('/child'))).retry_at = ''
        ingest(s, [incoming('backlog')], config)
    await service.pending()
    assert len(analyses) == 4  # changed child and backlog, despite prior ready documents
    assert not any(url.endswith('/grandchild') for url, _ in calls)
    with sessions() as s:
        assert s.get(Article, uid).text == incoming().text
        assert len(s.scalars(select(DocumentAnalysis)).all()) == 4
    engine.dispose()


@pytest.mark.asyncio
async def test_provider_rejects_wrong_ids_and_non_chinese():
    class Provider(StructuredProvider):
        async def complete(self, prompt, schema_type):
            assert '不调用任何工具' in prompt
            return json.dumps({'documents': self.output})

    provider = Provider()
    provider.output = [summary('wrong')]
    with pytest.raises(ValueError, match='来源'):
        await provider.analyze([{'id': 'real', 'text': 'source'}])
    provider.output = [summary('real') | {'summary_zh': 'English only'}]
    with pytest.raises(ValueError, match='中文'):
        await provider.analyze([{'id': 'real', 'text': 'source'}])
    provider.output = [summary('real')]
    assert (await provider.analyze([{'id': 'real', 'text': 'source'}])).documents[0].source_id == 'real'


@pytest.mark.asyncio
async def test_failed_page_is_visible_without_fabricated_analysis(tmp_path, monkeypatch):
    engine, sessions = database(f'sqlite:///{tmp_path}/failed.db')
    config = RadarConfig(reading=ReadingConfig(enabled=True, mention_catalog={}))
    calls = []

    async def failed(self, url, **kwargs):
        calls.append(url)
        raise PageUnavailable('auth_required', '页面需要登录授权。')

    monkeypatch.setattr(PageFetcher, 'bytes', failed)
    with sessions.begin() as s:
        ingest(s, [incoming()], config)
        uid = s.scalar(select(Article.id))
    service = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    await service.pending()
    await service.pending()
    assert len(calls) == 1
    with sessions() as s:
        result = resource_views(s, [uid], config.translation)[uid][0]
        assert result['status'] == 'auth_required' and result['summary_zh'] is None
        assert service.evidence([as_dict(s.get(Article, uid))])[0]['resources'] == []
    await service.pending(force=True)
    assert len(calls) == 2
    engine.dispose()


def test_api_reads_saved_analysis_only_and_detail_contains_body(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[reading]\nenabled=true\nmention_catalog={}\n')
    settings = Settings(config_path=str(path), database_url=f'sqlite:///{tmp_path}/api.db',
                        reader_token='reader', admin_token='admin', scheduler_enabled=False)
    app = create_app(settings)
    with app.state.sessions.begin() as s:
        ingest(s, [incoming()], app.state.pipeline.config)
        uid = s.scalar(select(Article.id))
        doc = s.scalar(select(WebDocument))
        doc.title, doc.text, doc.status, doc.analysis_id = 'AI results', 'Original saved text', 'fetched', 'analysis'
        s.add(DocumentAnalysis(id='analysis', status='ready', **{k: v for k, v in summary('analysis').items() if k != 'source_id'}))
        s.add(Digest(date='2026-09-07', title='日报', overview='概览', stories=[{'source_ids': [uid]}],
                     provider='test', window_start='2026-09-06', window_end='2026-09-07', source_count=1, coverage=[]))

    async def forbidden(*args, **kwargs):
        raise AssertionError('Read endpoints must not fetch, translate or analyze')

    monkeypatch.setattr(PageFetcher, 'bytes', forbidden)
    monkeypatch.setattr(StructuredProvider, 'analyze', forbidden)
    monkeypatch.setattr(TranslationService, 'request', forbidden)
    with TestClient(app) as client:
        h = {'Authorization': 'Bearer reader'}
        assert client.get('/v1/articles').status_code == 401
        assert client.post('/v1/admin/jobs?kind=read', headers=h).status_code == 403
        listing = client.get('/v1/articles', headers=h).json()['items'][0]
        assert listing['resources'][0]['summary_zh'] == summary('analysis')['summary_zh']
        assert 'text' not in listing['resources'][0]
        detail = client.get(f'/v1/articles/{uid}', headers=h).json()
        assert detail['text'] == incoming().text
        assert detail['resources'][0]['text'] == 'Original saved text'
        digest = client.get('/v1/digests/latest', headers=h).json()
        assert digest['sources'][0]['resources'] == listing['resources']


def test_expanded_x_url_does_not_consume_two_link_slots(tmp_path):
    engine, sessions = database(f'sqlite:///{tmp_path}/links.db')
    config = RadarConfig(reading=ReadingConfig(enabled=True, mention_catalog={}))
    with sessions.begin() as s:
        ingest(s, [incoming(platform='x', handle='OpenAI', metrics={'like_count': 500},
            text='AI paper https://t.co/short', references=[{'url': 'https://example.org/paper',
                'short_url': 'https://t.co/short'}])], config)
        assert [d.url for d in s.scalars(select(WebDocument))] == ['https://example.org/paper']
    engine.dispose()


@pytest.mark.asyncio
async def test_translation_restores_exact_markdown_urls(tmp_path, monkeypatch, respx_mock):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only')
    engine, sessions = database(f'sqlite:///{tmp_path}/translation.db')
    service = TranslationService(sessions, TranslationConfig(enabled=True))
    url = 'https://example.org/paper?v=3&utm_source=post'
    route = respx_mock.post('https://api.deepseek.com/chat/completions').respond(200, json={
        'id': 'chat_1', 'object': 'chat.completion', 'created': 1788652800, 'model': 'test',
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': json.dumps({'translations': [{'id': 'body-0', 'zh': '参见[论文](⟪原文链接-0⟫)。',
                'approved': True, 'issues': []}]})}}]})
    output = await service.request([{'id': 'body-0', 'source': f'See [paper]({url}).',
                                      'draft': f'参见[论文]({url})，当前版本。'}], review=True)
    assert output['body-0'].zh == f'参见[论文]({url})。'
    sent = json.loads(route.calls[0].request.content)
    assert '⟪原文链接-0⟫' in sent['messages'][1]['content']
    assert url not in sent['messages'][1]['content']
    engine.dispose()


@pytest.mark.asyncio
async def test_force_read_resumes_only_unfinished_translation(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only')
    engine, sessions = database(f'sqlite:///{tmp_path}/resume.db')
    config = RadarConfig(reading=ReadingConfig(enabled=True, mention_catalog={}),
                         translation=TranslationConfig(enabled=True))
    with sessions.begin() as s:
        ingest(s, [incoming()], config)
        doc = s.scalar(select(WebDocument))
        doc.title, doc.text, doc.content_hash = 'AI paper', 'Saved source text', 'hash'
        doc.status, doc.retry_at = 'fetched', '2999-01-01'
        doc.analysis_id = fingerprint('hash', config.reading.revision)
        s.add(DocumentAnalysis(id=doc.analysis_id, status='ready'))
        row = ensure_translation(s, doc.title, doc.text, config.translation)
        row.status, row.retry_at, row.attempts = 'review_required', '2999-01-01', 3
    translations = TranslationService(sessions, config.translation)
    calls = []

    async def translate(key, force=False):
        calls.append(force)
        with sessions.begin() as s:
            s.get(Translation, key).status = 'ready'

    translations.translate_one = translate
    reader = ReadingService(sessions, config, translations)
    await reader.pending()
    assert calls == []
    await reader.pending(force=True)
    await reader.pending(force=True)
    assert calls == [True]
    engine.dispose()
