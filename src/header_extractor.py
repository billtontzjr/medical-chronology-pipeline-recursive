"""Patient header extraction (Phase 7a).

Two-stage extraction:

1. Deterministic regex pass over all OCR text. Looks for ``Patient Name:``,
   ``DOB``, ``Date of Injury`` and friends with their values. If all
   three required fields are found this stage's result is used directly.
2. Claude fallback when the regex pass is incomplete. The fallback
   returns JSON with a verbatim quote for every value; each quote is
   verified by substring search against the source OCR before its value
   is accepted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from src.anthropic_client import AnthropicClient
from src.prompts.header import build_header_prompt
from src.verifier import _build_offset_map, normalize_date, verify_quote


log = logging.getLogger(__name__)


DOB_PATTERNS = [
    re.compile(
        r"(?:Date of Birth|D\.?O\.?B\.?|Birthdate)\s*:?\s*"
        r"([\d/\-]+|[A-Za-z]+\s+\d{1,2},?\s*\d{4})",
        re.IGNORECASE,
    ),
]
DOI_PATTERNS = [
    re.compile(
        r"(?:Date of Injury|D\.?O\.?I\.?|Date of Accident|Date of Incident)\s*:?\s*"
        r"([\d/\-]+|[A-Za-z]+\s+\d{1,2},?\s*\d{4})",
        re.IGNORECASE,
    ),
]
NAME_PATTERNS = [
    re.compile(r"Patient\s*Name\s*:\s*([A-Z][A-Za-z\.\-' ]{2,80})", re.IGNORECASE),
    re.compile(r"Patient\s*:\s*([A-Z][A-Za-z\.\-' ]{2,80})", re.IGNORECASE),
    re.compile(r"\bName\s*:\s*([A-Z][A-Za-z\.\-' ]{2,80})", re.IGNORECASE),
]


@dataclass
class Header:
    patient_name: Optional[str] = None
    dob: Optional[str] = None
    doi: Optional[str] = None

    @property
    def complete(self) -> bool:
        return all((self.patient_name, self.dob, self.doi))


def _first_match(patterns, text: str) -> Optional[str]:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1).strip().rstrip(".")
    return None


_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# Field labels that OCR often runs straight into the name on the same line.
_TRAILING_LABEL_RE = re.compile(
    r"\s+(DOB|D\.?O\.?B\.?|Date of Birth|Sex|Gender|MRN|Acct|Account|Age|"
    r"Date|Provider|Attending|Room)\b.*$",
    re.IGNORECASE,
)


def _normalize_name(raw: Optional[str]) -> Optional[str]:
    """Clean a regex-captured patient name.

    The capture is greedy over letters and spaces, so OCR frequently drags
    in what follows the name on the same line: a state code ("NANCY L
    SMITH TX"), the next field label ("JANE DOE DOB"), or a wide gap
    into an unrelated column. Cut at wide gaps and trailing labels, and
    drop a trailing US state code when a full name still remains.
    """
    if raw is None:
        return None
    s = raw.split("\n", 1)[0]
    s = re.split(r"\s{2,}|\t", s.strip())[0]
    s = _TRAILING_LABEL_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    tokens = s.split()
    while len(tokens) > 2 and tokens[-1].upper() in _US_STATES:
        tokens.pop()
    s = " ".join(tokens).strip(" .,-")
    return s or None


def regex_extract(all_text: str) -> Header:
    name = _normalize_name(_first_match(NAME_PATTERNS, all_text))
    dob_raw = _first_match(DOB_PATTERNS, all_text)
    doi_raw = _first_match(DOI_PATTERNS, all_text)
    dob, _ = normalize_date(dob_raw) if dob_raw else (None, True)
    doi, _ = normalize_date(doi_raw) if doi_raw else (None, True)
    return Header(patient_name=name, dob=dob, doi=doi)


def _claude_fallback(
    anthropic_client: AnthropicClient,
    all_text: str,
    *,
    model: Optional[str] = None,
) -> Tuple[Header, dict]:
    """Ask Claude for header values and verify every quote.

    Returns the Header plus a debug dict with the raw Claude response
    for inclusion in gaps.md if needed.
    """
    prompt = build_header_prompt(all_text)
    raw = anthropic_client.complete(prompt, model=model, max_tokens=600)
    raw_clean = raw.strip()
    if raw_clean.startswith("```"):
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_clean, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw_clean)
    except json.JSONDecodeError:
        log.warning("Claude header fallback returned non-JSON: %r", raw_clean[:200])
        return Header(), {"error": "non_json", "raw": raw_clean[:500]}

    source_norm, idx_map = _build_offset_map(all_text)

    def _accept(value_key: str, quote_key: str) -> Optional[str]:
        value = data.get(value_key)
        quote = data.get(quote_key)
        if not value or not quote:
            return None
        verified, _, _ = verify_quote(quote, source_norm, idx_map)
        return value if verified else None

    name = _accept("patient_name", "patient_name_quote")
    dob_raw = _accept("dob", "dob_quote")
    doi_raw = _accept("doi", "doi_quote")

    dob, _ = normalize_date(dob_raw) if dob_raw else (None, True)
    doi, _ = normalize_date(doi_raw) if doi_raw else (None, True)
    return Header(patient_name=name, dob=dob, doi=doi), {"raw": raw_clean[:500]}


def extract_header(
    all_text: str,
    *,
    anthropic_client: Optional[AnthropicClient] = None,
    model: Optional[str] = None,
) -> Header:
    """Run regex stage then Claude fallback if needed.

    The Claude fallback only fires when the regex pass is missing at
    least one of the three required fields AND an anthropic client is
    available. Otherwise the regex result is returned as-is.
    """
    regex = regex_extract(all_text)
    if regex.complete or anthropic_client is None:
        return regex

    fallback, _debug = _claude_fallback(anthropic_client, all_text, model=model)

    return Header(
        patient_name=regex.patient_name or fallback.patient_name,
        dob=regex.dob or fallback.dob,
        doi=regex.doi or fallback.doi,
    )


def concatenated_ocr(extracted_dir: Path, max_chars: int = 100_000) -> str:
    """Concatenate all OCR text files in directory order, capped.

    Capped at 100k chars because headers virtually always appear on the
    front matter of one of the first documents.
    """
    buf: list[str] = []
    total = 0
    for path in sorted(extracted_dir.glob("*.txt")):
        chunk = path.read_text(encoding="utf-8")
        buf.append(f"=== FILE {path.name} ===\n{chunk}")
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n\n".join(buf)[:max_chars]


__all__ = [
    "Header",
    "regex_extract",
    "extract_header",
    "concatenated_ocr",
]
