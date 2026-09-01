"""Streamlit UI for precision-chronology.

Uses per-phase st.status containers for progress display, background
pipeline execution via threading, and a top-of-page status indicator
that reads from state.json on disk so progress survives browser refresh.
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

from src.cross_checker import CROSS_CHECK_JACCARD_THRESHOLD
from src.pipeline import PrecisionChronologyPipeline
from src.progress import ProgressReporter
from src.session_state import (
    PHASE_ASSEMBLY,
    PHASE_ORDER,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
)


load_dotenv()

# First entry is the default for new runs and the fallback for resumed
# sessions. Opus 5 is the recommended balance of accuracy and cost.
# Fable 5.1 is Anthropic's most capable model at roughly twice the price;
# it requires 30-day data retention on the Anthropic organization.
MODEL_OPTIONS = ["claude-opus-5", "claude-fable-5-1", "claude-sonnet-4-6", "claude-opus-4-7"]
MODEL_HELP = (
    "claude-opus-5: recommended (highest accuracy at standard cost). "
    "claude-fable-5-1: most capable model, about 2x the cost of Opus 5; "
    "requires 30-day data retention enabled on your Anthropic organization. "
    "claude-sonnet-4-6: faster and cheaper, lower accuracy. "
    "claude-opus-4-7: previous generation."
)

PHASE_LABELS = [
    "Downloading PDFs from Dropbox",
    "Running OCR with page markers",
    "Extracting atomic facts (Claude)",
    "Verifying every quote against the source",
    "Assembling chronology entries (Claude)",
    "Cross-checking entries vs. verified facts",
    "Header, summary, gaps, JSON, DOCX",
    "Uploading outputs to Dropbox",
]


@st.cache_resource
def get_pipeline() -> PrecisionChronologyPipeline:
    return PrecisionChronologyPipeline(
        google_api_key=os.environ.get("GOOGLE_VISION_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        dropbox_token=os.environ.get("DROPBOX_ACCESS_TOKEN"),
    )


# ------------------------------------------------------ StreamlitProgressReporter


class StreamlitProgressReporter(ProgressReporter):
    """Routes progress to per-phase st.status containers."""

    def __init__(self) -> None:
        self._containers = {}
        self._current_phase = 0

    def phase_start(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        self._current_phase = phase_num
        container = st.status(
            f"Phase {phase_num}/{total_phases}: {phase_name}",
            expanded=True,
        )
        self._containers[phase_num] = container

    def phase_progress(
        self, current: int, total: int, item_description: Optional[str] = None
    ) -> None:
        container = self._containers.get(self._current_phase)
        if container and total > 0:
            container.progress(current / total)
            if item_description:
                container.caption(f"{current}/{total} — {item_description}")

    def phase_complete(
        self, phase_num: int, summary: Optional[str] = None
    ) -> None:
        container = self._containers.get(phase_num)
        if container:
            label = f"Phase {phase_num} complete"
            if summary:
                label += f": {summary}"
            container.update(label=label, state="complete", expanded=False)

    def phase_log(self, message: str, level: str = "info") -> None:
        container = self._containers.get(self._current_phase)
        if container:
            container.write(message)

    def pipeline_complete(self, summary: Optional[str] = None) -> None:
        st.success(f"✅ Pipeline complete{f': {summary}' if summary else ''}")

    def pipeline_failed(self, error: str) -> None:
        st.error(f"❌ Pipeline failed: {error}")

    def phase_skipped(
        self, phase_num: int, total_phases: int, phase_name: str
    ) -> None:
        container = st.status(
            f"Phase {phase_num}/{total_phases}: {phase_name} — skipped",
            expanded=False,
        )
        container.update(state="complete")
        self._containers[phase_num] = container


# -------------------------------------------------------- status indicator


def _running_status_body(pipeline: PrecisionChronologyPipeline) -> None:
    """Top-of-page status indicator. Reads state.json from disk."""
    sessions = pipeline.list_sessions()
    for sess in sessions:
        if sess.status != STATUS_IN_PROGRESS:
            continue
        current_phase = None
        for i, phase_name in enumerate(PHASE_ORDER):
            ph = sess.phases.get(phase_name)
            if ph and ph.status == STATUS_IN_PROGRESS:
                current_phase = (i + 1, PHASE_LABELS[i], ph)
                break
        if current_phase:
            num, label, ph = current_phase
            progress_str = ""
            if ph.progress:
                c, t = ph.progress.get("current", 0), ph.progress.get("total", 0)
                item = ph.progress.get("item", "")
                progress_str = f": {c}/{t}" + (f" ({item})" if item else "")
            elapsed = ""
            try:
                updated = datetime.fromisoformat(sess.updated_at)
                secs = int((datetime.now() - updated).total_seconds())
                elapsed = f". Last update {secs}s ago"
            except Exception:
                pass
            st.info(
                f"🔄 **Pipeline running** — {sess.session_id}: "
                f"Phase {num} of 8 ({label}){progress_str}{elapsed}"
            )
            return


# Self-refreshing banner: live run progress updates in place every few
# seconds without rerunning (and dimming) the rest of the page.
if hasattr(st, "fragment"):
    _running_status_view = st.fragment(run_every="5s")(_running_status_body)
else:
    _running_status_view = _running_status_body


def _render_running_status(pipeline: PrecisionChronologyPipeline) -> None:
    _running_status_view(pipeline)


def _render_verification_report(pipeline: PrecisionChronologyPipeline, session_id: str) -> None:
    store = pipeline.store
    state = store.load(session_id)
    verification = state.phases.get("verification")
    cross_check = state.phases.get("cross_check")

    with st.expander("📋 Verification report", expanded=True):
        col_a, col_b, col_c = st.columns(3)
        if verification:
            col_a.metric("Verified facts", verification.data.get("verified", 0))
            col_b.metric("Rejected facts", verification.data.get("rejected", 0))
        if cross_check:
            col_c.metric(
                "Cross-check warnings", cross_check.data.get("warnings", 0)
            )
        if verification and verification.data.get("reasons"):
            st.write("Rejection breakdown:")
            for reason, count in verification.data["reasons"].items():
                st.write(f"- `{reason}`: {count}")

        rej_path = store.rejected_facts_path(session_id)
        if rej_path.exists() and rej_path.stat().st_size > 0:
            st.download_button(
                "Download rejected facts (JSONL)",
                rej_path.read_bytes(),
                file_name="verification_rejected.jsonl",
                mime="application/json",
                key=f"dl_rejected_{session_id}",
            )
        ccr_path = store.output_dir(session_id) / "cross_check_report.md"
        if ccr_path.exists():
            st.download_button(
                "Download cross-check report",
                ccr_path.read_bytes(),
                file_name="cross_check_report.md",
                mime="text/markdown",
                key=f"dl_crosscheck_{session_id}",
            )


def _render_outputs(pipeline: PrecisionChronologyPipeline, session_id: str) -> None:
    output_dir = pipeline.store.output_dir(session_id)
    files = sorted(output_dir.glob("*"))
    if not files:
        st.info("No output files yet.")
        return

    # ZIP all outputs into a single download
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    st.download_button(
        "⬇️  Download all outputs (ZIP)",
        buf.getvalue(),
        file_name=f"{session_id}_outputs.zip",
        mime="application/zip",
        key=f"dl_zip_{session_id}",
    )

    tabs = st.tabs([f.name for f in files])
    for tab, f in zip(tabs, files):
        with tab:
            try:
                if f.suffix == ".json":
                    st.json(json.loads(f.read_text(encoding="utf-8")))
                elif f.suffix in {".md", ".txt", ".jsonl"}:
                    st.code(f.read_text(encoding="utf-8"), language=None)
                else:
                    st.write(f"Binary file ({f.stat().st_size} bytes)")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not render {f.name}: {exc}")
            st.download_button(
                f"Download {f.name}",
                f.read_bytes(),
                file_name=f.name,
                key=f"dl_{session_id}_{f.name}",
            )


def _render_started_confirmation(pipeline: PrecisionChronologyPipeline) -> None:
    """Durable, self-refreshing confirmation for a run just started from this
    tab. Survives the post-click rerun (unlike a transient st.info) and shows
    the run's live phase/status inline so the user sees it is working without
    switching to the Sessions tab."""
    sid = st.session_state.get("started_session_id")
    if not sid:
        return
    try:
        sess = pipeline.store.load(sid)
    except Exception:
        st.session_state.pop("started_session_id", None)
        return

    if sess.status == STATUS_COMPLETE:
        st.success(f"✅ Run **{sid}** finished. See it in the Sessions tab.")
        return
    if sess.status == STATUS_FAILED:
        st.error(
            f"❌ Run **{sid}** failed: {sess.last_error or 'see Sessions tab'}"
        )
        return

    # in progress
    num, label = 1, PHASE_LABELS[0]
    for i, phase_name in enumerate(PHASE_ORDER):
        ph = sess.phases.get(phase_name)
        if ph and ph.status == STATUS_IN_PROGRESS:
            num, label = i + 1, PHASE_LABELS[i]
            break
    st.success(
        f"▶️ Run **{sid}** started and is working — Phase {num} of 8 ({label}). "
        "Watch live progress in the Sessions tab; you can leave this page."
    )


def _new_run_tab(pipeline: PrecisionChronologyPipeline) -> None:
    st.subheader("Start a new chronology run")
    # A self-refreshing confirmation panel for a run started from this tab.
    if hasattr(st, "fragment"):
        st.fragment(run_every="4s")(_render_started_confirmation)(pipeline)
    else:
        _render_started_confirmation(pipeline)
    col1, col2 = st.columns(2)
    with col1:
        dropbox_link = st.text_input(
            "Dropbox folder path or shared link",
            value=st.session_state.get("last_link", ""),
            placeholder="/McKnight case",
            help=(
                "For folders in your own Dropbox, paste the folder PATH "
                "(e.g. /McKnight case) — this is the most reliable. Shared "
                "link URLs also work but can fail on some accounts."
            ),
        )
        patient_id = st.text_input(
            "Patient ID (optional)", value=st.session_state.get("last_patient", "")
        )
    with col2:
        destination = st.text_input(
            "Destination Dropbox folder",
            value="",
            placeholder="/Precision chronology pipeline outputs/...",
        )
        model_ext = st.selectbox("Extraction model", MODEL_OPTIONS, index=0, help=MODEL_HELP)
        model_asm = st.selectbox("Assembly model", MODEL_OPTIONS, index=0, help=MODEL_HELP)
        strict = st.checkbox(
            "Strict cross-check (fail on any below-threshold phrase)", value=False
        )

    # Always clickable; validate on click. A disabled= gate tied to the
    # text field is unreliable (uncommitted paste, dimmed reruns).
    if st.button("▶️  Start pipeline", type="primary"):
        if not (dropbox_link or "").strip():
            st.error("Enter a Dropbox folder path or shared link first.")
        else:
            try:
                state = pipeline.create_session(
                    dropbox_link=dropbox_link,
                    patient_id=patient_id or None,
                    destination_folder=destination or None,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state["active_session_id"] = state.session_id
                st.session_state["started_session_id"] = state.session_id
                st.session_state["last_link"] = dropbox_link
                st.session_state["last_patient"] = patient_id
                st.query_params["session_id"] = state.session_id

                # Run in background
                pipeline.run_in_background(
                    state.session_id,
                    model_extraction=model_ext,
                    model_assembly=model_asm,
                    strict_cross_check=strict,
                )
                # Rerun once so the durable, self-refreshing confirmation
                # panel at the top of this tab picks up the new session.
                st.rerun()


def _render_session_progress(sess) -> None:
    """Show real-time progress for an in-progress session."""
    for i, phase_name in enumerate(PHASE_ORDER):
        ph = sess.phases.get(phase_name)
        if not ph:
            continue
        label = PHASE_LABELS[i]
        if ph.status == STATUS_COMPLETE:
            st.write(f"✅ Phase {i+1}/8: {label}")
        elif ph.status == STATUS_IN_PROGRESS:
            progress_text = ""
            if ph.progress:
                c = ph.progress.get("current", 0)
                t = ph.progress.get("total", 0)
                item = ph.progress.get("item", "")
                progress_text = f" ({c}/{t}" + (f" — {item}" if item else "") + ")"
            st.write(f"🔄 Phase {i+1}/8: {label}{progress_text}")
            if ph.progress and ph.progress.get("total", 0) > 0:
                st.progress(ph.progress["current"] / ph.progress["total"])
        elif ph.status == STATUS_FAILED:
            st.write(f"❌ Phase {i+1}/8: {label}")
            if ph.error:
                st.error(ph.error)
        else:
            st.write(f"⏳ Phase {i+1}/8: {label} — pending")


def _render_disk_gauge(pipeline: PrecisionChronologyPipeline) -> None:
    try:
        total, used, free = pipeline.store.disk_usage()
    except Exception:
        return
    used_gb = used / (1024 ** 3)
    total_gb = total / (1024 ** 3)
    pct = used / total if total else 0.0
    if pct >= 0.85:
        st.error(
            f"Disk: {used_gb:.2f} / {total_gb:.2f} GB used ({pct * 100:.0f}%). "
            "Auto-prune triggers at 70% and runs on the next New Run; "
            "consider deleting old sessions manually below."
        )
    elif pct >= 0.70:
        st.warning(
            f"Disk: {used_gb:.2f} / {total_gb:.2f} GB used ({pct * 100:.0f}%). "
            "Auto-prune will free space on your next New Run."
        )
    else:
        st.caption(f"Disk: {used_gb:.2f} / {total_gb:.2f} GB used ({pct * 100:.0f}%)")


def _process_pending_deletes(pipeline: PrecisionChronologyPipeline) -> None:
    """Execute deletes queued by button clicks BEFORE anything renders.

    The Sessions tab auto-refreshes while a run is active, and a click
    that races the refresh can be dropped. Buttons therefore only queue
    the session id in st.session_state; the actual delete happens here,
    at the top of the next script run, where it cannot be lost.
    """
    pending = st.session_state.pop("pending_delete", None)
    if pending:
        try:
            pipeline.store.delete(pending)
            st.success(f"Session {pending} permanently deleted.")
        except OSError as exc:
            st.error(str(exc))

    if st.session_state.pop("pending_delete_all_completed", False):
        deleted, failed = [], []
        for sess in pipeline.list_sessions():
            if sess.status == STATUS_COMPLETE:
                try:
                    pipeline.store.delete(sess.session_id)
                    deleted.append(sess.session_id)
                except OSError:
                    failed.append(sess.session_id)
        if deleted:
            st.success(
                f"Permanently deleted {len(deleted)} completed session(s): "
                + ", ".join(deleted)
            )
        if failed:
            st.error("Could not delete: " + ", ".join(failed))
        if not deleted and not failed:
            st.info("No completed sessions to delete.")


def _sessions_tab_body(pipeline: PrecisionChronologyPipeline) -> None:
    st.subheader("Sessions")
    _process_pending_deletes(pipeline)
    _render_disk_gauge(pipeline)
    sessions = pipeline.list_sessions()
    if not sessions:
        st.info("No sessions yet. Start one in the New Run tab.")
        return

    completed_count = sum(1 for s in sessions if s.status == STATUS_COMPLETE)
    if completed_count:
        with st.expander(f"🗑️ Bulk cleanup ({completed_count} completed session(s))"):
            st.caption(
                "Outputs are already uploaded to Dropbox at the end of each "
                "run. Deleting here permanently removes the session's PDFs, "
                "OCR text, extracted facts, and outputs from this server's disk."
            )
            confirm_all = st.checkbox(
                "I understand this permanently deletes all completed sessions "
                "from the server",
                key="confirm_delete_all",
            )
            if st.button(
                "Delete ALL completed sessions", disabled=not confirm_all
            ):
                st.session_state["pending_delete_all_completed"] = True
                st.rerun()

    for sess in sessions:
        badge = {
            STATUS_COMPLETE: "✅",
            STATUS_FAILED: "❌",
            STATUS_PAUSED: "⏸️",
        }.get(sess.status, "🔄")
        with st.expander(
            f"{badge}  {sess.session_id}  ·  patient={sess.patient_id or '—'}  ·  updated {sess.updated_at}"
        ):
            if sess.last_error:
                st.error(sess.last_error)

            if sess.status == STATUS_IN_PROGRESS:
                _render_session_progress(sess)
            else:
                st.write("Phase status:")
                for phase_name in PHASE_ORDER:
                    ph = sess.phases.get(phase_name)
                    status = ph.status if ph else "pending"
                    st.write(f"- {phase_name}: {status}")

            col1, col2, col3, col4 = st.columns(4)
            if col1.button("Resume", key=f"resume_{sess.session_id}"):
                pipeline.run_in_background(
                    sess.session_id,
                    model_extraction=MODEL_OPTIONS[0],
                    model_assembly=MODEL_OPTIONS[0],
                    strict_cross_check=False,
                )
                st.info("Pipeline resumed in background.")
                time.sleep(2)
                st.rerun()
            if col2.button("Pause", key=f"pause_{sess.session_id}"):
                pipeline.request_pause(sess.session_id)
                st.success("Pause requested. The run will exit at the next phase boundary.")
            if col3.button("Delete", key=f"del_{sess.session_id}"):
                if pipeline.is_running(sess.session_id):
                    st.warning(
                        "This session is currently running. Pause it first, "
                        "then delete."
                    )
                else:
                    # Queue the delete; it executes at the top of the next
                    # script run so the auto-refresh cannot swallow it.
                    st.session_state["pending_delete"] = sess.session_id
                    st.rerun()
            if col4.button(
                "Re-run from assembly",
                key=f"reasm_{sess.session_id}",
                help=(
                    "Reuse the downloaded PDFs, OCR text, and extracted facts; "
                    "re-run only assembly → cross-check → DOCX → upload with the "
                    "latest logic. Fast — no re-OCR or re-extraction."
                ),
            ):
                reasm_state = pipeline.store.load(sess.session_id)
                pipeline.store.reset_from_phase(reasm_state, PHASE_ASSEMBLY)
                pipeline.run_in_background(
                    sess.session_id,
                    model_extraction=MODEL_OPTIONS[0],
                    model_assembly=MODEL_OPTIONS[0],
                    strict_cross_check=False,
                )
                st.info("Re-running from assembly (reusing extracted facts).")
                time.sleep(2)
                st.rerun()

            if sess.status == STATUS_COMPLETE:
                _render_verification_report(pipeline, sess.session_id)
                _render_outputs(pipeline, sess.session_id)


# The sessions panel refreshes ITSELF every few seconds via st.fragment,
# so live run progress updates without rerunning (and dimming) the whole
# page. The previous approach — time.sleep(5) + st.rerun() at the end of
# the tab — kept the entire script in a perpetual running state, which
# dimmed every widget and swallowed button clicks (including a
# permanently grayed-out Start pipeline button). Both tabs execute on
# every Streamlit run, so the New Run tab was collateral damage.
if hasattr(st, "fragment"):
    _sessions_view = st.fragment(run_every="5s")(_sessions_tab_body)
else:  # very old Streamlit: render statically, user refreshes manually
    _sessions_view = _sessions_tab_body


def _sessions_tab(pipeline: PrecisionChronologyPipeline) -> None:
    _sessions_view(pipeline)


def main() -> None:
    st.set_page_config(page_title="precision-chronology", layout="wide")
    st.title("precision-chronology")
    st.caption(
        f"Default threshold for cross-check: {CROSS_CHECK_JACCARD_THRESHOLD}. "
        "Every clinical claim is backed by a verbatim source quote."
    )

    pipeline = get_pipeline()

    # Top-of-page running indicator
    _render_running_status(pipeline)

    # Resume via query params
    qp_session = st.query_params.get("session_id")
    if qp_session and "active_session_id" not in st.session_state:
        if pipeline.store.exists(qp_session):
            st.session_state["active_session_id"] = qp_session

    tabs = st.tabs(["New Run", "Sessions"])
    with tabs[0]:
        _new_run_tab(pipeline)
    with tabs[1]:
        _sessions_tab(pipeline)


if __name__ == "__main__":
    main()
