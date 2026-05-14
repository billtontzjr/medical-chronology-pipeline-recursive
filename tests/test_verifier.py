"""Tests for the deterministic verifier (Phase 4).

The verifier is the load-bearing piece of precision-chronology. These
tests aim for >=95% line coverage so a regression that re-opens the
hallucination class is caught immediately.
"""

import json
from pathlib import Path

import pytest

from src.schemas import ChunkExtraction, ExtractedFact
from src.session_state import SessionStore
from src.verifier import (
    MAX_QUOTE_WORDS,
    MIN_QUOTE_CHARS,
    _build_offset_map,
    normalize_date,
    normalize_whitespace,
    verify_quote,
    verify_session,
)


def _ws_then_verify(source, quote):
    norm, idx_map = _build_offset_map(source)
    return verify_quote(quote, norm, idx_map)


# ------------------------------------------------------------------- basics

def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("  hello\nworld\t\tfoo ") == "hello world foo"


def test_offset_map_lengths_match():
    src = "  hi  there\n\nfriend  "
    norm, idx_map = _build_offset_map(src)
    assert len(norm) == len(idx_map)
    assert norm == "hi there friend"


def test_exact_match_returns_offset_in_original():
    source = "Patient reports right knee pain after fall."
    verified, off, reason = _ws_then_verify(source, "right knee pain")
    assert verified is True
    assert reason is None
    assert source[off : off + len("right knee pain")] == "right knee pain"


def test_match_across_line_break_returns_original_offset():
    source = "L4-L5 disc\nherniation with right radiculopathy"
    quote = "L4-L5 disc herniation"
    verified, off, _ = _ws_then_verify(source, quote)
    assert verified is True
    # offset points at the 'L' in original; the original substring there is "L4-L5 disc"
    assert source[off : off + len("L4-L5 disc")] == "L4-L5 disc"


def test_punctuation_difference_rejected():
    # Source lacks the trailing period the quote claims.
    source = "Chief complaint back pain"
    verified, _, reason = _ws_then_verify(source, "Chief complaint back pain.")
    assert verified is False
    assert reason == "quote_not_found"


def test_empty_quote_rejected():
    verified, _, reason = _ws_then_verify("anything", "")
    assert verified is False
    assert reason == "quote_empty"


def test_whitespace_only_quote_rejected():
    verified, _, reason = _ws_then_verify("anything", "   \n\t  ")
    assert verified is False
    assert reason == "quote_empty"


def test_none_quote_rejected():
    norm, idx_map = _build_offset_map("source")
    verified, _, reason = verify_quote(None, norm, idx_map)
    assert verified is False
    assert reason == "quote_empty"


def test_quote_too_short():
    verified, _, reason = _ws_then_verify("abcd is here", "abcd")
    assert verified is False
    assert reason == "quote_too_short"
    assert MIN_QUOTE_CHARS == 5


def test_quote_too_long_rejected():
    # 31 word quote - all in the source
    words = " ".join(f"w{i}" for i in range(31))
    source = "intro " + words + " outro"
    verified, _, reason = _ws_then_verify(source, words)
    assert verified is False
    assert reason == "quote_too_long"
    assert MAX_QUOTE_WORDS == 30


def test_quote_with_leading_trailing_whitespace_matches():
    source = "lumbar tenderness on palpation"
    verified, off, _ = _ws_then_verify(source, "  lumbar tenderness  ")
    assert verified is True
    assert source[off : off + len("lumbar tenderness")] == "lumbar tenderness"


def test_quote_not_in_source():
    verified, _, reason = _ws_then_verify("Patient denies pain.", "tibial plateau fracture")
    assert verified is False
    assert reason == "quote_not_found"


# -------------------------------------------------------------- date norm

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3/14/2024", "03/14/2024"),
        ("03/14/2024", "03/14/2024"),
        ("3-14-2024", "03/14/2024"),
        ("12-1-2023", "12/01/2023"),
        ("March 14, 2024", "03/14/2024"),
        ("Mar 14, 2024", "03/14/2024"),
        ("March 14 2024", "03/14/2024"),
        ("Mar 14 2024", "03/14/2024"),
        ("14-Mar-2024", "03/14/2024"),
        ("14-March-2024", "03/14/2024"),
    ],
)
def test_date_normalize_accepted_formats(raw, expected):
    out, ok = normalize_date(raw)
    assert ok is True
    assert out == expected


def test_date_normalize_none_passthrough():
    out, ok = normalize_date(None)
    assert out is None
    assert ok is True


def test_date_normalize_unparseable_returns_original_and_flag():
    out, ok = normalize_date("yesterday")
    assert ok is False
    assert out == "yesterday"


