"""Streamlit UI for precision-chronology.

Mirrors the predecessor's two-tab workflow (New Run + Sessions) while
exposing the new anti-hallucination signals: per-stage model selection,
strict cross-check toggle, and a Verification Report panel that surfaces
verified/rejected counts and the cross-check warnings.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

from src.cross_checker import CROSS_CHECK_JACCARD_THRESHOLD
from src.pipeline import PrecisionChronologyPipeline
from src.session_state import PHASE_ORDER, STATUS_COMPLETE, STATUS_FAILED, STATUS_PAUSED


load_dotenv()

MODEL_OPTIONS = ["claude-sonnet-4-6", "claude-opus-4-7"]


@st.cache_resource
def get_pipeline() -> PrecisionChronologyPipeline:
    return PrecisionChronologyPipeline(
        google_api_key=os.environ.get("GOOGLE_VISION_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        dropbox_token=os.environ.get("DROPBOX_ACCESS_TOKEN"),
    )


def _run_pipeline(
    pipeline: PrecisionChronologyPipeline,
    session_id: str,
    *,
    model_extraction: str,
    model_assembly: str,
    strict_cross_check: bool,
) -> dict:
    status = st.status("Running pipeline…", expanded=True)
    log_lines: list[str] = []

    def cb(msg: str) -> None:
        log_lines.append(msg)
        status.write(msg)

    result = asyncio.run(
        pipeline.run(
            session_id,
            progress_callback=cb,
            model_extraction=model_extraction,
            model_assembly=model_assembly,
            strict_cross_check=strict_cross_check,
        )
    )

    label_map = {
        "complete": ("✅ Pipeline complete", "complete"),
        "paused": ("⏸️  Paused", "running"),
        "failed": ("❌ Pipeline failed", "error"),
    }
    label, state_arg = label_map.get(result.get("status", ""), ("Pipeline finished", "complete"))
    status.update(label=label, state=state_arg)
    return result


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
            )
        ccr_path = store.output_dir(session_id) / "cross_check_report.md"
        if ccr_path.exists():
            st.download_button(
                "Download cross-check report",
                ccr_path.read_bytes(),
                file_name="cross_check_report.md",
                mime="text/markdown",
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
        result = _run_pipeline(
            pipeline,
            state.session_id,
            model_extraction=model_ext,
            model_assembly=model_asm,
            strict_cross_check=strict,
        )
        st.write(result)
        if result.get("status") == "complete":
            _render_verification_report(pipeline, state.session_id)
            _render_outputs(pipeline, state.session_id)


def _sessions_tab(pipeline: PrecisionChronologyPipeline) -> None:
    st.subheader("Sessions")
    sessions = pipeline.list_sessions()
    if not sessions:
        st.info("No sessions yet. Start one in the New Run tab.")
        return
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
            st.write("Phase status:")
            for phase_name in PHASE_ORDER:
                ph = sess.phases.get(phase_name)
                status = ph.status if ph else "pending"
                st.write(f"- {phase_name}: {status}")

            col1, col2, col3 = st.columns(3)
            if col1.button("Resume", key=f"resume_{sess.session_id}"):
                st.session_state["active_session_id"] = sess.session_id
                result = _run_pipeline(
                    pipeline,
                    sess.session_id,
                    model_extraction=MODEL_OPTIONS[0],
                    model_assembly=MODEL_OPTIONS[0],
                    strict_cross_check=False,
                )
                st.write(result)
            if col2.button("Pause", key=f"pause_{sess.session_id}"):
                pipeline.request_pause(sess.session_id)
                st.success("Pause requested. The run will exit at the next phase boundary.")
            if col3.button("Delete", key=f"del_{sess.session_id}"):
                pipeline.store.delete(sess.session_id)
                st.rerun()

            if sess.status == STATUS_COMPLETE:
                _render_verification_report(pipeline, sess.session_id)
                _render_outputs(pipeline, sess.session_id)


def main() -> None:
    st.set_page_config(page_title="precision-chronology", layout="wide")
    st.title("precision-chronology")
    st.caption(
        f"Default threshold for cross-check: {CROSS_CHECK_JACCARD_THRESHOLD}. "
        "Every clinical claim is backed by a verbatim source quote."
    )

    pipeline = get_pipeline()

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
