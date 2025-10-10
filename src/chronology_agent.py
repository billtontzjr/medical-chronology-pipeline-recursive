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
        # Sanitize API key - remove any whitespace/newlines that break HTTP headers
        if api_key:
            api_key = api_key.strip()

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

    def _process_batch(self, documents: List[Dict], batch_num: int, total_batches: int) -> str:
        """
        Process a batch of documents and return chronology markdown.

        Args:
            documents: List of document dictionaries
            batch_num: Current batch number (1-indexed)
            total_batches: Total number of batches

        Returns:
            Markdown chronology for this batch
        """
        self.logger.info(f"Processing batch {batch_num}/{total_batches} ({len(documents)} documents)")

        # Build documents text for this batch
        documents_text = "\n\n".join([
            f"=== DOCUMENT: {doc['filename']} ===\n{doc['content']}"
            for doc in documents
        ])

        # Condensed rules for batch processing
        rules = """Create medical chronology entries following these rules:

**Format**: [MM/DD/YYYY]. [Facility]. [Provider Name], [Credentials]. [Visit Type].
Then one paragraph: Chief Complaint: ... History: ... Exam: ... Assessment: ... Plan: ...

**Imaging**: ONLY Impression section
**Therapy**: Consolidate follow-ups into one entry with all dates
**Tone**: Direct, factual, clinical. In-paragraph headings.
**Focus**: Orthopedic/spine/neuro findings. No routine vitals.
**No Lists**: Convert bullets to sentences."""

        prompt = f"""Generate chronology entries from these {len(documents)} medical documents.

{rules}

**DOCUMENTS:**
{documents_text}

**OUTPUT:**
Write chronology entries in proper format, one entry per document/visit.
Do NOT include header or JSON - just the chronology entries."""

        # Call Claude
        response = self.client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=8000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )

        return response.content[0].text.strip()

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

            # Dynamic batching based on estimated token size
            # Estimate: ~4 chars per token (conservative)
            MAX_BATCH_TOKENS = 150000  # Stay well under 200K limit

            batches = []
            current_batch = []
            current_batch_tokens = 0

            for doc in documents:
                # Estimate tokens for this document
                doc_tokens = len(doc['content']) // 4

                # If adding this doc would exceed limit, start new batch
                if current_batch and (current_batch_tokens + doc_tokens) > MAX_BATCH_TOKENS:
                    batches.append(current_batch)
                    current_batch = [doc]
                    current_batch_tokens = doc_tokens
                else:
                    current_batch.append(doc)
                    current_batch_tokens += doc_tokens

            # Add final batch
            if current_batch:
                batches.append(current_batch)

            total_batches = len(batches)
            self.logger.info(f"Created {total_batches} batches from {len(documents)} documents")

            if progress_callback:
                if total_batches > 1:
                    progress_callback(f"🤖 Processing {len(documents)} documents in {total_batches} batches...")
                else:
                    progress_callback(f"🤖 Generating chronology from {len(documents)} documents...")

            # Process each batch
            batch_results = []
            for batch_num, batch in enumerate(batches, 1):
                batch_docs = len(batch)
                if progress_callback and total_batches > 1:
                    progress_callback(f"📝 Batch {batch_num}/{total_batches} ({batch_docs} documents)...")
                elif progress_callback:
                    progress_callback(f"📝 Processing {batch_docs} documents...")

                batch_chronology = self._process_batch(batch, batch_num, total_batches)
                batch_results.append(batch_chronology)
                self.logger.info(f"Batch {batch_num}/{total_batches} completed ({batch_docs} documents)")

            # Combine all batch results
            if progress_callback and total_batches > 1:
                progress_callback(f"🔄 Combining {total_batches} batches into final chronology...")

            # Simply concatenate entries (they're already in chronological order per batch)
            combined_entries = "\n\n".join(batch_results)

            # Add header manually (don't send all entries back to API - too large)
            if progress_callback:
                progress_callback(f"📋 Adding header and generating summary...")

            # Generic header
            header = """MEDICAL RECORDS SUMMARY
[Patient Name - See Records]
Date of Birth: [See Records]
Date of Injury: [See Records]

"""

            chronology_md = header + combined_entries

            # Generate a simple summary based on document count only
            summary_md = f"""Executive Summary

This medical chronology was generated from {len(documents)} medical documents processed in {total_batches} batch(es).

The chronology contains entries organized chronologically documenting the patient's medical journey including:
- Office visits and consultations
- Imaging studies and diagnostic tests
- Treatment plans and interventions
- Follow-up care and therapy sessions

Please review the complete chronology below for detailed medical information."""

            gaps_md = f"""Document Analysis

**Processing Summary:**
- Total Documents Processed: {len(documents)}
- Processing Method: {'Single batch' if total_batches == 1 else f'Multiple batches ({total_batches} batches)'}
- OCR Quality: Review individual entries for any OCR artifacts or unclear text

**Recommendations:**
- Verify all dates and provider information against source documents
- Cross-reference critical findings with original records
- Note any documents that may require manual review for OCR quality"""

            # Generate simple JSON structure
            chronology_json_text = f"""{{
  "metadata": {{
    "generated": "{datetime.now().isoformat()}",
    "documents_processed": {len(documents)},
    "batches": {total_batches}
  }},
  "chronology": "{chronology_md.replace('"', '\\"').replace('\n', '\\n')}"
}}"""

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
