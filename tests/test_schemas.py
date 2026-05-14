"""Pydantic validation tests for the fact schemas."""

import pytest
from pydantic import ValidationError

from src.schemas import (
    ChunkExtraction,
    ExtractedFact,
    RejectedFact,
    VerifiedFact,
)


def _base_fact_kwargs(**overrides):
    base = dict(
        source_file="record_001.txt",
        source_page=3,
        chunk_id="record_001_chunk_00",
        visit_date="03/14/2024",
        facility="Pacific Spine Clinic",
        provider_name="Jane Doe",
        provider_credentials="MD",
        visit_type="office visit",
        fact_category="assessment",
        finding_text="L4-L5 disc herniation with right-sided radiculopathy.",
        verbatim_quote="L4-L5 herniation with right radiculopathy",
        extraction_confidence="high",
    )
    base.update(overrides)
    return base


def test_extracted_fact_minimal_required_fields():
    fact = ExtractedFact(
        source_file="r.txt",
        source_page=1,
        chunk_id="r_chunk_00",
        fact_category="other",
        finding_text="x",
        verbatim_quote="x",
        extraction_confidence="low",
    )
    assert fact.visit_date is None
    assert fact.facility is None


def test_extracted_fact_rejects_invalid_category():
    with pytest.raises(ValidationError):
        ExtractedFact(**_base_fact_kwargs(fact_category="bogus"))


def test_extracted_fact_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        ExtractedFact(**_base_fact_kwargs(extraction_confidence="certain"))


def test_chunk_extraction_aggregates_facts():
    fact = ExtractedFact(**_base_fact_kwargs())
    chunk = ChunkExtraction(
        source_file="record_001.txt",
        chunk_id="record_001_chunk_00",
        chunk_start_char=0,
        chunk_end_char=8000,
        facts=[fact, fact],
    )
    assert len(chunk.facts) == 2
    assert chunk.extraction_notes is None


def test_verified_fact_carries_offset_and_flag():
    vf = VerifiedFact(
        **_base_fact_kwargs(),
        verification_offset=4231,
    )
    assert vf.verified is True
    assert vf.verification_offset == 4231


def test_verified_fact_rejects_false_verified_flag():
    with pytest.raises(ValidationError):
        VerifiedFact(**_base_fact_kwargs(), verification_offset=0, verified=False)


def test_rejected_fact_requires_reason():
    rf = RejectedFact(
        **_base_fact_kwargs(),
        rejection_reason="quote_not_found",
    )
    assert rf.verified is False
    assert rf.rejection_reason == "quote_not_found"


def test_rejected_fact_rejects_true_verified_flag():
    with pytest.raises(ValidationError):
        RejectedFact(
            **_base_fact_kwargs(),
            rejection_reason="x",
            verified=True,
        )


def test_extracted_fact_round_trip_json():
    fact = ExtractedFact(**_base_fact_kwargs())
    payload = fact.model_dump_json()
    revived = ExtractedFact.model_validate_json(payload)
    assert revived == fact
