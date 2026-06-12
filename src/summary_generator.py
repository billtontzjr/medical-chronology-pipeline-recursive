"""Executive summary and gaps generation (Phase 7b).

The summary is composed from the assembled chronology markdown rather
than from the raw verified-fact JSON. The chronology is already a
condensed prose form of the verified facts (one paragraph per visit),
which keeps the summary prompt under Claude's 1M-token context even
for very large record sets.

The gaps analysis runs on deterministically-computed signals (long
date gaps, undated counts, missing-facility counts, low-confidence
counts) plus a small sample of low-confidence facts. We never dump
the full fact JSONL into a Claude prompt.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.anthropic_client import AnthropicClient
from src.prompts.summary import build_gaps_prompt, build_summary_prompt
from src.schemas import VerifiedFact
from src.session_state import SessionStore


log = logging.getLogger(__name__)


# Window for "long gap" alerting in days. Two records more than this far
# apart are surfaced to the gaps analysis explicitly.
LONG_GAP_DAYS = 90


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


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except ValueError:
        return None


def _compute_gap_signals(facts: List[VerifiedFact]) -> Tuple[Dict, List[VerifiedFact]]:
    """Compute deterministic gap signals.

    Returns ``(signals_dict, low_confidence_sample)``.
    """
    dated = sorted(
        {(_parse_date(f.visit_date), f.visit_date) for f in facts if _parse_date(f.visit_date)}
    )
    parsed_dates = [d for d, _ in dated if d is not None]

    long_gaps: List[Dict] = []
    for i in range(len(parsed_dates) - 1):
        a, b = parsed_dates[i], parsed_dates[i + 1]
        delta = (b - a).days
        if delta > LONG_GAP_DAYS:
            long_gaps.append(
                {
                    "from": a.strftime("%m/%d/%Y"),
                    "to": b.strftime("%m/%d/%Y"),
                    "days": delta,
                }
            )
    long_gaps.sort(key=lambda g: g["days"], reverse=True)

    undated = [f for f in facts if not f.visit_date]
    missing_facility = [f for f in facts if not f.facility]
    missing_provider = [f for f in facts if not f.provider_name]
    low_conf = [f for f in facts if f.extraction_confidence == "low"]

    by_facility: Dict[str, int] = {}
    for f in facts:
        key = f.facility or "(unknown facility)"
        by_facility[key] = by_facility.get(key, 0) + 1
    top_facilities = sorted(by_facility.items(), key=lambda x: x[1], reverse=True)[:10]

    signals = {
        "total_facts": len(facts),
        "earliest_date": parsed_dates[0].strftime("%m/%d/%Y") if parsed_dates else None,
        "latest_date": parsed_dates[-1].strftime("%m/%d/%Y") if parsed_dates else None,
        "distinct_dated_visits": len({f.visit_date for f in facts if f.visit_date}),
        "undated_fact_count": len(undated),
        "facts_missing_facility": len(missing_facility),
        "facts_missing_provider": len(missing_provider),
        "low_confidence_count": len(low_conf),
        "long_gaps_over_threshold_days": long_gaps[:25],
        "long_gap_threshold_days": LONG_GAP_DAYS,
        "top_facilities_by_fact_count": [
            {"facility": fac, "fact_count": n} for fac, n in top_facilities
        ],
    }
    return signals, low_conf


def generate_summary_and_gaps(
    session_store: SessionStore,
    session_id: str,
    anthropic_client: AnthropicClient,
    *,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Write ``output/summary.md`` and ``output/gaps.md``.

    Returns ``{"summary_path", "gaps_path", "fact_count", ...}``.
    """
    facts = _load_verified(session_store.verified_facts_path(session_id))
    out_dir = session_store.output_dir(session_id)
    summary_path = out_dir / "summary.md"
    gaps_path = out_dir / "gaps.md"
    chronology_path = out_dir / "chronology.md"

    if not facts:
        summary_path.write_text(
            "No verified facts were available for this session.\n", encoding="utf-8"
        )
        gaps_path.write_text(
            "No verified facts were available; the entire record needs manual review.\n",
            encoding="utf-8",
        )
        return {"summary_path": str(summary_path), "gaps_path": str(gaps_path), "fact_count": 0}

    # --- Summary: drive from chronology MD ------------------------------
    if chronology_path.exists():
        chronology_md = chronology_path.read_text(encoding="utf-8")
    else:
        # Defensive: if assembly failed but we still want a summary,
        # build a thin fact list as a fallback.
        log.warning(
            "summary: chronology.md missing; falling back to compact fact list"
        )
        chronology_md = "\n\n".join(
            f"{f.visit_date or 'undated'}. {f.facility or 'Unknown facility'}. "
            f"{f.provider_name or 'Unknown provider'}. {f.finding_text}"
            for f in facts[:5000]
        )

    if progress_callback:
        progress_callback(
            f"Summary: composing executive summary from chronology "
            f"({len(chronology_md):,} chars)"
        )
    summary_prompt = build_summary_prompt(chronology_md)
    summary_text = anthropic_client.complete(
        summary_prompt, model=model, max_tokens=2000
    ).strip()
    summary_path.write_text(summary_text + "\n", encoding="utf-8")

    # --- Gaps: deterministic signals + low-conf sample ------------------
    if progress_callback:
        progress_callback(
            f"Summary: computing gap signals across {len(facts):,} verified facts"
        )
    signals, low_conf = _compute_gap_signals(facts)

    if progress_callback:
        progress_callback(
            f"Summary: gaps analysis ({signals['low_confidence_count']} low-conf, "
            f"{len(signals['long_gaps_over_threshold_days'])} long gaps)"
        )
    gaps_prompt = build_gaps_prompt(signals, low_conf)
    gaps_text = anthropic_client.complete(
        gaps_prompt, model=model, max_tokens=1500
    ).strip()
    gaps_path.write_text(gaps_text + "\n", encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "gaps_path": str(gaps_path),
        "fact_count": len(facts),
        "low_confidence_count": signals["low_confidence_count"],
        "long_gap_count": len(signals["long_gaps_over_threshold_days"]),
    }


__all__ = ["generate_summary_and_gaps"]
