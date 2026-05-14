"""Tests for the cross-checker (Phase 6)."""

import json
from pathlib import Path

import pytest

from src.cross_checker import (
    CROSS_CHECK_JACCARD_THRESHOLD,
    CrossCheckFailed,
    cross_check,
    cross_check_and_report,
    jaccard,
    parse_entries,
    tokenize,
)
from src.schemas import VerifiedFact


# -------------------------------------------------------------- tokenize/jacc

def test_identical_strings_score_one():
    a = tokenize("L4-L5 disc herniation right radiculopathy")
    b = tokenize("L4-L5 disc herniation right radiculopathy")
    assert jaccard(a, b) == 1.0


def test_disjoint_strings_score_zero():
    a = tokenize("cervical spine fracture acute")
    b = tokenize("ankle sprain mild swelling")
    assert jaccard(a, b) == 0.0


def test_partial_overlap_expected_score():
    a = tokenize("right knee meniscus tear MRI shows")
    b = tokenize("MRI right knee shows lateral meniscus")
    # After stopword removal+lowercase+punct:
    # a -> {right, knee, meniscus, tear, mri, shows}      (6)
    # b -> {mri, right, knee, shows, lateral, meniscus}   (6)
    # intersection: right, knee, meniscus, mri, shows = 5
    # union: right, knee, meniscus, tear, mri, shows, lateral = 7
    assert abs(jaccard(a, b) - (5 / 7)) < 1e-9


def test_stopwords_dropped():
    toks = tokenize("the patient reports pain in the back")
    # 'the', 'patient', 'reports', 'in' all stopwords
    assert toks == {"pain", "back"}


def test_punctuation_stripped_and_lowercased():
    assert tokenize("Right knee, pain.") == {"right", "knee", "pain"}


def test_jaccard_double_empty_is_zero():
    assert jaccard(set(), set()) == 0.0


# --------------------------------------------------------------- entry parser

SAMPLE_CHRONOLOGY = """<<HEADER>>

03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. Chief Complaint: Low back pain. History of Present Illness: Onset after lifting. Physical Examination: Lumbar tenderness, positive SLR right. Assessment: L4-L5 disc herniation with right radiculopathy. Plan: PT and follow-up two weeks.

10/24/2023. MRI Associates. Rudy Heiser, DC. MRI Lumbar Spine without contrast. Impression: 2 mm right foraminal herniation L5-S1 with annular tear.
"""


def test_parse_entries_two_paragraphs():
    entries = parse_entries(SAMPLE_CHRONOLOGY)
    assert len(entries) == 2
    e1 = entries[0]
    assert e1.visit_date == "03/14/2024"
    assert e1.facility == "Pacific Spine Clinic"
    labels = [p.label for p in e1.phrases if p.label]
    assert "Chief Complaint" in labels
    assert "Assessment" in labels
    assert "Plan" in labels


def test_parse_entries_handles_subset_of_labels():
    md = "03/14/2024. ClinicA. Doe, MD. Office. Impression: Mild bulge L4-L5."
    entries = parse_entries(md)
    assert len(entries) == 1
    labels = [p.label for p in entries[0].phrases if p.label]
    assert labels == ["Impression"]


# --------------------------------------------------------------- cross_check

def _vf(**overrides) -> VerifiedFact:
    base = dict(
        source_file="record",
        source_page=1,
        chunk_id="record_chunk_00",
        visit_date="03/14/2024",
        facility="Pacific Spine Clinic",
        provider_name="Jane Doe",
        provider_credentials="MD",
        visit_type="office visit",
        fact_category="assessment",
        finding_text="L4-L5 disc herniation with right radiculopathy",
        verbatim_quote="L4-L5 disc herniation with right radiculopathy",
        extraction_confidence="high",
        verification_offset=0,
    )
    base.update(overrides)
    return VerifiedFact(**base)


def test_threshold_partitioning_strong_match_passes(tmp_path: Path):
    vfile = tmp_path / "v.jsonl"
    vfile.write_text(_vf().model_dump_json() + "\n")
    md = (
        "03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. "
        "Assessment: L4-L5 disc herniation with right radiculopathy."
    )
    report = cross_check(md, vfile, threshold=0.5)
    assert report.entries_checked == 1
    assert not report.warnings


def test_threshold_partitioning_weak_match_flags(tmp_path: Path):
    vfile = tmp_path / "v.jsonl"
    vfile.write_text(_vf().model_dump_json() + "\n")
    md = (
        "03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. "
        "Assessment: Cervical fracture with tibial plateau injury."
    )
    report = cross_check(md, vfile, threshold=0.5)
    assert len(report.warnings) == 1
    w = report.warnings[0]
    assert w.visit_date == "03/14/2024"
    assert w.best_score < 0.5


def test_strict_mode_raises_on_weak_match(tmp_path: Path):
    vfile = tmp_path / "v.jsonl"
    vfile.write_text(_vf().model_dump_json() + "\n")
    md = (
        "03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. "
        "Assessment: Lateral collateral ligament tear."
    )
    with pytest.raises(CrossCheckFailed):
        cross_check(md, vfile, threshold=0.5, strict=True)


def test_cross_check_and_report_writes_file(tmp_path: Path):
    vfile = tmp_path / "v.jsonl"
    vfile.write_text(_vf().model_dump_json() + "\n")
    md_path = tmp_path / "chronology.md"
    md_path.write_text(
        "03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. "
        "Assessment: Mismatch text here."
    )
    report_path = tmp_path / "cross_check_report.md"
    report = cross_check_and_report(md_path, vfile, report_path, threshold=0.9)
    assert report_path.exists()
    content = report_path.read_text()
    assert "Cross-check report" in content
    assert "warning" in content.lower()


def test_threshold_default_constant_is_035():
    assert abs(CROSS_CHECK_JACCARD_THRESHOLD - 0.35) < 1e-9


def test_candidates_fall_back_to_date_only_when_facility_misses(tmp_path: Path):
    vfile = tmp_path / "v.jsonl"
    vfile.write_text(_vf(facility="Different Clinic").model_dump_json() + "\n")
    md = (
        "03/14/2024. Pacific Spine Clinic. Jane Doe, MD. Office visit. "
        "Assessment: L4-L5 disc herniation with right radiculopathy."
    )
    report = cross_check(md, vfile, threshold=0.5)
    # Facility differs but date matches → fallback finds the fact → strong jaccard, no warning
    assert not report.warnings


def test_missing_verified_file_treats_as_no_candidates(tmp_path: Path):
    vfile = tmp_path / "missing.jsonl"
    md = (
        "03/14/2024. ClinicA. Doe, MD. Office visit. "
        "Assessment: anything at all."
    )
    report = cross_check(md, vfile, threshold=0.5)
    assert len(report.warnings) == 1
