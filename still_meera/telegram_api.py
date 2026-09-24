"""Minimal Telegram Bot API client (long polling or webhook). Uses plain HTTPS via requests."""
from __future__ import annotations

import re

import requests

API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096  # Telegram hard limit for sendMessage text


class TelegramError(Exception):
    pass


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<redacted-token>")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<redacted-token>", text)


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    def _call(self, method: str, http_timeout: float | None = None, **params):
        url = f"{API}/bot{self.token}/{method}"
        try:
            resp = self.session.post(url, json=params, timeout=http_timeout or self.timeout + 10)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(_redact(f"Telegram {method} request failed: {exc}", self.token)) from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method} error: {data.get('description', 'unknown error')}")
        return data["result"]

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["channel_post"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=timeout + 15, **params)

    def set_webhook(self, url: str, secret_token: str) -> None:
        self._call("setWebhook", url=url, secret_token=secret_token,
                   allowed_updates=["channel_post"], max_connections=5)

    def delete_webhook(self) -> None:
        self._call("deleteWebhook")

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")

    def send_message(self, chat_id: int, text: str) -> dict:
        # Plain text (no parse_mode) so drafts copy cleanly and never fail on markup.
        return self._call(
            "sendMessage", chat_id=chat_id, text=text,
            link_preview_options={"is_disabled": True},
        )

    def download_file(self, file_id: str) -> bytes:
        info = self._call("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise TelegramError("Telegram did not return a file path (file may exceed the 20 MB bot download limit)")
        try:
            resp = self.session.get(f"{API}/file/bot{self.token}/{path}", timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise TelegramError(_redact(f"Voice download failed: {exc}", self.token)) from None
        return resp.content
