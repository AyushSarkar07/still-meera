"""Offline tests: all Telegram, Gemini and news calls are mocked."""
import pytest

from still_meera import fakes, formatter
from still_meera.config import Config
from still_meera.db import Store
from still_meera.gemini import (AIPermanentError, AITransientError, MalformedAIResponse,
                                parse_json, validate_draft, validate_triage)
from still_meera.news import fetch_headlines
from still_meera.pipeline import Pipeline, Status

CHAT = -1001234567890


def post(update_id, message_id, chat=CHAT, **content):
    return {"update_id": update_id,
            "channel_post": {"message_id": message_id, "chat": {"id": chat, "type": "channel"}, **content}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    def make(ai=None, news=None, threshold=6, tg=None, max_attempts=2, db=None):
        cfg = Config(telegram_chat_id=CHAT, review_chat_id=CHAT, triage_threshold=threshold,
                     db_path=db or tmp_path / "t.db", max_attempts=max_attempts)
        store = Store(cfg.db_path)
        tg = tg or fakes.FakeTelegram(CHAT)
        ai = ai or fakes.FakeAI()
        clock = Clock()
        pipe = Pipeline(cfg, store, tg, ai, news_fetcher=news or fakes.fake_news(), clock=clock)
        return pipe, store, tg, ai, clock
    return make


# ---------------------------------------------------------------- triage routing
def test_high_score_note_is_drafted_and_delivered(env):
    pipe, store, tg, ai, _ = env()
    assert pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE)) == "processed"
    note = store.get_note(1)
    assert note["status"] == Status.DELIVERED
    texts = [m["text"] for m in tg.sent]
    assert texts[0].startswith("📝 Still Meera · note #1")
    assert texts[1] == fakes.SAMPLE_DRAFT["post"]          # draft alone, easy to copy
    assert texts[2].startswith("📰 Related news · note #1") and "example.com/sample-headline-1" in texts[2]
    assert "used in draft" in texts[2] and "Why: [MOCK]" in texts[2]
    assert texts[3].startswith("✅ Check before publishing")
    assert "Retailer meeting details may be confidential" in texts[3]


def test_low_score_note_is_held_and_preserved(env):
    ai = fakes.FakeAI(triage_responses=[fakes.SAMPLE_TRIAGE_LOW])
    pipe, store, tg, ai, _ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_WEAK_NOTE))
    note = store.get_note(1)
    assert note["status"] == Status.HELD
    assert note["original_text"] == fakes.SAMPLE_WEAK_NOTE
    assert note["triage_json"]["decision"] == "hold"
    assert ai.calls["draft"] == 0
    assert "held" in tg.sent[0]["text"] and "/draft 1" in tg.sent[0]["text"]
    assert len(tg.sent) == 2 and tg.sent[1]["text"].startswith("📰 Related news")  # held notes get news too


def test_threshold_is_configurable_and_applied_by_app_not_model(env):
    triage = dict(fakes.SAMPLE_TRIAGE_HIGH, score=7, decision="develop")
    pipe, store, *_ = env(ai=fakes.FakeAI(triage_responses=[triage]), threshold=8)
    pipe.handle_update(post(1, 10, text="note"))
    assert store.get_note(1)["status"] == Status.HELD


def test_score_exactly_at_threshold_qualifies(env):
    triage = dict(fakes.SAMPLE_TRIAGE_HIGH, score=6)
    pipe, store, *_ = env(ai=fakes.FakeAI(triage_responses=[triage]))
    pipe.handle_update(post(1, 10, text="note"))
    assert store.get_note(1)["status"] == Status.DELIVERED


def test_force_draft_command_on_held_note(env):
    ai = fakes.FakeAI(triage_responses=[fakes.SAMPLE_TRIAGE_LOW])
    pipe, store, tg, ai, _ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_WEAK_NOTE))
    assert pipe.handle_update(post(2, 11, text="/draft 1")) == "command_draft"
    assert store.get_note(1)["status"] == Status.DELIVERED
    assert ai.calls["triage"] == 1  # cached triage reused, not re-paid


