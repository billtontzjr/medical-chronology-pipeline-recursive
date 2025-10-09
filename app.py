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

    # Check API keys
    dropbox_app_key = os.getenv('DROPBOX_APP_KEY')
    dropbox_app_secret = os.getenv('DROPBOX_APP_SECRET')
    dropbox_refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')
    google_api_key = os.getenv('GOOGLE_CLOUD_API_KEY')
    anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')

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
    1. Enter Dropbox link or path:
       - **Shared link**: Copy link from Dropbox
       - **Direct path**: `/folder/patient` format
    2. Provide patient identifier
    3. Click 'Generate Chronology'
    4. Download results
    """)

# Main content
col1, col2 = st.columns([2, 1])

with col1:
    st.header("📋 Input")

    # Dropbox link input
    dropbox_link = st.text_input(
        "Dropbox Shared Link or Path",
        placeholder="https://www.dropbox.com/scl/fo/... OR /My Folder/Patient Name",
        help="Paste a Dropbox shared link OR enter a direct path (e.g., /2025 expert files/patient name)"
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

        try:
            # Phase 1: Download
            with status:
                st.write("📥 Phase 1: Downloading files from Dropbox...")
                phase1_progress = st.progress(0)

            # Run pipeline
            async def run():
                return await pipeline.run_pipeline(dropbox_link, patient_id)

            result = asyncio.run(run())

            if result['success']:
                with status:
                    phase1_progress.progress(25)
                    st.write(f"✅ Downloaded {result['files_processed']} PDF files")

                    st.write("🔍 Phase 2: OCR text extraction...")
                    phase2_progress = st.progress(50)
                    st.write(f"✅ Extracted text from {result['files_processed']} files")

                    st.write("🤖 Phase 3: Generating chronology with Claude Agent...")
                    phase3_progress = st.progress(75)
                    st.write("✅ Chronology generated")

                    st.write("📝 Phase 4: Validating outputs...")
                    phase4_progress = st.progress(100)
                    st.write("✅ Pipeline complete!")

                status.update(label="✅ Pipeline completed successfully!", state="complete")

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
