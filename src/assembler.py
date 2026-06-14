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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.anthropic_client import AnthropicClient
from src.extractor import _report_progress
from src.prompts.assembly import build_assembly_prompt, build_multi_provider_prompt
from src.schemas import VerifiedFact
from src.session_state import PHASE_ASSEMBLY, SessionStore
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


# Consolidated key: (visit_date, visit_category).
#
# Facility is intentionally NOT part of the key: the same encounter is often
# recorded under slightly different facility names ("Sharp Rees-Stealy" vs
# "Sharp Rees-Stealy DFR" vs "Sharp Rees-Stealy Medical Group") and must not be
# split into duplicate entries. visit_category keeps ER visits separate from
# everything else — office visits, follow-ups, consults, urgent care, etc. all
# collapse into a single entry per date — per the desired output. The actual
# facility name and visit_type label shown in the entry are chosen from the
# group's facts (most common value), not from the key.
ConsolidatedVisitKey = Tuple[str, str]

# Higher rank wins when choosing the single provider to attribute a visit to:
# attending physician (MD/DO) over mid-levels (PA/NP) over nursing (RN) over
# unknown. Ties are broken by how many of the visit's facts mention them.
_PROVIDER_RANK = {
    "md": 5, "do": 5,
    "dpm": 4, "od": 4, "dc": 4, "psyd": 4, "pharmd": 4,
    "np": 4, "fnp": 4, "pa": 4, "pa-c": 4,
    "dpt": 3, "pt": 3,
    "rn": 2,
}


def _visit_category(visit_type: Optional[str]) -> str:
    """Bucket a raw visit_type for grouping. Only ER visits stay distinct."""
    vt = (visit_type or "").strip().lower()
    if "emerg" in vt or vt in {"er", "ed", "er visit", "ed visit"}:
        return "Emergency"
    return "Visit"


def _consolidated_key_for_fact(fact: VerifiedFact) -> ConsolidatedVisitKey:
    # Strip the date so whitespace/format variance ("10/10/2022" vs
    # "10/10/2022 ") doesn't split one date of service into two entries.
    return ((fact.visit_date or "").strip(), _visit_category(fact.visit_type))


def _most_common(values: List[Optional[str]]) -> Optional[str]:
    """Most frequent non-empty value, or None if all are empty."""
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def _provider_rank(credentials: Optional[str], name: Optional[str]) -> int:
    """Rank a provider by credential (see ``_PROVIDER_RANK``); default 1.

    Periods are stripped first so "M.D." / "R.N." match "md" / "rn".
    """
    text = f"{credentials or ''} {name or ''}".lower().replace(".", "")
    best = 1
    for token, rank in _PROVIDER_RANK.items():
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", text):
            best = max(best, rank)
    return best


def _pick_primary_provider(
    facts: List[VerifiedFact],
) -> Tuple[Optional[str], Optional[str]]:
    """Choose the single provider to attribute a consolidated entry to.

    Prefers the highest-credentialed provider (attending MD/DO > PA/NP > RN >
    unknown); ties broken by how many of the visit's facts mention them.
    """
    by_name: Dict[str, Dict] = {}
    for f in facts:
        name = (f.provider_name or "").strip()
        if not name:
            continue
        rec = by_name.setdefault(name, {"count": 0, "cred": f.provider_credentials, "rank": 0})
        rec["count"] += 1
        r = _provider_rank(f.provider_credentials, name)
        if r > rec["rank"]:
            rec["rank"] = r
            rec["cred"] = f.provider_credentials
    if not by_name:
        return None, None
    best = max(by_name, key=lambda n: (by_name[n]["rank"], by_name[n]["count"]))
    return best, by_name[best]["cred"]


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
    state=None,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Produce ``output/chronology.md`` from ``verified_facts.jsonl``.

    Visit groups are narrated concurrently (``ASSEMBLY_WORKERS`` wide) then
    reassembled in sorted order. When ``state`` is provided, sub-phase
    progress is persisted so the UI shows a live counter.

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
        """Narrate one consolidated date-of-service entry. Returns (idx, key, text).

        All facts for the date+category are folded into ONE narrative — nothing
        is dropped — attributed to a single primary provider (attending
        preferred). The facility name and visit_type label are the most common
        values seen across the group's facts. This produces one tight paragraph
        per visit (no per-provider sub-headings / internal blank lines) and
        collapses facility-name variants and RN/MD duplicates of the same visit.

        Resilient: a single group that fails (after the client's own retries)
        is logged and omitted rather than killing the phase.
        """
        group_facts = groups[key]
        try:
            visit_facts, was_capped = _cap_visit_facts(group_facts)
            if was_capped:
                log.warning(
                    "Assembly: %s capped from %d to %d facts to stay under token budget",
                    key,
                    len(group_facts),
                    len(visit_facts),
                )

            # Pick the single attributed provider (attending preferred) and the
            # representative facility / visit-type label from the WHOLE group
            # (not just the capped sample), so consolidation is stable.
            primary_name, primary_cred = _pick_primary_provider(group_facts)
            visit_key = {
                "visit_date": key[0] or None,
                "facility": _most_common([f.facility for f in group_facts]),
                "visit_type": _most_common([f.visit_type for f in group_facts]),
                "provider_name": primary_name,
                "provider_credentials": primary_cred,
            }

            prompt = build_assembly_prompt(
                visit_key, visit_facts, known_credentials=credential_lookup
            )
            text = anthropic_client.complete(
                prompt, model=model, max_tokens=3000
            ).strip()
            # Strip stray "Provider:" prefix the model sometimes adds
            text = re.sub(r"^Provider:\s*", "", text)
            return idx, key, text
        except Exception:  # noqa: BLE001
            log.exception(
                "Assembly failed for date=%s category=%s; entry omitted", key[0], key[1]
            )
            return idx, key, ""

    # Fan visit groups out across a thread pool, then reassemble strictly in
    # sorted order so the chronology still reads chronologically regardless of
    # which call returned first.
    results: Dict[int, Tuple[ConsolidatedVisitKey, str]] = {}
    total_groups = len(ordered_keys)
    completed = 0
    _report_progress(session_store, state, PHASE_ASSEMBLY, 0, total_groups, "starting")
    with ThreadPoolExecutor(max_workers=ASSEMBLY_WORKERS) as pool:
        futures = [
            pool.submit(_assemble_one, idx, key)
            for idx, key in enumerate(ordered_keys, start=1)
        ]
        for fut in as_completed(futures):
            idx, key, text = fut.result()
            completed += 1
            if completed % 10 == 0 or completed == total_groups:
                if progress_callback:
                    progress_callback(
                        f"Assembly: {completed}/{total_groups} visit entries narrated"
                    )
                _report_progress(
                    session_store, state, PHASE_ASSEMBLY, completed, total_groups, "narrating"
                )
            if not text:
                log.warning(
                    "Empty assembly output for date=%s category=%s", key[0], key[1]
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