def test_no_news_used_means_no_sources_message(env):
    draft = dict(fakes.SAMPLE_DRAFT, used_news_ids=[])
    pipe, store, tg, *_ = env(ai=fakes.FakeAI(draft_responses=[draft]))
    pipe.handle_update(post(1, 10, text="note"))
    assert not any(m["text"].startswith("🔗") for m in tg.sent)
    assert "No news hook used" in tg.sent[-1]["text"]


def test_draft_cannot_cite_headlines_that_were_not_supplied():
    d = validate_draft(dict(fakes.SAMPLE_DRAFT, used_news_ids=[0, 5, -1, "x"]), news_count=1)
    assert d["used_news_ids"] == [0]


# ---------------------------------------------------------------- duplicates
def test_duplicate_update_id_is_ignored(env):
    pipe, store, tg, ai, _ = env()
    pipe.handle_update(post(1, 10, text="note"))
    assert pipe.handle_update(post(1, 10, text="note")) == "duplicate_update"
    assert ai.calls["triage"] == 1


def test_duplicates_are_ignored_across_restart(env, tmp_path):
    db = tmp_path / "persist.db"
    pipe, store, tg, ai, _ = env(db=db)
    pipe.handle_update(post(1, 10, text="note"))
    store.close()
    pipe2, store2, tg2, ai2, _ = env(db=db)  # simulated restart: fresh objects, same DB
    assert pipe2.handle_update(post(1, 10, text="note")) == "duplicate_update"
    assert pipe2.handle_update(post(99, 10, text="note")) == "duplicate_message"  # same message, new update id
    assert ai2.calls["triage"] == 0 and tg2.sent == []


def test_offset_persists(tmp_path):
    s = Store(tmp_path / "o.db")
    s.set_offset(42)
    s.close()
    assert Store(tmp_path / "o.db").get_offset() == 42


# ---------------------------------------------------------------- channel filtering
def test_other_chat_is_ignored(env):
    pipe, store, tg, ai, _ = env()
    assert pipe.handle_update(post(1, 10, chat=-100999, text="note")) == "ignored_wrong_chat"
    assert store.list_notes() == [] and ai.calls["triage"] == 0


def test_non_channel_updates_are_ignored(env):
    pipe, store, *_ = env()
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": CHAT}, "text": "hi"}}
    assert pipe.handle_update(update) == "ignored_not_channel_post"
    assert store.list_notes() == []


def test_unsupported_content_is_ignored(env):
    pipe, store, *_ = env()
    assert pipe.handle_update(post(1, 10, photo=[{"file_id": "x"}])) == "ignored_unsupported"


# ---------------------------------------------------------------- bot-output loops
def test_bot_messages_do_not_trigger_drafts(env):
    pipe, store, tg, ai, _ = env()
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    for i, sent in enumerate(tg.sent):
        outcome = pipe.handle_update(post(100 + i, sent["message_id"], text=sent["text"]))
        assert outcome == "ignored_bot_output"
    assert ai.calls["triage"] == 1


def test_reposted_draft_with_new_message_id_is_ignored(env):
    pipe, store, tg, ai, _ = env()
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    assert pipe.handle_update(post(2, 555, text=fakes.SAMPLE_DRAFT["post"])) == "ignored_bot_output"


def test_via_bot_and_forwarded_bot_posts_are_ignored(env):
    pipe, *_ = env()
    assert pipe.handle_update(post(1, 10, text="x", via_bot={"id": 1, "is_bot": True})) == "ignored_bot_output"
    fwd = {"type": "user", "sender_user": {"id": 2, "is_bot": True}}
    assert pipe.handle_update(post(2, 11, text="y", forward_origin=fwd)) == "ignored_bot_output"


