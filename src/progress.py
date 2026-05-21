"""Progress reporting interface for the pipeline.

Decouples progress reporting from any specific UI framework. The
pipeline and its sub-phases (extraction, assembly) call methods on a
ProgressReporter; concrete implementations route those calls to
logging, Streamlit, or any other sink.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional


class ProgressReporter(ABC):
    """Abstract progress reporter for the precision-chronology pipeline."""

    @abstractmethod
    def phase_start(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        """Called when a phase begins."""

    @abstractmethod
    def phase_progress(
        self, current: int, total: int, item_description: Optional[str] = None
    ) -> None:
        """Called for sub-phase progress (e.g. chunk 5/86)."""

    @abstractmethod
    def phase_complete(
        self, phase_num: int, summary: Optional[str] = None
    ) -> None:
        """Called when a phase finishes successfully."""

    @abstractmethod
    def phase_log(self, message: str, level: str = "info") -> None:
        """Emit a log message within the current phase."""

    @abstractmethod
    def pipeline_complete(self, summary: Optional[str] = None) -> None:
        """Called when the entire pipeline finishes successfully."""

    @abstractmethod
    def pipeline_failed(self, error: str) -> None:
        """Called when the pipeline fails."""

    @abstractmethod
    def phase_skipped(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        """Called when a phase is skipped (already complete on resume)."""


class ConsoleProgressReporter(ProgressReporter):
    """Routes progress to Python logging. Used by CLI and tests."""

    def __init__(self, logger_name: str = "precision_chronology") -> None:
        self._log = logging.getLogger(logger_name)

    def phase_start(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        self._log.info("▶️  Phase %d/%d: %s", phase_num, total_phases, phase_name)

    def phase_progress(
        self, current: int, total: int, item_description: Optional[str] = None
    ) -> None:
        desc = f" — {item_description}" if item_description else ""
        self._log.info("   ↳ %d/%d%s", current, total, desc)

    def phase_complete(
        self, phase_num: int, summary: Optional[str] = None
    ) -> None:
        msg = f"✅ Phase {phase_num} complete"
        if summary:
            msg += f": {summary}"
        self._log.info(msg)

    def phase_log(self, message: str, level: str = "info") -> None:
        getattr(self._log, level, self._log.info)(message)

    def pipeline_complete(self, summary: Optional[str] = None) -> None:
        self._log.info("✅ Pipeline complete%s", f": {summary}" if summary else "")

    def pipeline_failed(self, error: str) -> None:
        self._log.error("❌ Pipeline failed: %s", error)

    def phase_skipped(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        self._log.info(
            "⏭️  Phase %d/%d: %s already complete — skipping",
            phase_num, total_phases, phase_name,
        )


class CallbackProgressReporter(ProgressReporter):
    """Wraps the legacy Callable[[str], None] callback for backward compat."""

    def __init__(self, callback) -> None:
        self._cb = callback
        self._log = logging.getLogger("precision_chronology")

    def phase_start(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        self._cb(f"▶️  Phase {phase_num}/{total_phases}: {phase_name}")

    def phase_progress(
        self, current: int, total: int, item_description: Optional[str] = None
    ) -> None:
        desc = f" — {item_description}" if item_description else ""
        self._cb(f"   ↳ {current}/{total}{desc}")

    def phase_complete(
        self, phase_num: int, summary: Optional[str] = None
    ) -> None:
        msg = f"✅ Phase {phase_num} complete"
        if summary:
            msg += f": {summary}"
        self._cb(msg)

    def phase_log(self, message: str, level: str = "info") -> None:
        self._cb(message)
        getattr(self._log, level, self._log.info)(message)

    def pipeline_complete(self, summary: Optional[str] = None) -> None:
        self._cb(f"✅ Pipeline complete{f': {summary}' if summary else ''}")

    def pipeline_failed(self, error: str) -> None:
        self._cb(f"❌ Pipeline failed: {error}")

    def phase_skipped(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        self._cb(
            f"⏭️  Phase {phase_num}/{total_phases}: {phase_name} already complete — skipping"
        )


__all__ = [
    "ProgressReporter",
    "ConsoleProgressReporter",
    "CallbackProgressReporter",
]
