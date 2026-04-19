"""Streamlit UI for the resumable Medical Chronology Pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import streamlit as st
from dotenv import load_dotenv


# Fast-moving progress messages (OCR page-by-page) go to a single slot that
# updates in place, instead of scrolling a new line for every page.
LIVE_MSG_PATTERN = re.compile(r"Page\s+\d+\s*/\s*\d+")

from src.pipeline import DEFAULT_DESTINATION_PREFIX, MedicalChronologyPipeline
from src.session_state import (
    PHASE_DOWNLOAD,
    PHASE_GENERATE,
    PHASE_HEADER,
    PHASE_OCR,
    PHASE_ORDER,
    PHASE_SUMMARY,
    PHASE_UPLOAD,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
    SessionState,
)
from src.tools.dropbox_tool import normalize_dropbox_folder

load_dotenv()

st.set_page_config(
    page_title="Medical Chronology Generator",
    page_icon="🏥",
    layout="wide",
)


# ---------------------------------------------------------------- env / keys
def _env(key: str) -> Optional[str]:
    v = os.getenv(key, "")
    v = v.strip() if v else ""
    return v or None


DROPBOX_APP_KEY = _env("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = _env("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = _env("DROPBOX_REFRESH_TOKEN")
GOOGLE_CLOUD_API_KEY = _env("GOOGLE_CLOUD_API_KEY")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

DROPBOX_OAUTH_OK = bool(DROPBOX_APP_KEY and DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN)


# ------------------------------------------------------------ pipeline cache
# User-facing model options. The key is what appears in the selectbox; the
# value is the model ID sent to the Anthropic API.
MODEL_OPTIONS = {
    "Sonnet 4.6 — recommended (balanced quality & cost)": "claude-sonnet-4-6",
    "Sonnet 4.5 — tested baseline (fallback)": "claude-sonnet-4-5-20250929",
    "Opus 4.7 — highest quality (≈5× more expensive)": "claude-opus-4-7",
    "Haiku 4.5 — fastest and cheapest (lower quality)": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL_LABEL = "Sonnet 4.6 — recommended (balanced quality & cost)"


@st.cache_resource(show_spinner=False)
def get_pipeline(
    google_api_key: str, anthropic_api_key: str, model_id: str
) -> MedicalChronologyPipeline:
    return MedicalChronologyPipeline(
        google_api_key=google_api_key,
        anthropic_api_key=anthropic_api_key,
        dropbox_token=DROPBOX_REFRESH_TOKEN,
        model=model_id,
    )


# --------------------------------------------------------------- UI helpers
PHASE_LABELS = {
    PHASE_DOWNLOAD: "1. Download",
    PHASE_OCR: "2. OCR",
    PHASE_GENERATE: "3. Generate",
    PHASE_HEADER: "4a. Header",
    PHASE_SUMMARY: "4b. Summary/Gaps",
    PHASE_UPLOAD: "5. Upload",
}

PHASE_STATUS_ICON = {
    "pending": "⚪",
    "in_progress": "🟡",
    "complete": "🟢",
    "failed": "🔴",
    "paused": "⏸️",
}


def render_phase_tracker(state: SessionState, container) -> None:
    cols = container.columns(len(PHASE_ORDER))
    for col, phase in zip(cols, PHASE_ORDER):
        p = state.phases.get(phase)
        status = p.status if p else "pending"
        icon = PHASE_STATUS_ICON.get(status, "⚪")
        col.markdown(f"**{icon} {PHASE_LABELS[phase]}**")
        col.caption(status)


def session_badge(status: str) -> str:
    icons = {
        STATUS_COMPLETE: "🟢",
        STATUS_IN_PROGRESS: "🟡",
        STATUS_PAUSED: "⏸️",
        STATUS_FAILED: "🔴",
    }
    return f"{icons.get(status, '⚪')} {status}"


def recent_destinations(pipeline: MedicalChronologyPipeline, limit: int = 5) -> List[str]:
    seen: List[str] = []
    for s in pipeline.list_sessions():
        dest = s.destination_folder
        if dest and dest not in seen:
            seen.append(dest)
        if len(seen) >= limit:
            break
    return seen


def _render_completed_session(
    pipeline: MedicalChronologyPipeline,
    state: SessionState,
    *,
    key_prefix: str = "default",
) -> None:
    """Show output tabs, download buttons, and re-upload controls.

    ``key_prefix`` disambiguates widget keys when the same session is
    rendered in multiple places on one page (e.g. the New Run tab's
    current-session panel AND the Sessions tab expander). Streamlit
    requires every widget key to be unique across the whole script run.
    """
    st.markdown("### 📄 Generated files")
    out_dir = Path(pipeline.store.output_dir(state.session_id))
    files = sorted([p for p in out_dir.iterdir() if p.is_file()])
    if not files:
        st.warning("No output files found.")
        return

    tabs = st.tabs([p.name for p in files])
    for tab, p in zip(tabs, files):
        with tab:
            content = p.read_text(encoding="utf-8")
            if p.suffix == ".json":
                try:
                    st.json(json.loads(content))
                except json.JSONDecodeError:
                    st.code(content, language="json")
            else:
                st.markdown(content)
            st.download_button(
                label=f"⬇️ Download {p.name}",
                data=content,
                file_name=p.name,
                mime="application/json" if p.suffix == ".json" else "text/markdown",
                key=f"dl_{key_prefix}_{state.session_id}_{p.name}",
            )

    st.markdown("### 📤 Re-upload to Dropbox")
    st.caption(f"Last uploaded to `{state.destination_folder}`")
    recents = recent_destinations(pipeline)
    with st.form(f"reupload_{key_prefix}_{state.session_id}"):
        new_dest = st.text_input(
            "New Dropbox destination folder",
            value=state.destination_folder,
            help="Must start with '/'.",
        )
        if recents:
            picked = st.selectbox(
                "…or pick a recent folder", options=["(keep above)"] + recents
            )
            if picked != "(keep above)":
                new_dest = picked
        submitted = st.form_submit_button("🔄 Re-upload now")
    if submitted:
        if not new_dest:
            st.error("Destination is required.")
        else:
            pipeline.update_destination(state.session_id, new_dest)
            status_box = st.status(f"Uploading to {new_dest}…", expanded=True)

            def _cb(msg: str) -> None:
                status_box.write(msg)

            result = asyncio.run(
                pipeline.run(state.session_id, progress_callback=_cb)
            )
            if result["status"] == "complete":
                status_box.update(label="✅ Uploaded and verified", state="complete")
                st.rerun()
            else:
                status_box.update(label="❌ Upload failed", state="error")
                st.error(result.get("error", "See logs."))


# -------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Configuration")

    if DROPBOX_OAUTH_OK:
        st.success("✅ Dropbox OAuth configured")
    else:
        st.error("❌ Dropbox OAuth not configured")
        st.info("Run: `python setup_dropbox_oauth.py`")

    google_key_input: Optional[str] = GOOGLE_CLOUD_API_KEY
    if GOOGLE_CLOUD_API_KEY:
        st.success("✅ Google Vision API key loaded")
    else:
        st.error("❌ Google Vision API key missing")
        google_key_input = st.text_input("Google Cloud API Key", type="password")

    anthropic_key_input: Optional[str] = ANTHROPIC_API_KEY
    if ANTHROPIC_API_KEY:
        st.success("✅ Anthropic API key loaded")
    else:
        st.error("❌ Anthropic API key missing")
        anthropic_key_input = st.text_input("Anthropic API Key", type="password")

    st.markdown("### 🧠 Claude model")
    env_default_model = os.getenv("ANTHROPIC_MODEL", "").strip()
    # If an env var pins the model, keep that selection locked in.
    if env_default_model:
        # Try to find a matching UI label; otherwise keep the raw model ID.
        env_label = next(
            (k for k, v in MODEL_OPTIONS.items() if v == env_default_model),
            f"Env override: {env_default_model}",
        )
        st.info(f"Model pinned by `ANTHROPIC_MODEL` env var: {env_default_model}")
        selected_model_id = env_default_model
    else:
        default_idx = list(MODEL_OPTIONS.keys()).index(DEFAULT_MODEL_LABEL)
        selected_label = st.selectbox(
            "Model",
            options=list(MODEL_OPTIONS.keys()),
            index=default_idx,
            help="Picking a different model takes effect on the NEXT phase that calls Claude. Already-written batches won't be regenerated.",
        )
        selected_model_id = MODEL_OPTIONS[selected_label]
    st.caption(f"Using: `{selected_model_id}`")

    st.markdown("---")
    st.markdown("### 📖 Workflow")
    st.markdown(
        "1. Paste a Dropbox shared link to a patient folder.\n"
        "2. Choose where on Dropbox the outputs go.\n"
        "3. Start the run. Close the tab at any time — progress is saved.\n"
        "4. Come back to **Sessions** to resume, re-upload, or download."
    )


# Short-circuit: require the essentials
KEYS_OK = bool(DROPBOX_OAUTH_OK and google_key_input and anthropic_key_input)
if not KEYS_OK:
    st.warning("Configure the API keys in the sidebar (or your `.env`) to continue.")
    st.stop()

pipeline = get_pipeline(google_key_input, anthropic_key_input, selected_model_id)

# Top-level session routing via ?session_id= query param (makes resume links shareable)
# (Use st.query_params where available; fall back gracefully.)
try:
    qp = st.query_params  # type: ignore[attr-defined]
    active_session_id: Optional[str] = qp.get("session_id")
except Exception:
    active_session_id = None


tab_new, tab_sessions = st.tabs(["🚀 New Run", "📚 Sessions"])


# ---------------------------------------------------------------- NEW RUN tab
with tab_new:
    st.header("Start a new chronology")

    col_left, col_right = st.columns([2, 1])
    with col_left:
        dropbox_link = st.text_input(
            "Dropbox folder shared link",
            placeholder="https://www.dropbox.com/scl/fo/...",
            help="Right-click the patient folder in Dropbox → Share → Create link.",
        )
        st.link_button("📂 Open Dropbox", "https://www.dropbox.com/home")

        patient_id = st.text_input(
            "Patient ID",
            placeholder="e.g. john_doe",
            help="Used to prefix the session folder and Dropbox destination.",
        )

    with col_right:
        st.markdown("### Output destination")
        recents = recent_destinations(pipeline)
        default_dest = (
            f"{DEFAULT_DESTINATION_PREFIX}/{patient_id}" if patient_id else DEFAULT_DESTINATION_PREFIX
        )
        dest_choice = st.radio(
            "Where should outputs go?",
            options=["Default", "Recent folder", "Custom"],
            index=0,
            horizontal=True,
        )
        if dest_choice == "Default":
            destination = default_dest
            st.caption(f"→ `{destination}/<session_id>`")
        elif dest_choice == "Recent folder" and recents:
            destination = st.selectbox("Recent folders", recents)
        elif dest_choice == "Recent folder":
            st.info("No recent folders yet — use Default or Custom.")
            destination = default_dest
        else:
            destination = st.text_input(
                "Custom Dropbox folder",
                value=default_dest,
                help="Must be a Dropbox path starting with '/'.",
            )
        destination = normalize_dropbox_folder(destination) if destination else default_dest

    st.markdown("---")
    start = st.button("🚀 Start chronology run", type="primary", use_container_width=True)

    if start:
        if not dropbox_link:
            st.error("Provide a Dropbox shared link.")
        elif not patient_id:
            st.error("Provide a patient ID.")
        else:
            # If destination was left at the prefix, append session-level folder
            final_destination = destination
            if final_destination == DEFAULT_DESTINATION_PREFIX or final_destination == default_dest:
                # let the pipeline default handle it (session_id suffix)
                final_destination = None
            state = pipeline.create_session(
                dropbox_link=dropbox_link,
                patient_id=patient_id,
                destination_folder=final_destination,
            )
            st.session_state["active_session_id"] = state.session_id
            st.success(f"Session created: `{state.session_id}`")
            st.rerun()

    # If a session is active in this browser, show its live run panel
    live_sid = st.session_state.get("active_session_id") or active_session_id
    if live_sid:
        try:
            state = pipeline.load_session(live_sid)
        except FileNotFoundError:
            st.session_state.pop("active_session_id", None)
            state = None

        if state is not None:
            st.markdown("---")
            st.subheader(f"Current session: `{state.session_id}`")
            st.caption(
                f"Status: {session_badge(state.status)}  •  "
                f"Destination: `{state.destination_folder}`"
            )
            tracker = st.container()
            render_phase_tracker(state, tracker)

            if state.last_error:
                st.error(f"Last error: {state.last_error}")

            col_run, col_pause = st.columns(2)
            with col_run:
                run_now = st.button(
                    "▶️ Run / Resume",
                    key=f"run_{state.session_id}",
                    type="primary",
                    use_container_width=True,
                    disabled=state.status == STATUS_COMPLETE,
                )
            with col_pause:
                pause_now = st.button(
                    "⏸ Pause (safe stop)",
                    key=f"pause_{state.session_id}",
                    use_container_width=True,
                    help="Writes a PAUSE marker; the pipeline stops cleanly at its next checkpoint.",
                    disabled=state.status not in (STATUS_IN_PROGRESS,),
                )

            if pause_now:
                pipeline.request_pause(state.session_id)
                st.info(
                    "Pause requested. The run will stop at the next safe checkpoint. "
                    "You can also just close this tab — progress is checkpointed."
                )

            if run_now:
                status_box = st.status("Running pipeline…", expanded=True)
                # Fast-moving (page-level) messages overwrite a single slot;
                # everything else appends to the main status log.
                with status_box:
                    live_slot = st.empty()

                def _cb(msg: str) -> None:
                    if LIVE_MSG_PATTERN.search(msg):
                        live_slot.write(msg)
                    else:
                        # Clear the live slot when we move past that phase,
                        # then record the milestone in the log.
                        live_slot.empty()
                        status_box.write(msg)

                result = asyncio.run(pipeline.run(state.session_id, progress_callback=_cb))

                if result["status"] == "complete":
                    status_box.update(label="✅ Pipeline complete", state="complete")
                elif result["status"] == "paused":
                    status_box.update(label="⏸ Paused — you can resume anytime", state="running")
                else:
                    status_box.update(label="❌ Pipeline failed", state="error")
                    st.error(result.get("error", "Unknown error"))

                # Refresh UI with latest state
                st.rerun()

            # If completed, show outputs + re-upload
            if state.status == STATUS_COMPLETE:
                _render_completed_session(pipeline, state, key_prefix="newrun")


# -------------------------------------------------------------- SESSIONS tab
with tab_sessions:
    st.header("Sessions")
    st.caption("Every run is resumable. Close this tab anytime — work is checkpointed to disk.")

    sessions = pipeline.list_sessions()
    if not sessions:
        st.info("No sessions yet. Start one from the **New Run** tab.")
    else:
        for s in sessions:
            with st.expander(
                f"{session_badge(s.status)}  **{s.session_id}**  "
                f"•  patient: `{s.patient_id or '—'}`  •  updated {s.updated_at}",
                expanded=(s.status in (STATUS_IN_PROGRESS, STATUS_PAUSED, STATUS_FAILED)),
            ):
                st.caption(f"Destination: `{s.destination_folder}`")
                render_phase_tracker(s, st.container())

                if s.last_error:
                    st.error(s.last_error)

                col_open, col_resume, col_pause, col_del = st.columns(4)
                with col_open:
                    if st.button("👁️ Open", key=f"open_{s.session_id}"):
                        st.session_state["active_session_id"] = s.session_id
                        st.rerun()
                with col_resume:
                    if st.button(
                        "▶️ Run / Resume",
                        key=f"resume_{s.session_id}",
                        disabled=s.status == STATUS_COMPLETE,
                    ):
                        st.session_state["active_session_id"] = s.session_id
                        st.rerun()
                with col_pause:
                    if st.button(
                        "⏸ Pause",
                        key=f"pausebtn_{s.session_id}",
                        disabled=s.status != STATUS_IN_PROGRESS,
                    ):
                        pipeline.request_pause(s.session_id)
                        st.toast("Pause requested")
                with col_del:
                    if st.button(
                        "🗑️ Delete",
                        key=f"del_{s.session_id}",
                        help="Removes local session files. Does NOT delete Dropbox outputs.",
                    ):
                        pipeline.store.delete(s.session_id)
                        st.rerun()

                if s.status == STATUS_COMPLETE:
                    _render_completed_session(pipeline, s, key_prefix="sessions")


st.markdown("---")
st.caption("Medical Chronology Pipeline v2 — pause, resume, reliable upload")