# ---------------------------------------------------------------- voice + failures
def test_voice_note_is_transcribed_then_drafted(env):
    pipe, store, tg, ai, _ = env()
    pipe.handle_update(post(1, 10, voice={"file_id": "F", "mime_type": "audio/ogg"}))
    note = store.get_note(1)
    assert note["transcript"] == fakes.SAMPLE_TRANSCRIPT
    assert note["status"] == Status.DELIVERED and ai.calls["transcribe"] == 1


def test_failed_transcription_keeps_note_and_retries_without_repaying(env):
    ai = fakes.FakeAI(transcripts=[AITransientError("Gemini API error 503")])
    pipe, store, tg, ai, clock = env(ai=ai)
    pipe.handle_update(post(1, 10, voice={"file_id": "F", "mime_type": "audio/ogg"}))
    note = store.get_note(1)
    assert note["status"] == Status.RETRY_PENDING and note["voice_file_id"] == "F"
    assert "transcription failed" in tg.sent[-1]["text"] and "original note is saved" in tg.sent[-1]["text"]
    assert pipe.run_due_retries() == 0      # not due yet
    clock.t += 3600
    assert pipe.run_due_retries() == 1
    assert store.get_note(1)["status"] == Status.DELIVERED
    assert ai.calls["transcribe"] == 2


def test_voice_download_failure_is_handled(env):
    pipe, store, tg, ai, _ = env(tg=fakes.FakeTelegram(CHAT, fail_download=True))
    pipe.handle_update(post(1, 10, voice={"file_id": "F"}))
    note = store.get_note(1)
    assert note["status"] == Status.RETRY_PENDING and note["stage"] == "download"
    assert ai.calls["transcribe"] == 0


def test_malformed_ai_response_stops_after_max_attempts(env):
    ai = fakes.FakeAI(triage_responses=[{"nope": 1}, {"score": "high"}, fakes.SAMPLE_TRIAGE_HIGH])
    pipe, store, tg, ai, clock = env(ai=ai, max_attempts=2)
    pipe.handle_update(post(1, 10, text="my note"))
    assert store.get_note(1)["status"] == Status.RETRY_PENDING
    clock.t += 3600
    pipe.run_due_retries()
    note = store.get_note(1)
    assert note["status"] == Status.FAILED and note["attempts"] == 2
    assert note["original_text"] == "my note"
    clock.t += 99999
    assert pipe.run_due_retries() == 0      # never retries on its own again
    assert ai.calls["triage"] == 2
    assert "/retry 1" in tg.sent[-1]["text"]


def test_permanent_api_error_does_not_retry(env):
    ai = fakes.FakeAI(triage_responses=[AIPermanentError("Gemini API error 404: model not found")])
    pipe, store, tg, ai, clock = env(ai=ai, max_attempts=3)
    pipe.handle_update(post(1, 10, text="note"))
    assert store.get_note(1)["status"] == Status.FAILED
    clock.t += 99999
    assert pipe.run_due_retries() == 0


def test_draft_failure_resumes_without_repeating_triage(env):
    ai = fakes.FakeAI(draft_responses=[AITransientError("Gemini API error 500")])
    pipe, store, tg, ai, clock = env(ai=ai)
    pipe.handle_update(post(1, 10, text="note"))
    clock.t += 3600
    pipe.run_due_retries()
    assert store.get_note(1)["status"] == Status.DELIVERED
    assert ai.calls == {"transcribe": 0, "triage": 1, "pick_news": 1, "draft": 2}


def test_manual_retry_command_gives_one_more_attempt(env):
    ai = fakes.FakeAI(triage_responses=[AIPermanentError("400 bad request")])
    pipe, store, tg, ai, _ = env(ai=ai)
    pipe.handle_update(post(1, 10, text="note"))
    assert store.get_note(1)["status"] == Status.FAILED
    assert pipe.handle_update(post(2, 11, text="/retry 1")) == "command_retry"
    assert store.get_note(1)["status"] == Status.DELIVERED


