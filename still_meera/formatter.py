"""Builds the Telegram review messages and splits them under the length limit."""
from __future__ import annotations

from .telegram_api import MAX_MESSAGE_CHARS

SAFE_LIMIT = MAX_MESSAGE_CHARS - 96  # headroom for "(part x/y)" labels


def split_text(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split on paragraph, then line, then word boundaries so nothing exceeds `limit`."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in _pieces(text, limit):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _pieces(text: str, limit: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            out.append(para)
            continue
        for line in para.split("\n"):
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                out.append(line[:cut].rstrip())
                line = line[cut:].lstrip()
            out.append(line)
    return out


def label_parts(chunks: list[str], label: str) -> list[str]:
    if len(chunks) == 1:
        return chunks
    return [f"{c}\n\n({label} part {i}/{len(chunks)})" for i, c in enumerate(chunks, 1)]


def _criteria_line(triage: dict) -> str:
    c = triage.get("criteria") or {}
    if not c:
        return ""
    names = [("clarity", "clarity"), ("audience_relevance", "relevance"),
             ("specificity", "specificity"), ("substance", "substance")]
    parts = [f"{short} {c[k]:g}" for k, short in names if isinstance(c.get(k), (int, float))]
    return ("Criteria: " + " · ".join(parts)) if parts else ""


def score_message(note_id: int, triage: dict, threshold: float, kind: str) -> str:
    lines = [
        f"📝 Still Meera · note #{note_id} ({kind})",
        f"Score: {triage['score']:g}/10 (threshold {threshold:g}) → DRAFTED",
        f"Why: {triage['reason']}",
    ]
    crit = _criteria_line(triage)
    if crit:
        lines.append(crit)
    lines.append("A score is a writing-potential rating, not a fact-check. Draft is in the next message.")
    return "\n".join(lines)


def held_message(note_id: int, triage: dict, threshold: float, kind: str) -> str:
    lines = [
        f"⏸ Still Meera · note #{note_id} ({kind}) held",
        f"Score: {triage['score']:g}/10 (below threshold {threshold:g})",
        f"Why: {triage['reason']}",
    ]
    crit = _criteria_line(triage)
    if crit:
        lines.append(crit)
    if triage.get("missing_information"):
        lines.append("Would help: " + "; ".join(triage["missing_information"]))
    lines.append(f"The note is saved. To draft it anyway, post: /draft {note_id}")
    return "\n".join(lines)


def off_topic_message(note_id: int, triage: dict, kind: str) -> str:
    return "\n".join([
        f"⏸ Still Meera · note #{note_id} ({kind}) not related",
        "Score: 0/10. Not about skincare, Skinstinct or your work, so nothing was drafted "
        "and no news was searched.",
        f"Why: {triage['reason']}",
        f"If this was meant as a note, post: /draft {note_id}",
    ])


def news_message(note_id: int, news_state: dict, used_ids: list[int], lookback_days: int) -> str:
    items = news_state.get("items") or []
    if not items:
        lines = [f"📰 Related news · note #{note_id}",
                 f"No closely related news found from the last {lookback_days} days."]
        lines.extend(f"• {w}" for w in news_state.get("warnings") or [])
        return "\n".join(lines)
    lines = [f"📰 Related news · note #{note_id} (last {lookback_days} days; headlines only, "
             "the bot has not read the articles)"]
    for n, item in enumerate(items, 1):
        meta = ", ".join(x for x in (item.get("source"), item.get("published")) if x)
        used = " · used in draft" if (n - 1) in used_ids else ""
        lines.append(f"{n}. {item['title']}" + (f" ({meta})" if meta else "") + used)
        if item.get("why"):
            lines.append(f"   Why: {item['why']}")
        lines.append(f"   {item['link']}")
    lines.append("Open and read each article before relying on it.")
    return "\n".join(lines)


def check_message(note_id: int, triage: dict, draft: dict, news_warnings: list[str],
                  news_used: bool) -> str:
    flags = triage.get("risk_flags", {})
    lines = [f"✅ Check before publishing · note #{note_id}"]

    def section(title: str, items: list[str]) -> None:
        if items:
            lines.append(title)
            lines.extend(f"• {i}" for i in items)

    section("Claims that need a source:", flags.get("unsupported_claims", []))
    section("Possible private customer info (remove or get consent):", flags.get("private_customer_info", []))
    section("Details to confirm in the draft:", draft.get("check_before_publishing", []))
    section("Missing from the original note:", triage.get("missing_information", []))
    section("News lookup:", news_warnings)
    if not news_used:
        lines.append("No news hook used in this draft.")
    if not any(l.startswith("•") for l in lines):
        lines.append("• Nothing specific flagged. Still read every claim before posting.")
    lines.append("Publishing is manual: copy the draft, edit, and post on LinkedIn yourself.")
    return "\n".join(lines)


def failure_message(note_id: int, stage: str, error: str, attempts: int, max_attempts: int,
                    will_retry: bool) -> str:
    stage_names = {"download": "voice download", "transcribe": "transcription", "triage": "scoring",
                   "draft": "drafting", "deliver": "sending the draft"}
    what = stage_names.get(stage, stage)
    if will_retry:
        tail = f"Will retry automatically (attempt {attempts}/{max_attempts})."
    else:
        tail = f"Stopped after {attempts} attempt(s) to avoid repeat charges. Post /retry {note_id} to try once more."
    return (f"⚠️ Still Meera · note #{note_id}: {what} failed.\n{error}\n"
            f"Your original note is saved. {tail}")
