"""Streamlit web app for Medical Chronology Pipeline."""

import streamlit as st
import asyncio
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from src.pipeline import MedicalChronologyPipeline

# Load environment variables
load_dotenv()

# Page config
st.set_page_config(
    page_title="Medical Chronology Generator",
    page_icon="🏥",
    layout="wide"
)

# Title
st.title("🏥 Medical Chronology Generator")
st.markdown("---")

# Sidebar for configuration
with st.sidebar:
    st.header("⚙️ Configuration")

    # Check API keys and sanitize (remove whitespace that breaks HTTP headers)
    dropbox_app_key = os.getenv('DROPBOX_APP_KEY', '').strip() or None
    dropbox_app_secret = os.getenv('DROPBOX_APP_SECRET', '').strip() or None
    dropbox_refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN', '').strip() or None
    google_api_key = os.getenv('GOOGLE_CLOUD_API_KEY', '').strip() or None
    anthropic_api_key = os.getenv('ANTHROPIC_API_KEY', '').strip() or None

    # Check if Dropbox OAuth is configured
    if dropbox_app_key and dropbox_app_secret and dropbox_refresh_token:
        st.success("✅ Dropbox OAuth configured")
        dropbox_configured = True
    else:
        st.error("❌ Dropbox OAuth not configured")
        st.info("Run: `python setup_dropbox_oauth.py`")
        dropbox_configured = False

    if google_api_key:
        st.success("✅ Google Vision API key loaded")
    else:
        st.error("❌ Google Vision API key missing")
        google_api_key = st.text_input("Google Cloud API Key", type="password")

    if anthropic_api_key:
        st.success("✅ Anthropic API key loaded")
    else:
        st.error("❌ Anthropic API key missing")
        anthropic_api_key = st.text_input("Anthropic API Key", type="password")

    st.markdown("---")
    st.markdown("### 📖 How it works")
    st.markdown("""
    1. Click "Open Dropbox" button
    2. Navigate to patient folder in Dropbox
    3. Copy URL from browser address bar
    4. Paste URL into input field
    5. Enter patient identifier
    6. Click 'Generate Chronology'
    7. Download results
    """)

# Main content
col1, col2 = st.columns([2, 1])

with col1:
    st.header("📋 Input")

    # Dropbox link input
    dropbox_link = st.text_input(
        "Dropbox Folder URL or Path",
        placeholder="Paste URL from browser address bar or use /folder/path format",
        help="Click 'Open Dropbox' below, navigate to folder, then copy the URL from your browser's address bar"
    )

    # Open Dropbox button
    st.link_button(
        "📂 Open Dropbox",
        "https://www.dropbox.com/home",
        help="Click to open Dropbox, navigate to the patient folder, then copy the URL from your browser"
    )

    # Patient ID input
    patient_id = st.text_input(
        "Patient ID",
        placeholder="e.g., john_doe",
        help="Unique identifier for the patient (used for organizing files)"
    )

    # Generate button
    generate_btn = st.button("🚀 Generate Chronology", type="primary", use_container_width=True)

with col2:
    st.header("📊 Status")
    status_container = st.container()

# Results section
st.markdown("---")
results_header = st.empty()
results_container = st.container()

# Process pipeline
if generate_btn:
    if not dropbox_configured:
        st.error("❌ Dropbox OAuth not configured. Run: python setup_dropbox_oauth.py")
    elif not google_api_key:
        st.error("❌ Google Vision API key missing")
    elif not anthropic_api_key:
        st.error("❌ Anthropic API key missing")
    elif not dropbox_link:
        st.error("❌ Please provide a Dropbox shared link")
    elif not patient_id:
        st.error("❌ Please provide a patient ID")
    else:
        # Initialize pipeline (OAuth credentials loaded from .env automatically)
        pipeline = MedicalChronologyPipeline(
            google_api_key=google_api_key,
            anthropic_api_key=anthropic_api_key
        )

        # Status updates
        with status_container:
            status = st.status("🔄 Running pipeline...", expanded=True)
            progress_placeholder = st.empty()

        try:
            # Real-time progress callback
            def update_progress(message: str):
                with progress_placeholder:
                    st.info(message)

            # Run pipeline with progress updates
            async def run():
                return await pipeline.run_pipeline(dropbox_link, patient_id, progress_callback=update_progress)

            result = asyncio.run(run())

            if result['success']:
                status.update(label="✅ Pipeline completed successfully!", state="complete")
                progress_placeholder.empty()  # Clear progress messages

                # Show results
                results_header.header("📄 Generated Files")

                with results_container:
                    # Create tabs for each output file
                    if result['output_files']:
                        tabs = st.tabs(list(result['output_files'].keys()))

                        for tab, (filename, filepath) in zip(tabs, result['output_files'].items()):
                            with tab:
                                # Read file content
                                with open(filepath, 'r', encoding='utf-8') as f:
                                    content = f.read()

                                # Display preview
                                if filename.endswith('.json'):
                                    st.json(content)
                                else:
                                    st.markdown(content)

                                # Download button
                                st.download_button(
                                    label=f"⬇️ Download {filename}",
                                    data=content,
                                    file_name=filename,
                                    mime="application/json" if filename.endswith('.json') else "text/markdown"
                                )

                        # Summary info
                        st.success(f"""
                        ### 🎉 Success!
                        - **Session ID:** {result['session_id']}
                        - **Files Processed:** {result['files_processed']}
                        - **Output Directory:** `{result['output_dir']}`
                        """)

                        if result['missing_files']:
                            st.warning(f"⚠️ Missing files: {', '.join(result['missing_files'])}")
                    else:
                        st.warning("No output files were generated")

            else:
                status.update(label="❌ Pipeline failed", state="error")
                st.error(f"Error: {result.get('error', 'Unknown error')}")

                with st.expander("📁 Session Information"):
                    st.json({
                        'session_id': result.get('session_id'),
                        'input_dir': result.get('input_dir'),
                        'extracted_dir': result.get('extracted_dir'),
                        'output_dir': result.get('output_dir')
                    })

        except Exception as e:
            status.update(label="❌ Pipeline failed", state="error")
            st.error(f"Unexpected error: {str(e)}")
            st.exception(e)

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; font-size: 0.9em;'>
    <p>Medical Chronology Pipeline v1.0 | Powered by Claude Agent SDK</p>
</div>
""", unsafe_allow_html=True)
