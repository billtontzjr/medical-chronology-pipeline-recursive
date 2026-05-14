"""Executive-summary and gaps-analysis prompt builders.

Inputs are the verified facts (compacted). The model never sees the raw
OCR, so it cannot reintroduce hallucinated content at this stage.
"""

from __future__ import annotations

import json
from typing import Iterable

from src.schemas import VerifiedFact


def _compact_fact(fact: VerifiedFact) -> dict:
    return {
        "date": fact.visit_date,
        "facility": fact.facility,
        "provider": fact.provider_name,
        "credentials": fact.provider_credentials,
        "visit_type": fact.visit_type,
        "category": fact.fact_category,
        "finding": fact.finding_text,
        "confidence": fact.extraction_confidence,
    }


def _compact_payload(facts: Iterable[VerifiedFact]) -> str:
    return json.dumps([_compact_fact(f) for f in facts], indent=2)


SUMMARY_TEMPLATE = """You are writing a 2- to 4-paragraph executive summary of a patient's medical history based ONLY on the verified facts JSON below. Each fact has a date, facility, provider, category, and a short clinical finding.

RULES:
- Prose only. No bullets. No numbered lists. No bolding. No em-dashes.
- Do not invent any clinical content. Every claim must derive from the verified facts.
- Begin with the mechanism of injury or earliest finding, walk forward chronologically, and end with the most recent treatment status.
- Keep paragraphs short (3 to 5 sentences each).

VERIFIED FACTS:
{facts_json}

OUTPUT (summary prose only, no preamble):
"""


GAPS_TEMPLATE = """You are writing a gaps analysis for a medical chronology based ONLY on the verified facts JSON below. Identify and list as prose (no bullets):
- Visits where the visit_date is missing or ambiguous.
- Visits where the provider or facility is unclear or missing.
- Long stretches between dates where additional records would likely exist.
- Categories of records you would expect but cannot find (e.g., imaging referenced in a follow-up but no imaging report present).
- Any facts marked with low confidence.

RULES:
- Prose only. No bullets. No bolding. No em-dashes.
- Be specific. Mention dates and facilities where possible.
- 2 to 4 short paragraphs.

VERIFIED FACTS:
{facts_json}

OUTPUT (gaps prose only, no preamble):
"""


def build_summary_prompt(facts: Iterable[VerifiedFact]) -> str:
    return SUMMARY_TEMPLATE.format(facts_json=_compact_payload(facts))


def build_gaps_prompt(facts: Iterable[VerifiedFact]) -> str:
    return GAPS_TEMPLATE.format(facts_json=_compact_payload(facts))


__all__ = ["build_summary_prompt", "build_gaps_prompt"]