def test_date_normalize_invalid_numeric_returns_original():
    out, ok = normalize_date("13/40/2024")
    assert ok is False
    assert out == "13/40/2024"


def test_date_normalize_empty_string_flagged():
    out, ok = normalize_date("   ")
    assert ok is False
    assert out == "   "


# ---------------------------------------------------------- verify_session

def _fact(**overrides) -> ExtractedFact:
    base = dict(
        source_file="record",
        source_page=1,
        chunk_id="record_chunk_00",
        visit_date="3/14/2024",
        facility="Pacific Spine Clinic",
        provider_name="Jane Doe",
        provider_credentials="MD",
        visit_type="office visit",
        fact_category="assessment",
        finding_text="L4-L5 disc herniation",
        verbatim_quote="L4-L5 disc herniation with right radiculopathy",
        extraction_confidence="high",
    )
    base.update(overrides)
    return ExtractedFact(**base)


def _write_chunk_json(facts_dir: Path, chunk_id: str, facts):
    payload = ChunkExtraction(
        source_file="record",
        chunk_id=chunk_id,
        chunk_start_char=0,
        chunk_end_char=1000,
        facts=facts,
        extraction_notes=None,
    )
    (facts_dir / f"{chunk_id}.json").write_text(payload.model_dump_json(indent=2))


def test_verify_session_roundtrip(tmp_path: Path):
    store = SessionStore(str(tmp_path))
    sid = "demo"
    store.create(sid, "P001", "https://dropbox/x", "/out")
    extracted = store.extracted_dir(sid)
    facts_dir = store.extracted_facts_dir(sid)

    src_text = (
        "=== PAGE 1 ===\n"
        "Pacific Spine Clinic - office visit 03/14/2024.\n"
        "Assessment: L4-L5 disc herniation with right radiculopathy.\n"
        "Plan: PT and follow-up in two weeks.\n"
    )
    (extracted / "record.txt").write_text(src_text)

    good = _fact()
    bad = _fact(
        verbatim_quote="patient reports cervical fracture",
        finding_text="Cervical fracture (fabricated)",
    )
    bad_short = _fact(
        verbatim_quote="L4",  # under MIN_QUOTE_CHARS
    )
    bad_date = _fact(
        visit_date="yesterday",
        verbatim_quote="follow-up in two weeks",
    )
    _write_chunk_json(facts_dir, "record_chunk_00", [good, bad, bad_short, bad_date])

    report = verify_session(store, sid)
    assert report["files_processed"] == 1
    assert report["verified"] == 2  # good + bad_date (quote is fine; only date fails)
    assert report["rejected"] == 2

    verified_path = store.verified_facts_path(sid)
    rejected_path = store.rejected_facts_path(sid)
    verified_lines = [json.loads(l) for l in verified_path.read_text().splitlines() if l]
    rejected_lines = [json.loads(l) for l in rejected_path.read_text().splitlines() if l]

    assert all(v["verified"] is True for v in verified_lines)
    assert all(r["verified"] is False for r in rejected_lines)

    # bad_date should be verified but downgraded to low confidence and keep raw date string
    bad_date_verified = [v for v in verified_lines if v["verbatim_quote"].startswith("follow-up")]
    assert len(bad_date_verified) == 1
    assert bad_date_verified[0]["extraction_confidence"] == "low"
    assert bad_date_verified[0]["visit_date"] == "yesterday"

    reasons = report["reasons"]
    assert "quote_not_found" in reasons
    assert "quote_too_short" in reasons


def test_verify_session_handles_missing_source_file(tmp_path: Path):
    store = SessionStore(str(tmp_path))
    sid = "demo2"
    store.create(sid, "P002", "https://dropbox/x", "/out")
    facts_dir = store.extracted_facts_dir(sid)
    # No file in extracted/ — source_missing path
    _write_chunk_json(facts_dir, "record_chunk_00", [_fact()])
    report = verify_session(store, sid)
    assert report["verified"] == 0
    assert report["rejected"] == 1
    assert report["reasons"]["source_missing"] == 1


def test_verify_session_writes_empty_files_when_no_chunks(tmp_path: Path):
    store = SessionStore(str(tmp_path))
    sid = "demo3"
    store.create(sid, "P003", "https://dropbox/x", "/out")
    report = verify_session(store, sid)
    assert report["files_processed"] == 0
    assert report["verified"] == 0
    assert report["rejected"] == 0
    assert store.verified_facts_path(sid).read_text() == ""
    assert store.rejected_facts_path(sid).read_text() == ""
