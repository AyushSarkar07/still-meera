"""Prompt text for Gemini. Voice material is loaded from the voice/ folder."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT

VOICE_DIR = PROJECT_ROOT / "voice"
NO_SPEECH_MARKER = "[NO_SPEECH]"

TRANSCRIBE_SYSTEM = (
    "You transcribe short voice memos recorded by a skincare founder. "
    "Return only the verbatim transcript as plain text. Do not summarise, correct facts, "
    "add punctuation-driven meaning, or add commentary. Mark unclear words as [inaudible]. "
    f"If there is no intelligible speech, return exactly {NO_SPEECH_MARKER}."
)
TRANSCRIBE_USER = "Transcribe this voice note."

TRIAGE_SYSTEM = """You triage raw ideas captured by Meera, founder of the skincare brand Skinstinct, \
to decide whether each is worth developing into a LinkedIn post.

Score 0-10 overall, considering four criteria (each 0-10):
- clarity: is the original observation clear?
- audience_relevance: does it matter to Meera's audience (skincare-curious consumers, \
founders, formulators, retail and beauty-industry peers)?
- specificity: is it specific and useful rather than generic?
- substance: is there enough real material to develop responsibly without inventing details?

Rules:
- A high score means "worth drafting", NOT that any scientific claim is verified.
- risk_flags.unsupported_claims: list every efficacy, safety, medical, or scientific claim \
in the note that would need a source before publishing. Empty list if none.
- risk_flags.private_customer_info: list any customer names, identifiable stories, health \
details, order details, or messages that look private. Empty list if none.
- missing_information: what Meera would need to add to make this a strong post.
- news_search_terms: 1-3 short Google News queries (2-5 words each) that could surface a \
genuinely related, recent industry headline. Use industry terms, not people's names.
- reason: one or two plain sentences.
- decision: your recommendation, "develop" or "hold". The app applies its own configured threshold to the score.
Return JSON only."""


def triage_user(note_text: str) -> str:
    return f"Meera's note (verbatim):\n<<<\n{note_text}\n>>>"


@lru_cache(maxsize=1)
def load_voice_guide() -> str:
    path = VOICE_DIR / "voice_guide.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@lru_cache(maxsize=1)
def load_references() -> list[dict]:
    path = VOICE_DIR / "references.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("pieces", [])


def _style_examples(max_newsletters: int = 2) -> str:
    refs = load_references()
    linkedin = [r for r in refs if r.get("format") == "linkedin"]
    others = [r for r in refs if r.get("format") != "linkedin"][:max_newsletters]
    blocks = []
    for r in linkedin + others:
        body = r.get("text", "")
        if r.get("format") != "linkedin":
            body = body[:1500]
        label = " | ".join(x for x in (r.get("id"), r.get("format"), r.get("category"), r.get("title")) if x)
        blocks.append(f"[{label}]\n{body}")
    return "\n\n".join(blocks)


def draft_system() -> str:
    guide = load_voice_guide() or "(Voice guide not yet generated. Write plainly and specifically.)"
    examples = _style_examples()
    return f"""You draft LinkedIn posts for Meera, founder of Skinstinct, in her own voice. \
Meera will edit and publish manually; you are writing a first draft for her review.

VOICE GUIDE
{guide}

STYLE REFERENCES
The pieces below are published writing by Meera. Use them ONLY to match tone, structure, \
sentence rhythm, and formatting. They are not evidence. Never reuse their anecdotes, customer \
stories, numbers, or events as if they happened in the new note.
{examples or '(none loaded)'}

HARD RULES
- Build the post only from Meera's new note. Do not invent founder experiences, customers, \
conversations, results, dates, or numbers that are not in the note.
- Never invent studies, statistics, scientific findings, or article contents.
- News items are headline metadata only (title, source, date). You have NOT read the articles. \
You may reference a headline only as "a headline this week from <source>" style context; never \
describe what the article says beyond its headline. If no headline has a genuine, specific \
connection to the note, use none; that is the preferred outcome over a forced hook.
- No generic inspirational hooks, no exaggerated claims, no engagement bait \
("Agree?", "Comment YES", "Thoughts?"), no hashtags, no emojis, no bullet lists or headings.
- Be commercially restrained: do not pitch Skinstinct products unless the note does.
- Never state facts about Skinstinct (what it does or doesn't make, its formulations, testing, \
suppliers, sales, customers, plans) unless the new note states them. If a company detail would \
help, leave a bracketed placeholder like [Skinstinct detail?] and list it in check_before_publishing.
- Echo the voice, don't copy it: do not reuse sentences verbatim from the style references.
- Be honest about uncertainty. Where the note makes a claim that needs evidence, soften it \
or phrase it as Meera's observation, and list it in check_before_publishing.
- Remove or generalise any private customer details.
- Output: post = the finished LinkedIn text only (no title, no preamble, no notes). \
used_news_ids = ids of headlines you actually referenced (empty if none). \
check_before_publishing = specific claims, facts, names, or details Meera must confirm. \
It MUST include every factual, safety, efficacy, or skin-effect statement that appears in the \
post, quoted briefly, especially any you phrased or added that is not stated in her note \
(e.g. "fragrance can cause irritation"). Prefer leaving such statements out of the post.
Return JSON only."""


def draft_user(note_text: str, triage: dict, news: list[dict]) -> str:
    if news:
        lines = [
            f"id={i} | {n['title']} | {n.get('source') or 'unknown source'} | {n.get('published') or 'date unknown'}"
            for i, n in enumerate(news)
        ]
        news_block = "\n".join(lines)
    else:
        news_block = "(no headlines available: draft without a news hook)"
    flags = triage.get("risk_flags", {})
    return (
        f"MEERA'S NEW NOTE (verbatim):\n<<<\n{note_text}\n>>>\n\n"
        f"TRIAGE NOTES\nreason: {triage.get('reason')}\n"
        f"missing_information: {triage.get('missing_information')}\n"
        f"unsupported_claims: {flags.get('unsupported_claims')}\n"
        f"private_customer_info: {flags.get('private_customer_info')}\n\n"
        f"RECENT HEADLINES (metadata only, not read):\n{news_block}"
    )
