"""Command line entry point: python -m still_meera <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import Config, ConfigError


def cmd_run(cfg: Config) -> int:
    from .db import Store
    from .gemini import GeminiClient
    from .pipeline import Pipeline
    from .telegram_api import TelegramClient, TelegramError

    missing = cfg.missing_live_settings()
    if missing:
        print("Cannot start live mode. Missing in .env: " + ", ".join(missing))
        print("Try the offline demo instead:  python -m still_meera demo")
        return 2

    tg = TelegramClient(cfg.telegram_bot_token)
    try:
        me = tg.get_me()
    except TelegramError as exc:
        print(f"Telegram check failed: {exc}")
        return 1
    store = Store(cfg.db_path)
    pipe = Pipeline(cfg, store, tg, GeminiClient(cfg.gemini_api_key, cfg.gemini_model))
    pipe.recover_interrupted()
    print(f"Still Meera running as @{me.get('username')} · watching chat {cfg.telegram_chat_id} · "
          f"model {cfg.gemini_model} · threshold {cfg.triage_threshold:g}. Ctrl+C to stop.")

    offset = store.get_offset()
    backoff = 5
    try:
        while True:
            pipe.run_due_retries()
            try:
                updates = tg.get_updates(offset, timeout=30)
                backoff = 5
            except TelegramError as exc:
                logging.warning("%s (retrying in %ss)", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            for update in updates:
                outcome = pipe.handle_update(update)
                logging.info("update %s → %s", update.get("update_id"), outcome)
                offset = update["update_id"] + 1
                store.set_offset(offset)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        store.close()
    return 0


def cmd_find_chat(cfg: Config) -> int:
    """Print channel IDs from recent updates (does not acknowledge them)."""
    from .telegram_api import TelegramClient, TelegramError
    if not cfg.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.")
        return 2
    try:
        updates = TelegramClient(cfg.telegram_bot_token).get_updates(None, timeout=0)
    except TelegramError as exc:
        print(exc)
        return 1
    chats = {}
    for u in updates:
        chat = (u.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title", "")
    if not chats:
        print("No channel posts seen yet. Make the bot an admin of the channel, post any message there, then rerun.")
        return 1
    for cid, title in chats.items():
        print(f"{cid}  {title}")
    print("Copy the right number into TELEGRAM_CHAT_ID in .env.")
    return 0


def cmd_notes(cfg: Config, status: str | None) -> int:
    from .db import Store
    store = Store(cfg.db_path)
    for n in store.list_notes(status):
        t = n.get("triage_json") if isinstance(n.get("triage_json"), dict) else {}
        preview = (n.get("original_text") or n.get("transcript") or "(voice, not transcribed)")[:60]
        print(f"#{n['id']:<4} {n['kind']:5} {n['status']:13} score={t.get('score', '-')!s:4} "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(n['created_at']))}  {preview!r}")
    return 0


def cmd_show(cfg: Config, note_id: int) -> int:
    from .db import Store
    note = Store(cfg.db_path).get_note(note_id)
    if not note:
        print(f"No note #{note_id}")
        return 1
    print(json.dumps(note, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="still_meera", description="Still Meera: Telegram → LinkedIn draft assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Start the live bot (Telegram long polling)")
    sub.add_parser("demo", help="Offline demo with sample inputs and mocked APIs")
    sub.add_parser("check", help="Show which settings are present (never prints secrets)")
    sub.add_parser("find-chat", help="List channel IDs the bot can see (to fill TELEGRAM_CHAT_ID)")
    p_notes = sub.add_parser("notes", help="List stored notes")
    p_notes.add_argument("--status", help="held | delivered | failed | retry_pending")
    p_show = sub.add_parser("show", help="Show one stored note, including its draft")
    p_show.add_argument("note_id", type=int)
    p_voice = sub.add_parser("extract-voice", help="Extract the published samples (.txt or .pdf) into voice/references.json")
    p_voice.add_argument("pdf_path", metavar="source_path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "urllib3"):  # request logs could include URLs
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "demo":
        from .demo import run_demo
        run_demo()
        return 0
    if args.command == "extract-voice":
        from .voice_extract import extract
        return extract(args.pdf_path)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    if args.command == "check":
        print(cfg.describe())
        missing = cfg.missing_live_settings()
        print("\nReady for live mode." if not missing else "\nStill missing: " + ", ".join(missing))
        return 0
    if args.command == "find-chat":
        return cmd_find_chat(cfg)
    if args.command == "notes":
        return cmd_notes(cfg, args.status)
    if args.command == "show":
        return cmd_show(cfg, args.note_id)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
