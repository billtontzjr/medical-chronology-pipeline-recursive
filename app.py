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

from src.session_model import saved_model
from src.manual_review import DRAFT_LABEL
from src.word_export import chronology_docx, output_zip, saved_records
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

from src.access_control import require_team_access
require_team_access(st)


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
OPENAI_API_KEY = _env("OPENAI_API_KEY")

DROPBOX_OAUTH_OK = bool(DROPBOX_APP_KEY and DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN)


# ------------------------------------------------------------ pipeline cache
# User-facing model options. The key is what appears in the selectbox; the
# value is the model ID sent to the Anthropic API.
MODEL_OPTIONS = {
    "OpenAI GPT-6 Astra": "gpt-6-astra",
    "Claude Fable 5.1": "claude-fable-5-1",
    "Claude Opus 5": "claude-opus-5",
    "Sonnet 4.6 — balanced quality & cost": "claude-sonnet-4-6",
    "Sonnet 4.5 — tested baseline (fallback)": "claude-sonnet-4-5-20250929",
    "Haiku 4.5 — fastest and cheapest (lower quality)": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL_LABEL = "Claude Opus 5"


def get_pipeline(
    google_api_key: str, anthropic_api_key: str, model_id: str, openai_api_key: str = ""
) -> MedicalChronologyPipeline:
    return MedicalChronologyPipeline(
        google_api_key=google_api_key,
        anthropic_api_key=anthropic_api_key,
        dropbox_token=DROPBOX_REFRESH_TOKEN,
        model=model_id,
        openai_api_key=openai_api_key,
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


def session_badge(status: str, manual_review_count: int = 0) -> str:
    if status == STATUS_COMPLETE and manual_review_count:
        return f"🟠 {DRAFT_LABEL} ({manual_review_count} source sections)"
    icons = {
        "pending": "⚪",
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


def _pipeline_for_saved_run(pipeline, session_id):
    model = saved_model(pipeline.chronology_agent, pipeline.store.extracted_dir(session_id),
                        pipeline.store.batches_dir(session_id), MODEL_OPTIONS.values())
    st.info(f"Saved run model: {model}. The sidebar model applies to new runs.")
    return get_pipeline(google_key_input, anthropic_key_input or "", model, openai_key_input)


def run_session_with_progress(
    pipeline: MedicalChronologyPipeline,
    session_id: str,
    *,
    key_prefix: str,
) -> None:
    try:
        pipeline = _pipeline_for_saved_run(pipeline, session_id)
    except ValueError as exc:
        st.error(str(exc))
        return
    status_box = st.status("Running pipeline…", expanded=True)
    with status_box:
        live_slot = st.empty()

    def _cb(msg: str) -> None:
        if LIVE_MSG_PATTERN.search(msg):
            live_slot.write(msg)
        else:
            live_slot.empty()
            status_box.write(msg)

    result = asyncio.run(pipeline.run(session_id, progress_callback=_cb))

    if result["status"] == "complete":
        status_box.update(label=(DRAFT_LABEL if result.get("manual_review_count") else "✅ Pipeline complete"), state="complete")
    elif result["status"] == "paused":
        status_box.update(label="⏸ Paused — you can resume anytime", state="running")
    else:
        status_box.update(label="❌ Pipeline failed", state="error")
        st.error(result.get("error", "Unknown error"))

    st.session_state["active_session_id"] = session_id
    st.rerun()


def _render_source_review(pipeline, state, key_prefix):
    if state.phases[PHASE_DOWNLOAD].status == STATUS_COMPLETE and not state.phases[PHASE_DOWNLOAD].data.get('manifest_version'):
        st.warning('This older run has no confirmed source inventory. Start a refreshed run before relying on it for complete coverage.')
    if st.button('Start refreshed run', key=f'refresh_{key_prefix}_{state.session_id}',
                 help='Creates a new case run using the saved source link and original model. Downloads and extracts fresh copies into a new session; preserves the old draft.'):
        try:
            saved_pipeline = _pipeline_for_saved_run(pipeline, state.session_id)
            refreshed = saved_pipeline.create_session(state.dropbox_link, state.patient_id)
            # Pin the selected saved model before any generation takes place.
            from src.session_model import save_model_config
            save_model_config(saved_pipeline.store.extracted_dir(refreshed.session_id), saved_pipeline.chronology_agent.model)
            st.session_state['active_session_id'] = refreshed.session_id
            st.rerun()
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
    coverage = state.phases[PHASE_OCR].data.get('coverage', {})
    if coverage.get('files_needing_review'):
        st.warning(f"{coverage['files_needing_review']} source file(s) have unread pages or unknown OCR coverage. "
                   "Review the original pages before relying on the chronology.")
        st.download_button("Download page coverage report", json.dumps(coverage, indent=2),
                           file_name="ocr_coverage.json", mime="application/json",
                           key=f"{key_prefix}_coverage_{state.session_id}")
    diagnostics = sorted(pipeline.store.batches_dir(state.session_id).glob('batch_*.deposition-work.json'))
    if diagnostics and state.status == STATUS_FAILED:
        st.caption("Deposition review details include source passages and rejected responses. "
                   "They stay in this session unless you download them.")
        for path in diagnostics:
            st.download_button(f"Download deposition review details ({path.stem.split('.')[0]})",
                               path.read_bytes(), file_name=path.name, mime="application/json",
                               key=f"{key_prefix}_diagnostic_{state.session_id}_{path.name}")

    scope_details = sorted(pipeline.store.batches_dir(state.session_id).glob('batch_*.scope-work.json'))
    for path in scope_details:
        details = json.loads(path.read_text())
        if details.get('status') != 'blocked':
            continue
        batch_name = path.name.split('.')[0]
        with st.expander(f"Source-screening review: {batch_name}", expanded=True):
            st.write("Completed batches are saved. This batch needs review before generation can continue.")
            attempts = details.get('attempts', [])
            if attempts:
                st.write(attempts[-1].get('error', 'Review the saved source classifications.'))
                st.json(attempts[-1].get('details', []))
            st.write("Source IDs for this batch:")
            st.json(details.get('sources', []))
            st.caption("The download contains private source summaries and rejected responses.")
            st.download_button(f"Download source-screening review details ({batch_name})",
                               path.read_bytes(), file_name=path.name, mime="application/json",
                               key=f"{key_prefix}_scope_{state.session_id}_{path.name}")


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
    if state.phases[PHASE_HEADER].data.get('manual_review_count'):
        st.warning(DRAFT_LABEL + '. Download the manual-review list with the chronology. '
                   'Uncertain source sections are withheld; automated verification does not resolve these flags.')
    st.markdown("### 📄 Generated files")
    out_dir = Path(pipeline.store.output_dir(state.session_id))
    files = sorted([p for p in out_dir.iterdir() if p.is_file()])
    if not files:
        st.warning("No output files found.")
        return

    separate_billing = False
    chronology_path = out_dir / "chronology.md"
    if chronology_path.exists():
        separate_billing = st.checkbox(
            "Move billing entries to a Word appendix",
            value=False, key=f"billing_word_{key_prefix}_{state.session_id}",
            help="Uses saved record types, with explicit-label support for older drafts. Does not decide whether clinical notes exist, remove duplicates, or verify facts. Markdown stays unchanged.",
        )
        st.download_button(
            "⬇️ Download Word document (.docx)",
            data=chronology_docx(chronology_path.read_text(encoding="utf-8"), separate_billing=separate_billing, records=saved_records(out_dir)),
            file_name="chronology.docx",
            type="primary", use_container_width=True,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key=f"word_{key_prefix}_{state.session_id}",
        )
        st.caption("Word export formats the existing draft without another AI call. Review against source records before relying on it.")

    # Build an in-memory ZIP so the user can download everything at once
    # (goes to the browser's Downloads folder — the web equivalent of the
    # desktop). Built fresh on each render so it always reflects disk state.
    st.download_button(
        label="⬇️ Download all as ZIP (to your computer)",
        data=output_zip(out_dir, separate_billing=separate_billing),
        file_name=f"{state.session_id}.zip",
        mime="application/zip",
        key=f"dlzip_{key_prefix}_{state.session_id}",
        use_container_width=True,
    )

    preview_files = [p for p in files if p.suffix != '.docx']
    tabs = st.tabs([p.name for p in preview_files]) if preview_files else []
    for tab, p in zip(tabs, preview_files):
        with tab:
            content = p.read_text(encoding="utf-8")
            if p.suffix == ".json":
                try:
                    st.json(json.loads(content))
                except json.JSONDecodeError:
                    st.code(content, language="json")
            else:
                # Escape '$' so Streamlit's KaTeX renderer doesn't treat
                # pairs of dollar signs (e.g. currency amounts) as math.
                st.markdown(content.replace("$", "\\$"))
            st.download_button(
                label=f"⬇️ Download {p.name}",
                data=content,
                file_name=p.name,
                mime="application/json" if p.suffix == ".json" else "text/markdown",
                key=f"dl_{key_prefix}_{state.session_id}_{p.name}",
            )

    st.markdown("### 🔍 Verify chronology")
    st.caption("Checks the extracted source text and saves verification.md. Completed source-group checks are saved; running verification again resumes unchanged work. Human review remains required.")
    verify_now = st.button(
        "Run verification",
        key=f"verify_{key_prefix}_{state.session_id}",
        use_container_width=True,
    )
    if verify_now:
        try:
            pipeline = _pipeline_for_saved_run(pipeline, state.session_id)
        except ValueError as exc:
            st.error(str(exc))
            return
        status_box = st.status("Verifying chronology…", expanded=True)

        def _verify_cb(msg: str) -> None:
            status_box.write(msg)

        result = pipeline.verify_session(state.session_id, progress_callback=_verify_cb)
        if result.get("success"):
            status_box.update(label=("Verification report saved—manual review remains" if result.get("manual_review_count") else "✅ Verification report saved"), state="complete")
            st.rerun()
        else:
            status_box.update(label="❌ Verification failed", state="error")
            st.error(result.get("error", "Verification failed."))

    st.markdown("### 📤 Re-upload to Dropbox")
    st.caption(f"Last uploaded to `{state.destination_folder}`")
    recents = recent_destinations(pipeline)
    with st.form(f"reupload_{key_prefix}_{state.session_id}"):
        new_dest = st.text_input(
            "New Dropbox destination folder",
            value=state.destination_folder,
            help=(
                "Enter a Dropbox path like `/Medical chronology pipeline outputs/New case`, OR paste a "
                "Dropbox URL from your browser's address bar "
                "(e.g. `https://www.dropbox.com/home/Team%20Folder/...`) — "
                "the app converts it to a path automatically."
            ),
            placeholder="/Medical chronology pipeline outputs/New case  or  https://www.dropbox.com/home/…",
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
            try:
                pipeline.update_destination(state.session_id, new_dest)
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
                return
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

    openai_key_input = OPENAI_API_KEY or ""
    if OPENAI_API_KEY:
        st.success("OpenAI API key loaded")
    else:
        openai_key_input = st.text_input("OpenAI API Key (for Astra)", type="password")

    st.markdown("### 🧠 Chronology model")
    env_default_model = os.getenv("ANTHROPIC_MODEL", "").strip()
    default_idx = next((i for i, v in enumerate(MODEL_OPTIONS.values()) if v == env_default_model),
                       list(MODEL_OPTIONS).index(DEFAULT_MODEL_LABEL))
    selected_label = st.selectbox(
        "Model for new runs", options=list(MODEL_OPTIONS), index=default_idx,
        help="Saved runs resume and verify with their original model. Choose a model here for a new run.",
    )
    selected_model_id = MODEL_OPTIONS[selected_label]
    st.caption(f"Using: `{selected_model_id}`")

    st.markdown("---")
    st.markdown("### 📖 Workflow")
    st.markdown(
        "1. Paste a Dropbox shared link to a patient folder.\n"
        "2. Choose where on Dropbox the outputs go.\n"
        "3. Start the run. Completed stages and source reviews are saved.\n"
        "4. Come back to **Sessions** to resume, re-upload, or download."
    )


# Short-circuit: require the essentials
selected_key = openai_key_input if selected_model_id.startswith("gpt-") else anthropic_key_input
KEYS_OK = bool(DROPBOX_OAUTH_OK and google_key_input and selected_key)
if not KEYS_OK:
    st.warning("Configure the API keys in the sidebar (or your `.env`) to continue.")
    st.stop()

pipeline = get_pipeline(google_key_input, anthropic_key_input or "", selected_model_id, openai_key_input)

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
                help=(
                    "Enter a Dropbox path, OR paste a Dropbox URL from your "
                    "browser's address bar — the app converts URLs to paths "
                    "automatically."
                ),
                placeholder="/Medical chronology pipeline outputs/New case  or  https://www.dropbox.com/home/…",
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
            try:
                state = pipeline.create_session(
                    dropbox_link=dropbox_link,
                    patient_id=patient_id,
                    destination_folder=final_destination,
                )
            except ValueError as exc:
                st.error(str(exc))
                st.stop()
            st.session_state["active_session_id"] = state.session_id
            st.success(f"Session created: `{state.session_id}`")
            st.rerun()

    # If a session is active in this browser, show its live run panel
    live_sid = st.session_state.get("active_session_id") or active_session_id
    if live_sid:
        try:
            state = pipeline.load_session(live_sid)
        except (FileNotFoundError, ValueError):
            st.session_state.pop("active_session_id", None)
            state = None

        if state is not None:
            st.markdown("---")
            st.subheader(f"Current session: `{state.session_id}`")
            st.caption(
                f"Status: {session_badge(state.status, state.phases[PHASE_HEADER].data.get('manual_review_count', 0))}  •  "
                f"Destination: `{state.destination_folder}`"
            )
            tracker = st.container()
            render_phase_tracker(state, tracker)

            if state.last_error:
                st.error(f"Last error: {state.last_error}")
            _render_source_review(pipeline, state, "newrun")

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
                run_session_with_progress(pipeline, state.session_id, key_prefix="newrun")

            # If completed, show outputs + re-upload
            if state.status == STATUS_COMPLETE:
                _render_completed_session(pipeline, state, key_prefix="newrun")


# -------------------------------------------------------------- SESSIONS tab
with tab_sessions:
    st.header("Sessions")
    st.caption("Completed work is saved. If a browser or service stops, resume the case to continue from its last checkpoint.")

    sessions = pipeline.list_sessions()
    if not sessions:
        st.info("No sessions yet. Start one from the **New Run** tab.")
    else:
        for s in sessions:
            with st.expander(
                f"{session_badge(s.status, s.phases[PHASE_HEADER].data.get('manual_review_count', 0))}  **{s.session_id}**  "
                f"•  patient: `{s.patient_id or '—'}`  •  updated {s.updated_at}",
                expanded=(s.status in (STATUS_IN_PROGRESS, STATUS_PAUSED, STATUS_FAILED)),
            ):
                st.caption(f"Destination: `{s.destination_folder}`")
                render_phase_tracker(s, st.container())

                if s.last_error:
                    st.error(s.last_error)
                _render_source_review(pipeline, s, "sessions")

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
                        run_session_with_progress(
                            pipeline, s.session_id, key_prefix="sessions"
                        )
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
                        "Archive",
                        key=f"del_{s.session_id}",
                        help="Moves this session to the private archive. Preserves its files and Dropbox outputs.",
                    ):
                        try:
                            pipeline.archive_session(s.session_id)
                            st.rerun()
                        except RuntimeError as exc:
                            st.error(str(exc))

                if s.status == STATUS_COMPLETE:
                    _render_completed_session(pipeline, s, key_prefix="sessions")


st.markdown("---")
st.caption("Medical Chronology Pipeline v2 — pause, resume, reliable upload")
