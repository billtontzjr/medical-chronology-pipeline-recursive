"""Focused tests for filesystem and download path safety."""

from pathlib import Path

import pytest

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
