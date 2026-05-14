"""Header-extraction fallback prompt.

Used only when the regex pass cannot find one or more of patient_name,
DOB, or DOI with confidence. The prompt forces strict JSON output and
demands a verbatim quote for every value so the verifier can confirm
the value is in fact present in the source.
"""

from __future__ import annotations


PROMPT_TEMPLATE = """You are reading the concatenated OCR text of a patient's medical records. Locate three header values and return them as strict JSON, with a verbatim quote for each value that proves where the value came from.

REQUIRED FIELDS:
- patient_name: the patient's full name (First Middle Last). If the records use ALL CAPS, return the name in title case.
- dob: the patient's date of birth in MM/DD/YYYY format if possible.
- doi: the date of injury (or date of accident / incident) in MM/DD/YYYY format if possible.

RULES:
1. For each value, include a verbatim_quote field that is a literal substring of the source text. Do not clean up OCR errors in the quote. Do not paraphrase.
2. If you cannot find a value with a clean verbatim quote, set the value to null and quote to null. Do not guess.
3. Output a single JSON object exactly matching this schema (no preamble, no markdown fences, no commentary):

{{
  "patient_name": string or null,
  "patient_name_quote": string or null,
  "dob": string or null,
  "dob_quote": string or null,
  "doi": string or null,
  "doi_quote": string or null
}}

SOURCE TEXT:
{source}

OUTPUT (JSON only):
"""


def build_header_prompt(source_text: str, max_chars: int = 12000) -> str:
    """Render the header extraction prompt.

    We cap the source size to keep the prompt bounded; the predecessor
    saw records sometimes ran to many MB of OCR. Headers almost always
    appear on the first page of one of the early documents, so the
    leading slice is sufficient.
    """
    text = source_text[:max_chars]
    return PROMPT_TEMPLATE.format(source=text)


__all__ = ["build_header_prompt"]
