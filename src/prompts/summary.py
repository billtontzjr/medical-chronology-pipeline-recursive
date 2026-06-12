"""Executive-summary and gaps-analysis prompt builders.

The summary prompt is now driven by the assembled chronology markdown
rather than the raw fact list. The chronology is already a condensed
prose version of the verified facts (one paragraph per visit), so it
is the right level of abstraction for an executive summary AND it is
orders of magnitude smaller than dumping every fact JSON.

The gaps prompt is driven by deterministically-computed signals
(date gaps, undated counts, missing facilities, low-confidence counts)
plus a small sample of low-confidence facts. This keeps the gaps call
predictable in size even for massive record sets.

Both prompts cap their inputs against ``SAFE_PROMPT_CHARS`` so a single
call can never exceed Claude's 1M-token context.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional

from src.schemas import VerifiedFact
from src.token_budget import SAFE_PROMPT_CHARS, truncate_to_chars


SUMMARY_TEMPLATE = """You are writing a 2- to 4-paragraph executive summary of a patient's medical history based ONLY on the chronology entries below. Each entry is a single paragraph describing one visit.

RULES:
- Prose only. No bullets. No numbered lists. No bolding. No em-dashes.
- Do not invent any clinical content. Every claim must derive from the chronology entries.
- Begin with the mechanism of injury or earliest finding, walk forward chronologically, and end with the most recent treatment status.
- Keep paragraphs short (3 to 5 sentences each).
- If the chronology was truncated for length, focus on the earliest and most recent visits; do not fabricate visits you cannot see.

CHRONOLOGY ENTRIES:
{chronology}

OUTPUT (summary prose only, no preamble):
"""


GAPS_TEMPLATE = """You are writing a gaps analysis for a medical chronology. Use the deterministic gap signals below as your factual basis. The signals were computed from the verified facts; you do not need to recount them, you need to narrate them.

DETERMINISTIC GAP SIGNALS:
{gap_signals_json}

SAMPLE OF LOW-CONFIDENCE FACTS (up to a small representative subset):
{low_confidence_sample_json}

RULES:
- Prose only. No bullets. No bolding. No em-dashes.
- 2 to 4 short paragraphs.
- Discuss: long stretches between dates, undated or partially-attributed facts, low-confidence findings warranting manual review, and any pattern of missing facility or provider attribution.
- Mention specific dates and facilities where the signals provide them.
- If the chronology spans years, name the earliest and latest dates.

OUTPUT (gaps prose only, no preamble):
"""


def build_summary_prompt(
    chronology_md: str,
    *,
    max_chars: int = SAFE_PROMPT_CHARS,
) -> str:
    """Build the executive-summary prompt from the assembled chronology MD.

    The chronology is truncated to ``max_chars`` so a single call
    cannot exceed the prompt budget. A warning is logged when
    truncation happens (callers see it in Render logs).
    """
    body, _ = truncate_to_chars(chronology_md, max_chars=max_chars, label="summary")
    return SUMMARY_TEMPLATE.format(chronology=body)


def build_gaps_prompt(
    gap_signals: dict,
    low_confidence_sample: Iterable[VerifiedFact],
    *,
    max_chars: int = SAFE_PROMPT_CHARS,
) -> str:
    """Build the gaps-analysis prompt from precomputed signals."""
    signals_json = json.dumps(gap_signals, indent=2, default=str)
    sample = [
        {
            "date": f.visit_date,
            "facility": f.facility,
            "provider": f.provider_name,
            "category": f.fact_category,
            "finding": f.finding_text,
        }
        for f in list(low_confidence_sample)[:50]
    ]
    sample_json = json.dumps(sample, indent=2)
    body = GAPS_TEMPLATE.format(
        gap_signals_json=signals_json,
        low_confidence_sample_json=sample_json,
    )
    body, _ = truncate_to_chars(body, max_chars=max_chars, label="gaps")
    return body


__all__ = ["build_summary_prompt", "build_gaps_prompt"]
