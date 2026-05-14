"""Extraction prompt builder.

The prompt asks Claude to read a chunk of OCR'd medical record text and
emit a strict-JSON ``ChunkExtraction`` payload. Every fact must carry a
``verbatim_quote`` that is an exact substring of the source chunk; the
deterministic verifier rejects any fact whose quote does not appear
character-for-character.
"""

from __future__ import annotations

import json

from src.schemas import ChunkExtraction


def _schema_summary() -> str:
    """Compact human-readable schema description from the Pydantic model.

    We expose the field names + descriptions but skip the full JSON
    Schema noise. This keeps the prompt small while leaving the model
    no excuse for missing or mis-spelled fields.
    """
    schema = ChunkExtraction.model_json_schema()
    defs = schema.get("$defs", {})
    fact_schema = defs.get("ExtractedFact", {})
    lines = [
        "ChunkExtraction object:",
        "  source_file: string (echo the SOURCE FILE value below)",
        "  chunk_id: string (echo the CHUNK ID value below)",
        "  chunk_start_char: integer (start char offset of this chunk in the full file)",
        "  chunk_end_char: integer (end char offset of this chunk in the full file)",
        "  facts: array of ExtractedFact",
        "  extraction_notes: string or null",
        "",
        "ExtractedFact object:",
    ]
    props = fact_schema.get("properties", {})
    order = [
        "source_file",
        "source_page",
        "chunk_id",
        "visit_date",
        "facility",
        "provider_name",
        "provider_credentials",
        "visit_type",
        "fact_category",
        "finding_text",
        "verbatim_quote",
        "extraction_confidence",
    ]
    for name in order:
        info = props.get(name, {})
        desc = info.get("description", "")
        lines.append(f"  {name}: {desc}")
    return "\n".join(lines)


_SCHEMA = _schema_summary()


PROMPT_TEMPLATE = """You are extracting atomic clinical facts from a fragment of an OCR'd medical record. Every fact you extract must be supported by an exact verbatim substring of the source text below.

RULES:
1. Every fact must have a verbatim_quote that is a literal, character-for-character substring of the source chunk. Do not clean up OCR errors. Do not paraphrase the quote. Do not add or remove words.
2. If you cannot find a clean verbatim quote of fifteen words or fewer that supports a fact, do not extract that fact.
3. Do not infer facts. If the chunk does not state the visit date explicitly, leave visit_date null. Do not guess from filename or surrounding context.
4. Extract one fact per distinct clinical claim. A single visit usually produces multiple facts: separate the chief complaint, history, exam findings, assessment, and plan into distinct facts.
5. Use extraction_confidence: low for any fact requiring interpretation, where OCR text is partially illegible, or where the speaker is ambiguous.
6. fact_category must be one of: chief_complaint, history, physical_exam, assessment, diagnosis, plan, medication, procedure_performed, imaging_finding, lab_result, referral, work_status, patient_quote, other.
7. extraction_confidence must be one of: high, medium, low.
8. visit_date, when stated explicitly, should be in MM/DD/YYYY format. If only month-day or partial date is present, set it to null.
9. Output a single JSON object matching the ChunkExtraction schema. No preamble, no markdown fences, no commentary.

SCHEMA:
{schema}

SOURCE FILE: {source_file}
CHUNK ID: {chunk_id}
CHUNK START CHAR: {chunk_start_char}
CHUNK END CHAR: {chunk_end_char}

SOURCE CHUNK TEXT:
{chunk_text}

OUTPUT (JSON only):
"""


def build_extraction_prompt(
    chunk_text: str,
    source_file: str,
    chunk_id: str,
    chunk_start_char: int,
    chunk_end_char: int,
) -> str:
    """Render the extraction prompt for one chunk."""
    return PROMPT_TEMPLATE.format(
        schema=_SCHEMA,
        source_file=source_file,
        chunk_id=chunk_id,
        chunk_start_char=chunk_start_char,
        chunk_end_char=chunk_end_char,
        chunk_text=chunk_text,
    )


__all__ = ["build_extraction_prompt"]
