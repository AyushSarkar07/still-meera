"""Google News RSS lookup. Returns headline metadata only (never article bodies)."""
from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "StillMeera/0.1 (personal content assistant)"


class NewsUnavailable(Exception):
    pass


def fetch_headlines(terms: list[str], max_items: int = 5, lookback_days: int = 30,
                    http_get=requests.get) -> tuple[list[dict], list[str]]:
    """Return (headlines, warnings). Never raises: news is optional context."""
    items: list[dict] = []
    warnings: list[str] = []
    seen_titles: set[str] = set()
    cutoff = time.time() - lookback_days * 86400

    for term in terms[:3]:
        query = f"{term} when:{lookback_days}d"
        try:
            resp = http_get(RSS_URL.format(q=quote_plus(query)), timeout=10,
                            headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            warnings.append(f"Google News unavailable for '{term}': {type(exc).__name__}")
            continue
        if getattr(feed, "bozo", False) and not feed.entries:
            warnings.append(f"Google News returned an unreadable feed for '{term}'")
            continue
        for entry in feed.entries:
            item = _to_item(entry, term)
            if not item:
                continue
            if item["_ts"] and item["_ts"] < cutoff:
                continue
            key = item["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            items.append(item)

    items.sort(key=lambda i: i["_ts"] or 0, reverse=True)
    for i in items:
        i.pop("_ts", None)
    return items[:max_items], warnings


def _to_item(entry, term: str) -> dict | None:
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None
    source = ""
    src = entry.get("source")
    if isinstance(src, dict):
        source = src.get("title", "")
    # Google News titles end with " - Source"; strip it when we know the source.
    if source and title.endswith(f" - {source}"):
        title = title[: -len(source) - 3].strip()
    ts = None
    published = ""
    if entry.get("published_parsed"):
        ts = calendar.timegm(entry.published_parsed)
        published = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"title": title, "source": source, "published": published, "link": link,
            "query": term, "_ts": ts}
