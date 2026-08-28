"""Direct Anthropic API agent for generating medical chronologies."""

import os
import json
import time
import re
from pathlib import Path
from typing import Dict, List, Optional, Callable, Tuple
from datetime import datetime
import logging

try:
    from anthropic import Anthropic, APIError, APIStatusError
except ImportError:
    raise ImportError("anthropic package not installed. Run: pip install anthropic")


class ChronologyAgent:
    """Generate medical chronologies using direct Anthropic API calls."""

    # Default model. Override per-run via the UI selector, or globally via
    # the ANTHROPIC_MODEL env var. Opus 5 is the most accurate option for
    # medical-legal chronology work, where hallucination risk matters more
    # than the modest cost difference over Sonnet.
    DEFAULT_MODEL = "claude-opus-5"

    # Models that still accept sampling parameters (temperature). Opus 4.7+
    # and the Claude 5 family reject temperature with a 400 error; reasoning
    # mode replaces it as the consistency mechanism on those models.
    _TEMPERATURE_SUPPORTED_PREFIXES = (
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-haiku",
        "claude-3",
    )

    def __init__(self, api_key: str, model: Optional[str] = None):
        """
        Initialize the chronology agent.

        Args:
            api_key: Anthropic API key.
            model: Override the Claude model ID. Defaults to
                ``DEFAULT_MODEL`` or the ``ANTHROPIC_MODEL`` env var.
        """
        # Sanitize API key - remove any whitespace/newlines that break HTTP headers
        if api_key:
            api_key = api_key.strip()

        self.model = model or os.getenv("ANTHROPIC_MODEL") or self.DEFAULT_MODEL

        # A plain float timeout is accepted by every SDK generation. Passing
        # a custom httpx.Client breaks on the 1.x SDK, whose HTTP layer
        # moved to httpx2 and rejects an httpx.Client instance.
        self.client = Anthropic(
            api_key=api_key,
            timeout=300.0,   # 5 minutes per request
            max_retries=5,   # More retries for network issues
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

        # temperature is only sent to models that still support it; newer
        # models (Opus 4.7+, Claude 5 family) return a 400 if it is present
        request_kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model.startswith(self._TEMPERATURE_SUPPORTED_PREFIXES):
            request_kwargs["temperature"] = 0

        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(**request_kwargs)
                # Newer models may include thinking blocks in content;
                # extract only the text blocks
                text_parts = [
                    block.text for block in response.content
                    if getattr(block, "type", None) == "text"
                ]
                return "\n".join(text_parts).strip()

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

    def _parse_entry_date(self, entry: str) -> Optional[datetime]:
        """
        Parse the date from the beginning of a chronology entry.
        
        Args:
            entry: A chronology entry paragraph starting with MM/DD/YYYY
            
        Returns:
            datetime object if date found and valid, None otherwise
        """
        # Match MM/DD/YYYY at the start of the entry
        date_pattern = r'^(\d{1,2})/(\d{1,2})/(\d{4})'
        match = re.match(date_pattern, entry.strip())
        
        if match:
            try:
                month, day, year = match.groups()
                return datetime(int(year), int(month), int(day))
            except (ValueError, TypeError) as e:
                self.logger.warning(f"Invalid date found in entry: {match.group(0)} - {e}")
                return None
        return None
    
    def _extract_entry_key(self, entry: str) -> Tuple[str, str, str]:
        """
        Extract a deduplication key from an entry: (date, facility, provider).

        The entry header format is:
        MM/DD/YYYY. Facility Name. Provider Name, Credentials. Visit Type.

        Args:
            entry: A chronology entry paragraph

        Returns:
            Tuple of (date_str, facility_lower, provider_lower) for dedup matching
        """
        first_line = entry.strip().split('\n')[0]

        # Parse the header parts separated by '. '
        parts = first_line.split('. ')

        date_str = parts[0].strip() if len(parts) > 0 else ''
        facility = parts[1].strip().lower() if len(parts) > 1 else ''
        provider = parts[2].strip().lower() if len(parts) > 2 else ''

        return (date_str, facility, provider)

    def _enforce_single_paragraph(self, entry: str) -> str:
        """
        Flatten a multi-paragraph entry into a single paragraph.

        Preserves the header line (date. facility. provider. visit type.)
        and merges all subsequent lines/paragraphs into one continuous paragraph.

        Args:
            entry: A chronology entry that may contain multiple paragraphs

        Returns:
            Entry condensed to header + single summary paragraph
        """
        lines = entry.strip().split('\n')
        if not lines:
            return entry

        # The header is the first line (date. facility. provider. visit type.)
        header = lines[0].strip()

        # Everything after the header is the body
        body_lines = [l.strip() for l in lines[1:] if l.strip()]

        if not body_lines:
            return header

        # Join all body lines into a single flowing paragraph
        body = ' '.join(body_lines)

        # Clean up any double spaces that may result from joining
        body = re.sub(r'\s{2,}', ' ', body)

        return f"{header}\n{body}"

    def _is_billing_entry(self, entry: str) -> bool:
        """
        Detect whether an entry was built from billing/administrative content
        rather than a clinical record.

        Args:
            entry: A chronology entry string

        Returns:
            True if the entry appears to be billing-derived
        """
        entry_lower = entry.lower()

        # Clinical entries contain exam/assessment content
        clinical_markers = [
            'chief complaint:', 'history of present illness:', 'physical examination:',
            'assessment:', 'plan:', 'impression:', 'examination:', 'history:'
        ]
        has_clinical_content = any(marker in entry_lower for marker in clinical_markers)

        billing_markers = [
            'billing', 'invoice', 'itemized statement', 'charges', 'cpt',
            'billed', 'payment', 'ledger', 'account statement', 'balance due',
            'statement of charges', 'superbill'
        ]
        has_billing_content = any(marker in entry_lower for marker in billing_markers)

        return has_billing_content and not has_clinical_content

    def _deduplicate_entries(self, entries: List[str]) -> List[str]:
        """
        Remove duplicate entries for the same date/facility/provider.

        When duplicates are found, clinical entries are always preferred over
        billing-derived entries; among entries of the same kind, the longest
        (most detailed) is kept. Entries sharing a date but with different
        facilities or providers are all kept, since they represent distinct
        visits that each belong in the chronology.

        Args:
            entries: List of chronology entry strings

        Returns:
            Deduplicated list of entries
        """
        if not entries:
            return entries

        # Group entries by their dedup key
        seen: Dict[Tuple[str, str, str], str] = {}
        duplicates_removed = 0

        for entry in entries:
            key = self._extract_entry_key(entry)

            if key in seen:
                duplicates_removed += 1
                existing = seen[key]
                existing_is_billing = self._is_billing_entry(existing)
                entry_is_billing = self._is_billing_entry(entry)

                # Clinical content always wins over billing content
                if existing_is_billing and not entry_is_billing:
                    seen[key] = entry
                elif entry_is_billing and not existing_is_billing:
                    pass  # keep existing clinical entry
                elif len(entry) > len(existing):
                    # Same kind: keep the longer/more detailed entry
                    seen[key] = entry
            else:
                seen[key] = entry

        if duplicates_removed > 0:
            self.logger.info(f"Removed {duplicates_removed} duplicate entries (clinical preferred over billing)")

        return list(seen.values())

    def _sort_entries_chronologically(self, entries_text: str) -> str:
        """
        Sort chronology entries by date (oldest to newest), deduplicate,
        and enforce single-paragraph format.

        Args:
            entries_text: Combined chronology entries (may be from multiple batches)

        Returns:
            Sorted, deduplicated entries joined with double newlines
        """
        # Split into fragments (separated by double newlines)
        fragments = [e.strip() for e in entries_text.split('\n\n') if e.strip()]

        if not fragments:
            return entries_text

        # Step 0: Reattach continuation fragments. A multi-paragraph entry gets
        # split apart here because its body paragraphs don't start with a date —
        # rejoin any fragment lacking a leading MM/DD/YYYY to the entry before it.
        entries: List[str] = []
        for fragment in fragments:
            if self._parse_entry_date(fragment) is None and entries:
                entries[-1] = entries[-1] + '\n' + fragment
            else:
                entries.append(fragment)

        # Step 1: Enforce single-paragraph format on each entry
        entries = [self._enforce_single_paragraph(e) for e in entries]

        # Step 2: Deduplicate entries with same date/facility/provider
        entries = self._deduplicate_entries(entries)

        # Step 3: Parse dates and sort chronologically
        dated_entries: List[Tuple[Optional[datetime], str]] = []
        entries_without_dates: List[str] = []

        for entry in entries:
            parsed_date = self._parse_entry_date(entry)
            if parsed_date:
                dated_entries.append((parsed_date, entry))
            else:
                # Keep entries without parseable dates at the end
                entries_without_dates.append(entry)
                self.logger.warning(f"Entry without valid date will be placed at end: {entry[:100]}...")

        # Sort by date (oldest first)
        dated_entries.sort(key=lambda x: x[0])

        # Extract just the entry text (not the date)
        sorted_entries = [entry for _, entry in dated_entries]

        # Add entries without dates at the end
        sorted_entries.extend(entries_without_dates)

        self.logger.info(
            f"Post-processing complete: {len(sorted_entries)} entries "
            f"(sorted {len(dated_entries)} dated, {len(entries_without_dates)} undated)"
        )

        # Join with double newlines
        return '\n\n'.join(sorted_entries)
    
    def _chunk_large_document(self, filename: str, content: str, max_chunk_chars: int = 40000) -> List[Dict[str, str]]:
        """
        Split a large document into smaller chunks.

        Each chunk after the first is prefixed with the document's opening text
        as reference context, because facility/provider/patient identifiers
        typically appear only once at the top of a record. Without this,
        later chunks lose the facility name and entries get misattributed.

        Args:
            filename: Original filename
            content: Document content
            max_chunk_chars: Maximum characters per chunk

        Returns:
            List of document chunks
        """
        if len(content) <= max_chunk_chars:
            return [{'filename': filename, 'content': content}]

        # Capture the document opening (usually contains facility name,
        # provider, and patient identifiers) to carry into later chunks
        header_context = content[:1500].strip()
        context_block = (
            "[REFERENCE CONTEXT - DOCUMENT HEADER FROM START OF THIS FILE. "
            "Use ONLY to identify the facility, provider, and patient for entries "
            "from this chunk. Do NOT create chronology entries from this context block.]\n"
            f"{header_context}\n"
            "[END REFERENCE CONTEXT]\n\n"
        )

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
                # Prefix continuation chunks with the document header context
                if chunk_num > 1:
                    chunk_text = context_block + chunk_text
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
            if chunk_num > 1:
                chunk_text = context_block + chunk_text
            chunks.append({
                'filename': f"{filename} (part {chunk_num})",
                'content': chunk_text
            })

        self.logger.info(f"Split {filename} into {len(chunks)} chunks (header context carried into continuation chunks)")
        return chunks

    def _read_extracted_files(self, input_dir: str) -> List[Dict[str, str]]:
        """Read all extracted text files from the input directory and chunk large ones."""
        input_path = Path(input_dir)
        documents = []

        for txt_file in sorted(input_path.rglob('*.txt')):
            try:
                with open(txt_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    display_name = str(txt_file.relative_to(input_path))

                    # Chunk if too large (20K chars = ~80K tokens with overhead, very conservative)
                    chunks = self._chunk_large_document(
                        display_name, content, max_chunk_chars=20000
                    )
                    documents.extend(chunks)

                    if len(chunks) > 1:
                        self.logger.info(
                            f"Loaded {display_name} ({len(content)} chars) "
                            f"as {len(chunks)} chunks"
                        )
                    else:
                        self.logger.info(f"Loaded {display_name} ({len(content)} chars)")
            except Exception as e:
                self.logger.error(f"Failed to read {txt_file.name}: {e}")

        return documents

    def _map_dates_to_documents(self, documents: List[Dict[str, str]]) -> Dict[str, List[Dict]]:
        """
        Map dates found in documents to the documents themselves.
        
        Args:
            documents: List of document dictionaries
            
        Returns:
            Dictionary mapping date strings (MM/DD/YYYY) to list of relevant documents
        """
        date_map = {}
        date_pattern = r'(\d{1,2})/(\d{1,2})/(\d{4})'
        
        for doc in documents:
            # Find all dates in the document
            matches = re.finditer(date_pattern, doc['content'])
            found_dates = set()
            
            for match in matches:
                try:
                    month, day, year = match.groups()
                    # Normalize date format to MM/DD/YYYY
                    date_obj = datetime(int(year), int(month), int(day))
                    date_str = date_obj.strftime('%m/%d/%Y')
                    found_dates.add(date_str)
                except ValueError:
                    continue
            
            # Add doc to map for each found date
            for date_str in found_dates:
                if date_str not in date_map:
                    date_map[date_str] = []
                date_map[date_str].append(doc)
                
        return date_map

    def _verify_entry_batch(self, entries: List[str], relevant_docs: List[Dict]) -> str:
        """
        Verify a batch of entries against specific source documents.
        """
        if not entries or not relevant_docs:
            return ""

        entries_text = "\n\n".join(entries)
        
        # Prepare source text (limit length per doc to avoid context limits)
        source_text = ""
        for doc in relevant_docs:
            source_text += f"=== DOCUMENT: {doc['filename']} ===\n{doc['content'][:15000]}\n\n"

        prompt = f"""You are a medical record auditor. Verify these chronology entries against the provided source documents.

**CHRONOLOGY ENTRIES TO VERIFY:**
{entries_text}

**SOURCE DOCUMENTS:**
{source_text}

**TASK:**
Check each entry for:
1. **Hallucinations**: Information NOT in source documents
2. **Date Errors**: Wrong dates
3. **Misattributions**: Wrong provider/facility
4. **Exaggerations**: Facts overstated

**OUTPUT FORMAT:**
For EACH issue found, output EXACTLY this format:
Entry Date: [date]
Issue Type: [Hallucination/Date Error/Misattribution/Exaggeration]
Description: [Specific description of the error and what the source actually says]
Severity: [Critical/Moderate/Minor]

If an entry is correct, DO NOT output anything for it.
If no issues found in any entries, output "No issues found."
"""
        # Generous budget: on reasoning models, thinking tokens count
        # against max_tokens, so leave headroom above the expected output
        return self._call_api_with_retry(prompt, max_tokens=8000)

    def verify_chronology(
        self,
        chronology_path: str,
        extracted_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Dict:
        """
        Verify chronology against source documents using smart date matching.
        """
        try:
            if progress_callback:
                progress_callback("🔍 Loading documents for verification...")

            # Read chronology
            with open(chronology_path, 'r', encoding='utf-8') as f:
                chronology_text = f.read()

            # Read source documents
            documents = self._read_extracted_files(extracted_dir)

            if progress_callback:
                progress_callback(f"🧠 Mapping {len(documents)} documents by date...")
            
            # Map dates to documents
            date_map = self._map_dates_to_documents(documents)
            
            # Parse chronology into entries
            entries = [e.strip() for e in chronology_text.split('\n\n') if e.strip()]
            # Skip header if present
            if entries and "MEDICAL RECORDS SUMMARY" in entries[0]:
                entries = entries[1:]

            verification_results = []
            
            # Group entries by date
            entries_by_date = {}
            for entry in entries:
                date_match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})', entry)
                if date_match:
                    try:
                        m, d, y = date_match.groups()
                        date_obj = datetime(int(y), int(m), int(d))
                        date_str = date_obj.strftime('%m/%d/%Y')
                        
                        if date_str not in entries_by_date:
                            entries_by_date[date_str] = []
                        entries_by_date[date_str].append(entry)
                    except ValueError:
                        continue

            total_dates = len(entries_by_date)
            processed_dates = 0

            if progress_callback:
                progress_callback(f"🕵️ Verifying {len(entries)} entries across {total_dates} dates...")

            # Verify each date group
            for date_str, date_entries in entries_by_date.items():
                processed_dates += 1
                if progress_callback:
                    progress_callback(f"Checking {date_str} ({processed_dates}/{total_dates})...")

                relevant_docs = date_map.get(date_str, [])
                
                if not relevant_docs:
                    # No docs found for this date - flag as potential hallucination
                    for entry in date_entries:
                        verification_results.append(
                            f"Entry Date: {date_str}\n"
                            f"Issue Type: Potential Hallucination (No Source)\n"
                            f"Description: No source documents found containing the date {date_str}. "
                            f"This entry may be hallucinated or the date is incorrect.\n"
                            f"Severity: Critical\n"
                        )
                    continue

                # Verify against relevant docs
                result = self._verify_entry_batch(date_entries, relevant_docs)
                if result and "No issues found" not in result:
                    verification_results.append(result)

            # Compile final report
            if not verification_results:
                final_report = "✅ No significant issues detected. All entries verified against source documents."
            else:
                final_report = "# ⚠️ Verification Issues Found\n\n" + "\n\n".join(verification_results)

            if progress_callback:
                progress_callback("✅ Verification complete!")

            return {
                'success': True,
                'verification': final_report,
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

**3. DUPLICATE PREVENTION & SAME-DATE VISITS (CRITICAL):**
- Create ONLY ONE entry per unique date + facility + provider combination
- If multiple pages or sections of a document refer to the same visit, merge them into a SINGLE entry
- Do NOT create separate entries for different sections (e.g., history, exam, plan) of the SAME visit
- If two documents describe the same visit on the same date at the same facility, produce ONE combined entry
- HOWEVER: If the SAME DATE has multiple DISTINCT visits (different providers, different facilities, or clearly separate encounters such as an office visit AND an imaging study), create a SEPARATE entry for EACH distinct visit. Do not skip or merge genuinely different visits just because they share a date.

**4. BILLING RECORDS (CRITICAL):**
- When a file contains BOTH billing/administrative records AND clinical records (chief complaint, HPI, exam, assessment) for the same date of service, ALWAYS build the entry from the CLINICAL record — NEVER from the billing record
- Billing content includes: CPT codes, charge lists, itemized statements, ledgers, invoices, superbills, payment records
- NEVER create an entry from billing content alone when a clinical note exists for that visit
- If a date of service appears ONLY in billing records with no clinical note, do not fabricate clinical details — state only that the service was billed (e.g., "Office visit billed; no clinical note available in records.")

**5. FACILITY ATTRIBUTION (CRITICAL):**
- Many records state the facility name ONLY ONCE at the top of the document. Carry that facility name forward and use it for EVERY entry generated from that document, including entries for later dates of service in the same document
- If a chunk begins with a [REFERENCE CONTEXT] block, use the facility, provider, and patient information from that block to attribute entries — but do NOT create entries from the context block itself
- NEVER guess, invent, or substitute a facility name. If the facility genuinely cannot be determined from the document, write "Facility not documented" in the header
- Do not use a facility name from a DIFFERENT document for entries from this one

**6. THERAPY VISITS (CRITICAL):**
- ALWAYS specify the TYPE of therapy in both the header and the summary when the record identifies it: physical therapy, occupational therapy, speech therapy, chiropractic therapy, psychological/psychotherapy, trauma therapy, etc.
- Example header: "Visit Type: Physical Therapy Initial Evaluation" — never just "Therapy"
- If the record does not specify the therapy type, write "Therapy (type not specified in record)"

**7. IMAGING STUDIES (CRITICAL):**
- The entry header MUST state the imaging MODALITY and BODY PART, e.g., "MRI of the Lumbar Spine without Contrast", "X-ray of the Right Knee", "CT of the Cervical Spine"
- NEVER write just "Imaging" as the visit type or reason
- The summary MUST include the radiologist's Impression/Conclusion: "Impression: [text from report]."
- If an office visit note documents that imaging was ordered or reviewed, mention the modality and body part in that visit's entry as well

**8. FORMATTING RULES (MAINTAIN CURRENT FORMAT):**
- Each date of service entry MUST be ONE CONTINUOUS PARAGRAPH with NO line breaks within the entry
- All labels (Provider:, Chief Complaint:, Assessment:, Plan:, etc.) flow together in the same paragraph
- The ONLY separator between different date entries is a SINGLE blank line
- NEVER use horizontal rules (---) or multiple blank lines between entries
- NEVER split an entry into multiple paragraphs — keep everything in one flowing paragraph

**Additional Guidelines:**
- Tone: Direct, factual, clinical language with in-paragraph headings
- No bulleted lists: Convert all bullets to flowing sentences
- Imaging reports: Include only Impression section
- Therapy notes: Consolidate multiple routine follow-up sessions into one entry listing all dates, always stating the therapy type"""

        prompt = f"""Generate chronology entries from these {len(documents)} medical documents.

{rules}

**DOCUMENTS:**
{documents_text}

**OUTPUT:**
Write chronology entries in proper format, one entry per document/visit.
Do NOT include header or JSON - just the chronology entries."""

        # Call Claude with retry logic. Generous budget: on reasoning models,
        # thinking tokens count against max_tokens
        return self._call_api_with_retry(prompt, max_tokens=16000)

    # ------------------------------------------------------------------ batches
    def _plan_batches(self, documents: List[Dict]) -> List[List[Dict]]:
        """Split documents into token-bounded batches."""
        MAX_BATCH_TOKENS = 60000  # conservative — leaves room for prompt + response
        batches: List[List[Dict]] = []
        current: List[Dict] = []
        current_tokens = 0
        for doc in documents:
            doc_tokens = len(doc["content"]) // 4  # ~4 chars/token
            if current and (current_tokens + doc_tokens) > MAX_BATCH_TOKENS:
                batches.append(current)
                current = [doc]
                current_tokens = doc_tokens
            else:
                current.append(doc)
                current_tokens += doc_tokens
        if current:
            batches.append(current)
        return batches

    def generate_batches(
        self,
        input_dir: str,
        batches_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
    ) -> Dict:
        """Run Claude over every batch, saving each result to disk.

        Idempotent: batches whose output file already exists (and is non-empty)
        are skipped on rerun, so this supports resume after a pause or crash.

        Args:
            input_dir: directory containing extracted ``*.txt`` files.
            batches_dir: directory to write ``batch_NN.md`` files to.
            progress_callback: optional callable invoked with status strings.
            should_pause: optional callable returning True to request a
                cooperative pause. Checked between batches. Raises
                :class:`PauseRequested` if it returns True.

        Returns:
            ``{'success': bool, 'total_batches': int, 'documents': int, ...}``
        """
        from .session_state import PauseRequested

        documents = self._read_extracted_files(input_dir)
        if not documents:
            return {"success": False, "error": "No extracted text files found"}

        batches = self._plan_batches(documents)
        total_batches = len(batches)
        batches_path = Path(batches_dir)
        batches_path.mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback(
                f"🤖 {len(documents)} documents → {total_batches} batch(es)"
            )

        BATCH_DELAY = 3
        completed = 0
        skipped = 0
        for batch_num, batch in enumerate(batches, 1):
            batch_file = batches_path / f"batch_{batch_num:03d}.md"
            if batch_file.exists() and batch_file.stat().st_size > 0:
                completed += 1
                skipped += 1
                if progress_callback:
                    progress_callback(
                        f"⏭️  Batch {batch_num}/{total_batches} already done — skipping"
                    )
                continue

            if should_pause and should_pause():
                raise PauseRequested(f"Paused before batch {batch_num}")

            if progress_callback:
                progress_callback(
                    f"📝 Batch {batch_num}/{total_batches} ({len(batch)} docs)…"
                )
            batch_md = self._process_batch(batch, batch_num, total_batches)

            # Write atomically so a crash mid-write doesn't leave a partial file
            tmp = batch_file.with_suffix(".md.tmp")
            tmp.write_text(batch_md, encoding="utf-8")
            os.replace(tmp, batch_file)
            completed += 1

            if batch_num < total_batches:
                time.sleep(BATCH_DELAY)

        if progress_callback:
            msg = f"✅ {completed}/{total_batches} batches ready"
            if skipped:
                msg += f" ({skipped} resumed from disk)"
            progress_callback(msg)

        return {
            "success": True,
            "total_batches": total_batches,
            "documents": len(documents),
            "batches_completed": completed,
            "batches_skipped_from_disk": skipped,
        }

    # -------------------------------------------------------------- rendering
    def _combine_batches(self, batches_dir: str) -> str:
        """Read every ``batch_NNN.md`` in order, concatenate, sort, dedup."""
        batches_path = Path(batches_dir)
        files = sorted(batches_path.glob("batch_*.md"))
        if not files:
            return ""
        combined = "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files)
        return self._sort_entries_chronologically(combined)

    def extract_header(
        self,
        input_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, str]:
        """Extract patient name, date of birth, and date of injury from records.

        Sends a short slice of the most likely-informative documents to Claude
        and asks for a strict JSON response. Returns placeholders if the model
        cannot confidently identify a field.
        """
        if progress_callback:
            progress_callback("🪪 Extracting patient header from records…")

        docs = self._read_extracted_files(input_dir)
        if not docs:
            return {
                "patient_name": "[See Records]",
                "date_of_birth": "[See Records]",
                "date_of_injury": "[See Records]",
            }

        # Build a short corpus: first 2000 chars of each doc, up to ~40k chars total
        snippets: List[str] = []
        total = 0
        BUDGET = 40000
        for d in docs:
            snippet = d["content"][:2000]
            if total + len(snippet) > BUDGET:
                break
            snippets.append(f"=== {d['filename']} ===\n{snippet}")
            total += len(snippet)
        corpus = "\n\n".join(snippets)

        prompt = (
            "You are reading excerpts from medical records for a single patient. "
            "Return STRICT JSON with three fields: patient_name (FULL NAME in ALL CAPS), "
            "date_of_birth (formatted 'Month Day, YYYY'), and date_of_injury "
            "(formatted 'Month Day, YYYY'). If a field cannot be determined with "
            "high confidence, use the string '[See Records]'. Do NOT include any "
            "other text — just the JSON object.\n\n"
            f"RECORDS EXCERPTS:\n{corpus}\n\n"
            'Respond with ONLY the JSON object, no code fences, no commentary.'
        )
        try:
            raw = self._call_api_with_retry(prompt, max_tokens=2000)
            # Strip potential code fences defensively
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            return {
                "patient_name": str(data.get("patient_name") or "[See Records]"),
                "date_of_birth": str(data.get("date_of_birth") or "[See Records]"),
                "date_of_injury": str(data.get("date_of_injury") or "[See Records]"),
            }
        except Exception as e:
            self.logger.warning(f"Header extraction failed, using placeholders: {e}")
            return {
                "patient_name": "[See Records]",
                "date_of_birth": "[See Records]",
                "date_of_injury": "[See Records]",
            }

    def generate_summary_and_gaps(
        self,
        chronology_md: str,
        source_filenames: List[str],
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, str]:
        """Produce an executive summary and a gaps analysis from the chronology.

        Falls back to short placeholder text if the API call fails — callers
        should treat failure as non-fatal.
        """
        if progress_callback:
            progress_callback("🧾 Generating executive summary and gaps analysis…")

        # Keep chronology input bounded for this call
        MAX_CHARS = 120000
        snippet = chronology_md if len(chronology_md) <= MAX_CHARS else chronology_md[:MAX_CHARS]
        file_list = "\n".join(f"- {name}" for name in source_filenames)

        prompt = (
            "You are a medical-legal analyst. Given the medical chronology "
            "below, produce TWO markdown documents separated exactly by the "
            "marker line '===GAPS===' on its own line.\n\n"
            "FIRST document: an Executive Summary (plain prose, no bullets, "
            "2-4 short paragraphs) covering: (1) the mechanism and date of "
            "injury, (2) the principal diagnoses, (3) the course of treatment "
            "including imaging and interventions, and (4) current status and "
            "outstanding issues. Do not use bold, bullets, or headings.\n\n"
            "SECOND document: a Gaps and Quality Notes analysis (plain prose) "
            "flagging: missing or unexplained gaps in care, dates referenced in "
            "records that lack corresponding entries, OCR-looking artifacts, "
            "ambiguous provider attributions, and any records that warrant "
            "manual review. Use a direct factual tone. No bullets, no bold.\n\n"
            f"SOURCE FILES USED:\n{file_list}\n\n"
            f"CHRONOLOGY:\n{snippet}\n\n"
            "Remember: output the Executive Summary FIRST, then a line reading "
            "exactly ===GAPS=== , then the Gaps document. Do NOT restate the "
            "chronology. Do NOT include preambles."
        )
        try:
            raw = self._call_api_with_retry(prompt, max_tokens=8000)
            if "===GAPS===" in raw:
                summary_part, gaps_part = raw.split("===GAPS===", 1)
            else:
                # Fallback: treat entire response as summary
                summary_part, gaps_part = raw, "Gaps analysis unavailable."
            return {
                "summary_md": summary_part.strip(),
                "gaps_md": gaps_part.strip(),
            }
        except Exception as e:
            self.logger.warning(f"Summary/gaps generation failed: {e}")
            return {
                "summary_md": (
                    "Executive summary could not be generated automatically. "
                    "Please review chronology.md directly."
                ),
                "gaps_md": (
                    "Automated gaps analysis failed. Review source documents "
                    "against chronology.md for completeness."
                ),
            }

    def assemble_outputs(
        self,
        input_dir: str,
        batches_dir: str,
        output_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """Stitch per-batch output into final chronology + summary + gaps + JSON.

        Call after :meth:`generate_batches`. Safe to call multiple times — it
        simply rewrites the output files from the batch directory.
        """
        if progress_callback:
            progress_callback("🔄 Stitching batches and sorting chronologically…")

        body = self._combine_batches(batches_dir)
        if not body:
            return {"success": False, "error": "No batch output files found to assemble"}

        # Extract real patient header
        header_info = self.extract_header(input_dir, progress_callback)
        header = (
            "MEDICAL RECORDS SUMMARY\n"
            f"{header_info['patient_name']}\n"
            f"Date of Birth: {header_info['date_of_birth']}\n"
            f"Date of Injury: {header_info['date_of_injury']}\n\n"
        )
        chronology_md = header + body

        # Real summary + gaps
        input_path = Path(input_dir)
        source_files = [str(p.relative_to(input_path)) for p in sorted(input_path.rglob("*.txt"))]
        docs_md = self.generate_summary_and_gaps(
            chronology_md, source_files, progress_callback
        )
        summary_md = docs_md["summary_md"]
        gaps_md = docs_md["gaps_md"]

        # Structured JSON (proper serialization, no hand-rolled escaping)
        chronology_json = {
            "metadata": {
                "patient_name": header_info["patient_name"],
                "date_of_birth": header_info["date_of_birth"],
                "date_of_injury": header_info["date_of_injury"],
                "generated": datetime.now().isoformat(timespec="seconds"),
                "documents_processed": len(source_files),
                "batches": len(list(Path(batches_dir).glob("batch_*.md"))),
            },
            "chronology_markdown": chronology_md,
            "source_files": source_files,
        }

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        files_written: Dict[str, str] = {}

        def _atomic_write(path: Path, text: str) -> None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

        chronology_file = output_path / "chronology.md"
        _atomic_write(chronology_file, chronology_md)
        files_written["chronology.md"] = str(chronology_file)

        json_file = output_path / "chronology.json"
        _atomic_write(json_file, json.dumps(chronology_json, indent=2))
        files_written["chronology.json"] = str(json_file)

        summary_file = output_path / "summary.md"
        _atomic_write(summary_file, summary_md)
        files_written["summary.md"] = str(summary_file)

        gaps_file = output_path / "gaps.md"
        _atomic_write(gaps_file, gaps_md)
        files_written["gaps.md"] = str(gaps_file)

        if progress_callback:
            progress_callback("✅ Output files written")

        return {
            "success": True,
            "files": files_written,
            "header": header_info,
            "source_files": source_files,
        }

    # ------------------------------------------- legacy all-in-one convenience
    def generate_chronology(
        self,
        input_dir: str,
        output_dir: str,
        base_dir: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """Backwards-compatible one-shot: batches + assemble, no resume.

        Kept for any external caller; the pipeline now drives
        :meth:`generate_batches` and :meth:`assemble_outputs` separately so it
        can checkpoint between them.
        """
        try:
            # Use a tmp batches dir inside the output dir for legacy callers
            batches_dir = str(Path(output_dir) / "_batches")
            gen = self.generate_batches(input_dir, batches_dir, progress_callback)
            if not gen["success"]:
                return gen
            return self.assemble_outputs(input_dir, batches_dir, output_dir, progress_callback)
        except Exception as e:
            self.logger.error(f"Chronology generation failed: {e}")
            return {"success": False, "error": str(e)}
