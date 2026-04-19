"""Resume-aware orchestrator for the medical chronology pipeline.

Responsibilities:

* Create (or resume) a session rooted at ``data/sessions/<session_id>/``
* Drive each phase (download, OCR, generate, assemble, upload) in order
* Checkpoint progress to ``state.json`` so a killed process can resume
* Honor a cooperative pause signal (``PAUSE`` file) at phase boundaries
* Upload outputs reliably to a caller-chosen Dropbox destination folder

The pipeline itself does not depend on Streamlit. The UI calls
:meth:`MedicalChronologyPipeline.create_session` once, then
:meth:`MedicalChronologyPipeline.run` (optionally many times) to resume.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .chronology_agent import ChronologyAgent
from .ocr_client import OCRClient
from .session_state import (
    PauseRequested,
    SessionState,
    SessionStore,
    PHASE_DOWNLOAD,
    PHASE_GENERATE,
    PHASE_HEADER,
    PHASE_OCR,
    PHASE_SUMMARY,
    PHASE_UPLOAD,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
)
from .tools.dropbox_tool import DropboxTool, normalize_dropbox_folder


DEFAULT_DESTINATION_PREFIX = "/Medical chronology pipeline outputs"


class MedicalChronologyPipeline:
    """Orchestrate a resumable medical chronology run."""

    def __init__(
        self,
        *,
        google_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        dropbox_token: Optional[str] = None,
        base_dir: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.dropbox_tool = DropboxTool(access_token=dropbox_token, use_oauth=True)
        self.ocr_client = OCRClient(google_api_key)
        self.chronology_agent = ChronologyAgent(anthropic_api_key, model=model)

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
        """Create a new session and persist initial state."""
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

    def update_destination(self, session_id: str, new_destination: str) -> SessionState:
        """Change where the outputs will be uploaded. Resets upload phase."""
        state = self.store.load(session_id)
        state.destination_folder = normalize_dropbox_folder(new_destination)
        # Reset upload so a future run re-uploads to the new destination
        up = state.phases.get(PHASE_UPLOAD)
        if up is not None:
            up.status = "pending"
            up.completed_at = None
            up.data = {}
        self.store.save(state)
        return state

    # --------------------------------------------------------------- pause API
    def _should_pause(self, session_id: str) -> bool:
        return self.store.pause_requested(session_id)

    def _check_pause(self, session_id: str) -> None:
        if self._should_pause(session_id):
            raise PauseRequested(f"Pause requested for session {session_id}")

    # -------------------------------------------------------------------- run
    async def run(
        self,
        session_id: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """Run (or resume) the pipeline for ``session_id`` through all phases.

        Idempotent: phases whose outputs already exist on disk are skipped.
        Returns a result dict regardless of success/pause/failure — callers
        inspect ``result['status']``.
        """
        self.store.clear_pause(session_id)
        state = self.store.load(session_id)
        state.status = STATUS_IN_PROGRESS
        state.last_error = None
        self.store.save(state)

        def cb(msg: str) -> None:
            self.logger.info(msg)
            if progress_callback:
                progress_callback(msg)

        try:
            # Phase 1 — download
            self._check_pause(session_id)
            if state.phases[PHASE_DOWNLOAD].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_DOWNLOAD, STATUS_IN_PROGRESS)
                cb("📥 Phase 1/5: Downloading PDFs from Dropbox…")
                await self._phase_download(state, cb)
                self.store.mark_phase(state, PHASE_DOWNLOAD, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 1/5: Download already complete — skipping")

            # Phase 2 — OCR
            self._check_pause(session_id)
            if state.phases[PHASE_OCR].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_OCR, STATUS_IN_PROGRESS)
                cb("🔎 Phase 2/5: Running OCR on PDFs…")
                await self._phase_ocr(state, cb)
                self.store.mark_phase(state, PHASE_OCR, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 2/5: OCR already complete — skipping")

            # Phase 3 — generate batches (resumable at batch granularity)
            self._check_pause(session_id)
            if state.phases[PHASE_GENERATE].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_GENERATE, STATUS_IN_PROGRESS)
                cb("🤖 Phase 3/5: Generating chronology with Claude…")
                await self._phase_generate(state, cb, session_id)
                self.store.mark_phase(state, PHASE_GENERATE, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 3/5: Generation already complete — skipping")

            # Phase 4 — assemble outputs (header + summary + gaps + JSON)
            self._check_pause(session_id)
            if (
                state.phases[PHASE_HEADER].status != STATUS_COMPLETE
                or state.phases[PHASE_SUMMARY].status != STATUS_COMPLETE
            ):
                self.store.mark_phase(state, PHASE_HEADER, STATUS_IN_PROGRESS)
                self.store.mark_phase(state, PHASE_SUMMARY, STATUS_IN_PROGRESS)
                cb("🧾 Phase 4/5: Building final documents…")
                await self._phase_assemble(state, cb)
                self.store.mark_phase(state, PHASE_HEADER, STATUS_COMPLETE)
                self.store.mark_phase(state, PHASE_SUMMARY, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 4/5: Assembly already complete — skipping")

            # Phase 5 — Dropbox upload
            self._check_pause(session_id)
            if state.phases[PHASE_UPLOAD].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_UPLOAD, STATUS_IN_PROGRESS)
                cb(f"📤 Phase 5/5: Uploading outputs to {state.destination_folder}…")
                await self._phase_upload(state, cb)
                self.store.mark_phase(state, PHASE_UPLOAD, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 5/5: Upload already complete — skipping")

            state = self.store.load(session_id)
            state.status = STATUS_COMPLETE
            self.store.save(state)
            cb("🎉 Pipeline complete.")

            return {
                "status": "complete",
                "session_id": session_id,
                "session_dir": str(self.store.session_dir(session_id)),
                "output_dir": str(self.store.output_dir(session_id)),
                "destination_folder": state.destination_folder,
                "output_files": self._list_output_files(session_id),
            }

        except PauseRequested as e:
            state = self.store.load(session_id)
            state.status = STATUS_PAUSED
            self.store.save(state)
            cb(f"⏸️  Paused: {e}")
            return {
                "status": "paused",
                "session_id": session_id,
                "session_dir": str(self.store.session_dir(session_id)),
            }
        except Exception as e:
            self.logger.exception("Pipeline failed")
            state = self.store.load(session_id)
            state.status = STATUS_FAILED
            state.last_error = str(e)
            self.store.save(state)
            return {
                "status": "failed",
                "session_id": session_id,
                "error": str(e),
                "session_dir": str(self.store.session_dir(session_id)),
            }

    # ------------------------------------------------------------------ phases
    async def _phase_download(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        input_dir = str(self.store.input_dir(state.session_id))
        # Already-downloaded PDFs remain on disk; the tool overwrites on match
        # so re-runs are safe. If any PDFs exist we assume the previous run
        # downloaded everything (the download API doesn't support partial
        # resume cleanly).
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
        if not result["success"]:
            raise RuntimeError(f"Dropbox download failed: {result.get('error')}")
        downloaded = [d["name"] for d in result["downloaded"]]
        if not downloaded:
            raise RuntimeError("No PDF files found in the Dropbox link")
        cb(f"   ↳ downloaded {len(downloaded)} PDFs")
        self.store.update_phase_data(
            state, PHASE_DOWNLOAD, {"files": downloaded}
        )

    async def _phase_ocr(self, state: SessionState, cb: Callable[[str], None]) -> None:
        input_dir = self.store.input_dir(state.session_id)
        extracted_dir = self.store.extracted_dir(state.session_id)

        pdf_paths = sorted(list(input_dir.glob("*.pdf")) + list(input_dir.glob("*.PDF")))
        if not pdf_paths:
            raise RuntimeError("No downloaded PDFs found to OCR")

        # Resume: skip PDFs whose .txt already exists and is non-empty
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

        # Drive OCR one file at a time; batch_extract respects that.
        results = await self.ocr_client.batch_extract(
            [str(p) for p in pending], max_concurrent=1, progress_callback=cb
        )
        for r in results:
            if r["success"]:
                self.ocr_client.save_extracted_text(r, str(extracted_dir))
            else:
                self.logger.error(
                    f"OCR failed for {r.get('file_name')}: {r.get('error')}"
                )

        # Verify at least something came out
        if not list(extracted_dir.glob("*.txt")):
            raise RuntimeError("No text could be extracted from PDFs")

    async def _phase_generate(
        self, state: SessionState, cb: Callable[[str], None], session_id: str
    ) -> None:
        extracted_dir = str(self.store.extracted_dir(state.session_id))
        batches_dir = str(self.store.batches_dir(state.session_id))

        # Off the event loop — Claude calls are blocking
        def _work() -> Dict:
            return self.chronology_agent.generate_batches(
                input_dir=extracted_dir,
                batches_dir=batches_dir,
                progress_callback=cb,
                should_pause=lambda: self._should_pause(session_id),
            )

        result = await asyncio.to_thread(_work)
        if not result["success"]:
            raise RuntimeError(result.get("error", "Batch generation failed"))
        self.store.update_phase_data(
            state,
            PHASE_GENERATE,
            {
                "total_batches": result["total_batches"],
                "documents": result["documents"],
                "batches_skipped_from_disk": result.get("batches_skipped_from_disk", 0),
            },
        )

    async def _phase_assemble(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        extracted_dir = str(self.store.extracted_dir(state.session_id))
        batches_dir = str(self.store.batches_dir(state.session_id))
        output_dir = str(self.store.output_dir(state.session_id))

        def _work() -> Dict:
            return self.chronology_agent.assemble_outputs(
                input_dir=extracted_dir,
                batches_dir=batches_dir,
                output_dir=output_dir,
                progress_callback=cb,
            )

        result = await asyncio.to_thread(_work)
        if not result["success"]:
            raise RuntimeError(result.get("error", "Assembly failed"))
        self.store.update_phase_data(
            state,
            PHASE_HEADER,
            {"patient_header": result.get("header", {})},
        )

    async def _phase_upload(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        output_dir = self.store.output_dir(state.session_id)
        destination = normalize_dropbox_folder(state.destination_folder)

        def _work() -> Dict:
            return self.dropbox_tool.upload_folder(
                local_dir=str(output_dir),
                dropbox_folder=destination,
                max_retries=5,
                verify=True,
            )

        result = await asyncio.to_thread(_work)
        if not result["success"]:
            failed_names = [f.get("dropbox_path") for f in result.get("failed", [])]
            raise RuntimeError(
                f"Dropbox upload failed for {len(failed_names)} file(s): "
                f"{failed_names} — {result.get('error', '')}"
            )
        cb(f"   ↳ uploaded {len(result['uploaded'])} files (verified)")
        self.store.update_phase_data(
            state,
            PHASE_UPLOAD,
            {
                "destination": destination,
                "uploaded": [u["name"] for u in result["uploaded"]],
                "verified_all": all(u.get("verified") for u in result["uploaded"]),
            },
        )

    # ------------------------------------------------------------------ utils
    def _list_output_files(self, session_id: str) -> Dict[str, str]:
        out = self.store.output_dir(session_id)
        return {p.name: str(p) for p in sorted(out.iterdir()) if p.is_file()}
