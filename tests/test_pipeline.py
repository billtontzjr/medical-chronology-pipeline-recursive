"""End-to-end pipeline integration test.

The Dropbox and Google Vision side-effects are mocked. The Anthropic
client is replaced with a deterministic stub that returns canned JSON
for the extraction prompt and canned prose for the assembly /
summary / gaps prompts. The fixture's verbatim quotes are literal
substrings of the fixture text so the verifier passes them.

The test asserts that all eight expected output files appear in
``data/sessions/<sid>/output/`` after a single ``pipeline.run()`` call.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_records.txt"


EXTRACTION_RESPONSE = json.dumps(
    {
        "source_file": "sample",
        "chunk_id": "sample_chunk_00",
        "chunk_start_char": 0,
        "chunk_end_char": 800,
        "extraction_notes": None,
        "facts": [
            {
                "source_file": "sample",
                "source_page": 1,
                "chunk_id": "sample_chunk_00",
                "visit_date": "03/14/2024",
                "facility": "Pacific Spine Clinic",
                "provider_name": "Jane Smith",
                "provider_credentials": "MD",
                "visit_type": "office visit",
                "fact_category": "chief_complaint",
                "finding_text": "Low back pain after MVC",
                "verbatim_quote": "low back pain after a motor vehicle collision",
                "extraction_confidence": "high",
            },
            {
                "source_file": "sample",
                "source_page": 1,
                "chunk_id": "sample_chunk_00",
                "visit_date": "03/14/2024",
                "facility": "Pacific Spine Clinic",
                "provider_name": "Jane Smith",
                "provider_credentials": "MD",
                "visit_type": "office visit",
                "fact_category": "assessment",
                "finding_text": "L4-L5 disc herniation with right radiculopathy",
                "verbatim_quote": "L4-L5 disc herniation with right radiculopathy",
                "extraction_confidence": "high",
            },
            {
                "source_file": "sample",
                "source_page": 2,
                "chunk_id": "sample_chunk_00",
                "visit_date": "10/24/2023",
                "facility": "MRI Associates",
                "provider_name": "Rudy Heiser",
                "provider_credentials": "DC",
                "visit_type": "imaging",
                "fact_category": "imaging_finding",
                "finding_text": "L5-S1 herniation with annular tear",
                "verbatim_quote": "2mm right foraminal herniation at L5-S1 with annular tear",
                "extraction_confidence": "high",
            },
            {
                "source_file": "sample",
                "source_page": 3,
                "chunk_id": "sample_chunk_00",
                "visit_date": "04/02/2024",
                "facility": "Pacific Spine Clinic",
                "provider_name": "Jane Smith",
                "provider_credentials": "MD",
                "visit_type": "follow-up",
                "fact_category": "plan",
                "finding_text": "Continue PT for four more weeks",
                "verbatim_quote": "Continue PT for four more weeks",
                "extraction_confidence": "high",
            },
        ],
    }
)


_ASSEMBLY_DATE_RE = re.compile(r'"visit_date": "(\d{2}/\d{2}/\d{4})"')


def _assembly_response_for(prompt: str) -> str:
    m = _ASSEMBLY_DATE_RE.search(prompt)
    date_str = m.group(1) if m else "MM/DD/YYYY"
    if date_str == "03/14/2024":
        return (
            "03/14/2024. Pacific Spine Clinic. Jane Smith, MD. Office visit. "
            "Chief Complaint: low back pain after a motor vehicle collision. "
            "Assessment: L4-L5 disc herniation with right radiculopathy. "
            "Plan: Physical therapy and follow-up in two weeks."
        )
    if date_str == "10/24/2023":
        return (
            "10/24/2023. MRI Associates. Rudy Heiser, DC. MRI Lumbar Spine. "
            "Impression: 2mm right foraminal herniation at L5-S1 with annular tear."
        )
    if date_str == "04/02/2024":
        return (
            "04/02/2024. Pacific Spine Clinic. Jane Smith, MD. Follow-up. "
            "Plan: Continue PT for four more weeks."
        )
    return f"{date_str}. Unknown. Unknown. Unknown. Plan: see records."


def _anthropic_stub(prompt: str, **_kwargs) -> str:
    if "SOURCE CHUNK TEXT" in prompt:
        return EXTRACTION_RESPONSE
    if "VISIT IDENTITY" in prompt:
        return _assembly_response_for(prompt)
    if "executive summary" in prompt.lower():
        return (
            "Jane Doe sustained injuries in a rear-end motor vehicle collision on 09/20/2023. "
            "She presented to Pacific Spine Clinic on 03/14/2024 with low back pain and was assessed with L4-L5 disc herniation.\n\n"
            "MRI on 10/24/2023 confirmed 2mm right foraminal herniation at L5-S1 with annular tear. "
            "She returned to Pacific Spine Clinic on 04/02/2024 reporting decreased pain after physical therapy."
        )
    if "gaps analysis" in prompt.lower():
        return (
            "Records do not include the emergency department visit on or near the date of injury 09/20/2023. "
            "Imaging on 10/24/2023 is documented, but referring provider notes are absent. "
            "Therapy records between 03/14/2024 and 04/02/2024 are not in evidence."
        )
    if "patient_name_quote" in prompt:
        return json.dumps(
            {
                "patient_name": "Jane Doe",
                "patient_name_quote": "Patient Name: JANE DOE",
                "dob": "03/14/1980",
                "dob_quote": "Date of Birth: 03/14/1980",
                "doi": "09/20/2023",
                "doi_quote": "Date of Injury: 09/20/2023",
            }
        )
    return ""


def _build_pipeline(tmp_path: Path):
    """Construct PrecisionChronologyPipeline with all external deps mocked."""
    from src.pipeline import PrecisionChronologyPipeline

    pipeline = PrecisionChronologyPipeline.__new__(PrecisionChronologyPipeline)
    pipeline.dropbox_tool = MagicMock()
    pipeline.dropbox_tool.upload_folder.return_value = {
        "success": True,
        "uploaded": [],
        "failed": [],
    }
    pipeline.ocr_client = MagicMock()
    pipeline.anthropic_client = MagicMock()
    pipeline.anthropic_client.complete.side_effect = _anthropic_stub
    pipeline.model_extraction = None
    pipeline.model_assembly = None
    pipeline.base_dir = str(tmp_path)
    from src.session_state import SessionStore

    pipeline.store = SessionStore(str(tmp_path))
    import logging

    pipeline.logger = logging.getLogger("test_pipeline")
    return pipeline


@pytest.mark.timeout(60)
def test_pipeline_end_to_end_produces_all_outputs(tmp_path: Path) -> None:
    pipeline = _build_pipeline(tmp_path)

    # Create session
    state = pipeline.create_session(
        dropbox_link="https://dropbox.example/x", patient_id="JANE", destination_folder="/out"
    )
    sid = state.session_id

    # Pre-populate input PDFs (skip download phase work) and OCR text (skip OCR work)
    input_dir = pipeline.store.input_dir(sid)
    extracted_dir = pipeline.store.extracted_dir(sid)
    (input_dir / "sample.pdf").write_bytes(b"%PDF-1.4 placeholder")
    (extracted_dir / "sample.txt").write_text(FIXTURE_PATH.read_text(encoding="utf-8"))

    result = asyncio.run(pipeline.run(sid))

    assert result["status"] == "complete", result

    out_dir = pipeline.store.output_dir(sid)
    expected = {
        "chronology.md",
        "chronology.docx",
        "chronology.json",
        "cross_check_report.md",
        "summary.md",
        "gaps.md",
    }
    produced = {p.name for p in out_dir.iterdir()}
    missing = expected - produced
    assert not missing, f"missing outputs: {missing} (got {produced})"

    verified_path = pipeline.store.verified_facts_path(sid)
    rejected_path = pipeline.store.rejected_facts_path(sid)
    assert verified_path.exists() and verified_path.stat().st_size > 0
    assert rejected_path.exists()  # may be empty if all quotes verified

    md = (out_dir / "chronology.md").read_text(encoding="utf-8")
    assert "MEDICAL RECORDS SUMMARY" in md
    assert "JANE DOE" in md
    assert "03/14/2024" in md
    assert "10/24/2023" in md
    assert "04/02/2024" in md

    chronology_json = json.loads((out_dir / "chronology.json").read_text(encoding="utf-8"))
    assert chronology_json["metadata"]["patient_name"] == "JANE DOE"
    assert chronology_json["metadata"]["entry_count"] == 3
    assert chronology_json["metadata"]["verified_fact_count"] >= 3

    # DOCX should be a real Office Open XML file (PK zip magic)
    docx_bytes = (out_dir / "chronology.docx").read_bytes()
    assert docx_bytes[:2] == b"PK"
    assert len(docx_bytes) > 1000
