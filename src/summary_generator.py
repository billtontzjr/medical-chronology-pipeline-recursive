"""Executive summary and gaps generation (Phase 7b).

Both outputs are produced from the verified facts JSONL, never from the
raw OCR text. Since the verified facts have each survived the
deterministic substring verifier, any clinical content in the summary
or gaps prose is guaranteed to trace back to a literal source quote.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional

from src.anthropic_client import AnthropicClient
from src.prompts.summary import build_gaps_prompt, build_summary_prompt
from src.schemas import VerifiedFact
from src.session_state import SessionStore


log = logging.getLogger(__name__)


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


def generate_summary_and_gaps(
    session_store: SessionStore,
    session_id: str,
    anthropic_client: AnthropicClient,
    *,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Write ``output/summary.md`` and ``output/gaps.md``.

    Returns ``{"summary_path": str, "gaps_path": str, "fact_count": int}``.
    If there are zero verified facts the files contain a one-line note.
    """
    facts = _load_verified(session_store.verified_facts_path(session_id))
    out_dir = session_store.output_dir(session_id)
    summary_path = out_dir / "summary.md"
    gaps_path = out_dir / "gaps.md"

    if not facts:
        summary_path.write_text(
            "No verified facts were available for this session.\n", encoding="utf-8"
        )
        gaps_path.write_text(
            "No verified facts were available; the entire record needs manual review.\n",
            encoding="utf-8",
        )
        return {"summary_path": str(summary_path), "gaps_path": str(gaps_path), "fact_count": 0}

    if progress_callback:
        progress_callback(f"Summary: composing executive summary from {len(facts)} facts")
    summary_text = anthropic_client.complete(
        build_summary_prompt(facts), model=model, max_tokens=2000
    ).strip()
    summary_path.write_text(summary_text + "\n", encoding="utf-8")

    if progress_callback:
        progress_callback("Summary: composing gaps analysis")
    gaps_text = anthropic_client.complete(
        build_gaps_prompt(facts), model=model, max_tokens=1500
    ).strip()
    gaps_path.write_text(gaps_text + "\n", encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "gaps_path": str(gaps_path),
        "fact_count": len(facts),
    }


__all__ = ["generate_summary_and_gaps"]
