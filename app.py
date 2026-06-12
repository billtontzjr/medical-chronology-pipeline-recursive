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
    PHASE_ORDER,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
)


load_dotenv()

MODEL_OPTIONS = ["claude-sonnet-4-6", "claude-opus-4-7"]

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


def _render_running_status(pipeline: PrecisionChronologyPipeline) -> None:
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


def _new_run_tab(pipeline: PrecisionChronologyPipeline) -> None:
    st.subheader("Start a new chronology run")
    col1, col2 = st.columns(2)
    with col1:
        dropbox_link = st.text_input(
            "Dropbox shared link",
            value=st.session_state.get("last_link", ""),
            placeholder="https://www.dropbox.com/scl/fi/...",
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
        model_ext = st.selectbox("Extraction model", MODEL_OPTIONS, index=0)
        model_asm = st.selectbox("Assembly model", MODEL_OPTIONS, index=0)
        strict = st.checkbox(
            "Strict cross-check (fail on any below-threshold phrase)", value=False
        )

    if st.button("▶️  Start pipeline", type="primary", disabled=not dropbox_link):
        state = pipeline.create_session(
            dropbox_link=dropbox_link,
            patient_id=patient_id or None,
            destination_folder=destination or None,
        )
        st.session_state["active_session_id"] = state.session_id
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
        st.info(f"Pipeline started for session **{state.session_id}**. Check the Sessions tab for progress.")
        time.sleep(2)
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


def _sessions_tab(pipeline: PrecisionChronologyPipeline) -> None:
    st.subheader("Sessions")
    _render_disk_gauge(pipeline)
    sessions = pipeline.list_sessions()
    if not sessions:
        st.info("No sessions yet. Start one in the New Run tab.")
        return

    # Auto-refresh when any session is in progress
    any_running = any(s.status == STATUS_IN_PROGRESS for s in sessions)

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

            col1, col2, col3 = st.columns(3)
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
                pipeline.store.delete(sess.session_id)
                st.rerun()

            if sess.status == STATUS_COMPLETE:
                _render_verification_report(pipeline, sess.session_id)
                _render_outputs(pipeline, sess.session_id)

    if any_running:
        time.sleep(5)
        st.rerun()


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