def test_news_unavailable_still_drafts_without_hook(env):
    def broken_get(*a, **k):
        raise ConnectionError("SAMPLE: feed down")
    news = lambda terms, **kw: fetch_headlines(terms, http_get=broken_get, **kw)
    draft = dict(fakes.SAMPLE_DRAFT, used_news_ids=[0])
    pipe, store, tg, *_ = env(news=news, ai=fakes.FakeAI(draft_responses=[draft]))
    pipe.handle_update(post(1, 10, text="note"))
    note = store.get_note(1)
    assert note["status"] == Status.DELIVERED
    assert note["draft_json"]["used_news_ids"] == []      # can't cite a headline that doesn't exist
    assert "Google News unavailable" in tg.sent[-1]["text"]


def test_telegram_send_failure_keeps_draft_for_redelivery(env):
    tg = fakes.FakeTelegram(CHAT)
    pipe, store, tg, ai, clock = env(tg=tg)
    tg.fail_send = True
    pipe.handle_update(post(1, 10, text="note"))
    note = store.get_note(1)
    assert note["status"] == Status.RETRY_PENDING and note["draft_json"]
    tg.fail_send = False
    clock.t += 3600
    pipe.run_due_retries()
    assert store.get_note(1)["status"] == Status.DELIVERED
    assert ai.calls["draft"] == 1                          # redelivery didn't re-pay


def test_crash_recovery_resumes_interrupted_notes(env, tmp_path):
    db = tmp_path / "crash.db"
    pipe, store, *_ = env(db=db)
    store.insert_note(chat_id=CHAT, message_id=10, kind="text", original_text="note", status=Status.PROCESSING)
    pipe.recover_interrupted()
    pipe.run_due_retries()
    assert store.get_note(1)["status"] == Status.DELIVERED


# ---------------------------------------------------------------- validation + formatting
def test_parse_json_rejects_garbage():
    with pytest.raises(MalformedAIResponse):
        parse_json("Sure! Here's your post")
    with pytest.raises(MalformedAIResponse):
        parse_json("")
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_validate_triage_requires_score_in_range():
    with pytest.raises(MalformedAIResponse):
        validate_triage(dict(fakes.SAMPLE_TRIAGE_HIGH, score=14))
    t = validate_triage({"score": "7", "reason": "ok"})
    assert t["risk_flags"] == {"unsupported_claims": [], "private_customer_info": []}


def test_long_draft_is_split_under_telegram_limit(env):
    long_post = "\n\n".join(["A reasonably long paragraph about formulation choices. " * 12] * 20)
    pipe, store, tg, *_ = env(ai=fakes.FakeAI(draft_responses=[dict(fakes.SAMPLE_DRAFT, post=long_post)]))
    pipe.handle_update(post(1, 10, text="note"))
    parts = [m["text"] for m in tg.sent if "(draft part" in m["text"]]
    assert len(parts) >= 3 and all(len(p) <= 4096 for p in parts)


def test_split_handles_single_huge_line():
    chunks = formatter.split_text("word " * 3000)
    assert all(len(c) <= formatter.SAFE_LIMIT for c in chunks) and len(chunks) >= 3


@pytest.mark.parametrize("cmd", ["/draft 1", "/draft1", "/Draft #1", "/draft 1.", "/draft note 1", "/draft@SomeBot 1"])
def test_draft_command_variants(env, cmd):
    ai = fakes.FakeAI(triage_responses=[fakes.SAMPLE_TRIAGE_LOW])
    pipe, store, *_ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_WEAK_NOTE))
    assert pipe.handle_update(post(2, 11, text=cmd)) == "command_draft"


def test_unknown_command_gets_help_reply(env):
    pipe, store, tg, *_ = env()
    assert pipe.handle_update(post(1, 10, text="/drafts please")) == "ignored_unknown_command"
    assert "commands" in tg.sent[-1]["text"] and store.list_notes() == []
    assert pipe.handle_update(post(2, 11, text="/draft")) == "command_draft_missing_number"


