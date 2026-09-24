"""Mock Telegram / Gemini / news used by the offline demo and the tests.

Everything here is SAMPLE data. None of it is real Meera content or real API output.
"""
from __future__ import annotations

import itertools

from .gemini import validate_draft, validate_triage


class FakeTelegram:
    def __init__(self, chat_id: int, audio: bytes = b"SAMPLE-OGG-BYTES", fail_download: bool = False):
        self.chat_id = chat_id
        self.sent: list[dict] = []
        self.audio = audio
        self.fail_download = fail_download
        self.fail_send = False
        self._ids = itertools.count(9000)

    def send_message(self, chat_id: int, text: str) -> dict:
        from .telegram_api import MAX_MESSAGE_CHARS, TelegramError
        if self.fail_send:
            raise TelegramError("Telegram sendMessage error: SAMPLE outage")
        assert len(text) <= MAX_MESSAGE_CHARS, "message exceeds Telegram limit"
        msg = {"message_id": next(self._ids), "chat": {"id": chat_id}, "text": text}
        self.sent.append(msg)
        return msg

    def download_file(self, file_id: str) -> bytes:
        from .telegram_api import TelegramError
        if self.fail_download:
            raise TelegramError("Voice download failed: SAMPLE network error")
        return self.audio


class FakeAI:
    """Scripted responses. Pass callables or exceptions per call to simulate failures."""

    def __init__(self, triage_responses=None, draft_responses=None, transcripts=None):
        self.triage_responses = list(triage_responses or [])
        self.draft_responses = list(draft_responses or [])
        self.transcripts = list(transcripts or [])
        self.calls = {"transcribe": 0, "triage": 0, "draft": 0}

    def _next(self, queue, default):
        item = queue.pop(0) if queue else default
        if isinstance(item, Exception):
            raise item
        return item

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        self.calls["transcribe"] += 1
        return self._next(self.transcripts, SAMPLE_TRANSCRIPT)

    def triage(self, note_text: str) -> dict:
        self.calls["triage"] += 1
        return validate_triage(self._next(self.triage_responses, SAMPLE_TRIAGE_HIGH))

    def draft(self, note_text: str, triage: dict, news: list[dict]) -> dict:
        self.calls["draft"] += 1
        return validate_draft(self._next(self.draft_responses, SAMPLE_DRAFT), len(news))


def fake_news(items=None, warnings=None):
    def fetch(terms, max_items=5, lookback_days=30):
        return list(items if items is not None else SAMPLE_NEWS)[:max_items], list(warnings or [])
    return fetch


# ---------------------------------------------------------------- sample data
SAMPLE_TEXT_NOTE = (
    "[SAMPLE NOTE] Noticed at the retailer meeting today that buyers keep asking for "
    "'clean' claims but none of them could define it the same way. Two wanted fragrance-free, "
    "one meant no silicones. We keep our labels to what's actually in the bottle. Worth a post "
    "on why we don't use the word clean?"
)
SAMPLE_WEAK_NOTE = "[SAMPLE NOTE] skincare trends??"
SAMPLE_TRANSCRIPT = (
    "[SAMPLE TRANSCRIPT] Quick thought on sunscreen texture. Our testers kept saying they skip "
    "reapplying because of the white cast, not because they forget. I think we talk too much "
    "about SPF numbers and not enough about whether people will actually wear it."
)

SAMPLE_TRIAGE_HIGH = {
    "score": 8, "decision": "develop",
    "reason": "[MOCK] Clear, first-hand observation about how buyers use the word 'clean'; relevant to peers and consumers.",
    "criteria": {"clarity": 8, "audience_relevance": 8, "specificity": 7, "substance": 7},
    "missing_information": ["Which retailer category (kept anonymous?)"],
    "risk_flags": {"unsupported_claims": [], "private_customer_info": ["Retailer meeting details may be confidential"]},
    "news_search_terms": ["clean beauty labeling", "cosmetics claims regulation"],
}
SAMPLE_TRIAGE_LOW = {
    "score": 3, "decision": "hold",
    "reason": "[MOCK] Too vague to develop: no observation or point of view yet.",
    "criteria": {"clarity": 3, "audience_relevance": 5, "specificity": 1, "substance": 2},
    "missing_information": ["Which trend?", "What did you notice or disagree with?"],
    "risk_flags": {"unsupported_claims": [], "private_customer_info": []},
    "news_search_terms": [],
}
SAMPLE_DRAFT = {
    "post": (
        "[MOCK DRAFT] In a buyer meeting this week, 'clean' came up several times. "
        "Nobody in the room meant the same thing by it.\n\n"
        "For one person it meant fragrance-free. For another, no silicones. Those are both "
        "reasonable preferences. They're also completely different formulation choices.\n\n"
        "This is why we don't put 'clean' on our labels. We'd rather tell you what is in the "
        "bottle and why, and let you decide what matters for your skin.\n\n"
        "I don't think the word is going away. But I'd like the conversation to get more specific."
    ),
    "used_news_ids": [0],
    "check_before_publishing": ["Confirm you're comfortable referencing the buyer meeting publicly"],
}
SAMPLE_TRIAGE_VOICE = {
    "score": 7, "decision": "develop",
    "reason": "[MOCK] Specific tester observation about why people skip sunscreen reapplication.",
    "criteria": {"clarity": 7, "audience_relevance": 8, "specificity": 7, "substance": 6},
    "missing_information": ["How many testers, and in what setting?"],
    "risk_flags": {"unsupported_claims": ["People skip reapplying because of white cast, not forgetfulness"],
                   "private_customer_info": []},
    "news_search_terms": ["sunscreen reapplication"],
}
SAMPLE_VOICE_DRAFT = {
    "post": (
        "[MOCK DRAFT] Something from our recent tester feedback on sunscreen: the people who "
        "skipped reapplying mostly didn't say they forgot. They said they didn't like how it looked.\n\n"
        "That's a small sample and I don't want to overstate it. But it made me wonder whether we "
        "spend too much time on SPF numbers and not enough on whether someone will actually wear it "
        "twice in one afternoon."
    ),
    "used_news_ids": [],
    "check_before_publishing": ["Tester numbers and context before citing the feedback"],
}
SAMPLE_NEWS = [
    {"title": "[SAMPLE HEADLINE] Regulators weigh rules on 'clean' beauty marketing",
     "source": "Sample Trade Press", "published": "2026-09-20",
     "link": "https://example.com/sample-headline-1", "query": "clean beauty labeling"},
]
