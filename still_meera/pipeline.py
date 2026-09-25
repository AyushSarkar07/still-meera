"""Core workflow: route an update, then transcribe → triage → news → draft → deliver.

Off-topic posts stop after triage (score 0). Every relevant post gets a related-news
message, whether it is drafted or held.

Every stage stores its output on the note row before moving on, so a retry resumes
from the failed stage and never pays twice for work that already succeeded.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

from . import formatter
from .config import Config
from .db import Store
from .gemini import AIClient, AIError
from .news import fetch_headlines
from .telegram_api import TelegramError

log = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 60
STUCK_AFTER_SECONDS = 600  # longer than the serverless function time limit
COMMAND_RE = re.compile(r"^/(retry|draft|status)(?:@\w+)?\s*(?:note\s*)?#?\s*(\d+)?\s*[.!]?\s*$", re.IGNORECASE)
HELP_TEXT = ("ℹ️ Still Meera · commands: /draft N (draft held note N), /retry N (retry failed note N), "
             "/status. Anything not starting with / is treated as a new note.")


class Status:
    RECEIVED = "received"
    PROCESSING = "processing"
    HELD = "held"
    DELIVERED = "delivered"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"


class Pipeline:
    def __init__(self, cfg: Config, store: Store, telegram, ai: AIClient,
                 news_fetcher: Callable = fetch_headlines, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.telegram = telegram
        self.ai = ai
        self.news_fetcher = news_fetcher
        self.clock = clock

    # ------------------------------------------------------------------ routing
    def handle_update(self, update: dict) -> str:
        """Returns a short outcome label (useful for logs and tests)."""
        update_id = update.get("update_id")
        if update_id is None:
            return "ignored_malformed"
        if not self.store.claim_update(update_id):
            return "duplicate_update"

        post = update.get("channel_post")
        if not post:
            return "ignored_not_channel_post"
        chat_id = (post.get("chat") or {}).get("id")
        if chat_id != self.cfg.telegram_chat_id:
            log.warning("Ignored post from unconfigured chat %s", chat_id)
            return "ignored_wrong_chat"
        if self._is_bot_output(post):
            return "ignored_bot_output"

        text = post.get("text")
        if text:
            m = COMMAND_RE.match(text.strip())
            if m:
                return self._handle_command(m.group(1).lower(), m.group(2))
            if text.strip().startswith("/"):
                self._send(None, HELP_TEXT)
                return "ignored_unknown_command"

        voice = post.get("voice")
        if voice:
            fields = dict(kind="voice", voice_file_id=voice["file_id"],
                          voice_mime=voice.get("mime_type") or "audio/ogg",
                          original_text=post.get("caption"))
        elif text and text.strip():
            fields = dict(kind="text", original_text=text.strip())
        else:
            return "ignored_unsupported"

        note_id = self.store.insert_note(chat_id=chat_id, message_id=post["message_id"],
                                         update_id=update_id, **fields)
        if note_id is None:
            return "duplicate_message"
        self.process_note(note_id)
        return "processed"

    def _is_bot_output(self, post: dict) -> bool:
        chat_id = post["chat"]["id"]
        if self.store.is_bot_message(chat_id, post.get("message_id", -1)):
            return True
        if (post.get("from") or {}).get("is_bot") or post.get("via_bot"):
            return True
        origin = post.get("forward_origin") or {}
        if (origin.get("sender_user") or {}).get("is_bot") or (post.get("forward_from") or {}).get("is_bot"):
            return True
        text = (post.get("text") or "").strip()
        if text.startswith(("📝 Still Meera", "⏸ Still Meera", "⚠️ Still Meera", "🔗 Sources for note", "📰 Related news",
                            "✅ Check before publishing", "ℹ️ Still Meera")):
            return True
        # A draft reposted verbatim should not become a new note.
        if text and self._matches_existing_draft(text):
            return True
        return False

    def _matches_existing_draft(self, text: str) -> bool:
        return any(post.strip() == text for post in self.store.draft_posts())

    # ------------------------------------------------------------------ commands
    def _handle_command(self, cmd: str, arg: str | None) -> str:
        if cmd == "status":
            counts = self.store.status_counts()
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no notes yet"
            self._send(None, f"ℹ️ Still Meera status · {summary} · threshold {self.cfg.triage_threshold:g}")
            return "command_status"
        if not arg:
            self._send(None, f"ℹ️ Still Meera · add the note number, e.g. /{cmd} 3")
            return f"command_{cmd}_missing_number"
        note = self.store.get_note(int(arg))
        if not note or note["chat_id"] != self.cfg.telegram_chat_id:
            self._send(None, f"ℹ️ Still Meera · no note #{arg} found.")
            return f"command_{cmd}_missing"
        if cmd == "retry":
            if note["status"] not in (Status.FAILED, Status.RETRY_PENDING):
                self._send(note["id"], f"ℹ️ Still Meera · note #{note['id']} is '{note['status']}', nothing to retry.")
                return "command_retry_noop"
            # One manual extra attempt, resuming from the failed stage.
            self.store.update_note(note["id"], attempts=max(0, self.cfg.max_attempts - 1),
                                   status=Status.RECEIVED, next_retry_at=None)
            self.process_note(note["id"])
            return "command_retry"
        if cmd == "draft":
            if note["status"] != Status.HELD:
                self._send(note["id"], f"ℹ️ Still Meera · note #{note['id']} is '{note['status']}', not held.")
                return "command_draft_noop"
            self.process_note(note["id"], force=True)
            return "command_draft"
        return "ignored_unknown_command"

    # ------------------------------------------------------------------ stages
    def process_note(self, note_id: int, force: bool = False) -> str:
        note = self.store.get_note(note_id)
        self.store.update_note(note_id, status=Status.PROCESSING)
        stage = "start"
        try:
            # 1. voice → text
            if note["kind"] == "voice" and not note.get("transcript"):
                stage = "download"
                audio = self.telegram.download_file(note["voice_file_id"])
                stage = "transcribe"
                transcript = self.ai.transcribe(audio, note["voice_mime"] or "audio/ogg")
                self.store.update_note(note_id, transcript=transcript)
                note["transcript"] = transcript
            text = self.note_text(note)

            # 2. triage
            triage = note.get("triage_json")
            if not isinstance(triage, dict):
                stage = "triage"
                triage = self.ai.triage(text)
                self.store.update_note(note_id, triage_json=triage)
            qualifies = triage["score"] >= self.cfg.triage_threshold
            triage["decision"] = "develop" if qualifies else "hold"
            self.store.update_note(note_id, triage_json=triage)
            if not triage.get("relevant", True) and not force:
                # Not about her field at all ("hello"): no news search, no draft.
                self.store.update_note(note_id, status=Status.HELD, last_error=None, stage=None)
                stage = "deliver"
                self._send(note_id, formatter.off_topic_message(note_id, triage, note["kind"]))
                return Status.HELD

            # 3. related news for every relevant note (optional context; never blocks drafting)
            news_state = note.get("news_json")
            if not isinstance(news_state, dict):
                news_state = self._find_news(text, triage)
                self.store.update_note(note_id, news_json=news_state)

            if not qualifies and not force:
                self.store.update_note(note_id, status=Status.HELD, last_error=None, stage=None)
                stage = "deliver"
                self._send(note_id, formatter.held_message(note_id, triage, self.cfg.triage_threshold, note["kind"]))
                self._send_news(note_id, news_state, [])
                return Status.HELD

            # 4. draft
            draft = note.get("draft_json")
            if not isinstance(draft, dict):
                stage = "draft"
                draft = self.ai.draft(text, triage, news_state["items"])
                self.store.update_note(note_id, draft_json=draft)

            # 5. deliver
            stage = "deliver"
            self._deliver(note_id, note["kind"], triage, draft, news_state)
            self.store.update_note(note_id, status=Status.DELIVERED, last_error=None, stage=None)
            return Status.DELIVERED

        except (AIError, TelegramError) as exc:
            return self._record_failure(note_id, stage, exc)
        except Exception as exc:  # unexpected bug: keep the note, don't loop
            log.exception("Unexpected error on note %s", note_id)
            return self._record_failure(note_id, stage, exc, force_permanent=True)

    def _find_news(self, text: str, triage: dict) -> dict:
        """Fetch recent headlines, then keep only those Gemini judges related to the note."""
        terms = triage.get("news_search_terms", [])
        if not terms:
            return {"items": [], "warnings": ["No search terms suggested, so no news lookup."]}
        candidates, warnings = self.news_fetcher(terms, max_items=self.cfg.news_candidates,
                                                 lookback_days=self.cfg.news_lookback_days)
        if not candidates:
            return {"items": [], "warnings": warnings, "candidates": 0}
        try:
            picks = self.ai.pick_news(text, candidates)[: self.cfg.news_max_items]
        except AIError as exc:
            log.warning("News relevance check failed: %s", exc)
            return {"items": [], "candidates": len(candidates),
                    "warnings": warnings + ["Couldn't check which headlines are related, so none are shown."]}
        items = [dict(candidates[p["id"]], why=p["why"]) for p in picks]
        return {"items": items, "warnings": warnings, "candidates": len(candidates)}

    def _send_news(self, note_id: int, news_state: dict, used_ids: list[int]) -> None:
        msg = formatter.news_message(note_id, news_state, used_ids, self.cfg.news_lookback_days)
        for chunk in formatter.split_text(msg):
            self._send(note_id, chunk)

    def _deliver(self, note_id: int, kind: str, triage: dict, draft: dict, news_state: dict) -> None:
        self._send(note_id, formatter.score_message(note_id, triage, self.cfg.triage_threshold, kind))
        chunks = formatter.label_parts(formatter.split_text(draft["post"]), "draft")
        for chunk in chunks:
            self._send(note_id, chunk)
        self._send_news(note_id, news_state, draft["used_news_ids"])
        check = formatter.check_message(note_id, triage, draft, news_state.get("warnings", []),
                                        news_used=bool(draft["used_news_ids"]))
        for chunk in formatter.split_text(check):
            self._send(note_id, chunk)

    def _record_failure(self, note_id: int, stage: str, exc: Exception, force_permanent: bool = False) -> str:
        note = self.store.get_note(note_id)
        attempts = note["attempts"] + 1
        transient = getattr(exc, "transient", isinstance(exc, TelegramError)) and not force_permanent
        will_retry = transient and attempts < self.cfg.max_attempts
        error = str(exc) or type(exc).__name__
        status = Status.RETRY_PENDING if will_retry else Status.FAILED
        self.store.update_note(
            note_id, attempts=attempts, status=status, stage=stage, last_error=error[:500],
            next_retry_at=self.clock() + RETRY_BASE_SECONDS * (2 ** (attempts - 1)) if will_retry else None,
        )
        log.warning("Note %s %s failed (%s/%s): %s", note_id, stage, attempts, self.cfg.max_attempts, error)
        if stage != "deliver":  # if Telegram sending is down, a notice would fail too
            try:
                self._send(note_id, formatter.failure_message(note_id, stage, error, attempts,
                                                              self.cfg.max_attempts, will_retry))
            except TelegramError:
                log.warning("Could not send failure notice for note %s", note_id)
        return status

    # ------------------------------------------------------------------ helpers
    def run_due_retries(self, limit: int | None = None) -> int:
        ran = 0
        for note in self.store.due_retries(self.clock())[:limit]:
            # Claim first: on serverless, two requests may look at the same due note.
            if self.store.claim_note(note["id"], Status.RETRY_PENDING, Status.PROCESSING):
                self.process_note(note["id"])
                ran += 1
        return ran

    def recover_interrupted(self) -> None:
        """Notes left mid-flight by a crash resume on next start (no attempt is charged)."""
        self.store.requeue_interrupted()

    def housekeeping(self, retry_limit: int = 3) -> int:
        """Serverless replacement for the polling loop's background work.

        Notes whose request was killed mid-flight (timeout, crash) are requeued once they
        have been idle for STUCK_AFTER_SECONDS, then due retries run (a few per call, to
        stay within the function time limit).
        """
        self.store.requeue_interrupted(updated_before=self.clock() - STUCK_AFTER_SECONDS)
        return self.run_due_retries(limit=retry_limit)

    @staticmethod
    def note_text(note: dict) -> str:
        if note["kind"] == "voice":
            caption = note.get("original_text")
            return note["transcript"] + (f"\n\n(Caption: {caption})" if caption else "")
        return note["original_text"]

    def _send(self, note_id: int | None, text: str) -> None:
        chat_id = self.cfg.review_chat_id
        msg = self.telegram.send_message(chat_id, text)
        self.store.record_bot_message(msg["chat"]["id"], msg["message_id"], note_id)

