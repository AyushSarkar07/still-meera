"""Extract the 15 published writing samples into voice/references.json, preserving their IDs.

Usage:
  python -m still_meera extract-voice voice/source/published_samples.txt
  python -m still_meera extract-voice path/to/samples.pdf

IDs look like linkedin_post_001 or newsletter_007.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .config import PROJECT_ROOT

OUT_JSON = PROJECT_ROOT / "voice" / "references.json"
EXPECTED = 15

# Clean text source: "== linkedin_post_001 | linkedin | Category | optional title =="
HEADER_RE = re.compile(r"^==\s*(?P<id>[a-z_]+_\d{3})\s*\|\s*(?P<format>\w+)\s*\|\s*(?P<category>[^|=]+?)"
                       r"(?:\s*\|\s*(?P<title>.+?))?\s*==\s*$")
# Raw PDF text layout, where the header is broken across lines: "── linkedin\n_post\n001 ──\n_"
PDF_HEADER_RE = re.compile(r"──\s*(linkedin)\s*_\s*(post)\s*_?\s*(\d{3})\s*──\s*_?|──\s*(newsletter)\s*_?\s*(\d{3})\s*──\s*_?")


def normalise_pdf_text(text: str) -> str:
    """Turn the PDF's raw text into the clean header format (paragraph breaks are not recoverable)."""
    def header(m: re.Match) -> str:
        pid = f"linkedin_post_{m.group(3)}" if m.group(1) else f"newsletter_{m.group(5)}"
        return f"\n@@{pid}@@\n"
    text = PDF_HEADER_RE.sub(header, text)
    out, current, head = [], None, -1
    for line in text.splitlines():
        m = re.match(r"^@@(\S+)@@$", line.strip())
        if m:
            current = m.group(1)
            fmt = "linkedin" if current.startswith("linkedin") else "newsletter"
            out.append(f"== {current} | {fmt} | ? ==")
            head = len(out) - 1
            continue
        if current is None or line.startswith("━") or line.strip() in ("━", "END OF DOCUMENT"):
            continue
        cat = re.match(r"^Category:\s*(.+)$", line.strip())
        if cat and out[head].endswith("| ? =="):
            out[head] = out[head].replace("| ? ==", f"| {cat.group(1).strip()} ==")
            continue
        subj = re.match(r"^Subject:\s*(.+)$", line.strip())
        if subj and head >= 0:
            out[head] = out[head][:-3].rstrip() + f" | {subj.group(1).strip()} =="
            continue
        out.append(line)
    return "\n".join(out)


def parse_pieces(text: str) -> list[dict]:
    pieces: list[dict] = []
    current = None
    for line in text.splitlines():
        m = HEADER_RE.match(line.strip())
        if m:
            current = {"id": m.group("id"), "format": m.group("format").lower(),
                       "category": m.group("category").strip(), "title": (m.group("title") or "").strip(),
                       "lines": []}
            pieces.append(current)
        elif current is not None:
            current["lines"].append(line)
        # anything before the first header (file comments) is ignored
    for p in pieces:
        p["text"] = "\n".join(p.pop("lines")).strip()
        p["word_count"] = len(p["text"].split())
    return pieces


def load_source(path: str) -> str:
    if path.lower().endswith(".pdf"):
        from pypdf import PdfReader
        raw = "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
        return normalise_pdf_text(raw)
    return Path(path).read_text(encoding="utf-8")


def extract(path: str, id_pattern: str | None = None) -> int:
    pieces = parse_pieces(load_source(path))
    ids = [p["id"] for p in pieces]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "source": "Published writing samples by Meera Pillai (Skinstinct)",
        "note": ("Style references only. Not verified scientific evidence. Anecdotes, figures and "
                 "customer stories belong to their original piece and must not be reused as new facts."),
        "pieces": pieces,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = {f: sum(p["format"] == f for p in pieces) for f in sorted({p["format"] for p in pieces})}
    print(f"Extracted {len(pieces)} pieces → voice/references.json {counts}")
    for p in pieces:
        print(f"  {p['id']:<20} {p['category']:<24} {p['word_count']:>4} words  {p['title']}")
    if len(pieces) != EXPECTED or dupes:
        print(f"WARNING: expected {EXPECTED} unique IDs, got {len(pieces)} (duplicates: {dupes or 'none'})",
              file=sys.stderr)
        return 1
    return 0
