"""Offline demo: runs the real pipeline against mocked Telegram, Gemini and news.

No network calls, no credentials, no charges. All inputs and outputs are labelled SAMPLE/MOCK.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import fakes
from .config import Config
from .db import Store
from .pipeline import Pipeline

DEMO_CHAT = -1000000000001


def _post(update_id: int, message_id: int, **content) -> dict:
    return {"update_id": update_id,
            "channel_post": {"message_id": message_id, "chat": {"id": DEMO_CHAT, "type": "channel"}, **content}}


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(telegram_chat_id=DEMO_CHAT, review_chat_id=DEMO_CHAT, triage_threshold=6,
                     db_path=Path(tmp) / "demo.db", gemini_model="(mock)")
        store = Store(cfg.db_path)
        tg = fakes.FakeTelegram(DEMO_CHAT)
        ai = fakes.FakeAI(
            triage_responses=[fakes.SAMPLE_TRIAGE_HIGH, fakes.SAMPLE_TRIAGE_LOW,
                              fakes.SAMPLE_TRIAGE_VOICE, {"oops": "not a triage"}, {"still": "bad"}],
            draft_responses=[fakes.SAMPLE_DRAFT, fakes.SAMPLE_VOICE_DRAFT],
        )
        pipe = Pipeline(cfg, store, tg, ai, news_fetcher=fakes.fake_news())

        scenarios = [
            ("SAMPLE 1: strong text note → draft with news source",
             _post(1, 101, text=fakes.SAMPLE_TEXT_NOTE)),
            ("SAMPLE 2: vague text note → held (below 6/10)",
             _post(2, 102, text=fakes.SAMPLE_WEAK_NOTE)),
            ("SAMPLE 3: voice note → mock transcription → draft",
             _post(3, 103, voice={"file_id": "SAMPLE_FILE_ID", "mime_type": "audio/ogg", "duration": 14})),
            ("SAMPLE 4: same update delivered again (e.g. after restart) → ignored",
             _post(3, 103, voice={"file_id": "SAMPLE_FILE_ID", "mime_type": "audio/ogg"})),
            ("SAMPLE 5: post from a different chat → ignored",
             {"update_id": 5, "channel_post": {"message_id": 1, "chat": {"id": -1009999}, "text": "hi"}}),
            ("SAMPLE 6: malformed AI response twice → stops after MAX_ATTEMPTS, note kept",
             _post(6, 106, text="[SAMPLE NOTE] Retinol and vitamin C together: a myth worth unpacking.")),
        ]
        for title, update in scenarios:
            before = len(tg.sent)
            outcome = pipe.handle_update(update)
            if title.startswith("SAMPLE 6"):
                store.update_note(4, next_retry_at=0)  # skip the wait in the demo
                pipe.run_due_retries()
            print("=" * 72)
            print(f"{title}\n  outcome: {outcome}")
            for msg in tg.sent[before:]:
                print("-" * 72)
                print(msg["text"])
        print("=" * 72)
        # Bot output echoed back as a channel post must not trigger a draft.
        echoed = tg.sent[1]
        loop = pipe.handle_update(_post(7, echoed["message_id"], text=echoed["text"]))
        print(f"SAMPLE 7: bot's own draft echoed back as channel post → {loop}")
        print("=" * 72)
        print("Stored notes (SQLite):")
        for n in reversed(store.list_notes()):
            score = (n.get("triage_json") or {}).get("score") if isinstance(n.get("triage_json"), dict) else None
            print(f"  #{n['id']} {n['kind']:5} status={n['status']:13} score={score} attempts={n['attempts']}"
                  + (f" error={n['last_error']}" if n["last_error"] else ""))
        print(f"Mock Gemini calls: {ai.calls}")
        store.close()


if __name__ == "__main__":
    run_demo()
