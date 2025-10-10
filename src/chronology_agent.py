"""Direct Anthropic API agent for generating medical chronologies."""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Callable
from datetime import datetime
import logging

try:
    from anthropic import Anthropic
    import httpx
except ImportError:
    raise ImportError("anthropic package not installed. Run: pip install anthropic")


class ChronologyAgent:
    """Generate medical chronologies using direct Anthropic API calls."""

    def __init__(self, api_key: str):
        """
        Initialize the chronology agent.

        Args:
            api_key: Anthropic API key
        """
        # Configure HTTP client with aggressive retry and timeout settings
        http_client = httpx.Client(
            timeout=httpx.Timeout(
                connect=60.0,    # 60s to establish connection
                read=300.0,      # 5 minutes to read response
                write=60.0,      # 60s to send request
                pool=10.0        # 10s to get connection from pool
            ),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5
            ),
            follow_redirects=True,
            verify=True  # Verify SSL certificates
        )

        # Configure Anthropic client with custom HTTP client
        self.client = Anthropic(
            api_key=api_key,
            http_client=http_client,
            max_retries=5  # More retries for network issues
        )
        self.logger = logging.getLogger(__name__)

    def _load_rules(self, base_dir: str) -> str:
        """Load the CLAUDE.md rules file."""
        rules_path = Path(base_dir) / ".claude" / "CLAUDE.md"
        if rules_path.exists():
            with open(rules_path, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            self.logger.warning("CLAUDE.md not found, using basic rules")
            return "Generate a medical chronology from the provided documents."

    def _read_extracted_files(self, input_dir: str) -> List[Dict[str, str]]:
        """Read all extracted text files from the input directory."""
        input_path = Path(input_dir)
        documents = []

        for txt_file in sorted(input_path.glob('*.txt')):
            try:
                with open(txt_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    documents.append({
                        'filename': txt_file.name,
                        'content': content
                    })
                    self.logger.info(f"Loaded {txt_file.name} ({len(content)} chars)")
            except Exception as e:
                self.logger.error(f"Failed to read {txt_file.name}: {e}")

        return documents

    def generate_chronology(
        self,
        input_dir: str,
        output_dir: str,
        base_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Dict:
        """
        Generate medical chronology from extracted text files.

        Args:
            input_dir: Directory containing extracted .txt files
            output_dir: Directory to write output files
            base_dir: Base directory containing .claude/CLAUDE.md
            progress_callback: Optional callback for progress updates

        Returns:
            Dictionary with generation results
        """
        try:
            # Load rules
            if progress_callback:
                progress_callback("📖 Loading chronology generation rules...")
            rules = self._load_rules(base_dir)

            # Read all extracted documents
            if progress_callback:
                progress_callback(f"📄 Reading extracted documents from {Path(input_dir).name}...")
            documents = self._read_extracted_files(input_dir)

            if not documents:
                return {
                    'success': False,
                    'error': 'No extracted text files found'
                }

            # Build the prompt
            if progress_callback:
                progress_callback(f"🤖 Generating chronology from {len(documents)} documents...")

            # Prepare document content for Claude
            documents_text = "\n\n".join([
                f"=== DOCUMENT: {doc['filename']} ===\n{doc['content']}"
                for doc in documents
            ])

            prompt = f"""You are generating a medical chronology from OCR-extracted medical records.

**RULES AND FORMATTING:**
{rules}

**EXTRACTED DOCUMENTS ({len(documents)} files):**
{documents_text}

**YOUR TASK:**
1. Review all the documents above
2. Create a comprehensive medical chronology following ALL the rules specified
3. Generate the chronology in markdown format
4. Also create a JSON version with structured data
5. Write an executive summary
6. Note any gaps, OCR errors, or missing information

**RESPOND WITH:**
First, write the complete markdown chronology.
Then write "---JSON---" on its own line.
Then write the complete JSON version.
Then write "---SUMMARY---" on its own line.
Then write the executive summary.
Then write "---GAPS---" on its own line.
Then write the gaps analysis."""

            # Call Claude with timeout handling
            self.logger.info("Calling Claude API...")
            self.logger.info(f"Prompt length: {len(prompt)} characters")
            self.logger.info(f"Using model: claude-sonnet-4-5-20250929")

            try:
                self.logger.info("Initiating API request...")
                response = self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",  # Claude Sonnet 4.5 with enhanced memory
                    max_tokens=16000,
                    temperature=0,
                    messages=[{
                        "role": "user",
                        "content": prompt
                    }]
                )
                self.logger.info("API request successful!")
            except Exception as e:
                import traceback
                import sys

                self.logger.error(f"API call failed: {type(e).__name__}: {e}")
                self.logger.error(f"Error type: {type(e).__module__}.{type(e).__name__}")
                self.logger.error(f"Full error details: {repr(e)}")
                self.logger.error(f"Full traceback: {traceback.format_exc()}")

                # Log additional network diagnostic info
                if hasattr(e, '__cause__'):
                    self.logger.error(f"Caused by: {type(e.__cause__).__name__}: {e.__cause__}")

                # Check if it's a connection error - try once more with simpler request
                if "connection" in str(e).lower() or "timeout" in str(e).lower():
                    self.logger.info("Connection issue detected. Retrying with reduced prompt...")
                    # Simplify prompt - send just the documents without full rules
                    simplified_prompt = f"""Generate a medical chronology from these OCR-extracted documents.

Create a chronological summary following standard medical chronology format.

**DOCUMENTS:**
{documents_text}

Output the chronology in markdown format."""

                    try:
                        self.logger.info("Retrying with simplified prompt...")
                        response = self.client.messages.create(
                            model="claude-sonnet-4-5-20250929",
                            max_tokens=8000,
                            temperature=0,
                            messages=[{
                                "role": "user",
                                "content": simplified_prompt
                            }]
                        )
                        self.logger.info("Retry successful!")
                    except Exception as retry_error:
                        self.logger.error(f"Retry also failed: {retry_error}")
                        raise Exception(f"API connection failed after retry: {str(e)}")
                else:
                    raise

            # Parse response
            full_response = response.content[0].text

            # Split into sections
            parts = full_response.split('---JSON---')
            chronology_md = parts[0].strip()

            if len(parts) > 1:
                remaining = parts[1]
                json_parts = remaining.split('---SUMMARY---')
                chronology_json_text = json_parts[0].strip()

                if len(json_parts) > 1:
                    summary_parts = json_parts[1].split('---GAPS---')
                    summary_md = summary_parts[0].strip()
                    gaps_md = summary_parts[1].strip() if len(summary_parts) > 1 else ""
                else:
                    summary_md = ""
                    gaps_md = ""
            else:
                chronology_json_text = "{}"
                summary_md = ""
                gaps_md = ""

            # Write output files
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            files_written = {}

            # Write chronology.md
            chronology_file = output_path / 'chronology.md'
            with open(chronology_file, 'w', encoding='utf-8') as f:
                f.write(chronology_md)
            files_written['chronology.md'] = str(chronology_file)
            self.logger.info(f"✓ Written: chronology.md ({len(chronology_md)} chars)")

            # Write chronology.json
            try:
                # Try to parse and pretty-print JSON
                json_data = json.loads(chronology_json_text)
                json_file = output_path / 'chronology.json'
                with open(json_file, 'w', encoding='utf-8') as f:
                    json.dump(json_data, f, indent=2)
                files_written['chronology.json'] = str(json_file)
                self.logger.info(f"✓ Written: chronology.json")
            except json.JSONDecodeError as e:
                self.logger.warning(f"Failed to parse JSON: {e}")
                # Write raw JSON text anyway
                json_file = output_path / 'chronology.json'
                with open(json_file, 'w', encoding='utf-8') as f:
                    f.write(chronology_json_text)
                files_written['chronology.json'] = str(json_file)

            # Write summary.md
            summary_file = output_path / 'summary.md'
            with open(summary_file, 'w', encoding='utf-8') as f:
                f.write(summary_md)
            files_written['summary.md'] = str(summary_file)
            self.logger.info(f"✓ Written: summary.md")

            # Write gaps.md
            gaps_file = output_path / 'gaps.md'
            with open(gaps_file, 'w', encoding='utf-8') as f:
                f.write(gaps_md)
            files_written['gaps.md'] = str(gaps_file)
            self.logger.info(f"✓ Written: gaps.md")

            if progress_callback:
                progress_callback(f"✅ Generated chronology with {len(documents)} documents")

            return {
                'success': True,
                'files': files_written,
                'documents_processed': len(documents)
            }

        except Exception as e:
            self.logger.error(f"Chronology generation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }
