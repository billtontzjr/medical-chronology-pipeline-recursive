"""Direct Anthropic API agent for generating medical chronologies."""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Callable
from datetime import datetime
import logging

try:
    from anthropic import Anthropic
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
        self.client = Anthropic(api_key=api_key)
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

            # Call Claude
            self.logger.info("Calling Claude API...")

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=16000,
                temperature=0,
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )

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
