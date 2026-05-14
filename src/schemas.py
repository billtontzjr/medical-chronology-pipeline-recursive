"""Pydantic schemas for extracted, verified, and rejected clinical facts.

Every fact flowing through the pipeline carries a verbatim quote that must
appear as a literal substring of the source OCR text. The verifier converts
ExtractedFact records into VerifiedFact or RejectedFact records based on
whether the substring search succeeds.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


FactCategory = Literal[
    "chief_complaint",
    "history",
    "physical_exam",
    "assessment",
    "diagnosis",
    "plan",
    "medication",
    "procedure_performed",
    "imaging_finding",
    "lab_result",
    "referral",
    "work_status",
    "patient_quote",
    "other",
]

ExtractionConfidence = Literal["high", "medium", "low"]


class ExtractedFact(BaseModel):
    """A single atomic clinical fact extracted from a source chunk."""

    # Identity
    source_file: str = Field(..., description="OCR text filename")
    source_page: int = Field(..., description="Page number from === PAGE N === marker")
    chunk_id: str = Field(..., description="Chunk identifier within the source file")

    # Visit identity
    visit_date: Optional[str] = Field(
        None,
        description="Date of service, MM/DD/YYYY or null if not stated in the quote",
    )
    facility: Optional[str] = Field(
        None, description="Facility or practice name as written in the source"
    )
    provider_name: Optional[str] = Field(
        None, description="Provider name as written in the source"
    )
    provider_credentials: Optional[str] = Field(
        None,
        description="Provider credentials such as MD, DO, PA-C, DC, DPT",
    )
    visit_type: Optional[str] = Field(
        None,
        description=(
            "Visit type: office visit, ER, follow-up, imaging, "
            "procedure, therapy, telehealth, IME"
        ),
    )

    # The fact itself
    fact_category: FactCategory
    finding_text: str = Field(
        ..., description="Concise paraphrase of the fact in clinical language"
    )
    verbatim_quote: str = Field(
        ...,
        description=(
            "Exact substring of the source OCR text that supports this fact. "
            "Must match character-for-character."
        ),
    )

    # Provenance
    extraction_confidence: ExtractionConfidence = Field(
        ...,
        description=(
            "High when the source is unambiguous, medium when interpretation "
            "was required, low when uncertain"
        ),
    )


class ChunkExtraction(BaseModel):
    """The full output of one extraction API call on one chunk."""

    source_file: str
    chunk_id: str
    chunk_start_char: int
    chunk_end_char: int
    facts: list[ExtractedFact]
    extraction_notes: Optional[str] = Field(
        None,
        description="Notes from the model about ambiguity, unreadable text, or other issues",
    )


class VerifiedFact(ExtractedFact):
    """An ExtractedFact that has passed substring verification."""

    verified: Literal[True] = True
    verification_offset: int = Field(
        ...,
        description="Character offset where verbatim_quote was found in source",
    )


class RejectedFact(ExtractedFact):
    """An ExtractedFact whose verbatim_quote failed substring verification."""

    verified: Literal[False] = False
    rejection_reason: str
