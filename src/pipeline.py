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
import json
import logging
import re
import shutil
import uuid
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .chronology_agent import ChronologyAgent
from .ocr_client import OCRClient
from .ocr_coverage import collect_coverage, coverage_path, save_coverage
from .deposition_evidence import atomic_json
from .manual_review import collect_manual_reviews, review_markdown, DRAFT_LABEL
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
from .tools.dropbox_tool import DropboxTool
from .output_safety import OUTPUT_ROOT, validate_destination
from .session_lock import session_lock
from .download_snapshot import download_snapshot
from .session_model import saved_model, save_model_config


DEFAULT_DESTINATION_PREFIX = OUTPUT_ROOT


def safe_patient_id(patient_id: Optional[str]) -> str:
    """Convert a patient label into a safe session-id prefix."""
    if not patient_id:
        return ""
    value = patient_id.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    if not value:
        raise ValueError("Patient ID must include at least one letter or number.")
    return value[:80]


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
        openai_api_key: Optional[str] = None,
    ) -> None:
        self.dropbox_tool = DropboxTool(access_token=dropbox_token, use_oauth=True)
        self.ocr_client = OCRClient(google_api_key)
        self.chronology_agent = ChronologyAgent(anthropic_api_key, model=model, openai_api_key=openai_api_key)

        self.base_dir = str(Path(base_dir) if base_dir else Path(__file__).parent.parent)
        self.store = SessionStore(self.base_dir)

        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------ session mgmt
    def _lock_path(self, session_id):
        from .session_state import validate_session_id
        root = self.store.sessions_root.parent / 'locks'
        root.mkdir(parents=True, exist_ok=True)
        return root / (validate_session_id(session_id) + '.lock')

    def archive_session(self, session_id):
        with session_lock(self._lock_path(session_id)):
            source = self.store.session_dir(session_id)
            target = self.store.sessions_root.parent / 'archive' / session_id
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeError('An archived case already has this identifier.')
            source.rename(target)

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
        patient_prefix = safe_patient_id(patient_id)
        session_id = (f"{patient_prefix}_{timestamp}" if patient_prefix else timestamp) + '_' + uuid.uuid4().hex[:8]
        destination = validate_destination(
            destination_folder or self._default_destination(session_id), dropbox_link)
        return self.store.create(
            session_id=session_id,
            patient_id=patient_prefix,
            dropbox_link=dropbox_link,
            destination_folder=destination,
        )

    def refresh_session(self, session_id: str, *, model_candidates=()) -> SessionState:
        """Create an isolated new run without changing the original case."""
        with session_lock(self._lock_path(session_id)):
            original = self.store.load(session_id)
            model = saved_model(self.chronology_agent, self.store.extracted_dir(session_id),
                                self.store.batches_dir(session_id), model_candidates, persist=False)
            refreshed = self.create_session(original.dropbox_link, original.patient_id)
            save_model_config(self.store.batches_dir(refreshed.session_id), model)
            return refreshed

    def list_sessions(self) -> List[SessionState]:
        return self.store.list_sessions()

    def _check_saved_model(self, session_id):
        path = self.store.batches_dir(session_id) / 'run_model.json'
        if path.exists():
            model = saved_model(self.chronology_agent, self.store.extracted_dir(session_id),
                                path.parent, (), persist=False)
            if model != self.chronology_agent.model:
                raise ValueError('The pipeline model differs from the saved run model. '
                                 'Reload this run with its saved model before processing.')

    def load_session(self, session_id: str) -> SessionState:
        return self.store.load(session_id)

    def request_pause(self, session_id: str) -> None:
        self.store.request_pause(session_id)

    def update_destination(self, session_id: str, new_destination: str) -> SessionState:
        with session_lock(self._lock_path(session_id)):
            return self._update_destination(session_id, new_destination)

    def _update_destination(self, session_id: str, new_destination: str) -> SessionState:
        """Change where the outputs will be uploaded. Resets upload phase."""
        state = self.store.load(session_id)
        state.destination_folder = validate_destination(new_destination, state.dropbox_link)
        # Reset upload so a future run re-uploads to the new destination
        up = state.phases.get(PHASE_UPLOAD)
        if up is not None:
            up.status = "pending"
            up.completed_at = None
            up.data = {}
        self.store.save(state)
        return state

    def verify_session(
        self, session_id: str, progress_callback=None,
    ) -> Dict:
        try:
            with session_lock(self._lock_path(session_id)):
                self._check_saved_model(session_id)
                return self._verify_session(session_id, progress_callback)
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    def _verify_session(
        self,
        session_id: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """Verify a completed chronology against extracted source text."""
        state = self.store.load(session_id)
        chronology_path = self.store.output_dir(session_id) / "chronology.md"
        if not chronology_path.exists():
            return {
                "success": False,
                "error": "chronology.md does not exist for this session.",
            }

        pending_reviews = collect_manual_reviews(self.store.batches_dir(session_id))
        has_entries = re.search(r'^\d{1,2}/\d{1,2}/\d{4}\b', chronology_path.read_text(), re.M)
        if pending_reviews and not has_entries:
            result = {'success': True, 'verification': 'No dated entries are available for automated verification. '
                      'All withheld source sections remain pending manual review.', 'documents_checked': 0}
        else:
            result = self.chronology_agent.verify_chronology(
                chronology_path=str(chronology_path),
                extracted_dir=str(self.store.extracted_dir(session_id)),
                progress_callback=progress_callback,
                checkpoint_path=self.store.session_dir(session_id) / 'verification-work.json',
            )
        if not result.get("success"):
            return result

        report_path = self.store.output_dir(session_id) / "verification.md"
        report_text = result.get("verification", "")
        coverage = collect_coverage(self.store.input_dir(session_id), self.store.extracted_dir(session_id))
        if (not state.phases[PHASE_DOWNLOAD].data.get('manifest_version') or
                coverage['files_needing_review'] or not coverage['files']):
            report_text = ('SOURCE COMPLETENESS NOT ESTABLISHED. The source inventory or page extraction '
                           'requires review. This report cannot establish that all original records were read.\n\n' + report_text)

        if pending_reviews:
            report_text = (review_markdown(pending_reviews) + '\n\nAUTOMATED VERIFICATION OF DRAFT\n\n'
                           + report_text)
        temporary = report_path.with_suffix('.md.tmp')
        temporary.write_text(report_text, encoding='utf-8')
        temporary.replace(report_path)
        state.phases[PHASE_SUMMARY].data["verification_report"] = str(report_path)
        self.store.save(state)
        return {
            "success": True,
            "verification_path": str(report_path),
            "documents_checked": result.get("documents_checked", 0),
            "manual_review_count": len(pending_reviews),
            "review_status": result.get('review_status', 'incomplete'),
            "entries_reviewed": result.get('entries_reviewed', 0),
            "entries_unreviewed": result.get('entries_unreviewed', 0),
        }

    # --------------------------------------------------------------- pause API
    def _should_pause(self, session_id: str) -> bool:
        return self.store.pause_requested(session_id)

    def _check_pause(self, session_id: str) -> None:
        if self._should_pause(session_id):
            raise PauseRequested(f"Pause requested for session {session_id}")

    # -------------------------------------------------------------------- run
    async def run(
        self, session_id: str, progress_callback=None,
    ) -> Dict:
        try:
            with session_lock(self._lock_path(session_id)):
                self._check_saved_model(session_id)
                return await self._run_session(session_id, progress_callback)
        except Exception as exc:
            return {'status': 'failed', 'session_id': session_id, 'error': str(exc)}

    async def _run_session(
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
                self._validate_input_snapshot(state)
                cb("⏭️  Phase 1/5: Saved source inventory checked — skipping download")

            # Phase 2 — OCR
            self._check_pause(session_id)
            if state.phases[PHASE_GENERATE].status == STATUS_COMPLETE:
                prior_coverage = collect_coverage(self.store.input_dir(session_id), self.store.extracted_dir(session_id))
                if (prior_coverage['technical_failures'] or
                        any(r.get('coverage_status') == 'unknown_legacy' for r in prior_coverage['files'])):
                    raise RuntimeError('The saved extraction cannot be reconciled with this generated draft. Start a refreshed run; the existing draft is preserved.')

            if state.phases[PHASE_OCR].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_OCR, STATUS_IN_PROGRESS)
                cb("🔎 Phase 2/5: Running OCR on PDFs…")
                await self._phase_ocr(state, cb)
                self.store.mark_phase(state, PHASE_OCR, STATUS_COMPLETE)
            else:
                cb("⏭️  Phase 2/5: OCR already complete — skipping")

            # Legacy completed OCR phases also need an honest coverage report.
            coverage = collect_coverage(self.store.input_dir(session_id), self.store.extracted_dir(session_id))
            self.store.update_phase_data(state, PHASE_OCR, {'coverage': coverage})
            if coverage['files_needing_review']:
                cb(f"⚠️ OCR coverage: {coverage['files_needing_review']} file(s) need page review; see the coverage report")
            if coverage['technical_failures'] or any(r.get('coverage_status') == 'unknown_legacy' for r in coverage['files']):
                self.store.mark_phase(state, PHASE_OCR, STATUS_FAILED)
                raise RuntimeError("OCR has failed or missing pages. Resume to retry extraction before generating a chronology.")

            # Phase 3 — generate batches (resumable at batch granularity)
            self._check_pause(session_id)
            if state.phases[PHASE_GENERATE].status != STATUS_COMPLETE:
                self.store.mark_phase(state, PHASE_GENERATE, STATUS_IN_PROGRESS)
                cb(f"🤖 Phase 3/5: Generating chronology with {self.chronology_agent.model}…")
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
            review_count = state.phases[PHASE_HEADER].data.get('manual_review_count', 0)
            cb(f"⚠️ {DRAFT_LABEL}: {review_count} source section(s)." if review_count
               else "🎉 Pipeline complete.")

            return {
                "status": "complete",
                "session_id": session_id,
                "session_dir": str(self.store.session_dir(session_id)),
                "output_dir": str(self.store.output_dir(session_id)),
                "destination_folder": state.destination_folder,
                "output_files": self._list_output_files(session_id),
                "manual_review_count": review_count,
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
            for phase in state.phases.values():
                if phase.status == STATUS_IN_PROGRESS:
                    phase.status = STATUS_FAILED
                    phase.error = str(e)
            self.store.save(state)
            return {
                "status": "failed",
                "session_id": session_id,
                "error": str(e),
                "session_dir": str(self.store.session_dir(session_id)),
            }

    # ------------------------------------------------------------------ phases
    def _validate_input_snapshot(self, state):
        data = state.phases[PHASE_DOWNLOAD].data
        manifest = data.get('manifest')
        if data.get('manifest_version') != 1 or not manifest:
            raise RuntimeError('This older run has no confirmed source inventory. Use Start refreshed run to download and extract a new copy; the existing draft stays available.')
        root = self.store.input_dir(state.session_id)
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'}
        if actual != {m['path'] for m in manifest}:
            raise RuntimeError('Saved PDF inventory changed. Start a refreshed run before generating more output.')
        for item in manifest:
            path = root / item['path']
            if path.stat().st_size != item['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError('A saved PDF differs from the confirmed source inventory. Start a refreshed run.')

    async def _phase_download(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        input_dir = self.store.input_dir(state.session_id)
        staged = self.store.session_dir(state.session_id) / 'download-cache'
        manifest = download_snapshot(self.dropbox_tool, state.dropbox_link, staged, cb)
        # Copy only this reconciled inventory; cache survives interruption.
        expected = {item['path'] for item in manifest}
        for old in input_dir.rglob('*'):
            if old.is_file() and old.relative_to(input_dir).as_posix() not in expected:
                old.unlink()
        for item in manifest:
            target = input_dir / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + '.part')
            shutil.copyfile(staged / item['path'], temporary)
            temporary.replace(target)
        self.store.update_phase_data(state, PHASE_DOWNLOAD,
            {'files': sorted(expected), 'manifest': manifest, 'manifest_version': 1})
        cb(f'   ↳ downloaded and reconciled {len(manifest)} PDFs')

    async def _phase_ocr(self, state: SessionState, cb: Callable[[str], None]) -> None:
        input_dir = self.store.input_dir(state.session_id)
        extracted_dir = self.store.extracted_dir(state.session_id)

        pdf_paths = sorted(p for p in input_dir.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf')
        if not pdf_paths:
            raise RuntimeError("No downloaded PDFs found to OCR")

        # Resume: skip PDFs whose .txt already exists and is non-empty
        pending: List[Path] = []
        for p in pdf_paths:
            txt = extracted_dir / p.relative_to(input_dir).with_suffix(".txt")
            sidecar = coverage_path(p, input_dir, extracted_dir)
            try:
                saved = json.loads(sidecar.read_text()) if sidecar.exists() else {}
            except (ValueError, TypeError):
                saved = {}
            if (txt.exists() and txt.stat().st_size > 0 and saved.get('total_pages')
                    and not saved.get('technical_failure')
                    and saved.get('source_sha256') == hashlib.sha256(p.read_bytes()).hexdigest()
                    and saved.get('text_sha256') == hashlib.sha256(txt.read_bytes()).hexdigest()):
                continue
            pending.append(p)

        if not pending:
            cb("   ↳ all PDFs already OCR'd, skipping")
            return

        cb(f"   ↳ OCR'ing {len(pending)} of {len(pdf_paths)} PDFs")

        # Save each file immediately, so a restart during a later PDF retains it.
        for number, pdf in enumerate(pending, 1):
            cb(f"   ↳ extracting file {number}/{len(pending)}")
            # Incomplete marker prevents a crash from masquerading as legacy OCR.
            save_coverage({}, pdf, input_dir, extracted_dir)
            results = await self.ocr_client.batch_extract(
                [str(pdf)], max_concurrent=1, progress_callback=cb
            )
            if len(results) != 1:
                raise RuntimeError("OCR returned an incomplete file result list; resume to retry")
            r = results[0]
            if r["success"]:
                self.ocr_client.save_extracted_text(
                    r, str(extracted_dir), base_input_dir=str(input_dir)
                )
            else:
                # A retry must never reuse stale text from a previous extraction.
                stale = extracted_dir / pdf.relative_to(input_dir).with_suffix('.txt')
                if stale.exists():
                    stale.unlink()
                self.logger.error("OCR returned no usable text; page coverage records the affected file")
            save_coverage(r, pdf, input_dir, extracted_dir)

        coverage = collect_coverage(input_dir, extracted_dir)
        self.store.update_phase_data(state, PHASE_OCR, {'coverage': coverage})
        if coverage['technical_failures'] or any(r.get('coverage_status') == 'unknown_legacy' for r in coverage['files']):
            raise RuntimeError("OCR has failed or missing pages. Completed files are saved; resume to retry failed files.")
        # Verify at least something came out
        if not list(extracted_dir.rglob("*.txt")):
            raise RuntimeError("No text could be extracted from PDFs")

    async def _phase_generate(
        self, state: SessionState, cb: Callable[[str], None], session_id: str
    ) -> None:
        # Claude calls are synchronous (httpx.Client under the hood). Running
        # them directly in the main thread is the right call under Streamlit:
        # the script is already blocked on asyncio.run(), and the progress
        # callback touches Streamlit widgets — which REQUIRE the script run
        # context (i.e. the main thread). Using asyncio.to_thread here causes
        # live_slot.empty() to raise streamlit.errors.NoSessionContext.
        extracted_dir = str(self.store.extracted_dir(state.session_id))
        batches_dir = str(self.store.batches_dir(state.session_id))

        result = self.chronology_agent.generate_batches(
            input_dir=extracted_dir,
            batches_dir=batches_dir,
            progress_callback=cb,
            should_pause=lambda: self._should_pause(session_id),
        )
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

        result = self.chronology_agent.assemble_outputs(
            input_dir=extracted_dir,
            batches_dir=batches_dir,
            output_dir=output_dir,
            progress_callback=cb,
        )
        if not result["success"]:
            raise RuntimeError(result.get("error", "Assembly failed"))
        atomic_json(Path(output_dir) / 'ocr_coverage.json',
                    collect_coverage(self.store.input_dir(state.session_id), extracted_dir))
        self.store.update_phase_data(
            state,
            PHASE_HEADER,
            {"patient_header": result.get("header", {}),
             "manual_review_count": result.get("manual_review_count", 0)},
        )

    async def _phase_upload(
        self, state: SessionState, cb: Callable[[str], None]
    ) -> None:
        output_dir = self.store.output_dir(state.session_id)
        destination = validate_destination(state.destination_folder, state.dropbox_link)

        result = self.dropbox_tool.upload_folder(
            local_dir=str(output_dir),
            dropbox_folder=destination,
            max_retries=5,
            verify=True,
        )
        if not result["success"]:
            failed_names = [f.get("dropbox_path") for f in result.get("failed", [])]
            raise RuntimeError(
                f"Dropbox upload failed for {len(failed_names)} file(s): "
                f"{failed_names} — {result.get('error', '')}"
            )
        if not result.get('uploaded') or not all(u.get('verified') for u in result['uploaded']):
            raise RuntimeError('Upload returned without a verified content hash for every output. Resume to retry verification.')
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
