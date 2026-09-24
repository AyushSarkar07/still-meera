"""Offline tests for the serverless entry point (webhook + cron) and housekeeping."""
import time
from contextlib import contextmanager

import pytest

from still_meera import fakes
from still_meera.config import CRON_PATH, WEBHOOK_PATH, Config
from still_meera.db import Store
from still_meera.pipeline import Pipeline, Status
from still_meera.web import create_app

CHAT = -1001234567890
SECRET = "hook-secret_123"


def post(update_id, message_id, **content):
    return {"update_id": update_id,
            "channel_post": {"message_id": message_id, "chat": {"id": CHAT, "type": "channel"}, **content}}


@pytest.fixture
def web(tmp_path):
    db = tmp_path / "w.db"
    tg = fakes.FakeTelegram(CHAT)

    def make(**overrides):
        settings = dict(telegram_bot_token="123:abc", telegram_chat_id=CHAT, review_chat_id=CHAT,
                        gemini_api_key="k", gemini_model="m", database_url="postgres://test",
                        webhook_secret=SECRET, cron_secret="cron-secret", db_path=db)
        settings.update(overrides)
        cfg = Config(**settings)

        @contextmanager
        def factory(c):
            store = Store(db)  # SQLite stands in for Postgres; same SQL
            try:
                yield Pipeline(c, store, tg, fakes.FakeAI(), news_fetcher=fakes.fake_news())
            finally:
                store.close()

        return create_app(config_loader=lambda: cfg, pipeline_factory=factory).test_client()

    make.tg, make.db = tg, db
    return make


def test_health(web):
    assert web().get("/").get_json() == {"ok": True, "service": "still-meera"}


def test_webhook_processes_update_with_correct_secret(web):
    resp = web().post(WEBHOOK_PATH, json=post(1, 10, text=fakes.SAMPLE_TEXT_NOTE),
                      headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.status_code == 200 and resp.get_json()["outcome"] == "processed"
    assert Store(web.db).get_note(1)["status"] == Status.DELIVERED
    assert web.tg.sent


def test_webhook_rejects_wrong_or_missing_secret(web):
    client = web()
    assert client.post(WEBHOOK_PATH, json=post(1, 10, text="x")).status_code == 403
    assert client.post(WEBHOOK_PATH, json=post(1, 10, text="x"),
                       headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert not web.tg.sent


def test_webhook_refuses_when_secret_not_configured(web):
    assert web(webhook_secret="").post(WEBHOOK_PATH, json=post(1, 10, text="x")).status_code == 503


def test_webhook_requires_database_url(web):
    resp = web(database_url="").post(WEBHOOK_PATH, json=post(1, 10, text="x"),
                                     headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.status_code == 503  # Telegram keeps the update and retries


def test_telegram_redelivery_is_deduplicated(web):
    client = web()
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    client.post(WEBHOOK_PATH, json=post(1, 10, text=fakes.SAMPLE_TEXT_NOTE), headers=headers)
    sent = len(web.tg.sent)
    resp = client.post(WEBHOOK_PATH, json=post(1, 10, text=fakes.SAMPLE_TEXT_NOTE), headers=headers)
    assert resp.get_json()["outcome"] == "duplicate_update" and len(web.tg.sent) == sent


def test_cron_requires_bearer_secret(web):
    client = web()
    assert client.get(CRON_PATH).status_code == 403
    assert client.get(CRON_PATH, headers={"Authorization": "Bearer nope"}).status_code == 403
    resp = client.get(CRON_PATH, headers={"Authorization": "Bearer cron-secret"})
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "retries_run": 0}


def test_housekeeping_requeues_only_stale_stuck_notes_and_retries_them(tmp_path):
    store = Store(tmp_path / "h.db")
    cfg = Config(telegram_chat_id=CHAT, review_chat_id=CHAT)
    pipe = Pipeline(cfg, store, fakes.FakeTelegram(CHAT), fakes.FakeAI(), news_fetcher=fakes.fake_news())
    stale = store.insert_note(chat_id=CHAT, message_id=1, kind="text", original_text=fakes.SAMPLE_TEXT_NOTE,
                              status=Status.PROCESSING)
    live = store.insert_note(chat_id=CHAT, message_id=2, kind="text", original_text="in flight",
                             status=Status.PROCESSING)
    store._exec("UPDATE notes SET updated_at=? WHERE id=?", (time.time() - 3600, stale))

    assert pipe.housekeeping() == 1
    assert store.get_note(stale)["status"] == Status.DELIVERED
    assert store.get_note(live)["status"] == Status.PROCESSING  # a live request still owns it


def test_due_retry_is_claimed_once(tmp_path):
    store = Store(tmp_path / "c.db")
    note = store.insert_note(chat_id=CHAT, message_id=1, kind="text", original_text="x",
                             status=Status.RETRY_PENDING)
    assert store.claim_note(note, Status.RETRY_PENDING, Status.PROCESSING)
    assert not store.claim_note(note, Status.RETRY_PENDING, Status.PROCESSING)


def test_store_target_prefers_database_url(tmp_path):
    assert Config(db_path=tmp_path / "x.db").store_target == tmp_path / "x.db"
    assert Config(db_path=tmp_path / "x.db", database_url="postgres://h/db").store_target == "postgres://h/db"
