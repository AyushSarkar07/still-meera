"""Vercel entry point: Vercel serves the `app` object defined here."""
import logging

from still_meera.web import create_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
for noisy in ("httpx", "urllib3"):  # request logs could include URLs with the bot token
    logging.getLogger(noisy).setLevel(logging.WARNING)

app = create_app()
