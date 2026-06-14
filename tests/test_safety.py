"""Focused tests for filesystem and download path safety."""

import asyncio
from pathlib import Path

import pytest

from src.pipeline import MedicalChronologyPipeline
from src.session_state import SessionStore, validate_session_id
from src.tools.dropbox_tool import _dropbox_relative_path, _safe_local_path


def test_validate_session_id_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        validate_session_id("../outside")

    with pytest.raises(ValueError):
        validate_session_id("patient/name")


def test_session_store_keeps_paths_inside_sessions_root(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    session_dir = store.session_dir("patient_20260101_120000")

    assert session_dir.parent == tmp_path / "data" / "sessions"
    assert session_dir.name == "patient_20260101_120000"

    with pytest.raises(ValueError):
        store.session_dir("../../outside")


def test_dropbox_relative_path_preserves_nested_folder() -> None:
    rel_path = _dropbox_relative_path(
        "/Patients/Smith",
        "/Patients/Smith/Imaging/report.pdf",
        "report.pdf",
    )

    assert rel_path == "Imaging/report.pdf"


def test_safe_local_path_rejects_parent_segments(tmp_path: Path) -> None:
    target = Path(_safe_local_path(str(tmp_path), "Imaging/report.pdf"))
    assert target == tmp_path / "Imaging" / "report.pdf"

    escaped = Path(_safe_local_path(str(tmp_path), "../report.pdf"))
    assert escaped == tmp_path / "report.pdf"


def test_phase_ocr_recognizes_nested_extracted_text(tmp_path: Path) -> None:
    pipeline = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    pipeline.store = SessionStore(str(tmp_path))

    state = pipeline.store.create(
        session_id="nested_20260101_120000",
        patient_id="nested",
        dropbox_link="",
        destination_folder="/out",
    )
    input_dir = pipeline.store.input_dir(state.session_id)
    extracted_dir = pipeline.store.extracted_dir(state.session_id)

    nested_pdf = input_dir / "Records" / "report.pdf"
    nested_pdf.parent.mkdir(parents=True)
    nested_pdf.write_bytes(b"%PDF-1.4")

    nested_txt = extracted_dir / "Records" / "report.txt"
    nested_txt.parent.mkdir(parents=True)
    nested_txt.write_text("already extracted", encoding="utf-8")

    class NoCallOCRClient:
        async def batch_extract(self, *args, **kwargs):
            raise AssertionError("OCR should not rerun for nested extracted text")

    pipeline.ocr_client = NoCallOCRClient()

    messages = []
    asyncio.run(pipeline._phase_ocr(state, messages.append))

    assert messages == ["   ↳ all PDFs already OCR'd, skipping"]
