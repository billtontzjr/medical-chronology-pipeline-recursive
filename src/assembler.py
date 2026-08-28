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


# Consolidated key: (visit_date, visit_category, facility_cluster, subkey).
#
# The grouping implements a per-date-of-service HIERARCHY:
#   - The attending/primary physician's note for a care setting is the
#     day's primary entry (subkey "").
#   - Each distinct specialty CONSULTANT gets their own entry
#     (subkey "consult:<provider>").
#   - Each IMAGING study gets its own entry per modality, attributed to
#     the interpreting radiologist (facility cluster ignored so the
#     radiology department's separate name doesn't duplicate the study).
#   - Procedures, Emergency visits, and Therapy stay their own categories.
#   - Distinct care settings on one date (hospital vs SNF) are separate
#     facility clusters and therefore separate entries.
#   - ANCILLARY documentation (nursing, MDS, activities, social work,
#     case management, pharmacy, telephone orders, orthotics) NEVER forms
#     its own entry when the same date+setting has an entry-worthy note —
#     it is folded into the primary entry. A date+setting with ONLY
#     ancillary notes gets one consolidated entry (e.g. SNF daily care).
#
# facility_cluster is a session-wide cluster label: facility names that
# share a distinctive token ("Geisinger" / "GMC-Geisinger Medical Center"
# / "Geisinger Orthopaedics Woodbine") collapse into one cluster, while
# genuinely different institutions stay separate. The facility name shown
# in the entry is the most common raw value in the group, not the key.
ConsolidatedVisitKey = Tuple[str, str, str, str]

# Generic tokens dropped when tokenizing a facility name, so name variants
# of the same institution compare equal AND so generic care/department
# words cannot bridge two different institutions into one cluster (e.g.
# "Guardian Long Term Care Pharmacy" must not merge with "Geisinger
# Pharmacy Outpatient" via the word "pharmacy"). Only identity-bearing
# tokens (proper names, places) survive.
_FACILITY_STOPWORDS = {
    # corporate/generic
    "medical", "group", "center", "centre", "clinic", "clinics", "hospital",
    "health", "healthcare", "system", "associates", "institute", "office",
    "offices", "inc", "llc", "llp", "pc", "of", "the", "and", "at", "dba",
    "dfr", "corp", "corporation", "ltd", "community", "campus", "services",
    "service", "management", "living", "senior", "wellness", "education",
    "long", "term", "total", "practice", "primary", "family", "internal",
    "medicine",
    # care settings / departments — these describe WHAT a place does, not
    # WHICH institution it is, so they must never link two names
    "care", "pharmacy", "nursing", "skilled", "facility", "facilities",
    "rehabilitation", "rehab", "outpatient", "inpatient", "consult",
    "consults", "emergency", "department", "dept", "radiology", "imaging",
    "laboratory", "therapy", "urgent",
    # medical specialties
    "orthopaedics", "orthopedics", "orthopaedic", "orthopedic", "urology",
    "cardiology", "neurology", "oncology", "surgery", "surgical",
    "psychiatry", "podiatry", "dermatology", "pediatrics", "ophthalmology",
    "gastroenterology", "pulmonology", "nephrology", "obstetrics",
    "gynecology",
}

# Billing/administrative language. A non-clinical fact matching this pattern
# is treated as billing-derived: dropped when the same date has clinical
# facts, kept (minimally) when a date exists only in billing records.
_BILLING_RE = re.compile(
    r"\b(cpt|billing|billed|invoice|itemized|superbill|ledger|"
    r"balance due|amount due|statement of charges|account statement|"
    r"payment received|charge amount)\b",
    re.IGNORECASE,
)

# Fact categories that constitute clinical documentation (vs. billing or
# administrative content, which lands in "other"/"patient_quote").
_CLINICAL_CATEGORIES = {
    "chief_complaint", "history", "physical_exam", "assessment", "diagnosis",
    "plan", "medication", "procedure_performed", "imaging_finding",
    "lab_result", "referral", "work_status",
}

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


_IMAGING_VT_RE = re.compile(
    r"\b(imaging|mri|ct|x[- ]?ray|xr|ultrasound|radiology|radiologic|"
    r"fluoroscopy|myelogram|emg|ncv|nerve conduction)\b",
    re.IGNORECASE,
)
_THERAPY_VT_RE = re.compile(
    r"\b(therapy|therapeutic|chiropractic|chiro|rehab|rehabilitation|"
    r"acupuncture|pt|ot|slp)\b",
    re.IGNORECASE,
)
_PROCEDURE_VT_RE = re.compile(
    r"\b(operative|surgery|surgical|procedure|injection|epidural|"
    r"esi|tfesi|rfa|ablation|discogram)\b",
    re.IGNORECASE,
)


