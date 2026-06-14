"""Persistent session state for resumable pipeline runs.

Ported from the predecessor with the phase list updated for the
precision-chronology eight-phase pipeline (download, ocr, extraction,
verification, assembly, cross-check, header-and-summary, upload). Each
session owns a directory under ``data/sessions/<session_id>/`` containing
``state.json``. The state records which phases have completed so the
pipeline can skip already-finished work on resume.

A ``PAUSE`` file in the same directory is a cooperative pause signal --
the pipeline checks for it between phases and at safe checkpoints and
exits cleanly if present. Deleting the file and running the pipeline
again resumes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PHASE_DOWNLOAD = "download"
PHASE_OCR = "ocr"
PHASE_EXTRACTION = "extraction"
PHASE_VERIFICATION = "verification"
PHASE_ASSEMBLY = "assembly"
PHASE_CROSS_CHECK = "cross_check"
PHASE_HEADER_SUMMARY = "header_summary"
PHASE_UPLOAD = "upload"

PHASE_ORDER = [
    PHASE_DOWNLOAD,
    PHASE_OCR,
    PHASE_EXTRACTION,
    PHASE_VERIFICATION,
    PHASE_ASSEMBLY,
    PHASE_CROSS_CHECK,
    PHASE_HEADER_SUMMARY,
    PHASE_UPLOAD,
]

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"


@dataclass
class PhaseState:
    status: str = STATUS_PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    data: Dict = field(default_factory=dict)
    progress: Optional[Dict] = None  # {"current": int, "total": int, "item": str}


@dataclass
class SessionState:
    session_id: str
    patient_id: str
    dropbox_link: str
    destination_folder: str
    created_at: str
    updated_at: str
    status: str = STATUS_IN_PROGRESS
    last_error: Optional[str] = None
    phases: Dict[str, PhaseState] = field(default_factory=dict)
    runner_pid: Optional[int] = None  # PID of background runner thread/process

    @classmethod
    def new(
        cls,
        session_id: str,
        patient_id: str,
        dropbox_link: str,
        destination_folder: str,
    ) -> "SessionState":
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            session_id=session_id,
            patient_id=patient_id,
            dropbox_link=dropbox_link,
            destination_folder=destination_folder,
            created_at=now,
            updated_at=now,
            phases={p: PhaseState() for p in PHASE_ORDER},
        )

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "patient_id": self.patient_id,
            "dropbox_link": self.dropbox_link,
            "destination_folder": self.destination_folder,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "last_error": self.last_error,
            "phases": {k: asdict(v) for k, v in self.phases.items()},
            "runner_pid": self.runner_pid,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "SessionState":
        raw_phases = d.get("phases", {})
        phases: Dict[str, PhaseState] = {}
        for k, v in raw_phases.items():
            # Backward compat: older state.json files may lack 'progress'
            v_copy = dict(v)
            v_copy.setdefault("progress", None)
            phases[k] = PhaseState(**v_copy)
        for p in PHASE_ORDER:
            phases.setdefault(p, PhaseState())
        return cls(
            session_id=d["session_id"],
            patient_id=d.get("patient_id", ""),
            dropbox_link=d.get("dropbox_link", ""),
            destination_folder=d.get("destination_folder", ""),
            created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
            updated_at=d.get("updated_at", datetime.now().isoformat(timespec="seconds")),
            status=d.get("status", STATUS_IN_PROGRESS),
            last_error=d.get("last_error"),
            phases=phases,
            runner_pid=d.get("runner_pid"),
        )


class SessionStore:
    """File-backed session store.

    Layout::

        base_dir/
          data/
            sessions/
              <session_id>/
                state.json
                PAUSE                       (optional marker file)
                input/                      (downloaded PDFs)
                extracted/                  (OCR text with page markers)
                extracted_facts/            (per-chunk extraction JSON)
                verified_facts.jsonl
                verification_rejected.jsonl
                output/                     (chronology.md, summary.md, ...)
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.sessions_root = self.base_dir / "data" / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def session_dir(self, session_id: str) -> Path:
        d = self.sessions_root / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "state.json"

    def pause_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "PAUSE"

    def input_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id) / "input"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def extracted_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id) / "extracted"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def extracted_facts_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id) / "extracted_facts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def output_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id) / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def verified_facts_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "verified_facts.jsonl"

    def rejected_facts_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "verification_rejected.jsonl"

    # ------------------------------------------------------------------- crud
    def create(
        self,
        session_id: str,
        patient_id: str,
        dropbox_link: str,
        destination_folder: str,
    ) -> SessionState:
        state = SessionState.new(session_id, patient_id, dropbox_link, destination_folder)
        self.save(state)
        return state

    def exists(self, session_id: str) -> bool:
        return self.state_path(session_id).exists()

    def load(self, session_id: str) -> SessionState:
        path = self.state_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"No session state at {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return SessionState.from_dict(data)

    def save(self, state: SessionState) -> None:
        state.updated_at = datetime.now().isoformat(timespec="seconds")
        path = self.state_path(state.session_id)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.replace(tmp, path)  # atomic on POSIX

    def list_sessions(self) -> List[SessionState]:
        sessions: List[SessionState] = []
        if not self.sessions_root.exists():
            return sessions
        for child in sorted(self.sessions_root.iterdir(), reverse=True):
            if not child.is_dir() or not (child / "state.json").exists():
                continue
            try:
                sessions.append(self.load(child.name))
            except Exception:
                continue
        return sessions

    def delete(self, session_id: str) -> None:
        import shutil

        d = self.sessions_root / session_id
        if d.exists():
            shutil.rmtree(d)

    def reset_from_phase(self, state: SessionState, phase: str) -> SessionState:
        """Mark ``phase`` and every later phase pending so a Resume re-runs them.

        Earlier phases keep their completed status (and their on-disk artifacts:
        downloaded PDFs, OCR text, extracted facts, verified facts), so a Resume
        reuses all of that and only re-runs from ``phase`` onward. Useful for
        regenerating the chronology after an assembly/rendering change without
        re-OCRing or re-extracting.
        """
        if phase not in PHASE_ORDER:
            raise ValueError(f"Unknown phase: {phase}")
        start = PHASE_ORDER.index(phase)
        for ph in PHASE_ORDER[start:]:
            p = state.phases.setdefault(ph, PhaseState())
            p.status = STATUS_PENDING
            p.error = None
            p.completed_at = None
            p.progress = None
        state.status = STATUS_IN_PROGRESS
        state.last_error = None
        self.save(state)
        return state

    # ------------------------------------------------------------- disk mgmt
    def disk_usage(self) -> Tuple[int, int, int]:
        """Return ``(total, used, free)`` bytes for the sessions filesystem.

        Falls back to the base dir if ``sessions_root`` doesn't exist yet.
        """
        import shutil

        target = self.sessions_root if self.sessions_root.exists() else self.base_dir
        u = shutil.disk_usage(str(target))
        return u.total, u.used, u.free

    def disk_used_fraction(self) -> float:
        total, used, _ = self.disk_usage()
        if total <= 0:
            return 0.0
        return used / total

    def prune_completed_sessions(
        self,
        *,
        high_water: float = 0.70,
        low_water: float = 0.50,
        keep_minimum: int = 1,
        dry_run: bool = False,
    ) -> List[str]:
        """Free disk when usage gets uncomfortably high.

        When ``disk_used_fraction`` exceeds ``high_water``, delete the
        oldest completed sessions one by one until usage drops below
        ``low_water`` or only ``keep_minimum`` completed sessions remain.

        Failed/paused/in-progress sessions are NEVER pruned automatically.
        Use the explicit ``delete`` method for those.

        Returns the list of session_ids that were deleted.
        """
        used_frac = self.disk_used_fraction()
        if used_frac <= high_water:
            return []

        sessions = self.list_sessions()
        completed = sorted(
            [s for s in sessions if s.status == STATUS_COMPLETE],
            key=lambda s: s.updated_at,
        )  # oldest first

        if len(completed) <= keep_minimum:
            return []

        deleted: List[str] = []
        for sess in completed[:-keep_minimum]:
            if self.disk_used_fraction() <= low_water:
                break
            if not dry_run:
                try:
                    self.delete(sess.session_id)
                except Exception:
                    continue
            deleted.append(sess.session_id)
        return deleted

    # ---------------------------------------------------------------- phases
    def mark_phase(
        self,
        state: SessionState,
        phase: str,
        status: str,
        *,
        error: Optional[str] = None,
        data: Optional[Dict] = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        p = state.phases.setdefault(phase, PhaseState())
        p.status = status
        if status == STATUS_IN_PROGRESS and not p.started_at:
            p.started_at = now
        if status == STATUS_COMPLETE:
            p.completed_at = now
        if error is not None:
            p.error = error
        if data is not None:
            p.data.update(data)
        if status == STATUS_FAILED:
            state.status = STATUS_FAILED
            state.last_error = error
        elif all(
            state.phases.get(ph, PhaseState()).status == STATUS_COMPLETE
            for ph in PHASE_ORDER
        ):
            state.status = STATUS_COMPLETE
        self.save(state)

    def update_phase_data(self, state: SessionState, phase: str, data: Dict) -> None:
        p = state.phases.setdefault(phase, PhaseState())
        p.data.update(data)
        self.save(state)

    def update_phase_progress(
        self,
        state: SessionState,
        phase: str,
        current: int,
        total: int,
        item: str = "",
    ) -> None:
        """Persist sub-phase progress (e.g. chunk 5/86, visit 12/251)."""
        p = state.phases.setdefault(phase, PhaseState())
        p.progress = {"current": current, "total": total, "item": item}
        self.save(state)

    # ----------------------------------------------------------------- pause
    def request_pause(self, session_id: str) -> None:
        self.pause_path(session_id).write_text(
            datetime.now().isoformat(timespec="seconds")
        )

    def clear_pause(self, session_id: str) -> None:
        p = self.pause_path(session_id)
        if p.exists():
            p.unlink()

    def pause_requested(self, session_id: str) -> bool:
        return self.pause_path(session_id).exists()


class PauseRequested(Exception):
    """Raised inside the pipeline when a cooperative pause is detected."""
