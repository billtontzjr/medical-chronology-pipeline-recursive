"""Phase 6 cross-check.

Final verification pass over the assembled chronology. For each clinical
phrase in each entry we compute the token-set Jaccard similarity against
candidate verified facts that share the same visit identity. Anything
below ``CROSS_CHECK_JACCARD_THRESHOLD`` is flagged in a report so the
human reviewer can inspect it before the chronology goes out.

The check is soft by default; a strict mode is available for users who
want a hard block on weakly-supported phrases.
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.schemas import VerifiedFact


CROSS_CHECK_JACCARD_THRESHOLD = 0.35


STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "to",
        "for",
        "with",
        "and",
        "or",
        "in",
        "on",
        "at",
        "by",
        "is",
        "was",
        "were",
        "patient",
        "states",
        "reports",
        "denies",
    }
)

_PUNCT_TBL = str.maketrans({c: " " for c in string.punctuation})

_ENTRY_DATE_RE = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\.\s*(.*)$")

# Match: "Chief Complaint:" "History:" "History of Present Illness:" etc.
_SECTION_LABEL_RE = re.compile(
    r"(Chief Complaint|History of Present Illness|History|Physical Examination|Exam|"
    r"Diagnostics?|Assessment|Plan|Impression):",
    re.IGNORECASE,
)


# --------------------------------------------------------------- tokenization

def tokenize(s: str) -> set:
    if not s:
        return set()
    cleaned = s.translate(_PUNCT_TBL).lower()
    return {tok for tok in cleaned.split() if tok and tok not in STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0  # conservative: treat double-empty as no evidence
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ------------------------------------------------------------------ parsing

@dataclass
class EntryPhrase:
    label: Optional[str]
    text: str


@dataclass
class ParsedEntry:
    visit_date: str
    facility: Optional[str]
    raw_first_line: str
    phrases: List[EntryPhrase] = field(default_factory=list)


def _split_first_sentence(entry: str) -> Tuple[str, str, Optional[str]]:
    """Return (date_str, body, facility) by parsing the entry head.

    Expected head: ``MM/DD/YYYY. Facility Name. Provider Name, Credentials. Visit Type.``
    We strip those four sentence-segments off the front so the body
    passed to phrase-splitting contains only the clinical narrative.
    """
    m = _ENTRY_DATE_RE.match(entry)
    if not m:
        return "", entry, None
    date_str = m.group(1)
    rest = m.group(2)
    parts = rest.split(". ", 3)
    facility: Optional[str] = None
    if parts:
        facility = parts[0].strip() or None
    if len(parts) >= 4:
        body = parts[3]
    else:
        body = ". ".join(parts[1:]) if len(parts) > 1 else ""
    return date_str, body, facility


def parse_entries(chronology_md: str) -> List[ParsedEntry]:
    """Split a chronology markdown into ParsedEntry objects.

    An entry is any paragraph whose first non-whitespace line starts
    with ``MM/DD/YYYY.``. Other paragraphs (header line, undated
    appendix heading, blank lines) are ignored.
    """
    entries: List[ParsedEntry] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", chronology_md) if p.strip()]
    for para in paragraphs:
        first_line = para.splitlines()[0]
        if not _ENTRY_DATE_RE.match(first_line):
            continue
        date_str, body, facility = _split_first_sentence(para)
        phrases = _split_phrases(body)
        entries.append(
            ParsedEntry(
                visit_date=date_str,
                facility=facility,
                raw_first_line=first_line,
                phrases=phrases,
            )
        )
    return entries


def _split_phrases(body: str) -> List[EntryPhrase]:
    """Split entry body on section labels.

    Returns a list of (label, text) pairs. Text before the first label
    is yielded with ``label=None``.
    """
    if not body:
        return []
    spans: List[Tuple[Optional[str], int, int]] = []
    matches = list(_SECTION_LABEL_RE.finditer(body))
    if not matches:
        return [EntryPhrase(label=None, text=body.strip())]

    if matches[0].start() > 0:
        spans.append((None, 0, matches[0].start()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        spans.append((m.group(1), m.end(), end))

    phrases: List[EntryPhrase] = []
    for label, s, e in spans:
        text = body[s:e].strip().rstrip(".").strip()
        if not text:
            continue
        phrases.append(EntryPhrase(label=label, text=text))
    return phrases


# ----------------------------------------------------------------- candidates

def _load_verified(jsonl_path: Path) -> List[VerifiedFact]:
    if not jsonl_path.exists():
        return []
    facts: List[VerifiedFact] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        facts.append(VerifiedFact.model_validate_json(line))
    return facts


def _facility_match(entry_facility: Optional[str], fact_facility: Optional[str]) -> bool:
    if entry_facility is None or fact_facility is None:
        return True
    return tokenize(entry_facility) & tokenize(fact_facility) != set()


def _candidates_for(entry: ParsedEntry, facts: List[VerifiedFact]) -> List[VerifiedFact]:
    by_date = [f for f in facts if f.visit_date == entry.visit_date]
    by_date_and_facility = [f for f in by_date if _facility_match(entry.facility, f.facility)]
    return by_date_and_facility or by_date


# ------------------------------------------------------------------ checking

@dataclass
class Warning:
    visit_date: str
    facility: Optional[str]
    phrase_label: Optional[str]
    phrase_text: str
    best_score: float
    best_fact_finding: Optional[str]
    best_fact_quote: Optional[str]


@dataclass
class CrossCheckReport:
    warnings: List[Warning] = field(default_factory=list)
    entries_checked: int = 0
    phrases_checked: int = 0


class CrossCheckFailed(RuntimeError):
    """Raised when strict mode encounters any below-threshold phrase."""


def cross_check(
    chronology_md: str,
    verified_facts_path: Path,
    *,
    threshold: float = CROSS_CHECK_JACCARD_THRESHOLD,
    strict: bool = False,
) -> CrossCheckReport:
    facts = _load_verified(verified_facts_path)
    report = CrossCheckReport()

    for entry in parse_entries(chronology_md):
        report.entries_checked += 1
        candidates = _candidates_for(entry, facts)
        cand_tokens = [
            (f, tokenize(f.finding_text) | tokenize(f.verbatim_quote))
            for f in candidates
        ]
        for phrase in entry.phrases:
            report.phrases_checked += 1
            phrase_tokens = tokenize(phrase.text)
            best_score = 0.0
            best: Optional[VerifiedFact] = None
            for cand, tokens in cand_tokens:
                s = jaccard(phrase_tokens, tokens)
                if s > best_score:
                    best_score = s
                    best = cand
            if best_score < threshold:
                report.warnings.append(
                    Warning(
                        visit_date=entry.visit_date,
                        facility=entry.facility,
                        phrase_label=phrase.label,
                        phrase_text=phrase.text,
                        best_score=best_score,
                        best_fact_finding=best.finding_text if best else None,
                        best_fact_quote=best.verbatim_quote if best else None,
                    )
                )

    if strict and report.warnings:
        raise CrossCheckFailed(
            f"{len(report.warnings)} phrase(s) below threshold {threshold}"
        )
    return report


def write_report(report: CrossCheckReport, output_path: Path) -> None:
    lines: List[str] = []
    lines.append("# Cross-check report")
    lines.append("")
    lines.append(
        f"Checked {report.entries_checked} entries / {report.phrases_checked} phrases. "
        f"{len(report.warnings)} warning(s)."
    )
    lines.append("")
    if not report.warnings:
        lines.append("No phrases fell below the support threshold. Looks clean.")
    else:
        for i, w in enumerate(report.warnings, start=1):
            label = w.phrase_label or "(unlabeled)"
            lines.append(
                f"## {i}. {w.visit_date} {w.facility or 'Unknown facility'} - {label}"
            )
            lines.append("")
            lines.append(f"Phrase: {w.phrase_text}")
            lines.append("")
            lines.append(f"Best Jaccard: {w.best_score:.2f}")
            if w.best_fact_finding:
                lines.append(f"Closest finding: {w.best_fact_finding}")
                lines.append(f"Closest quote: {w.best_fact_quote}")
            else:
                lines.append("No candidate facts found for this visit.")
            lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def cross_check_and_report(
    chronology_md_path: Path,
    verified_facts_path: Path,
    output_path: Path,
    *,
    threshold: float = CROSS_CHECK_JACCARD_THRESHOLD,
    strict: bool = False,
) -> CrossCheckReport:
    md = chronology_md_path.read_text(encoding="utf-8")
    report = cross_check(md, verified_facts_path, threshold=threshold, strict=strict)
    write_report(report, output_path)
    return report


__all__ = [
    "CROSS_CHECK_JACCARD_THRESHOLD",
    "STOPWORDS",
    "tokenize",
    "jaccard",
    "parse_entries",
    "cross_check",
    "cross_check_and_report",
    "write_report",
    "CrossCheckFailed",
    "CrossCheckReport",
    "Warning",
    "EntryPhrase",
    "ParsedEntry",
]
