"""Offline tests for the extracted voice references and prompt assembly."""
import json

from still_meera import prompts
from still_meera.config import PROJECT_ROOT
from still_meera.voice_extract import normalise_pdf_text, parse_pieces

REFS = PROJECT_ROOT / "voice" / "references.json"


def test_fifteen_pieces_with_original_ids():
    pieces = json.loads(REFS.read_text())["pieces"]
    ids = [p["id"] for p in pieces]
    assert len(ids) == 15 == len(set(ids))
    assert ids[:4] == [f"linkedin_post_00{i}" for i in range(1, 5)]
    assert ids[4:] == [f"newsletter_{i:03d}" for i in range(1, 12)]
    assert all(p["text"] and p["category"] for p in pieces)


def test_draft_prompt_includes_guide_linkedin_posts_and_evidence_warning():
    system = prompts.draft_system()
    assert "voice guide" in system.lower()
    for i in range(1, 5):
        assert f"linkedin_post_00{i}" in system
    assert "not evidence" in system and "Never reuse their anecdotes" in system
    assert "Do not reuse these as if they were new" in system


def test_pdf_layout_is_normalised():
    raw = ("── linkedin\n_post\n001 ──\n_\nCategory: Ingredient Deep-Dive\nFirst line\nsecond line\n"
           "━━━━\n── newsletter\n001 ──\n_\nCategory: Brand Philosophy\nSubject: Hello there\nHi,\nBody\n")
    pieces = parse_pieces(normalise_pdf_text(raw))
    assert [p["id"] for p in pieces] == ["linkedin_post_001", "newsletter_001"]
    assert pieces[0]["category"] == "Ingredient Deep-Dive" and "second line" in pieces[0]["text"]
    assert pieces[1]["title"] == "Hello there" and pieces[1]["format"] == "newsletter"
