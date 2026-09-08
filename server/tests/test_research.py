"""Synthetic public API responses only: no network, account or model calls."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import httpx
import pytest

from radar import research
from radar.config import ResearchConfig

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(research, "datetime", FixedDateTime)


def config(**values):
    return ResearchConfig(**{"hf_enabled": True, "arxiv_enabled": True, **values})


def paper(identifier="2609.00001", *, votes=15, comments=2, hours=3):
    return {
        "paper": {"id": identifier, "title": "A generalization bound", "summary": "We prove a sample complexity bound.",
                  "authors": [{"name": "Ada Example"}, {"name": "Ben Example"}],
                  "publishedAt": (NOW - timedelta(hours=hours)).isoformat(), "upvotes": votes,
                  "ai_summary": "Invented machine conclusion, never source evidence."},
        "numComments": comments, "publishedAt": NOW.isoformat(),
        "title": "Daily recommendation title", "summary": "Daily machine summary",
        "submittedBy": {"name": "Community submitter"},
    }


async def hf_result(pages, **overrides):
    calls = []

    def respond(request):
        calls.append(request)
        page = pages[len(calls) - 1]
        return page if isinstance(page, httpx.Response) else httpx.Response(200, json=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await research.fetch_hf_papers(client, config(**overrides), 168)
    return result, calls


def atom(entries):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv Query</title>'
            f'<updated>{NOW.isoformat()}</updated>' + "".join(entries) + '</feed>').encode()


def entry(identifier="2609.00001v1", *, hours=2, title="A generalization bound",
          summary="We prove sample complexity guarantees.", published=True, authors=True):
    date = f'<published>{(NOW - timedelta(hours=hours)).isoformat()}</published>' if published else ""
    names = '<author><name>Ada Example</name></author><author><name>Ben Example</name></author>' if authors else ""
    return (f'<entry><id>http://arxiv.org/abs/{identifier}</id><title>{escape(title)}</title>'
            f'<summary>{escape(summary)}</summary>{date}<updated>{NOW.isoformat()}</updated>{names}'
            '<category term="cs.LG"/><link rel="alternate" href="https://arxiv.org/abs/'
            f'{identifier}"/><link title="pdf" rel="related" href="https://arxiv.org/pdf/{identifier}"/></entry>')


async def arxiv_result(entries, **overrides):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=atom(entries))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await research.fetch_arxiv_theory(client, config(**overrides), 168)
    return result, calls


async def test_hf_uses_author_abstract_and_paper_date_with_observed_metrics():
    source = paper("2609.00001v3", votes=24, comments=6)
    source["paper"].update(projectPage="https://example.org/project?utm_source=hf",
                           githubRepo="https://github.com/example/project")
    result, calls = await hf_result([[source]], hf_limit=1)
    assert result.status == "healthy" and len(calls) == 1
    assert calls[0].url.host == "huggingface.co"
    assert dict(calls[0].url.params) == {"sort": "trending", "limit": "100", "p": "0"}
    item = result.items[0]
    assert item.external_id == "arxiv:2609.00001" and item.url == "https://arxiv.org/abs/2609.00001"
    assert item.source_id == "hf-papers" and item.platform == "web"
    assert item.title == source["paper"]["title"] and item.author == "Ada Example, Ben Example"
    assert item.text == research.ABSTRACT_PREFIX + source["paper"]["summary"]
    assert item.published_at == NOW - timedelta(hours=3)
    assert item.published_precision == "date"
    assert item.metrics == {"like_count": 24, "comment_count": 6, "hf_upvotes": 24,
                            "hf_comments": 6, "hf_votes_observed_at": int(NOW.timestamp()), "arxiv_version": 3}
    assert {reference.url for reference in item.references} == {
        "https://huggingface.co/papers/2609.00001", "https://example.org/project",
        "https://github.com/example/project",
    }
    assert "Community submitter" not in item.author and "Invented" not in item.text


async def test_hf_old_trending_falls_back_once_then_sorts_by_real_votes_and_comments():
    result, calls = await hf_result([
        [paper("2608.00001", votes=900, hours=200), paper("2609.00001", votes=3)],
        [paper("2609.00002", votes=30, comments=4), paper("2609.00003", votes=70, comments=1),
         paper("2609.00004", votes=30, comments=9), paper("2609.00005", votes=20)],
    ], hf_limit=3)
    assert len(calls) == 2 and calls[1].url.params["sort"] == "publishedAt"
    assert result.status == "healthy"
    assert [item.external_id for item in result.items] == ["arxiv:2609.00003", "arxiv:2609.00004", "arxiv:2609.00002"]
    assert "6 条" in result.message and "3 篇" in result.message


async def test_hf_family_dedup_keeps_latest_version_and_never_sums_votes():
    older = paper("2609.00001v1", votes=23)
    newer = paper("2609.00001v3", votes=25)
    newer["paper"]["summary"] = "The third author abstract."
    result, calls = await hf_result([[older], [newer, paper("2609.00001v2", votes=24)]], hf_limit=2)
    assert len(calls) == 2 and len(result.items) == 1
    assert result.items[0].metrics["hf_upvotes"] == 25
    assert result.items[0].metrics["arxiv_version"] == 3
    assert result.items[0].text.endswith("The third author abstract.")


@pytest.mark.parametrize(("path", "value"), [
    (("paper",), None), (("paper", "id"), "not-an-arxiv-id"),
    (("paper", "title"), ""), (("paper", "summary"), None),
    (("paper", "authors"), []), (("paper", "authors"), [{"name": ""}]),
    (("paper", "publishedAt"), None), (("paper", "publishedAt"), "not-a-date"),
    (("paper", "publishedAt"), "2026-09-08"), (("paper", "publishedAt"), "2026-09-08T09:00:00"),
    (("paper", "upvotes"), None), (("paper", "upvotes"), -1), (("paper", "upvotes"), True),
    (("numComments",), None), (("numComments",), -2),
])
async def test_hf_bad_entry_does_not_discard_good_paper_or_fabricate_fields(path, value):
    broken = paper()
    target = broken
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    result, _ = await hf_result([[broken, paper("2609.00002")]], hf_limit=1)
    assert result.status == "partial" and len(result.items) == 1
    assert result.items[0].external_id == "arxiv:2609.00002"


async def test_hf_missing_comments_and_upvotes_are_not_invented_as_zero():
    first, second = paper("2609.00001"), paper("2609.00002")
    del first["numComments"]
    del second["paper"]["upvotes"]
    result, _ = await hf_result([[first, second, paper("2609.00003")]], hf_limit=1)
    assert result.status == "partial" and len(result.items) == 1
    assert result.items[0].external_id == "arxiv:2609.00003"


async def test_hf_invalid_links_are_omitted_without_inventing_project_links():
    source = paper()
    source["paper"].update(projectPage="javascript:alert(1)", githubRepo="https://user:secret@example.org/repo")
    result, _ = await hf_result([[source]], hf_limit=1)
    assert [reference.url for reference in result.items[0].references] == ["https://huggingface.co/papers/2609.00001"]


async def test_hf_author_limit_and_empty_selection_are_truthful():
    source = paper()
    source["paper"]["authors"] = [{"name": "Researcher " * 40}]
    result, _ = await hf_result([[source]], hf_limit=1)
    assert len(result.items[0].author) == 200
    result, calls = await hf_result([[paper(hours=300), paper(hours=-1)], []])
    assert result.items == [] and result.status == "healthy" and len(calls) == 2
    assert "0 篇" in result.message


async def test_hf_second_page_rate_limit_keeps_verified_first_page():
    result, calls = await hf_result([[paper()], httpx.Response(429)], hf_limit=2)
    assert len(calls) == 2 and len(result.items) == 1
    assert result.status == "partial" and "访问频率受限" in result.message


@pytest.mark.parametrize(("code", "status"), [(401, "access_restricted"), (403, "access_restricted"),
                                             (429, "rate_limited"), (500, "error")])
@pytest.mark.parametrize("fetch", [research.fetch_hf_papers, research.fetch_arxiv_theory])
async def test_api_errors_are_fixed_public_source_states_not_account_authorization(code, status, fetch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(code, text="private arbitrary upstream error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch(client, config(), 168)
    assert result.status == status and result.status != "auth_required" and result.items == []
    assert "private" not in result.message and len(requests) == 1


@pytest.mark.parametrize("fetch", [research.fetch_hf_papers, research.fetch_arxiv_theory])
async def test_network_failure_is_bounded_without_raw_exception(fetch):
    def respond(request):
        raise httpx.ReadTimeout("private provider context", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch(client, config(), 168)
    assert result.status == "error" and "private" not in result.message


@pytest.mark.parametrize("fetch", [research.fetch_hf_papers, research.fetch_arxiv_theory])
async def test_streaming_response_limit_stops_before_consuming_unbounded_data(fetch):
    class Huge(httpx.AsyncByteStream):
        read = 0

        async def __aiter__(self):
            for _ in range(20):
                self.read += 1
                yield b"x" * 1_000_000

    stream = Huge()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        result = await fetch(client, config(), 168)
    assert result.status == "error" and "5 MB" in result.message and stream.read == 6


@pytest.mark.parametrize("body", [{"error": "private"}, {"data": []}, "wrong root", None])
async def test_hf_rejects_nonlist_payload(body):
    result, calls = await hf_result([httpx.Response(200, content=json.dumps(body))])
    assert result.status == "error" and len(calls) == 1 and not result.items


async def test_disabled_sources_make_no_request():
    def unexpected(_):
        pytest.fail("disabled source attempted network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        for fetch in (research.fetch_hf_papers, research.fetch_arxiv_theory):
            result = await fetch(client, ResearchConfig(), 168)
            assert result.status == "disabled" and result.items == []


async def test_arxiv_single_request_original_dates_version_and_no_fabricated_heat():
    original = "We establish convergence bounds. Project: https://example.org/paper"
    result, calls = await arxiv_result([entry("2609.00001v2", summary=original)])
    assert result.status == "healthy" and len(calls) == 1
    assert calls[0].url.host == "export.arxiv.org"
    assert dict(calls[0].url.params) == {
        "search_query": "(cat:cs.LG OR cat:stat.ML)", "sortBy": "submittedDate",
        "sortOrder": "descending", "start": "0", "max_results": "60",
    }
    item = result.items[0]
    assert item.external_id == "arxiv:2609.00001" and item.url == "https://arxiv.org/abs/2609.00001"
    assert item.metrics == {"arxiv_version": 2} and item.source_id == "arxiv-theory"
    assert item.published_at == NOW - timedelta(hours=2)
    assert item.published_precision == "timestamp"
    assert item.text == research.ABSTRACT_PREFIX + original and item.author == "Ada Example, Ben Example"
    assert item.references == []  # Explicit abstract URLs are discovered by the existing reading path.
    assert "不代表社区热度" in result.message


@pytest.mark.parametrize("topic", ["sample complexity", "scaling laws",
                                  "information theory", "information-theoretic", "optimization bounds"])
async def test_arxiv_selects_specific_theory_terms(topic):
    result, _ = await arxiv_result([entry(title="A learning result", summary=f"We study {topic}.")])
    assert len(result.items) == 1


@pytest.mark.parametrize("topic", ["generalization", "convergence"])
async def test_arxiv_empirical_generalization_or_convergence_alone_is_not_theory(topic):
    result, _ = await arxiv_result([
        entry(title="An empirical learning benchmark", summary=f"Our application improves {topic} performance.")
    ])
    assert result.status == "healthy" and result.items == []


@pytest.mark.parametrize("signal", ["theoretical", "theorem", "proof", "prove", "bounds", "guarantees"])
@pytest.mark.parametrize("topic", ["generalization", "convergence"])
async def test_arxiv_generalization_and_convergence_require_theoretical_evidence(topic, signal):
    result, _ = await arxiv_result([
        entry(title=f"A {topic} result", summary=f"We establish {signal} for this learning algorithm.")
    ])
    assert result.status == "healthy" and len(result.items) == 1


async def test_arxiv_excludes_application_only_old_and_future_papers_and_caps_theory_selection():
    entries = [entry("2609.00001v1", title="Image synthesis", summary="Our new app generates images."),
               entry("2608.00002v4", hours=300), entry("2609.00003v1", hours=-1)]
    entries += [entry(f"2609.{i:05}v1", hours=i) for i in range(4, 11)]
    result, calls = await arxiv_result(entries)
    assert len(calls) == 1 and len(result.items) == 4
    assert [item.external_id for item in result.items] == [f"arxiv:2609.{i:05}" for i in range(4, 8)]
    assert result.status == "healthy" and "10 条" in result.message and "4 篇" in result.message


async def test_arxiv_missing_or_malformed_original_date_never_uses_feed_or_update_date():
    missing = entry("2609.00001v1", published=False)
    malformed = entry("2609.00002v1").replace((NOW - timedelta(hours=2)).isoformat(), "bad-date")
    naive = entry("2609.00003v1").replace((NOW - timedelta(hours=2)).isoformat(), "2026-09-08T10:00:00")
    result, _ = await arxiv_result([missing, malformed, naive, entry("2609.00004v1")])
    assert result.status == "partial" and len(result.items) == 1
    assert result.items[0].external_id == "arxiv:2609.00004"


async def test_arxiv_invalid_paper_and_author_do_not_discard_valid_entries():
    result, _ = await arxiv_result([entry("not-a-paper"), entry("2609.00002v1", authors=False), entry("2609.00003v1")])
    assert result.status == "partial" and len(result.items) == 1


async def test_arxiv_version_family_dedup_retains_highest_returned_version():
    result, _ = await arxiv_result([entry("2609.00001v1"), entry("2609.00001v3", summary="New convergence result."),
                                    entry("2609.00001v2")])
    assert len(result.items) == 1 and result.items[0].metrics["arxiv_version"] == 3
    assert result.items[0].text.endswith("New convergence result.")


async def test_shared_source_prefix_and_legacy_arxiv_family_ids():
    hf, _ = await hf_result([[paper("hep-th/9901001v2")]], hf_limit=1)
    arxiv, _ = await arxiv_result([entry("hep-th/9901001v2", hours=3, summary=paper()["paper"]["summary"])])
    assert hf.items[0].external_id == arxiv.items[0].external_id == "arxiv:hep-th/9901001"
    assert hf.items[0].text == arxiv.items[0].text


async def test_arxiv_empty_feed_is_successful_empty_selection():
    result, _ = await arxiv_result([])
    assert result.status == "healthy" and result.items == [] and "0 条" in result.message


@pytest.mark.parametrize("payload", [b"not XML", b"<html>blocked</html>", b"<feed", b"{}"])
async def test_arxiv_invalid_api_payload_is_not_an_empty_healthy_feed(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as client:
        result = await research.fetch_arxiv_theory(client, config(), 168)
    assert result.status == "error" and result.items == []


async def test_hf_body_does_not_change_with_votes_or_discussion_counts():
    a, b = paper(), deepcopy(paper())
    b["paper"]["upvotes"], b["numComments"] = 1000, 900
    first, _ = await hf_result([[a]], hf_limit=1)
    second, _ = await hf_result([[b]], hf_limit=1)
    assert first.items[0].text == second.items[0].text


async def test_hf_withdrawn_paper_is_not_selected_even_when_popular_and_recent():
    withdrawn = paper(votes=1000)
    withdrawn["paper"]["withdrawnAt"] = NOW.isoformat()
    result, _ = await hf_result([[withdrawn, paper("2609.00002")]], hf_limit=1)
    assert result.status == "healthy" and len(result.items) == 1
    assert result.items[0].external_id == "arxiv:2609.00002"
