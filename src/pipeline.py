"""Resume-aware orchestrator for the precision-chronology pipeline.

Mirrors the predecessor's structure (create_session / load_session /
list_sessions / async run) so the Streamlit UI and CLI need only
trivial changes. The eight-phase loop differs from the predecessor's
five-phase version by inserting the anti-hallucination middle phases:
extraction, verification, assembly, cross-check, header+summary.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.anthropic_client import AnthropicClient
from src.assembler import run_assembly
from src.progress import (
    CallbackProgressReporter,
    ConsoleProgressReporter,
    ProgressReporter,
)
from src.cross_checker import (
    CROSS_CHECK_JACCARD_THRESHOLD,
    CrossCheckFailed,
    cross_check_and_report,
)
from src.extractor import run_extraction
from src.header_extractor import concatenated_ocr, extract_header
from src.ocr_client import OCRClient
from src.renderers import (
    inject_header_into_chronology,
    render_chronology_docx,
    render_chronology_json,
)
from src.schemas import VerifiedFact
from src.session_state import (
    PHASE_ASSEMBLY,
    PHASE_CROSS_CHECK,
    PHASE_DOWNLOAD,
    PHASE_EXTRACTION,
    PHASE_HEADER_SUMMARY,
    PHASE_OCR,
    PHASE_UPLOAD,
    PHASE_VERIFICATION,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
    PauseRequested,
    SessionState,
    SessionStore,
)
from src.summary_generator import generate_summary_and_gaps
from src.tools.dropbox_tool import DropboxTool, normalize_dropbox_folder
from src.verifier import verify_session


DEFAULT_DESTINATION_PREFIX = "/Precision chronology pipeline outputs"


class PrecisionChronologyPipeline:
    """Orchestrate a resumable precision-chronology run."""

    PHASE_LABELS = [
        ("download", "Downloading PDFs from Dropbox"),
        ("ocr", "Running OCR with page markers"),
        ("extraction", "Extracting atomic facts (Claude)"),
        ("verification", "Verifying every quote against the source"),
        ("assembly", "Assembling chronology entries (Claude)"),
        ("cross_check", "Cross-checking entries vs. verified facts"),
        ("header_summary", "Header, summary, gaps, JSON, DOCX"),
        ("upload", "Uploading outputs to Dropbox"),
    ]

    def __init__(
        self,
        *,
        google_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        dropbox_token: Optional[str] = None,
        base_dir: Optional[str] = None,
        model_extraction: Optional[str] = None,
        model_assembly: Optional[str] = None,
    ) -> None:
        self.dropbox_tool = DropboxTool(access_token=dropbox_token, use_oauth=True)
        self.ocr_client = OCRClient(google_api_key)
        self.anthropic_client = AnthropicClient(anthropic_api_key)

        self.model_extraction = model_extraction
        self.model_assembly = model_assembly

        self.base_dir = str(Path(base_dir) if base_dir else Path(__file__).parent.parent)
        self.store = SessionStore(self.base_dir)

        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------ session mgmt
    def _default_destination(self, session_id: str) -> str:
        return f"{DEFAULT_DESTINATION_PREFIX}/{session_id}"

    def create_session(
        self,
        dropbox_link: str,
        patient_id: Optional[str],
        destination_folder: Optional[str] = None,
    ) -> SessionState:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"{patient_id}_{timestamp}" if patient_id else timestamp
        destination = normalize_dropbox_folder(
            destination_folder or self._default_destination(session_id)
        )
        return self.store.create(
            session_id=session_id,
            patient_id=patient_id or "",
            dropbox_link=dropbox_link,
            destination_folder=destination,
        )

    def list_sessions(self) -> List[SessionState]:
        return self.store.list_sessions()

    def load_session(self, session_id: str) -> SessionState:
        return self.store.load(session_id)

    def request_pause(self, session_id: str) -> None:
        self.store.request_pause(session_id)

    def clear_pause(self, session_id: str) -> None:
        self.store.clear_pause(session_id)

    def update_destination(self, session_id: str, new_destination: str) -> SessionState:
        state = self.store.load(session_id)
        state.destination_folder = normalize_dropbox_folder(new_destination)
        up = state.phases.get(PHASE_UPLOAD)
        if up is not None:
            up.status = "pending"
            up.completed_at = None
            up.data = {}
        self.store.save(state)
        return state

    # ----------------------------------------------------------------- pause
    def _should_pause(self, session_id: str) -> bool:
        return self.store.pause_requested(session_id)

    def _check_pause(self, session_id: str) -> None:
        if self._should_pause(session_id):
            raise PauseRequested(f"Pause requested for session {session_id}")

    # -------------------------------------------------------------- background
    def run_in_background(
        self,
        session_id: str,
        **run_kwargs,
    ) -> str:
        """Spawn a daemon thread that runs the pipeline to completion.

        Returns the session_id. The caller can poll ``state.json`` on
        disk via ``load_session`` to track progress.
        """
        import asyncio as _asyncio

        state = self.store.load(session_id)
        state.runner_pid = os.getpid()
        state.status = STATUS_IN_PROGRESS
        self.store.save(state)

        def _target() -> None:
            try:
                _asyncio.run(self.run(session_id, **run_kwargs))
            except Exception:
                self.logger.exception("Background pipeline thread failed")
            finally:
                # Clear runner_pid on completion
                try:
                    s = self.store.load(session_id)
                    s.runner_pid = None
                    self.store.save(s)
                except Exception:
                    pass

        t = threading.Thread(target=_target, daemon=True, name=f"pipeline-{session_id}")
        t.start()
        return session_id

    def is_running(self, session_id: str) -> bool:
        """Check if a background pipeline thread is alive for this session."""
        try:
            state = self.store.load(session_id)
        except FileNotFoundError:
            return False
        if state.status != STATUS_IN_PROGRESS:
            return False
        if state.runner_pid and state.runner_pid == os.getpid():
            for t in threading.enumerate():
                if t.name == f"pipeline-{session_id}" and t.is_alive():
                    return True
        return False

    # ----------------------------------------------------------------- run
    async def run(
        self,
        session_id: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        *,
        progress_reporter: Optional[ProgressReporter] = None,
        model_extraction: Optional[str] = None,
        model_assembly: Optional[str] = None,
        strict_cross_check: bool = False,
        cross_check_threshold: float = CROSS_CHECK_JACCARD_THRESHOLD,
    ) -> Dict:
        """Run (or resume) the pipeline. Returns a result dict regardless of
        success/pause/failure; callers inspect ``result['status']``.

        Accepts either a ``ProgressReporter`` instance or a legacy
        ``progress_callback`` callable. If both are provided, the
        reporter wins.
        """
        # Resolve reporter: explicit > legacy callback > default console
        if progress_reporter is not None:
            reporter = progress_reporter
        elif progress_callback is not None:
            reporter = CallbackProgressReporter(progress_callback)
        else:
            reporter = ConsoleProgressReporter()

        self.store.clear_pause(session_id)
        state = self.store.load(session_id)
        state.status = STATUS_IN_PROGRESS
        state.last_error = None
        self.store.save(state)

        # Legacy cb wrapper for phase runners that still accept Callable
        def cb(msg: str) -> None:
            self.logger.info(msg)
            reporter.phase_log(msg)

        model_ext = model_extraction or self.model_extraction
        model_asm = model_assembly or self.model_assembly
        total_phases = len(self.PHASE_LABELS)

        phases = [
            (PHASE_DOWNLOAD, self._phase_download),
            (PHASE_OCR, self._phase_ocr),
            (PHASE_EXTRACTION, lambda s, c: self._phase_extraction(s, c, model_ext)),
            (PHASE_VERIFICATION, self._phase_verification),
            (PHASE_ASSEMBLY, lambda s, c: self._phase_assembly(s, c, model_asm)),
            (
                PHASE_CROSS_CHECK,
                lambda s, c: self._phase_cross_check(
                    s, c, threshold=cross_check_threshold, strict=strict_cross_check
                ),
            ),
            (PHASE_HEADER_SUMMARY, lambda s, c: self._phase_header_summary(s, c, model_asm)),
            (PHASE_UPLOAD, self._phase_upload),
        ]

        try:
            for idx, (phase_name, runner) in enumerate(phases, start=1):
                self._check_pause(session_id)
                label = next(lab for name, lab in self.PHASE_LABELS if name == phase_name)
                if state.phases[phase_name].status == STATUS_COMPLETE:
                    reporter.phase_skipped(idx, total_phases, label)
                    continue
                self.store.mark_phase(state, phase_name, STATUS_IN_PROGRESS)
                reporter.phase_start(idx, total_phases, label)
                runner(state, cb)
                self.store.mark_phase(state, phase_name, STATUS_COMPLETE)
                reporter.phase_complete(idx)

            reporter.pipeline_complete()
            return {
                "status": "complete",
                "session_id": session_id,
                "destination": state.destination_folder,
            }

        except PauseRequested as exc:
            reporter.phase_log(f"⏸️  Paused: {exc}")
            state.status = STATUS_PAUSED
            self.store.save(state)
            return {"status": "paused", "session_id": session_id}

        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Pipeline failure")
            reporter.pipeline_failed(str(exc))
            for ph in state.phases.values():
                if ph.status == STATUS_IN_PROGRESS:
                    ph.status = STATUS_FAILED
                    ph.error = str(exc)
            state.status = STATUS_FAILED
            state.last_error = str(exc)
            self.store.save(state)
            return {"status": "failed", "session_id": session_id, "error": str(exc)}

    # ----------------------------------------------------------------- phases
    def _phase_download(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        input_dir = str(self.store.input_dir(state.session_id))
        existing = list(Path(input_dir).glob("*.pdf")) + list(Path(input_dir).glob("*.PDF"))
        if existing:
            cb(f"   ↳ {len(existing)} PDF(s) already on disk, skipping re-download")
            self.store.update_phase_data(
                state, PHASE_DOWNLOAD, {"files": [p.name for p in existing]}
            )
            return

        result = self.dropbox_tool.get_shared_link_files(
            state.dropbox_link, input_dir, extensions=[".pdf", ".PDF"]
        )
        if not result.get("success"):
            raise RuntimeError(f"Dropbox download failed: {result.get('error')}")
        downloaded = [d["name"] for d in result["downloaded"]]
        if not downloaded:
            raise RuntimeError("No PDF files found in the Dropbox link")
        cb(f"   ↳ downloaded {len(downloaded)} PDFs")
        self.store.update_phase_data(state, PHASE_DOWNLOAD, {"files": downloaded})

    def _phase_ocr(self, state: SessionState, cb: Callable[[str], None]) -> None:
        input_dir = self.store.input_dir(state.session_id)
        extracted_dir = self.store.extracted_dir(state.session_id)

        pdf_paths = sorted(list(input_dir.glob("*.pdf")) + list(input_dir.glob("*.PDF")))
        if not pdf_paths:
            raise RuntimeError("No downloaded PDFs found to OCR")

        pending: List[Path] = []
        for p in pdf_paths:
            txt = extracted_dir / (p.stem + ".txt")
            if txt.exists() and txt.stat().st_size > 0:
                continue
            pending.append(p)

        if not pending:
            cb("   ↳ all PDFs already OCR'd, skipping")
            return

        cb(f"   ↳ OCR'ing {len(pending)} of {len(pdf_paths)} PDFs")
        results = self.ocr_client.batch_extract(
            [str(p) for p in pending], progress_callback=cb
        )
        for r in results:
            if r.get("success"):
                self.ocr_client.save_extracted_text(r, str(extracted_dir))
            else:
                self.logger.error("OCR failed for %s: %s", r.get("file_name"), r.get("error"))

        if not list(extracted_dir.glob("*.txt")):
            raise RuntimeError("No text could be extracted from PDFs")

    def _phase_extraction(
        self,
        state: SessionState,
        cb: Callable[[str], None],
        model: Optional[str],
    ) -> None:
        summary = run_extraction(
            self.store,
            state.session_id,
            self.anthropic_client,
            model=model,
            progress_callback=cb,
        )
        self.store.update_phase_data(state, PHASE_EXTRACTION, summary)

    def _phase_verification(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        cb("   ↳ Running deterministic substring verifier")
        report = verify_session(self.store, state.session_id)
        cb(
            f"   ↳ {report['verified']} verified / {report['rejected']} rejected"
        )
        self.store.update_phase_data(state, PHASE_VERIFICATION, report)

    def _phase_assembly(
        self,
        state: SessionState,
        cb: Callable[[str], None],
        model: Optional[str],
    ) -> None:
        summary = run_assembly(
            self.store,
            state.session_id,
            self.anthropic_client,
            model=model,
            progress_callback=cb,
        )
        self.store.update_phase_data(state, PHASE_ASSEMBLY, summary)

    def _phase_cross_check(
        self,
        state: SessionState,
        cb: Callable[[str], None],
        *,
        threshold: float,
        strict: bool,
    ) -> None:
        md_path = self.store.output_dir(state.session_id) / "chronology.md"
        vfp = self.store.verified_facts_path(state.session_id)
        report_path = self.store.output_dir(state.session_id) / "cross_check_report.md"
        try:
            report = cross_check_and_report(
                md_path, vfp, report_path, threshold=threshold, strict=strict
            )
        except CrossCheckFailed as exc:
            raise RuntimeError(f"Strict cross-check failed: {exc}") from exc
        cb(
            f"   ↳ {len(report.warnings)} phrase warning(s) "
            f"across {report.entries_checked} entries"
        )
        self.store.update_phase_data(
            state,
            PHASE_CROSS_CHECK,
            {
                "warnings": len(report.warnings),
                "entries_checked": report.entries_checked,
                "phrases_checked": report.phrases_checked,
                "threshold": threshold,
                "strict": strict,
            },
        )

    def _phase_header_summary(
        self,
        state: SessionState,
        cb: Callable[[str], None],
        model: Optional[str],
    ) -> None:
        extracted_dir = self.store.extracted_dir(state.session_id)
        output_dir = self.store.output_dir(state.session_id)
        md_path = output_dir / "chronology.md"

        cb("   ↳ Extracting patient header")
        text = concatenated_ocr(extracted_dir)
        header = extract_header(
            text, anthropic_client=self.anthropic_client, model=model
        )

        inject_header_into_chronology(md_path, header)

        cb("   ↳ Generating summary + gaps")
        generate_summary_and_gaps(
            self.store,
            state.session_id,
            self.anthropic_client,
            model=model,
            progress_callback=cb,
        )

        cb("   ↳ Rendering JSON + DOCX")
        verified_facts = _load_verified_facts(
            self.store.verified_facts_path(state.session_id)
        )
        markdown = md_path.read_text(encoding="utf-8")
        render_chronology_json(
            header,
            markdown,
            verified_facts,
            output_path=output_dir / "chronology.json",
        )
        render_chronology_docx(markdown, header, output_dir / "chronology.docx")

        self.store.update_phase_data(
            state,
            PHASE_HEADER_SUMMARY,
            {
                "patient_name": header.patient_name,
                "dob": header.dob,
                "doi": header.doi,
            },
        )

    def _phase_upload(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        output_dir = self.store.output_dir(state.session_id)
        destination = normalize_dropbox_folder(state.destination_folder)
        result = self.dropbox_tool.upload_folder(
            local_dir=str(output_dir),
            dropbox_folder=destination,
        )
        if not result.get("success", True):
            failed_names = [f.get("name") for f in result.get("failed", [])]
            raise RuntimeError(
                f"Dropbox upload failed for {len(failed_names)} file(s): {failed_names}"
            )
        cb(f"   ↳ uploaded {len(result.get('uploaded', []))} files")
        self.store.update_phase_data(
            state,
            PHASE_UPLOAD,
            {
                "destination": destination,
                "uploaded": [u.get("name") for u in result.get("uploaded", [])],
            },
        )


def _load_verified_facts(jsonl_path: Path) -> List[VerifiedFact]:
    if not jsonl_path.exists():
        return []
    out: List[VerifiedFact] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(VerifiedFact.model_validate_json(line))
    return out


__all__ = ["PrecisionChronologyPipeline"]
