"""Direct Anthropic API agent for generating medical chronologies."""

import os
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable
from datetime import datetime
import logging

try:
    from anthropic import Anthropic, APIError, APIStatusError
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

    def _call_api_with_retry(self, prompt: str, max_tokens: int = 8000, max_retries: int = 5) -> str:
        """
        Call Anthropic API with exponential backoff retry logic for overload errors.

        Args:
            prompt: The prompt to send to Claude
            max_tokens: Maximum tokens in response
            max_retries: Maximum number of retry attempts

        Returns:
            Response text from Claude

        Raises:
            Exception: If all retries fail
        """
        base_delay = 2  # Start with 2 second delay

        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=max_tokens,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}]
                )
                return response.content[0].text.strip()

            except (APIError, APIStatusError) as e:
                error_message = str(e).lower()

                # Check if this is an overload error (500 status with "overloaded" message)
                is_overload = (
                    "overload" in error_message or
                    ("500" in error_message and "api_error" in error_message)
                )

                # Check if this is a rate limit error (429 status)
                is_rate_limit = "429" in error_message or "rate" in error_message

                if is_overload or is_rate_limit:
                    if attempt < max_retries - 1:
                        # Calculate exponential backoff with jitter
                        delay = base_delay * (2 ** attempt) + (time.time() % 1)  # Add jitter

                        error_type = "Overload" if is_overload else "Rate limit"
                        self.logger.warning(
                            f"{error_type} error on attempt {attempt + 1}/{max_retries}. "
                            f"Retrying in {delay:.1f} seconds..."
                        )
                        time.sleep(delay)
                        continue
                    else:
                        self.logger.error(f"Failed after {max_retries} attempts: {e}")
                        raise Exception(f"API call failed after {max_retries} retries: {e}")
                else:
                    # For other API errors, don't retry
                    self.logger.error(f"API error (non-retryable): {e}")
                    raise

            except Exception as e:
                # For unexpected errors, don't retry
                self.logger.error(f"Unexpected error: {e}")
                raise

        raise Exception(f"API call failed after {max_retries} attempts")

    def _load_rules(self, base_dir: str) -> str:
        """Load the CLAUDE.md rules file."""
        rules_path = Path(base_dir) / ".claude" / "CLAUDE.md"
        if rules_path.exists():
            with open(rules_path, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            self.logger.warning("CLAUDE.md not found, using basic rules")
            return "Generate a medical chronology from the provided documents."

    def _chunk_large_document(self, filename: str, content: str, max_chunk_chars: int = 40000) -> List[Dict[str, str]]:
        """
        Split a large document into smaller chunks.

        Args:
            filename: Original filename
            content: Document content
            max_chunk_chars: Maximum characters per chunk

        Returns:
            List of document chunks
        """
        if len(content) <= max_chunk_chars:
            return [{'filename': filename, 'content': content}]

        # Split into chunks
        chunks = []
        words = content.split()
        current_chunk = []
        current_length = 0

        for word in words:
            word_length = len(word) + 1  # +1 for space
            if current_length + word_length > max_chunk_chars and current_chunk:
                # Save current chunk
                chunk_text = ' '.join(current_chunk)
                chunk_num = len(chunks) + 1
                chunks.append({
                    'filename': f"{filename} (part {chunk_num})",
                    'content': chunk_text
                })
                current_chunk = [word]
                current_length = word_length
            else:
                current_chunk.append(word)
                current_length += word_length

        # Add final chunk
        if current_chunk:
            chunk_text = ' '.join(current_chunk)
            chunk_num = len(chunks) + 1
            chunks.append({
                'filename': f"{filename} (part {chunk_num})",
                'content': chunk_text
            })

        self.logger.info(f"Split {filename} into {len(chunks)} chunks")
        return chunks

    def _read_extracted_files(self, input_dir: str) -> List[Dict[str, str]]:
        """Read all extracted text files from the input directory and chunk large ones."""
        input_path = Path(input_dir)
        documents = []

        for txt_file in sorted(input_path.glob('*.txt')):
            try:
                with open(txt_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                    # Chunk if too large (20K chars = ~80K tokens with overhead, very conservative)
                    chunks = self._chunk_large_document(txt_file.name, content, max_chunk_chars=20000)
                    documents.extend(chunks)

                    if len(chunks) > 1:
                        self.logger.info(f"Loaded {txt_file.name} ({len(content)} chars) → {len(chunks)} chunks")
                    else:
                        self.logger.info(f"Loaded {txt_file.name} ({len(content)} chars)")
            except Exception as e:
                self.logger.error(f"Failed to read {txt_file.name}: {e}")

        return documents

    def verify_chronology(
        self,
        chronology_path: str,
        extracted_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Dict:
        """
        Verify chronology against source documents to detect hallucinations.

        Args:
            chronology_path: Path to generated chronology.md
            extracted_dir: Directory with extracted text files
            progress_callback: Optional progress callback

        Returns:
            Dictionary with verification results
        """
        try:
            if progress_callback:
                progress_callback("🔍 Loading chronology and source documents...")

            # Read chronology
            with open(chronology_path, 'r', encoding='utf-8') as f:
                chronology_text = f.read()

            # Read source documents
            documents = self._read_extracted_files(extracted_dir)

            if progress_callback:
                progress_callback(f"🤖 Analyzing chronology against {len(documents)} source documents...")

            # Build verification prompt
            source_summary = "\n\n".join([
                f"=== {doc['filename']} ===\n{doc['content'][:2000]}..."  # First 2000 chars of each
                for doc in documents[:10]  # Sample first 10 docs
            ])

            prompt = f"""You are a medical record auditor checking for hallucinations and inaccuracies.

**CHRONOLOGY TO VERIFY:**
{chronology_text[:10000]}

**SOURCE DOCUMENTS (SAMPLE):**
{source_summary}

**YOUR TASK:**
Review the chronology entries and identify any potential issues:

1. **Hallucinations**: Information in chronology NOT found in source documents
2. **Date Errors**: Dates that don't match source documents
3. **Misattributions**: Information attributed to wrong provider/facility
4. **Exaggerations**: Facts overstated or added beyond source
5. **Omissions**: Critical information missing from chronology

**OUTPUT FORMAT:**
For each issue found, provide:
- Entry Date: [date from chronology]
- Issue Type: [hallucination/date error/misattribution/etc]
- Description: [what's wrong]
- Severity: [critical/moderate/minor]

If no issues found, state: "No significant issues detected."

Begin your analysis:"""

            # Call Claude with retry logic
            self.logger.info("Running hallucination check...")
            verification_result = self._call_api_with_retry(prompt, max_tokens=4000)

            if progress_callback:
                progress_callback("✅ Verification complete!")

            return {
                'success': True,
                'verification': verification_result,
                'documents_checked': len(documents)
            }

        except Exception as e:
            self.logger.error(f"Verification failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

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

**Format**: MM/DD/YYYY. Facility. Provider Name, Credentials. Visit Type. Chief Complaint: ... History: ... Exam: ... Assessment: ... Plan: ...

**1. CRITICAL CHRONOLOGICAL SORTING (HIGHEST PRIORITY):**
- PRIMARY RULE: Output ALL entries in STRICT CHRONOLOGICAL ORDER from OLDEST date first to MOST RECENT date last
- Parse and sort every entry by date before writing output
- VERIFICATION STEP: Before presenting the final output, review all generated entries one last time to verify they are in strict chronological order (oldest to newest). Re-sort them if any are out of place.
- This is the most critical requirement and must not fail

**2. SUMMARIZATION & PRIORITIZATION RULES:**

**Length Limit:**
- Each date of service summary MUST be 5 to 7 sentences maximum
- Be concise while maintaining clinical accuracy

**Mandatory Content:**
- ALWAYS include the Assessment and Plan in every entry
- These are non-negotiable components

**General Prioritization:**
- Prioritize pertinent positive and negative findings from Physical Examination, Assessment, and Plan
- Include subjective complaints (Chief Complaint/History) but keep them very brief
- Focus on clinically relevant information only

**Domain-Specific Emphasis:**

For Orthopedic, Spine, or Pain Management visits:
- Dedicate sentences to objective findings: range of motion, strength testing, neurologic examination, specific tenderness/palpation findings
- Include imaging results if discussed
- Always include full Assessment and complete Plan
- Minimize subjective history to 1 sentence maximum

For Laboratory or Radiology-Only reports:
- Do NOT list individual lab results unless critically abnormal
- Simply state what was done and general outcome (e.g., "Laboratory values obtained," "Labs reviewed, stable," or "CT scan of lumbar spine completed")
- Include brief impression/findings only

For all other visit types (general medical, follow-ups, etc.):
- Briefly summarize main reason for visit (1 sentence)
- Include Assessment
- Include Plan
- Keep other details minimal

**3. FORMATTING RULES (MAINTAIN CURRENT FORMAT):**
- Each date of service entry MUST be ONE CONTINUOUS PARAGRAPH with NO line breaks within the entry
- All labels (Provider:, Chief Complaint:, Assessment:, Plan:, etc.) flow together in the same paragraph
- The ONLY separator between different date entries is a SINGLE blank line
- NEVER use horizontal rules (---) or multiple blank lines between entries

**Additional Guidelines:**
- Tone: Direct, factual, clinical language with in-paragraph headings
- No bulleted lists: Convert all bullets to flowing sentences
- Imaging reports: Include only Impression section
- Therapy notes: Consolidate multiple follow-up sessions into one entry listing all dates"""

        prompt = f"""Generate chronology entries from these {len(documents)} medical documents.

{rules}

**DOCUMENTS:**
{documents_text}

**OUTPUT:**
Write chronology entries in proper format, one entry per document/visit.
Do NOT include header or JSON - just the chronology entries."""

        # Call Claude with retry logic
        return self._call_api_with_retry(prompt, max_tokens=8000)

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
            # Very conservative limit to account for prompt overhead
            MAX_BATCH_TOKENS = 60000  # Much smaller batches to stay well under 200K with prompts

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

            # Process each batch with rate limiting
            batch_results = []
            BATCH_DELAY = 3  # 3 second delay between batches to avoid overwhelming API

            for batch_num, batch in enumerate(batches, 1):
                batch_docs = len(batch)
                if progress_callback and total_batches > 1:
                    progress_callback(f"📝 Batch {batch_num}/{total_batches} ({batch_docs} documents)...")
                elif progress_callback:
                    progress_callback(f"📝 Processing {batch_docs} documents...")

                batch_chronology = self._process_batch(batch, batch_num, total_batches)
                batch_results.append(batch_chronology)
                self.logger.info(f"Batch {batch_num}/{total_batches} completed ({batch_docs} documents)")

                # Add delay between batches (except after the last one)
                if batch_num < total_batches:
                    self.logger.info(f"Waiting {BATCH_DELAY}s before next batch (rate limiting)...")
                    time.sleep(BATCH_DELAY)

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
            # Escape the chronology text properly for JSON
            chronology_escaped = chronology_md.replace('"', '\\"').replace('\n', '\\n')
            chronology_json_text = f"""{{
  "metadata": {{
    "generated": "{datetime.now().isoformat()}",
    "documents_processed": {len(documents)},
    "batches": {total_batches}
  }},
  "chronology": "{chronology_escaped}"
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