# ---------------------------------------------------------------- relevance and related news
def test_off_topic_post_scores_zero_without_news_or_draft(env):
    fetched = []
    news = lambda terms, **kw: fetched.append(terms) or ([], [])
    ai = fakes.FakeAI(triage_responses=[fakes.SAMPLE_TRIAGE_OFF_TOPIC])
    pipe, store, tg, ai, _ = env(ai=ai, news=news)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_OFF_TOPIC_NOTE))
    note = store.get_note(1)
    assert note["status"] == Status.HELD
    assert note["triage_json"]["score"] == 0 and note["triage_json"]["relevant"] is False
    assert fetched == [] and ai.calls["pick_news"] == 0 and ai.calls["draft"] == 0
    assert len(tg.sent) == 1 and "not related" in tg.sent[0]["text"] and "0/10" in tg.sent[0]["text"]


def test_off_topic_note_can_still_be_forced_to_draft(env):
    ai = fakes.FakeAI(triage_responses=[fakes.SAMPLE_TRIAGE_OFF_TOPIC])
    pipe, store, *_ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_OFF_TOPIC_NOTE))
    pipe.handle_update(post(2, 11, text="/draft 1"))
    assert store.get_note(1)["status"] == Status.DELIVERED


def test_triage_without_relevant_field_counts_as_relevant():
    t = validate_triage(dict(fakes.SAMPLE_TRIAGE_HIGH))
    assert t["relevant"] is True and t["score"] == 8


def test_only_headlines_judged_related_are_shown(env):
    headlines = [dict(fakes.SAMPLE_NEWS[0], title=f"[SAMPLE HEADLINE] {i}", link=f"https://example.com/{i}")
                 for i in range(4)]
    picks = [{"relevant": [{"id": 2, "why": "[MOCK] closest"}, {"id": 0, "why": "[MOCK] related"}, {"id": 9, "why": "bad"}]}]
    draft = dict(fakes.SAMPLE_DRAFT, used_news_ids=[1])  # id 1 = second *shown* headline
    ai = fakes.FakeAI(news_picks=picks, draft_responses=[draft])
    pipe, store, tg, *_ = env(ai=ai, news=fakes.fake_news(headlines))
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    news_msg = next(m["text"] for m in tg.sent if m["text"].startswith("📰"))
    assert "example.com/2" in news_msg and "example.com/0" in news_msg
    assert "example.com/1" not in news_msg and "example.com/3" not in news_msg
    assert news_msg.index("example.com/2") < news_msg.index("example.com/0")  # most relevant first
    assert "[SAMPLE HEADLINE] 0 (Sample Trade Press, 2026-09-20) · used in draft" in news_msg


def test_no_related_headlines_says_so(env):
    ai = fakes.FakeAI(news_picks=[{"relevant": []}], draft_responses=[dict(fakes.SAMPLE_DRAFT, used_news_ids=[0])])
    pipe, store, tg, *_ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    news_msg = next(m["text"] for m in tg.sent if m["text"].startswith("📰"))
    assert "No closely related news found from the last 7 days" in news_msg
    assert store.get_note(1)["draft_json"]["used_news_ids"] == []  # draft only saw related headlines


def test_news_relevance_failure_never_blocks_the_draft(env):
    ai = fakes.FakeAI(news_picks=[AIPermanentError("Gemini API error 400")])
    pipe, store, tg, *_ = env(ai=ai)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    assert store.get_note(1)["status"] == Status.DELIVERED
    news_msg = next(m["text"] for m in tg.sent if m["text"].startswith("📰"))
    assert "Couldn't check which headlines are related" in news_msg


def test_news_is_searched_over_the_last_7_days(env):
    seen = {}
    def news(terms, max_items, lookback_days):
        seen.update(max_items=max_items, lookback_days=lookback_days)
        return [], []
    pipe, *_ = env(news=news)
    pipe.handle_update(post(1, 10, text=fakes.SAMPLE_TEXT_NOTE))
    assert seen == {"max_items": 15, "lookback_days": 7}
