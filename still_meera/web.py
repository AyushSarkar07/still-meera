"""HTTP entry point for serverless hosting (Vercel).

Replaces the long-polling loop:
- Telegram POSTs each channel post to WEBHOOK_PATH (set once with `python -m still_meera set-webhook`).
- Vercel Cron calls CRON_PATH to requeue interrupted notes and run due retries.
Each request opens its own database connection; nothing is kept between requests.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import contextmanager
from typing import Callable, ContextManager

from flask import Flask, jsonify, request

from .config import CRON_PATH, WEBHOOK_PATH, Config

log = logging.getLogger(__name__)


@contextmanager
def live_pipeline(cfg: Config):
    from .db import Store
    from .gemini import GeminiClient
    from .pipeline import Pipeline
    from .telegram_api import TelegramClient

    store = Store(cfg.store_target)
    try:
        yield Pipeline(cfg, store, TelegramClient(cfg.telegram_bot_token),
                       GeminiClient(cfg.gemini_api_key, cfg.gemini_model))
    finally:
        store.close()


def _secret_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def create_app(config_loader: Callable[[], Config] = Config.from_env,
               pipeline_factory: Callable[[Config], ContextManager] = live_pipeline) -> Flask:
    app = Flask(__name__)

    def missing_settings(cfg: Config) -> list[str]:
        # Serverless disks are temporary, so a real database is required here.
        return cfg.missing_live_settings() + ([] if cfg.database_url else ["DATABASE_URL"])

    @app.get("/")
    def health():
        return jsonify(ok=True, service="still-meera")

    @app.post(WEBHOOK_PATH)
    def telegram_webhook():
        cfg = config_loader()
        if not cfg.webhook_secret:
            log.error("TELEGRAM_WEBHOOK_SECRET is not set; refusing unauthenticated webhook calls")
            return jsonify(ok=False, error="not configured"), 503
        if not _secret_matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), cfg.webhook_secret):
            return jsonify(ok=False, error="forbidden"), 403
        missing = missing_settings(cfg)
        if missing:
            # Non-2xx makes Telegram keep the update and retry, so nothing is lost while you fix it.
            log.error("Missing settings: %s", ", ".join(missing))
            return jsonify(ok=False, error="not configured"), 503

        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            return jsonify(ok=True, outcome="ignored_malformed")
        with pipeline_factory(cfg) as pipe:
            outcome = pipe.handle_update(update)
            log.info("update %s → %s", update.get("update_id"), outcome)
            try:
                pipe.housekeeping()
            except Exception:  # the update itself is done; don't make Telegram resend it
                log.exception("Housekeeping after update failed")
        return jsonify(ok=True, outcome=outcome)

    @app.get(CRON_PATH)
    def cron_housekeeping():
        cfg = config_loader()
        if not cfg.cron_secret:
            log.error("CRON_SECRET is not set; refusing unauthenticated cron calls")
            return jsonify(ok=False, error="not configured"), 503
        if not _secret_matches(request.headers.get("Authorization", ""), f"Bearer {cfg.cron_secret}"):
            return jsonify(ok=False, error="forbidden"), 403
        missing = missing_settings(cfg)
        if missing:
            log.error("Missing settings: %s", ", ".join(missing))
            return jsonify(ok=False, error="not configured"), 503
        with pipeline_factory(cfg) as pipe:
            ran = pipe.housekeeping(retry_limit=10)
        return jsonify(ok=True, retries_run=ran)

    return app
