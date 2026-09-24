"""Configuration loaded from environment / local .env file.

Secrets are never printed. Use `describe()` for a safe summary.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Product assumption: 6/10 is a starting point, not a validated cut-off.
# Tune TRIAGE_THRESHOLD in .env after reviewing a few weeks of held notes.
DEFAULT_THRESHOLD = 6.0


class ConfigError(Exception):
    pass


@dataclass
class Config:
    telegram_bot_token: str = ""
    telegram_chat_id: int | None = None
    review_chat_id: int | None = None
    gemini_api_key: str = ""
    gemini_model: str = ""
    triage_threshold: float = DEFAULT_THRESHOLD
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "still_meera.db")
    max_attempts: int = 2
    news_max_items: int = 5
    news_lookback_days: int = 30

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

        def _int_or_none(name: str) -> int | None:
            raw = os.getenv(name, "").strip()
            if not raw or "replace" in raw.lower():
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number like -1001234567890") from exc

        try:
            threshold = float(os.getenv("TRIAGE_THRESHOLD", "").strip() or DEFAULT_THRESHOLD)
        except ValueError as exc:
            raise ConfigError("TRIAGE_THRESHOLD must be a number between 0 and 10") from exc
        if not 0 <= threshold <= 10:
            raise ConfigError("TRIAGE_THRESHOLD must be between 0 and 10")

        chat_id = _int_or_none("TELEGRAM_CHAT_ID")
        db_raw = os.getenv("STILL_MEERA_DB", "").strip()
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=chat_id,
            review_chat_id=_int_or_none("REVIEW_CHAT_ID") or chat_id,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "").strip(),
            triage_threshold=threshold,
            db_path=Path(db_raw) if db_raw else PROJECT_ROOT / "data" / "still_meera.db",
            max_attempts=int(os.getenv("MAX_ATTEMPTS", "2") or 2),
        )

    def missing_live_settings(self) -> list[str]:
        missing = []
        if not self.telegram_bot_token or "replace" in self.telegram_bot_token.lower():
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.telegram_chat_id is None:
            missing.append("TELEGRAM_CHAT_ID")
        if not self.gemini_api_key or "replace" in self.gemini_api_key.lower():
            missing.append("GEMINI_API_KEY")
        if not self.gemini_model:
            missing.append("GEMINI_MODEL")
        return missing

    def describe(self) -> str:
        """Human-readable summary that never reveals secret values."""
        def present(v: str) -> str:
            return "set" if v and "replace" not in v.lower() else "MISSING"

        return "\n".join([
            f"TELEGRAM_BOT_TOKEN: {present(self.telegram_bot_token)}",
            f"TELEGRAM_CHAT_ID:   {self.telegram_chat_id if self.telegram_chat_id is not None else 'MISSING'}",
            f"REVIEW_CHAT_ID:     {self.review_chat_id if self.review_chat_id is not None else 'MISSING'}",
            f"GEMINI_API_KEY:     {present(self.gemini_api_key)}",
            f"GEMINI_MODEL:       {self.gemini_model or 'MISSING'}",
            f"TRIAGE_THRESHOLD:   {self.triage_threshold} (product assumption, tune over time)",
            f"MAX_ATTEMPTS:       {self.max_attempts}",
            f"Database:           {self.db_path}",
        ])
