"""Direct X relationships from synthetic expanded profiles; no paid API calls."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from radar.config import RadarConfig
from radar.db import database
from radar.ranking import classify
from radar.schemas import AssociatedEntity, IncomingArticle
from radar.sources import (
    X_EXPANSIONS,
    X_TWEET_FIELDS,
    X_USER_FIELDS,
    extract_x_entities,
    fetch_x,
    x_page_items,
)
from radar.x_collection import XCollector


def user(uid="202", username="Ada", **values):
    return {"id": uid, "username": username, "name": "Ada Researcher", "description": "Studies AI reasoning.",
            "public_metrics": {"followers_count": 1234}, "url": "https://example.org/personal", **values}


def post(uid="900", **values):
    return {"id": uid, "author_id": "101", "text": "AI research with @Ada",
            "created_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "public_metrics": {"like_count": 40}, **values}


def mention(uid="202", username="Ada", **values):
    return {"id": uid, "username": username, "start": 17, "end": 21, **values}


def page(main=None, users=None, tweets=None):
    return {"data": [main or post(entities={"mentions": [mention()]})],
            "includes": {"users": users if users is not None else [user("101", "Publisher"), user()],
                         "tweets": tweets or []}}


def entity(**values):
    return AssociatedEntity(**{"external_id": "202", "handle": "Ada", "name": "Ada Researcher",
                               "description": "Studies AI", "followers_count": 12,
                               "url": "https://x.com/Ada", "relation": "mention", **values})


@pytest.mark.parametrize(("field", "value"), [
    ("platform", "facebook"), ("external_id", "not-an-id"), ("external_id", "２０２"),
    ("external_id", "202\n"), ("external_id", "2" * 101), ("handle", "bad-name"),
    ("handle", "a" * 16), ("handle", ""), ("handle", "Ada\n"),
    ("name", "x" * 121), ("description", "x" * 1001), ("followers_count", -1),
    ("followers_count", True), ("followers_count", "12"), ("relation", "retweeted"),
    ("url", "http://x.com/Ada"), ("url", "https://x.com/impostor"),
    ("url", "https://user:secret@x.com/Ada"), ("matched_text", "x" * 301),
])
def test_entity_schema_rejects_invalid_identity_or_unobserved_metrics(field, value):
    with pytest.raises(ValidationError):
        entity(**{field: value})


def test_direct_mention_is_bound_to_real_id_username_and_profile_not_display_name():
    payload = page()
    before = deepcopy(payload)
    item = x_page_items(payload)[0]
    assert item.author_external_id == "101"
    assert item.entities[0].model_dump() == {
        "platform": "x", "external_id": "202", "handle": "Ada", "name": "Ada Researcher",
        "description": "Studies AI reasoning.", "followers_count": 1234, "url": "https://x.com/Ada",
        "relation": "mention", "matched_text": "@Ada",
    }
    assert item.text == before["data"][0]["text"] and item.metrics == {"like_count": 40}
    assert payload == before and item.references == []


@pytest.mark.parametrize("profiles", [
    [], [user("303", "Ada")], [user("202", "Impostor", name="Ada")],
    [user("202", "Ada"), user("303", "ADA")],
    [user("202", "Ada"), user("202", "Impostor")],
    [user("２０２", "Ada")],
])
def test_missing_mismatched_and_ambiguous_profiles_never_create_entities(profiles):
    assert x_page_items(page(users=profiles))[0].entities == []


@pytest.mark.parametrize("metric", [None, {}, {"followers_count": -1}, {"followers_count": True},
                                   {"followers_count": "42"}])
def test_missing_profile_metric_is_not_invented_as_zero(metric):
    assert x_page_items(page(users=[user(public_metrics=metric)]))[0].entities == []


def test_actual_zero_followers_is_preserved_and_profiles_are_bounded():
    item = x_page_items(page(users=[user(name="N" * 140, description="D" * 1200,
                                        public_metrics={"followers_count": 0})]))[0]
    account = item.entities[0]
    assert account.followers_count == 0 and len(account.name) == 120 and len(account.description) == 1000


def test_identical_profiles_and_case_insensitive_mentions_deduplicate_by_user_id():
    main = post(entities={"mentions": [mention(), mention(username="ADA")]},
                referenced_tweets=[{"type": "quoted", "id": "800"}])
    item = x_page_items(page(main, users=[user(), user()], tweets=[post("800", author_id="202")]))[0]
    assert len(item.entities) == 1 and item.entities[0].relation == "mention"


def test_username_without_id_and_plain_at_text_without_entity_do_not_guess_profiles():
    missing = mention()
    del missing["id"]
    assert x_page_items(page(post(entities={"mentions": [missing]})))[0].entities == []
    assert x_page_items(page(post()))[0].entities == []


@pytest.mark.parametrize("includes", [None, [], {"users": None}, {"users": [None, {}], "tweets": None}])
def test_unavailable_profile_envelope_keeps_readable_main_post(includes):
    payload = page()
    payload["includes"] = includes
    item = x_page_items(payload)[0]
    assert item.entities == [] and item.text == payload["data"][0]["text"]


def test_quotes_and_replies_resolve_only_direct_authors_not_all_expanded_users():
    main = post(text="A useful AI discussion.", in_reply_to_user_id="303", referenced_tweets=[
        {"type": "quoted", "id": "800"}, {"type": "replied_to", "id": "801"},
        {"type": "retweeted", "id": "802"},
    ])
    quoted = post("800", author_id="202", text="A new AI model.", entities={"mentions": [mention("404", "Deeper")]},
                  referenced_tweets=[{"type": "quoted", "id": "802"}])
    replied = post("801", author_id="303", entities={"mentions": [mention("404", "Deeper")]})
    deeper = post("802", author_id="404")
    item = x_page_items(page(main, users=[user(), user("303", "ReplyAuthor"), user("404", "Deeper")],
                             tweets=[quoted, replied, deeper]))[0]
    assert [(account.external_id, account.relation) for account in item.entities] == [("202", "quote"), ("303", "reply")]
    assert all(account.matched_text is None for account in item.entities)
    assert item.text == main["text"] + f"\n\n[引用帖：@Ada，{quoted['created_at']}]\nA new AI model."
    assert item.references[0].kind == "reply" and item.references[0].url == "https://x.com/i/status/801"


def test_reply_user_expansion_can_exist_without_the_parent_post():
    item = x_page_items(page(post(in_reply_to_user_id="202")))[0]
    assert len(item.entities) == 1 and item.entities[0].relation == "reply"


def test_reply_parent_author_is_fallback_only_when_direct_user_id_is_absent():
    main = post(referenced_tweets=[{"type": "replied_to", "id": "800"}])
    item = x_page_items(page(main, tweets=[post("800", author_id="202")]))[0]
    assert len(item.entities) == 1 and item.entities[0].relation == "reply"
    main["in_reply_to_user_id"] = "303"
    item = x_page_items(page(main, users=[user(), user("303", "Other")], tweets=[post("800", author_id="202")]))[0]
    assert item.entities == []


def test_missing_quote_profile_preserves_original_quote_text_without_fabricating_account():
    main = post(referenced_tweets=[{"type": "quoted", "id": "800"}])
    item = x_page_items(page(main, users=[], tweets=[post("800", author_id="202", text="AI source text")]))[0]
    assert item.entities == [] and "AI source text" in item.text
    assert "引用帖：作者未返回" in item.text


def test_authors_self_mention_quote_and_reply_are_excluded():
    main = post(entities={"mentions": [mention("101", "Publisher")]}, in_reply_to_user_id="101",
                referenced_tweets=[{"type": "quoted", "id": "800"}])
    item = x_page_items(page(main, tweets=[post("800", author_id="101")]))[0]
    assert item.entities == [] and item.author_external_id == "101"


def test_two_main_posts_do_not_share_each_others_mentioned_profiles():
    payload = page(users=[user(), user("303", "Ben"), user("404", "Unused")])
    payload["data"] = [post(entities={"mentions": [mention()]}),
                       post("901", text="AI research with @Ben", entities={"mentions": [mention("303", "Ben")]})]
    first, second = x_page_items(payload)
    assert [account.external_id for account in first.entities] == ["202"]
    assert [account.external_id for account in second.entities] == ["303"]


def test_note_tweet_mentions_are_main_post_evidence_and_not_recursively_expanded():
    main = post(text="An excerpt…", note_tweet={"text": "AI research with @Ada", "entities": {"mentions": [mention()]}})
    item = x_page_items(page(main))[0]
    assert item.entities[0].matched_text == "@Ada" and item.text == main["note_tweet"]["text"]


@pytest.mark.parametrize(("start", "end"), [(2, 6), (3, 7)])
def test_matched_text_accepts_only_actual_span_with_astral_unicode_offsets(start, end):
    main = post(text="🤖 @Ada", entities={"mentions": [mention(start=start, end=end)]})
    assert x_page_items(page(main))[0].entities[0].matched_text == "@Ada"


@pytest.mark.parametrize("offsets", [{}, {"start": 0, "end": 4}, {"start": -1, "end": 3},
                                     {"start": True, "end": 3}, {"start": 3, "end": 2}])
def test_invalid_or_missing_offsets_do_not_invent_matched_text(offsets):
    main = post(entities={"mentions": [{"id": "202", "username": "Ada", **offsets}]})
    assert x_page_items(page(main))[0].entities[0].matched_text is None


def test_limit_is_thirty_unique_profiles_and_does_not_change_the_input():
    profiles = [user(str(200 + index), f"person{index}") for index in range(40)]
    mentions = [mention(profile["id"], profile["username"]) for profile in profiles]
    main = post(entities={"mentions": [mentions[0], *mentions]})
    result = extract_x_entities(main, {}, profiles)
    assert len(result) == len({account.external_id for account in result}) == 30
    with pytest.raises(ValidationError):
        IncomingArticle(**post_for_schema(), entities=[entity()] * 31)


def post_for_schema():
    return {"platform": "x", "external_id": "900", "url": "https://x.com/Publisher/status/900",
            "title": "Hello", "text": "Hello", "author": "Publisher", "published_at": datetime.now(UTC)}


def test_metadata_defaults_keep_other_sources_and_existing_imports_compatible():
    original = IncomingArticle(**post_for_schema())
    assert original.entities == [] and original.author_external_id == ""
    with pytest.raises(ValidationError):
        IncomingArticle(**post_for_schema(), author_external_id="x" * 101)


def test_ai_words_in_associated_profile_do_not_reclassify_unrelated_main_text():
    main = post(text="Happy birthday @Ada", entities={"mentions": [mention(start=15, end=19)]})
    item = x_page_items(page(main, users=[user(name="OpenAI Researcher", description="AI LLM GPT model agent research")]))[0]
    assert item.entities and classify(item) == []
    assert item.text == main["text"] and item.metrics == {"like_count": 40}


def expected_fields(params):
    assert params["tweet.fields"] == X_TWEET_FIELDS and params["expansions"] == X_EXPANSIONS
    assert params["user.fields"] == X_USER_FIELDS
    assert {"entities.mentions.username", "in_reply_to_user_id", "referenced_tweets.id.author_id"} <= set(X_EXPANSIONS.split(","))
    assert "in_reply_to_user_id" in X_TWEET_FIELDS.split(",")
    assert {"id", "name", "username", "description", "public_metrics", "url", "protected"} == set(X_USER_FIELDS.split(","))


async def test_legacy_fetch_requests_profiles_in_the_existing_single_search_request(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "synthetic-only")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch_x(client, RadarConfig(x_max_pages=1), [])
    assert len(calls) == 1 and result[0].entities[0].external_id == "202"
    expected_fields(calls[0].url.params)


def test_persistent_collector_uses_the_same_official_fields_without_profile_requests(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/profiles.db")
    try:
        collector = XCollector(sessions, RadarConfig())
        collector.initialize(["Publisher"], "synthetic-owner")
        _, params = collector.prepare("watch:Publisher", True, "synthetic-owner")
        expected_fields(params)
    finally:
        engine.dispose()
