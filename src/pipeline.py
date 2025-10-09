"""Main medical chronology pipeline orchestrator."""

import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import logging

from .tools.dropbox_tool import DropboxTool
from .ocr_client import OCRClient
from .hooks.formatting_guard import get_formatting_hooks

# Note: Claude Agent SDK import will be done dynamically
# from claude_agent_sdk import ClaudeAgent


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
        self.anthropic_api_key = anthropic_api_key

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

    async def run_pipeline(self, dropbox_link: str, patient_id: Optional[str] = None) -> Dict:
        """
        Run the complete medical chronology pipeline.

        Args:
            dropbox_link: Dropbox shared link to medical records
            patient_id: Optional patient identifier

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
            pdf_paths = [item['local_path'] for item in downloaded_files]

            ocr_results = await self.ocr_client.batch_extract(
                pdf_paths,
                max_concurrent=3
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
            self.logger.info("Phase 3: Generating medical chronology with Claude Agent...")

            # Build the directive prompt for the agent
            file_list = "\n".join([f"  - {Path(f).name}" for f in extracted_files])
            directive = f"""You are tasked with creating a medical chronology from OCR-extracted medical records.

**Input Details:**
- Number of files: {len(extracted_files)}
- Input directory: {dirs['extracted']}
- Output directory: {dirs['output']}

**Files to process:**
{file_list}

**Your Task:**
Follow ALL rules in .claude/CLAUDE.md to generate:
1. chronology.md - The formatted medical chronology
2. chronology.json - Structured data version
3. summary.md - Executive summary
4. gaps.md - Document gaps and OCR issues

**Important:**
- Read each .txt file in the input directory
- Extract information following the strict formatting rules
- Check for OCR errors and note them in gaps.md
- Use the quality-checker subagent for self-correction
- Write all outputs to: {dirs['output']}

Begin by scanning the input directory and reading all files."""

            # Import Claude Agent SDK
            try:
                from claude_agent_sdk import query, ClaudeAgentOptions
            except ImportError:
                raise Exception(
                    "Claude Agent SDK not installed. "
                    "Run: pip install claude-agent-sdk"
                )

            # Set API key in environment (required by SDK)
            os.environ['ANTHROPIC_API_KEY'] = self.anthropic_api_key

            # Create options for the agent
            options = ClaudeAgentOptions(
                system_prompt="You are a medical chronology expert. Follow the rules in .claude/CLAUDE.md precisely.",
                cwd=base_dir,  # Run in project root to access both input and output dirs
                permission_mode='acceptEdits'  # Auto-accept file edits
            )

            # Run the agent
            self.logger.info("Agent is processing medical records...")

            # Execute query and collect all messages
            messages = []
            async for message in query(prompt=directive, options=options):
                messages.append(message)
                # Log progress
                if hasattr(message, 'content'):
                    for block in message.content:
                        if hasattr(block, 'text') and block.text:
                            # Log first 100 chars of each message
                            preview = block.text[:100].replace('\n', ' ')
                            self.logger.info(f"Agent: {preview}...")

            self.logger.info("Agent processing complete")
            result = {'messages': messages}

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
                'agent_result': result
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
