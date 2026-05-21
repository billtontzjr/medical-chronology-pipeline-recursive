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
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.anthropic_client import AnthropicClient
from src.prompts.assembly import build_assembly_prompt, build_multi_provider_prompt
from src.schemas import VerifiedFact
from src.session_state import SessionStore


UNKNOWN_PLACEHOLDER = "Unknown"
HEADER_PLACEHOLDER = "<<HEADER_PLACEHOLDER>>"
UNDATED_HEADING = "## Undated Entries"


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
    facts = _load_verified(session_store.verified_facts_path(session_id))

    groups: Dict[ConsolidatedVisitKey, List[VerifiedFact]] = defaultdict(list)
    for fact in facts:
        groups[_consolidated_key_for_fact(fact)].append(fact)

    ordered_keys = sorted(groups.keys(), key=_sort_key)

    # Build a session-wide provider credential lookup for propagation
    credential_lookup = _build_credential_lookup(facts)

    if progress_callback:
        progress_callback(
            f"Assembly: {len(facts)} verified facts in {len(ordered_keys)} visit group(s)"
        )

    entries: List[str] = []
    undated_entries: List[str] = []

    for idx, key in enumerate(ordered_keys, start=1):
        visit_key = _consolidated_key_to_dict(key)
        if progress_callback:
            label = visit_key.get("visit_date") or "(undated)"
            facility = visit_key.get("facility") or "Unknown"
            progress_callback(f"Assembly: {idx}/{len(ordered_keys)} {label} {facility}")

        visit_facts = groups[key]
        provider_groups = _sub_group_by_provider(visit_facts)
        unique_providers = [
            p for p in provider_groups if p != UNKNOWN_PLACEHOLDER
        ]

        if len(unique_providers) <= 1:
            # Single provider or all unknown: use standard single-paragraph prompt
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

        text = anthropic_client.complete(prompt, model=model, max_tokens=max_tokens).strip()
        # Strip stray "Provider:" prefix the model sometimes adds
        import re as _re
        text = _re.sub(r"^Provider:\s*", "", text)
        if not text:
            log.warning("Empty assembly output for visit_key=%s", visit_key)
            continue

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
