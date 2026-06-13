"""Phase 5 assembler: group verified facts and ask Claude to narrate.

The assembler reads ``verified_facts.jsonl``, groups facts by
``(visit_date, facility, visit_type)``, asks Claude for one entry per
group, and writes the result to ``output/chronology.md``. When multiple
providers contribute to the same visit, the entry includes provider
sub-sections instead of separate chronology entries.

The assembler never sees the raw OCR text, which is the structural
reason the assembled chronology cannot contain clinical content that
did not come from a verified fact.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.anthropic_client import AnthropicClient
from src.prompts.assembly import build_assembly_prompt, build_multi_provider_prompt
from src.schemas import VerifiedFact
from src.session_state import SessionStore
from src.token_budget import SAFE_PROMPT_CHARS

# Each visit group's narration is an independent Claude call; only the final
# file ordering matters, which we preserve by mapping results back by index.
# Override with the ASSEMBLY_WORKERS env var.
ASSEMBLY_WORKERS = max(1, int(os.environ.get("ASSEMBLY_WORKERS", "8")))


UNKNOWN_PLACEHOLDER = "Unknown"
HEADER_PLACEHOLDER = "<<HEADER_PLACEHOLDER>>"
UNDATED_HEADING = "## Undated Entries"

# Hard cap on facts handed to Claude for a single visit group. Chunk
# overlap in extraction can produce duplicate facts; we dedupe first,
# but if the remaining count is still pathological (e.g. a giant
# undated bucket on a massive record set) we cap to this many facts
# to keep the prompt within budget.
MAX_FACTS_PER_VISIT_GROUP = 250

# Approximate chars per serialized VerifiedFact in JSON. Used to
# preflight the prompt size; real char count is measured by build_*.
APPROX_CHARS_PER_FACT = 600


# Consolidated key: (visit_date, facility, visit_type)
ConsolidatedVisitKey = Tuple[str, str, str]


def _consolidated_key_for_fact(fact: VerifiedFact) -> ConsolidatedVisitKey:
    return (
        fact.visit_date or "",
        fact.facility or UNKNOWN_PLACEHOLDER,
        fact.visit_type or UNKNOWN_PLACEHOLDER,
    )


def _consolidated_key_to_dict(key: ConsolidatedVisitKey) -> Dict:
    return {
        "visit_date": key[0] or None,
        "facility": key[1] if key[1] != UNKNOWN_PLACEHOLDER else None,
        "visit_type": key[2] if key[2] != UNKNOWN_PLACEHOLDER else None,
    }


def _sort_key(key: ConsolidatedVisitKey) -> Tuple[int, str]:
    """Sort dated visits ascending; undated visits sort last."""
    date_str = key[0]
    if not date_str:
        return (1, "")
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return (0, dt.isoformat())
    except ValueError:
        return (0, date_str)


def _sub_group_by_provider(
    facts: List[VerifiedFact],
) -> Dict[str, List[VerifiedFact]]:
    """Sub-group facts within a consolidated visit by provider_name."""
    groups: Dict[str, List[VerifiedFact]] = defaultdict(list)
    for fact in facts:
        provider = fact.provider_name or UNKNOWN_PLACEHOLDER
        groups[provider].append(fact)
    return dict(groups)


def _load_verified(jsonl_path: Path) -> List[VerifiedFact]:
    facts: List[VerifiedFact] = []
    if not jsonl_path.exists():
        return facts
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        facts.append(VerifiedFact.model_validate_json(line))
    return facts


def _dedupe_facts(facts: List[VerifiedFact]) -> List[VerifiedFact]:
    """Drop facts that duplicate another fact on the same visit.

    Chunks overlap by 800 chars, so facts sitting near a chunk boundary
    can appear in two adjacent chunks. We treat two facts as duplicates
    when they share visit_date, facility (case-insensitive), provider
    name (case-insensitive), finding_text (case-insensitive), and
    verbatim_quote (case-insensitive). Order is preserved.
    """
    seen: set = set()
    out: List[VerifiedFact] = []
    for f in facts:
        key = (
            f.visit_date or "",
            (f.facility or "").lower(),
            (f.provider_name or "").lower(),
            (f.finding_text or "").lower(),
            (f.verbatim_quote or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _cap_visit_facts(
    facts: List[VerifiedFact],
    *,
    max_facts: int = MAX_FACTS_PER_VISIT_GROUP,
    max_chars: int = SAFE_PROMPT_CHARS // 2,
) -> Tuple[List[VerifiedFact], bool]:
    """Cap a single visit group's facts so the assembly prompt cannot
    exceed the token budget.

    Strategy: keep a diverse sample by walking facts in order and
    skipping any whose (provider, category, finding_text) signature has
    already been seen. Stop when either ``max_facts`` is reached or the
    cumulative char-cost crosses ``max_chars``.
    """
    if len(facts) <= max_facts:
        approx_total = sum(len(f.verbatim_quote or "") + APPROX_CHARS_PER_FACT for f in facts)
        if approx_total <= max_chars:
            return facts, False

    seen_signature: set = set()
    kept: List[VerifiedFact] = []
    total_chars = 0
    for f in facts:
        sig = (
            (f.provider_name or "").lower(),
            f.fact_category,
            (f.finding_text or "")[:120].lower(),
        )
        if sig in seen_signature:
            continue
        seen_signature.add(sig)
        kept.append(f)
        total_chars += len(f.verbatim_quote or "") + APPROX_CHARS_PER_FACT
        if len(kept) >= max_facts or total_chars >= max_chars:
            break
    return kept, True


_CREDENTIAL_TOKENS = [
    "pa-c", "dpt", "pharmd", "psyd",
    "md", "do", "pa", "np", "rn", "pt", "od", "dc", "dpm", "fnp",
]


def _build_credential_lookup(facts: List[VerifiedFact]) -> Dict[str, str]:
    """Map normalized provider names to their known credentials.

    Scans ALL verified facts to build a session-wide lookup so that
    credentials can be propagated to visits where they are absent.
    """
    import re

    lookup: Dict[str, str] = {}
    for fact in facts:
        name = fact.provider_name
        cred = fact.provider_credentials
        if not name or not cred:
            continue
        # Normalize: lowercase, strip
        normalized = name.strip().lower()
        # Remove trailing credential tokens that may be embedded in the name
        for token in _CREDENTIAL_TOKENS:
            normalized = re.sub(
                rf"[,\s]+{re.escape(token)}\.?\s*$", "", normalized,
                flags=re.IGNORECASE,
            )
        # Handle "Last, First" -> "first last" for matching
        normalized = normalized.strip().rstrip(",").strip()
        # Remove middle initials for broader matching ("christopher t behr" -> "christopher behr")
        parts = normalized.split()
        core_parts = [p for p in parts if len(p) > 1 or p == parts[-1]]
        normalized_core = " ".join(core_parts)
        cred_clean = cred.strip().replace(".", "")
        if normalized_core and cred_clean:
            lookup[normalized_core] = cred_clean
    return lookup


def run_assembly(
    session_store: SessionStore,
    session_id: str,
    anthropic_client: AnthropicClient,
    *,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Produce ``output/chronology.md`` from ``verified_facts.jsonl``.

    Returns a summary dict with the entry count and the path written.
    The first line of the file is a header placeholder which M11
    replaces with the patient header once it is known.
    """
    log = logging.getLogger(__name__)
    raw_facts = _load_verified(session_store.verified_facts_path(session_id))

    # Dedupe across the whole session before grouping. Chunk overlap can
    # produce 2x duplicates per fact; on big record sets this is the
    # difference between a sane prompt and an over-budget one.
    facts = _dedupe_facts(raw_facts)
    duplicates_dropped = len(raw_facts) - len(facts)
    if duplicates_dropped:
        log.info("Assembly: dropped %d duplicate facts after dedup", duplicates_dropped)

    groups: Dict[ConsolidatedVisitKey, List[VerifiedFact]] = defaultdict(list)
    for fact in facts:
        groups[_consolidated_key_for_fact(fact)].append(fact)

    ordered_keys = sorted(groups.keys(), key=_sort_key)

    # Build a session-wide provider credential lookup for propagation
    credential_lookup = _build_credential_lookup(facts)

    if progress_callback:
        progress_callback(
            f"Assembly: {len(facts)} verified facts "
            f"(deduped from {len(raw_facts)}) in {len(ordered_keys)} visit group(s)"
        )

    def _assemble_one(
        idx: int, key: ConsolidatedVisitKey
    ) -> Tuple[int, ConsolidatedVisitKey, str]:
        """Narrate one visit group via Claude. Returns (idx, key, text).

        Resilient: a single group that fails (after the client's own
        retries) is logged and omitted rather than killing the phase.
        """
        visit_key = _consolidated_key_to_dict(key)
        try:
            visit_facts, was_capped = _cap_visit_facts(groups[key])
            if was_capped:
                log.warning(
                    "Assembly: visit %s capped from %d to %d facts to stay under token budget",
                    key,
                    len(groups[key]),
                    len(visit_facts),
                )
            provider_groups = _sub_group_by_provider(visit_facts)
            unique_providers = [
                p for p in provider_groups if p != UNKNOWN_PLACEHOLDER
            ]

            if len(unique_providers) <= 1:
                # Single provider or all unknown: standard single-paragraph prompt
                sample = visit_facts[0]
                single_key = dict(visit_key)
                single_key["provider_name"] = sample.provider_name
                single_key["provider_credentials"] = sample.provider_credentials
                prompt = build_assembly_prompt(
                    single_key, visit_facts, known_credentials=credential_lookup
                )
                max_tokens = 2000
            else:
                # Multiple providers: use multi-provider prompt
                prompt = build_multi_provider_prompt(
                    visit_key, provider_groups, known_credentials=credential_lookup
                )
                max_tokens = 4000  # larger output for multi-provider entries

            text = anthropic_client.complete(
                prompt, model=model, max_tokens=max_tokens
            ).strip()
            # Strip stray "Provider:" prefix the model sometimes adds
            text = re.sub(r"^Provider:\s*", "", text)
            return idx, key, text
        except Exception:  # noqa: BLE001
            log.exception(
                "Assembly failed for visit_key=%s; entry omitted", visit_key
            )
            return idx, key, ""

    # Fan visit groups out across a thread pool, then reassemble strictly in
    # sorted order so the chronology still reads chronologically regardless of
    # which call returned first.
    results: Dict[int, Tuple[ConsolidatedVisitKey, str]] = {}
    total_groups = len(ordered_keys)
    completed = 0
    with ThreadPoolExecutor(max_workers=ASSEMBLY_WORKERS) as pool:
        futures = [
            pool.submit(_assemble_one, idx, key)
            for idx, key in enumerate(ordered_keys, start=1)
        ]
        for fut in as_completed(futures):
            idx, key, text = fut.result()
            completed += 1
            if progress_callback and (completed % 10 == 0 or completed == total_groups):
                progress_callback(
                    f"Assembly: {completed}/{total_groups} visit entries narrated"
                )
            if not text:
                log.warning(
                    "Empty assembly output for visit_key=%s",
                    _consolidated_key_to_dict(key),
                )
                continue
            results[idx] = (key, text)

    entries: List[str] = []
    undated_entries: List[str] = []
    for idx in range(1, total_groups + 1):
        if idx not in results:
            continue
        key, text = results[idx]
        if key[0]:
            entries.append(text)
        else:
            undated_entries.append(text)

    out_path = session_store.output_dir(session_id) / "chronology.md"
    parts: List[str] = [HEADER_PLACEHOLDER, ""]
    parts.extend(entries)
    if undated_entries:
        parts.append("")
        parts.append(UNDATED_HEADING)
        parts.append("")
        parts.extend(undated_entries)

    out_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    if progress_callback:
        progress_callback(f"Assembly complete: {out_path}")
    return {
        "entries_written": len(entries) + len(undated_entries),
        "dated_entries": len(entries),
        "undated_entries": len(undated_entries),
        "output_path": str(out_path),
    }


__all__ = ["run_assembly", "HEADER_PLACEHOLDER", "UNDATED_HEADING"]