def _visit_category(visit_type: Optional[str]) -> str:
    """Bucket a raw visit_type for grouping.

    Emergency, Imaging, Therapy, and Procedure encounters each stay
    distinct from office/clinic visits so that multiple different visits
    on the same date of service each get their own chronology entry.
    """
    vt = (visit_type or "").strip().lower()
    if "emerg" in vt or vt in {"er", "ed", "er visit", "ed visit"}:
        return "Emergency"
    if _IMAGING_VT_RE.search(vt):
        return "Imaging"
    if _THERAPY_VT_RE.search(vt):
        return "Therapy"
    # A consultation is a visit even when its title names a procedural
    # specialty ("Orthopaedic Surgery Consultation" is a consult, not an
    # operation) — check before the procedure keywords.
    if _CONSULT_RE.search(vt):
        return "Visit"
    if _PROCEDURE_VT_RE.search(vt):
        return "Procedure"
    return "Visit"


def _facility_tokens(facility: Optional[str]) -> frozenset:
    """Distinctive tokens of a facility name, for clustering.

    Lowercases, strips punctuation, drops generic/department words and
    short tokens. What remains are the identity-bearing tokens
    ("geisinger", "laurel") that name variants of one institution share.
    """
    if not facility:
        return frozenset()
    text = re.sub(r"[^a-z0-9\s]", " ", facility.lower())
    return frozenset(
        t for t in text.split() if len(t) >= 4 and t not in _FACILITY_STOPWORDS
    )


def _cluster_facilities(facts: List[VerifiedFact]) -> Dict[str, str]:
    """Cluster raw facility names session-wide by shared distinctive tokens.

    Union-find: any two facility names sharing a distinctive token merge
    into one cluster ("Geisinger" / "GMC-Geisinger Medical Center" /
    "Geisinger Radiology"). Returns a map from raw facility name to a
    stable cluster label (the most common raw name in the cluster,
    lowercased). Names with no distinctive tokens map to themselves.
    """
    counts: Counter = Counter(f.facility for f in facts if f.facility)
    names = list(counts.keys())
    tokens = {n: _facility_tokens(n) for n in names}

    parent: Dict[str, str] = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    token_owners: Dict[str, str] = {}
    for n in names:
        for t in tokens[n]:
            if t in token_owners:
                union(token_owners[t], n)
            else:
                token_owners[t] = n

    # Acronym bridging: a short all-letters name ("MLNRC") merges with a
    # multi-word name whose initials spell it ("Mountain Laurel Nursing
    # and Rehabilitation Center").
    def _initials(name: str) -> str:
        words = [
            w for w in re.sub(r"[^a-z0-9\s]", " ", name.lower()).split()
            if w not in {"and", "of", "the", "at"}
        ]
        return "".join(w[0] for w in words) if len(words) >= 2 else ""

    compact = {n: re.sub(r"[^a-z0-9]", "", n.lower()) for n in names}
    initials_map: Dict[str, str] = {}
    for n in names:
        ini = _initials(n)
        if len(ini) >= 3:
            initials_map.setdefault(ini, n)
    for n in names:
        c = compact[n]
        if 3 <= len(c) <= 6 and c in initials_map and initials_map[c] != n:
            union(initials_map[c], n)

    # Label each cluster with its most common raw name (lowercased) so the
    # key is stable regardless of which variant a fact carries.
    cluster_members: Dict[str, List[str]] = defaultdict(list)
    for n in names:
        cluster_members[find(n)].append(n)
    label_of: Dict[str, str] = {}
    for members in cluster_members.values():
        label = max(members, key=lambda m: counts[m]).lower().strip()
        for m in members:
            label_of[m] = label
    return label_of


def _backfill_facilities(facts: List[VerifiedFact]) -> None:
    """Fill missing facility names from the fact's own source document.

    A facility is typically named once at the top of a record, so facts
    extracted from later chunks of the same file carry facility=None.
    Backfilling from the same source file (never from other documents)
    keeps every fact attributed to the facility its own record names,
    which prevents entries from showing a different record's facility.
    Mutates the facts in place.
    """
    by_file: Dict[str, List[VerifiedFact]] = defaultdict(list)
    for f in facts:
        by_file[f.source_file].append(f)
    for group in by_file.values():
        common = _most_common([f.facility for f in group])
        if not common:
            continue
        for f in group:
            if not f.facility:
                f.facility = common


def _is_billing_fact(fact: VerifiedFact) -> bool:
    """True when a fact appears billing/administrative rather than clinical."""
    if fact.fact_category in _CLINICAL_CATEGORIES:
        return False
    text = " ".join(
        filter(None, [fact.visit_type, fact.finding_text, fact.verbatim_quote])
    )
    return bool(_BILLING_RE.search(text))


