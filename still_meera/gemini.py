"""Gemini calls: transcription, triage, news relevance, drafting.

Uses the google-genai SDK Interactions API (`client.interactions.create`), which
Google's docs describe as generally available and recommended for new projects
(checked Sept 2026). The model ID always comes from GEMINI_MODEL; nothing is hard-coded.
`store=False` keeps Meera's notes from being retained server-side as interaction state.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Protocol

from . import prompts


class AIError(Exception):
    """Base class. `transient` means retrying later may succeed."""
    transient = False


class AITransientError(AIError):
    transient = True


class AIPermanentError(AIError):
    transient = False


class MalformedAIResponse(AIError):
    # Retrying once is reasonable (models are non-deterministic) but it costs money,
    # so the pipeline still counts it against MAX_ATTEMPTS.
    transient = True


class AIClient(Protocol):
    def transcribe(self, audio: bytes, mime_type: str) -> str: ...
    def triage(self, note_text: str) -> dict: ...
    def pick_news(self, note_text: str, news: list[dict]) -> list[dict]: ...
    def draft(self, note_text: str, triage: dict, news: list[dict]) -> dict: ...


TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "decision": {"type": "string", "enum": ["develop", "hold"]},
        "reason": {"type": "string"},
        "criteria": {
            "type": "object",
            "properties": {
                "clarity": {"type": "number"},
                "audience_relevance": {"type": "number"},
                "specificity": {"type": "number"},
                "substance": {"type": "number"},
            },
            "required": ["clarity", "audience_relevance", "specificity", "substance"],
        },
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {
            "type": "object",
            "properties": {
                "unsupported_claims": {"type": "array", "items": {"type": "string"}},
                "private_customer_info": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["unsupported_claims", "private_customer_info"],
        },
        "news_search_terms": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "score", "decision", "reason", "missing_information", "risk_flags",
                 "news_search_terms"],
}

NEWS_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["id", "why"],
            },
        },
    },
    "required": ["relevant"],
}

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "post": {"type": "string"},
        "used_news_ids": {"type": "array", "items": {"type": "integer"}},
        "check_before_publishing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["post", "used_news_ids", "check_before_publishing"],
}


def validate_triage(data: Any) -> dict:
    if not isinstance(data, dict):
        raise MalformedAIResponse("Triage response was not a JSON object")
    try:
        score = float(data["score"])
    except (KeyError, TypeError, ValueError):
        raise MalformedAIResponse("Triage response had no numeric score") from None
    if not 0 <= score <= 10:
        raise MalformedAIResponse(f"Triage score {score} is outside 0-10")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise MalformedAIResponse("Triage response had no reason")
    flags = data.get("risk_flags") or {}
    if not isinstance(flags, dict):
        flags = {}
    # Off-topic posts ("hello", tests, chat) score 0, applied by the app, not left to the model.
    relevant = data.get("relevant") is not False
    criteria = data.get("criteria") or {}
    if not relevant:
        score = 0.0
        criteria = {k: 0 for k in ("clarity", "audience_relevance", "specificity", "substance")}
    return {
        "relevant": relevant,
        "score": round(score, 1),
        "decision": data.get("decision"),  # overwritten by the pipeline using the threshold
        "reason": reason.strip(),
        "criteria": criteria,
        "missing_information": _str_list(data.get("missing_information")),
        "risk_flags": {
            "unsupported_claims": _str_list(flags.get("unsupported_claims")),
            "private_customer_info": _str_list(flags.get("private_customer_info")),
        },
        "news_search_terms": _str_list(data.get("news_search_terms"))[:3] if relevant else [],
    }


def validate_news_pick(data: Any, news_count: int) -> list[dict]:
    """Returns [{"id", "why"}] for headlines judged relevant, most relevant first."""
    if not isinstance(data, dict) or not isinstance(data.get("relevant"), list):
        raise MalformedAIResponse("News relevance response had no 'relevant' list")
    picks, seen = [], set()
    for p in data["relevant"]:
        if not isinstance(p, dict):
            continue
        i = p.get("id")
        # Only keep ids that point at headlines we actually supplied.
        if isinstance(i, int) and 0 <= i < news_count and i not in seen:
            seen.add(i)
            picks.append({"id": i, "why": str(p.get("why") or "").strip()})
    return picks


def validate_draft(data: Any, news_count: int) -> dict:
    if not isinstance(data, dict):
        raise MalformedAIResponse("Draft response was not a JSON object")
    post = data.get("post")
    if not isinstance(post, str) or len(post.strip()) < 40:
        raise MalformedAIResponse("Draft response had no usable post text")
    ids = []
    for i in data.get("used_news_ids") or []:
        # Only keep ids that point at headlines we actually supplied.
        if isinstance(i, int) and 0 <= i < news_count and i not in ids:
            ids.append(i)
    return {
        "post": post.strip(),
        "used_news_ids": ids,
        "check_before_publishing": _str_list(data.get("check_before_publishing")),
    }


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_json(text: str | None) -> Any:
    if not text:
        raise MalformedAIResponse("Gemini returned an empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        raise MalformedAIResponse("Gemini response was not valid JSON") from None


class GeminiClient:
    def __init__(self, api_key: str, model: str):
        from google import genai  # imported lazily so offline demo/tests need no SDK setup

        self.model = model
        self.client = genai.Client(api_key=api_key)

    def _create(self, **kwargs):
        try:
            interaction = self.client.interactions.create(model=self.model, store=False, **kwargs)
        except Exception as exc:  # SDK raises several error types; classify by status code
            raise _classify(exc) from None
        return getattr(interaction, "output_text", None)

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        text = self._create(
            system_instruction=prompts.TRANSCRIBE_SYSTEM,
            input=[
                {"type": "text", "text": prompts.TRANSCRIBE_USER},
                {"type": "audio", "data": base64.b64encode(audio).decode("ascii"), "mime_type": mime_type},
            ],
        )
        if not text or not text.strip():
            raise MalformedAIResponse("Transcription came back empty")
        if text.strip() == prompts.NO_SPEECH_MARKER:
            raise AIPermanentError("No intelligible speech found in the voice note")
        return text.strip()

    def triage(self, note_text: str) -> dict:
        text = self._create(
            system_instruction=prompts.TRIAGE_SYSTEM,
            input=prompts.triage_user(note_text),
            response_format={"type": "text", "mime_type": "application/json", "schema": TRIAGE_SCHEMA},
        )
        return validate_triage(parse_json(text))

    def pick_news(self, note_text: str, news: list[dict]) -> list[dict]:
        text = self._create(
            system_instruction=prompts.NEWS_PICK_SYSTEM,
            input=prompts.news_pick_user(note_text, news),
            response_format={"type": "text", "mime_type": "application/json", "schema": NEWS_PICK_SCHEMA},
        )
        return validate_news_pick(parse_json(text), len(news))

    def draft(self, note_text: str, triage: dict, news: list[dict]) -> dict:
        text = self._create(
            system_instruction=prompts.draft_system(),
            input=prompts.draft_user(note_text, triage, news),
            response_format={"type": "text", "mime_type": "application/json", "schema": DRAFT_SCHEMA},
        )
        return validate_draft(parse_json(text), len(news))


def _classify(exc: Exception) -> AIError:
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status is None and getattr(exc, "response", None) is not None:
        status = getattr(exc.response, "status_code", None)
    msg = f"Gemini API error{f' {status}' if status else ''}: {type(exc).__name__}"
    detail = str(exc).splitlines()[0][:200] if str(exc) else ""
    if detail:
        msg += f" - {detail}"
    if isinstance(status, int) and (status == 429 or status >= 500):
        return AITransientError(msg)
    if isinstance(status, int) and 400 <= status < 500:
        return AIPermanentError(msg)  # bad key, unknown model, bad request: retrying won't help
    return AITransientError(msg)  # network problems etc.
