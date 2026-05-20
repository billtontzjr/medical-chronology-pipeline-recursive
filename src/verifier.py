"""Phase 4 deterministic verifier.

Pure Python, no LLM calls. For every extracted fact in every chunk JSON
file, search the corresponding OCR source for the fact's
``verbatim_quote`` as a literal substring after collapsing whitespace
runs. Verified facts are persisted to ``verified_facts.jsonl`` with the
original-string offset where the quote was found; rejected facts go to
``verification_rejected.jsonl`` with a machine-readable reason.

This is the load-bearing component of precision-chronology. A bug here
re-introduces the hallucination class this whole project was built to
eliminate. The unit tests in tests/test_verifier.py are mandatory.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.schemas import ChunkExtraction, RejectedFact, VerifiedFact
from src.session_state import SessionStore


MIN_QUOTE_CHARS = 5
MAX_QUOTE_WORDS = 50


# ---------------------------------------------------------------- whitespace

def normalize_whitespace(s: str) -> str:
    """Collapse every run of whitespace into a single space; strip ends."""
    return " ".join(s.split())


def _build_offset_map(original: str) -> Tuple[str, List[int]]:
    """Return ``(normalized, idx_map)``.

    ``normalized`` is ``original`` with whitespace collapsed exactly the
    same way ``normalize_whitespace`` does it. ``idx_map[i]`` is the
    index in ``original`` of the character that ended up at index ``i``
    in ``normalized``. For collapsed-whitespace spans this is the index
    of the FIRST whitespace character of the run, which is the
    conservative choice for offset reporting.
    """
    out_chars: List[str] = []
    idx_map: List[int] = []
    prev_was_ws = True  # treat leading as whitespace so we strip prefix
    for i, ch in enumerate(original):
        if ch.isspace():
            if not prev_was_ws:
                out_chars.append(" ")
                idx_map.append(i)
                prev_was_ws = True
        else:
            out_chars.append(ch)
            idx_map.append(i)
            prev_was_ws = False
    if out_chars and out_chars[-1] == " ":
        out_chars.pop()
        idx_map.pop()
    return "".join(out_chars), idx_map


# -------------------------------------------------------------------- search

def verify_quote(
    quote: str,
    source_normalized: str,
    idx_map: List[int],
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Verify ``quote`` against an already-normalized source.

    Returns ``(verified, offset_in_original, rejection_reason)`` where
    exactly one of ``offset`` and ``rejection_reason`` is ``None``.
    """
    if quote is None or not quote.strip():
        return False, None, "quote_empty"

    q_norm = normalize_whitespace(quote)
    if len(q_norm) < MIN_QUOTE_CHARS:
        return False, None, "quote_too_short"

    word_count = len(q_norm.split())
    if word_count > MAX_QUOTE_WORDS:
        return False, None, "quote_too_long"

    hit = source_normalized.find(q_norm)
    if hit == -1:
        return False, None, "quote_not_found"

    return True, idx_map[hit], None


# ------------------------------------------------------------------ dates

NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$")

_MONTH_FORMATS = [
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
]


def normalize_date(raw: Optional[str]) -> Tuple[Optional[str], bool]:
    """Normalize a date string to MM/DD/YYYY.

    Returns ``(value, parsed_ok)``. ``parsed_ok`` is ``True`` when
    ``raw`` is ``None`` (nothing to parse) or when parsing succeeded.
    On failure the original string is returned alongside ``False`` so
    callers can flag the fact.
    """
    if raw is None:
        return None, True
    s = raw.strip()
    if not s:
        return raw, False

    m = NUMERIC_DATE_RE.match(s)
    if m:
        try:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(year=y, month=mo, day=d)
            return f"{dt.month:02d}/{dt.day:02d}/{dt.year:04d}", True
        except ValueError:
            return raw, False

    for fmt in _MONTH_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%m/%d/%Y"), True
        except ValueError:
            continue
    return raw, False


# ---------------------------------------------------------------- io helpers

def _atomic_replace_text(target: Path, content: str) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, target)


# ------------------------------------------------------------- session pass

def verify_session(session_store: SessionStore, session_id: str) -> Dict:
    """Run verification over every chunk-JSON in the session.

    Writes ``verified_facts.jsonl`` and ``verification_rejected.jsonl``
    atomically into the session root. Returns a report dict::

        {
            "verified": int,
            "rejected": int,
            "reasons": {reason: count, ...},
            "files_processed": int,
        }
    """
    facts_dir = session_store.extracted_facts_dir(session_id)
    extracted_dir = session_store.extracted_dir(session_id)
    verified_path = session_store.verified_facts_path(session_id)
    rejected_path = session_store.rejected_facts_path(session_id)

    source_cache: Dict[str, Tuple[str, List[int]]] = {}
    counts: Dict = {"verified": 0, "rejected": 0, "reasons": {}, "files_processed": 0}

    verified_lines: List[str] = []
    rejected_lines: List[str] = []

    chunk_paths = sorted(p for p in facts_dir.glob("*.json") if p.is_file())
    for chunk_path in chunk_paths:
        counts["files_processed"] += 1
        with open(chunk_path, "r", encoding="utf-8") as f:
            chunk = ChunkExtraction.model_validate_json(f.read())

        for fact in chunk.facts:
            stem = fact.source_file
            entry = source_cache.get(stem)
            if entry is None:
                src_path = extracted_dir / f"{stem}.txt"
                if not src_path.exists():
                    rejected = RejectedFact(
                        **fact.model_dump(),
                        rejection_reason="source_missing",
                    )
                    rejected_lines.append(rejected.model_dump_json())
                    counts["rejected"] += 1
                    counts["reasons"]["source_missing"] = (
                        counts["reasons"].get("source_missing", 0) + 1
                    )
                    continue
                text = src_path.read_text(encoding="utf-8")
                norm, idx_map = _build_offset_map(text)
                entry = (norm, idx_map)
                source_cache[stem] = entry
            source_norm, idx_map = entry

            verified, offset, reason = verify_quote(
                fact.verbatim_quote, source_norm, idx_map
            )

            new_date, parsed_ok = normalize_date(fact.visit_date)
            fact_data = fact.model_dump()
            fact_data["visit_date"] = new_date
            if fact.visit_date is not None and not parsed_ok:
                fact_data["extraction_confidence"] = "low"

            if verified:
                vfact = VerifiedFact(**fact_data, verification_offset=offset)
                verified_lines.append(vfact.model_dump_json())
                counts["verified"] += 1
            else:
                rfact = RejectedFact(**fact_data, rejection_reason=reason)
                rejected_lines.append(rfact.model_dump_json())
                counts["rejected"] += 1
                counts["reasons"][reason] = counts["reasons"].get(reason, 0) + 1

    _atomic_replace_text(verified_path, "\n".join(verified_lines) + ("\n" if verified_lines else ""))
    _atomic_replace_text(rejected_path, "\n".join(rejected_lines) + ("\n" if rejected_lines else ""))

    return counts


__all__ = [
    "MIN_QUOTE_CHARS",
    "MAX_QUOTE_WORDS",
    "normalize_whitespace",
    "verify_quote",
    "normalize_date",
    "verify_session",
]