def _drop_billing_when_clinical(facts: List[VerifiedFact]) -> Tuple[List[VerifiedFact], int]:
    """Drop billing-derived facts on dates that also have clinical facts.

    Chronology entries must be built from the clinical record, never from
    the billing record, when both exist for a date of service. Dates that
    appear ONLY in billing records keep their billing facts so the
    chronology can note the billed service instead of silently omitting
    the date.
    """
    clinical_dates = {
        (f.visit_date or "").strip()
        for f in facts
        if not _is_billing_fact(f)
    }
    kept: List[VerifiedFact] = []
    dropped = 0
    for f in facts:
        if _is_billing_fact(f) and (f.visit_date or "").strip() in clinical_dates:
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


# Ancillary documentation: folded into the day's primary entry, never a
# standalone entry when an entry-worthy note exists for the same
# date+setting. Detected by visit type wording or by provider credential.
_ANCILLARY_VT_RE = re.compile(
    r"\b(nursing note|nursing assessment|nursing flowsheet|nurses? notes?|"
    r"mds|activities|recreation|social work|case management|"
    r"care management|medication reconciliation|med rec|pharmacy|pharmacist|"
    r"pharmacokinetic|telephone order|verbal order|orthotic|orthotics|brace|"
    r"dietary|nutrition|dietitian|flowsheet)\b",
    re.IGNORECASE,
)
_ANCILLARY_CRED_RE = re.compile(
    r"(?<![a-z])(rn|lpn|lvn|cna|rph|pharmd|pharm tech|cpo|msw|lsw|lcsw|"
    r"rd|ldn|cm|activities director)(?![a-z])",
    re.IGNORECASE,
)
_PHYSICIAN_CRED_RE = re.compile(
    r"(?<![a-z])(md|do|pa|pa-c|np|fnp|dpm|dc|od|psyd)(?![a-z])",
    re.IGNORECASE,
)

_CONSULT_RE = re.compile(r"\bconsult", re.IGNORECASE)

_MODALITY_PATTERNS = [
    (re.compile(r"\bmri\b|magnetic resonance", re.I), "MRI"),
    (re.compile(r"x[- ]?ray|radiograph|\bxr\b", re.I), "X-ray"),
    (re.compile(r"\bct\b|computed tomograph|cat scan", re.I), "CT"),
    (re.compile(r"ultrasound|sonogra|\bus\b", re.I), "US"),
    (re.compile(r"\bemg\b|\bncv\b|nerve conduction", re.I), "EMG/NCV"),
    (re.compile(r"myelogram|fluoroscop", re.I), "Fluoro"),
]


def _imaging_modality(visit_type: Optional[str]) -> str:
    vt = (visit_type or "").strip()
    for pattern, label in _MODALITY_PATTERNS:
        if pattern.search(vt):
            return label
    return "Other"


def _is_ancillary_fact(fact: VerifiedFact) -> bool:
    """True for nursing/MDS/activities/social-work/pharmacy/orthotics/
    telephone-order documentation that folds into the day's primary entry.

    A physician-credentialed note is never ancillary except telephone or
    verbal orders, which are day-to-day care traffic rather than a visit.
    """
    vt = fact.visit_type or ""
    cred_text = f"{fact.provider_credentials or ''} {fact.provider_name or ''}".replace(".", "")
    if re.search(r"telephone order|verbal order", vt, re.IGNORECASE):
        return True
    # Admissions and discharges are always entry-worthy regardless of who
    # authored them (e.g. "Skilled Nursing Facility Admission").
    if re.search(r"admission|discharge", vt, re.IGNORECASE):
        return False
    if _PHYSICIAN_CRED_RE.search(cred_text):
        return False
    return bool(_ANCILLARY_VT_RE.search(vt) or _ANCILLARY_CRED_RE.search(cred_text))


