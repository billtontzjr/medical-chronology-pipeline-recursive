"""Main medical chronology pipeline orchestrator."""

import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Callable
import logging

from .tools.dropbox_tool import DropboxTool
from .ocr_client import OCRClient
from .chronology_agent import ChronologyAgent
from .hooks.formatting_guard import get_formatting_hooks


class MedicalChronologyPipeline:
    """Orchestrates the complete medical chronology generation pipeline."""

    def __init__(self, dropbox_token: str = None, google_api_key: str = None, anthropic_api_key: str = None):
        """
        Initialize the pipeline.

        Args:
            dropbox_token: Dropbox API access token (optional, uses OAuth if not provided)
            google_api_key: Google Cloud Vision API key
            anthropic_api_key: Anthropic API key for Claude
        """
        # Initialize Dropbox with OAuth (or fallback to token)
        self.dropbox_tool = DropboxTool(access_token=dropbox_token, use_oauth=True)
        self.ocr_client = OCRClient(google_api_key)
        self.chronology_agent = ChronologyAgent(anthropic_api_key)

        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def _setup_directories(self, base_dir: str, session_id: str) -> Dict[str, str]:
        """
        Create session-specific directory structure.

        Args:
            base_dir: Base directory for the pipeline
            session_id: Unique session identifier (timestamp-based)

        Returns:
            Dictionary with directory paths
        """
        dirs = {
            'input': os.path.join(base_dir, 'data', 'input', session_id),
            'extracted': os.path.join(base_dir, 'data', 'extracted', session_id),
            'output': os.path.join(base_dir, 'data', 'output', session_id)
        }

        for dir_path in dirs.values():
            Path(dir_path).mkdir(parents=True, exist_ok=True)

        return dirs

    async def run_pipeline(self, dropbox_link: str, patient_id: Optional[str] = None,
                          progress_callback: Optional[Callable[[str], None]] = None) -> Dict:
        """
        Run the complete medical chronology pipeline.

        Args:
            dropbox_link: Dropbox shared link to medical records
            patient_id: Optional patient identifier
            progress_callback: Optional callback for real-time progress updates

        Returns:
            Dictionary with pipeline results
        """
        # Create session ID
        session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        if patient_id:
            session_id = f"{patient_id}_{session_id}"

        self.logger.info(f"Starting pipeline session: {session_id}")

        # Get base directory (parent of src directory)
        base_dir = str(Path(__file__).parent.parent)

        # Setup directories
        dirs = self._setup_directories(base_dir, session_id)

        try:
            # Phase 1: Download from Dropbox
            self.logger.info("Phase 1: Downloading files from Dropbox...")
            download_result = self.dropbox_tool.get_shared_link_files(
                dropbox_link,
                dirs['input'],
                extensions=['.pdf', '.PDF']
            )

            if not download_result['success']:
                raise Exception(f"Dropbox download failed: {download_result.get('error')}")

            downloaded_files = download_result['downloaded']
            self.logger.info(f"Downloaded {len(downloaded_files)} PDF files")

            if not downloaded_files:
                raise Exception("No PDF files found in the Dropbox link")

            # Phase 2: OCR Processing
            self.logger.info("Phase 2: Extracting text with OCR...")
            if progress_callback:
                progress_callback(f"🔍 Starting OCR for {len(downloaded_files)} files...")

            pdf_paths = [item['local_path'] for item in downloaded_files]

            ocr_results = await self.ocr_client.batch_extract(
                pdf_paths,
                max_concurrent=1,  # Memory-safe: process one file at a time
                progress_callback=progress_callback
            )

            # Save extracted text
            extracted_files = []
            for result in ocr_results:
                if result['success']:
                    text_path = self.ocr_client.save_extracted_text(
                        result,
                        dirs['extracted']
                    )
                    extracted_files.append(text_path)
                    self.logger.info(
                        f"Extracted text from {result['file_name']} "
                        f"(confidence: {result['confidence']:.2%})"
                    )
                else:
                    self.logger.error(
                        f"OCR failed for {result['file_name']}: {result.get('error')}"
                    )

            if not extracted_files:
                raise Exception("No text could be extracted from PDFs")

            # Phase 3: Agent Processing
            self.logger.info("Phase 3: Generating medical chronology with Claude...")
            if progress_callback:
                progress_callback(f"🤖 Starting chronology generation...")

            # Generate chronology using direct API
            result = self.chronology_agent.generate_chronology(
                input_dir=dirs['extracted'],
                output_dir=dirs['output'],
                base_dir=base_dir,
                progress_callback=progress_callback
            )

            if not result['success']:
                raise Exception(f"Chronology generation failed: {result.get('error')}")

            # Phase 4: Validate outputs
            self.logger.info("Phase 4: Validating outputs...")

            required_files = [
                'chronology.md',
                'chronology.json',
                'summary.md',
                'gaps.md'
            ]

            output_files = {}
            missing_files = []

            for filename in required_files:
                file_path = os.path.join(dirs['output'], filename)
                if os.path.exists(file_path):
                    output_files[filename] = file_path
                    self.logger.info(f"✓ Generated: {filename}")
                else:
                    missing_files.append(filename)
                    self.logger.warning(f"✗ Missing: {filename}")

            # Phase 5: Upload to Dropbox
            self.logger.info("Phase 5: Uploading to Dropbox...")
            if progress_callback:
                progress_callback("📤 Uploading outputs to Dropbox...")

            # Define Dropbox destination path
            dropbox_base_path = "/Tontz Team Folder/Precision Life Care Planning/Confidential/Medical chronology pipeline outputs"
            dropbox_session_path = f"{dropbox_base_path}/{session_id}"

            upload_result = self.dropbox_tool.upload_folder(
                local_dir=dirs['output'],
                dropbox_folder=dropbox_session_path
            )

            if upload_result['success']:
                self.logger.info(f"✓ Uploaded {len(upload_result['uploaded'])} files to Dropbox")
                if progress_callback:
                    progress_callback(f"✅ Uploaded {len(upload_result['uploaded'])} files to Dropbox")
            else:
                self.logger.warning(f"⚠️  Dropbox upload failed: {upload_result.get('error')}")
                if progress_callback:
                    progress_callback(f"⚠️  Dropbox upload failed: {upload_result.get('error')}")

            # Return results
            return {
                'success': True,
                'session_id': session_id,
                'files_processed': len(extracted_files),
                'input_dir': dirs['input'],
                'extracted_dir': dirs['extracted'],
                'output_dir': dirs['output'],
                'output_files': output_files,
                'missing_files': missing_files,
                'agent_result': result,
                'dropbox_upload': upload_result,
                'dropbox_path': dropbox_session_path
            }

        except Exception as e:
            self.logger.error(f"Pipeline failed: {str(e)}")
            return {
                'success': False,
                'session_id': session_id,
                'error': str(e),
                'input_dir': dirs.get('input'),
                'extracted_dir': dirs.get('extracted'),
                'output_dir': dirs.get('output')
            }