def _build_visit_groups(
    facts: List[VerifiedFact],
) -> Dict[ConsolidatedVisitKey, List[VerifiedFact]]:
    """Group verified facts into one group per intended chronology entry,
    implementing the per-date hierarchy documented on ConsolidatedVisitKey.
    """
    cluster_of = _cluster_facilities(facts)

    groups: Dict[ConsolidatedVisitKey, List[VerifiedFact]] = defaultdict(list)
    ancillary: List[Tuple[str, str, VerifiedFact]] = []

    for f in facts:
        date = (f.visit_date or "").strip()
        category = _visit_category(f.visit_type)
        cluster = cluster_of.get(f.facility or "", "")
        if _is_ancillary_fact(f):
            ancillary.append((date, cluster, f))
            continue
        if category == "Imaging":
            # One entry per study (approximated by modality) per date; the
            # radiology department's separate facility label must not
            # duplicate the study, so the cluster is not part of the key.
            key = (date, "Imaging", "", _imaging_modality(f.visit_type))
        elif category in ("Visit", "Emergency") and _CONSULT_RE.search(f.visit_type or ""):
            # Each consulting provider is their own entry on the date. The
            # cluster is not part of the key: one consultant sees the
            # patient once that day even when the note carries different
            # facility-name variants.
            key = (date, category, "", f"consult:{(f.provider_name or '').strip().lower()}")
        else:
            key = (date, category, cluster, "")
        groups[key].append(f)

    # Fold ancillary facts into the primary entry for their date+setting.
    for date, cluster, f in ancillary:
        target: Optional[ConsolidatedVisitKey] = None
        for cand in (
            (date, "Visit", cluster, ""),
            (date, "Emergency", cluster, ""),
            (date, "Procedure", cluster, ""),
            (date, "Therapy", cluster, ""),
        ):
            if cand in groups:
                target = cand
                break
        if target is None:
            # Any entry-worthy non-imaging group at the same date+setting
            candidates = [
                k for k in groups
                if k[0] == date and k[2] == cluster and k[1] != "Imaging"
            ]
            if candidates:
                target = max(candidates, key=lambda k: len(groups[k]))
        if target is None:
            # Nothing entry-worthy that day at this setting (e.g. SNF
            # daily care): one consolidated ancillary entry.
            target = (date, "Visit", cluster, "ancillary")
        groups[target].append(f)

    # Fold unknown-facility groups into the single named group for the
    # same date+category+subkey when unambiguous.
    merged: Dict[ConsolidatedVisitKey, List[VerifiedFact]] = {}
    for key, group_facts in groups.items():
        date, category, cluster, subkey = key
        if cluster == "" and category != "Imaging":
            named = [
                k for k in groups
                if k[0] == date and k[1] == category and k[3] == subkey and k[2] != ""
            ]
            if len(named) == 1:
                merged.setdefault(named[0], []).extend(group_facts)
                continue
        merged.setdefault(key, []).extend(group_facts)
    return merged


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


# Within one date: ED first, then the attending/primary visit, procedures,
# imaging, therapy — mirroring how a reviewer reads a hospital day.
_CATEGORY_ORDER = {"Emergency": 0, "Visit": 1, "Procedure": 2, "Imaging": 3, "Therapy": 4}


def _sort_key(key: ConsolidatedVisitKey) -> Tuple[int, str, int, int, str, str]:
    """Sort dated visits ascending; undated visits sort last. Same-date
    entries order by category (ED, visit, procedure, imaging, therapy),
    primary entries before consults before ancillary-only, then cluster."""
    date_str, category, cluster, subkey = key
    cat_rank = _CATEGORY_ORDER.get(category, 9)
    sub_rank = 0 if subkey == "" else (2 if subkey == "ancillary" else 1)
    if not date_str:
        return (1, "", cat_rank, sub_rank, cluster, subkey)
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return (0, dt.isoformat(), cat_rank, sub_rank, cluster, subkey)
    except ValueError:
        return (0, date_str, cat_rank, sub_rank, cluster, subkey)


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

    # Fill facility gaps from each fact's own source document, so records
    # that name the facility only once attribute every entry correctly.
    _backfill_facilities(facts)

    # Clinical records beat billing records: drop billing-derived facts on
    # any date of service that also has clinical documentation.
    facts, billing_dropped = _drop_billing_when_clinical(facts)
    if billing_dropped:
        log.info(
            "Assembly: dropped %d billing-derived facts on dates with clinical records",
            billing_dropped,
        )

    groups = _build_visit_groups(facts)

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

            # Pick the single attributed provider and the representative
            # facility / visit-type label from the WHOLE group (not just the
            # capped sample), so consolidation is stable. Imaging entries are
            # attributed to the interpreting radiologist (the provider whose
            # facts carry the imaging findings); consult entries to the
            # consulting provider the group was keyed on.
            _, category, _, subkey = key
            provider_pool = group_facts
            if category == "Imaging":
                readers = [
                    f for f in group_facts if f.fact_category == "imaging_finding"
                ]
                if readers:
                    provider_pool = readers
            elif subkey.startswith("consult:"):
                consult_name = subkey[len("consult:"):]
                matching = [
                    f for f in group_facts
                    if (f.provider_name or "").strip().lower() == consult_name
                ]
                if matching:
                    provider_pool = matching
            primary_name, primary_cred = _pick_primary_provider(provider_pool)
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
            # Generous budget: on reasoning models, thinking tokens count
            # against max_tokens, so leave headroom above the expected output.
            text = anthropic_client.complete(
                prompt, model=model, max_tokens=8000
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
