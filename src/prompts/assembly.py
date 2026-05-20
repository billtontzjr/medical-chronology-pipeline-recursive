"""Assembly prompt builder.

The assembler hands Claude a group of verified facts that all share the
same visit (date + facility + provider) and asks for a single-paragraph
chronology entry in Dr. Tontz's established format. The prompt forbids
the model from adding any clinical content not present in the verified
facts.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from src.schemas import VerifiedFact


ORTHO_KEYWORDS = (
    "spine",
    "ortho",
    "orthopaedic",
    "orthopedic",
    "pain",
    "chiropractic",
    "PM&R",
    "physical medicine",
    "rehab",
)


def _is_ortho_visit(visit_key: Dict, facts: List[VerifiedFact]) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                visit_key.get("facility") or "",
                visit_key.get("provider_name") or "",
                " ".join(f.visit_type or "" for f in facts),
                " ".join(f.facility or "" for f in facts),
            ],
        )
    ).lower()
    return any(k.lower() in haystack for k in ORTHO_KEYWORDS)


PROMPT_TEMPLATE = """You are composing one chronology entry for a medical-legal report. Every clinical claim in your output MUST be supported by the verified facts JSON array below. You may organize, narrate, and connect; you may NOT invent.

VISIT IDENTITY:
{visit_key}

VERIFIED FACTS (the only source you may draw from):
{facts_json}

OUTPUT FORMAT:
Produce a SINGLE plain-text paragraph. The first sentence must follow this exact pattern:
  MM/DD/YYYY. Facility Name. Provider Name, Credentials. Visit Type.
Then a single paragraph with in-paragraph headings (no bolding) such as: Chief Complaint:, History of Present Illness:, Physical Examination:, Assessment:, Plan:.

HARD RULES:
- Do NOT include any clinical claim not in the verified facts array.
- Do NOT add bullets, numbered lists, or line breaks. One paragraph only.
- Do NOT use em-dashes. Use periods or commas.
- Do NOT include ICD-10 codes, page citations, or all-caps emphasis.
- 5 to 7 sentences maximum.
- If a field (provider credentials, visit type) is missing, omit the placeholder rather than writing "Unknown".
- If multiple facts conflict, note both versions explicitly without trying to reconcile.

PROVIDER NAME RULES:
- Always include credentials when any verified fact in this visit group contains them. Format as "First Last, Credential" exactly once at the start of the entry. Never duplicate the credential.
- If the source has "Last, First" format, flip to "First Last".
- If the source has the credential appearing twice (e.g. "Bobrik MD, Linda, MD"), deduplicate to "Linda Bobrik, MD".
- If no credential appears in any verified fact for the visit, do not append one.
- Recognized credentials: MD, DO, PA, PA-C, NP, RN, PT, DPT, OD, DC, DPM, PsyD, PharmD, FNP. Even if the source writes "M.D." with periods, output it as "MD" without periods.

PROVIDER CREDENTIAL PROPAGATION:
- A KNOWN_PROVIDER_CREDENTIALS lookup is provided below, mapping provider names to credentials known from other visits in this chronology.
- If the verified facts for THIS visit do not include a credential for a provider, but the KNOWN_PROVIDER_CREDENTIALS lookup contains that provider's credential, USE the credential from the lookup.
- Format remains "First Last, Credential" with no internal periods on credentials.

KNOWN_PROVIDER_CREDENTIALS:
{known_credentials_json}

ABBREVIATION RULES:
- Preserve clinical abbreviations exactly as they appear in the source records. Do NOT expand them. These should stay as abbreviations: ED, MVC, MVA, ROM, AROM, BUE, BLE, RUE, RLE, SLR, TFESI, ACDF, CMT, MT, EMS, ESI, NSAID, COPD, BPPV, GERD, MRI, CT, X-ray, EKG, ECG, IV, IM, PO, PRN, BID, TID, QID, qd, qhs, OTC, PT, OT, ST, SLP, DME, ADL, DOI, FNP, PA-C.
- Only standardize abbreviation formatting (consistent capitalization), do not expand to long form.
- Single exception: if the source uses "ER" or "Emergency Room" as the visit type label, standardize the visit type label to "ED". If the source uses "ER" or "Emergency Room" in the body of an entry, also standardize to "ED". Do NOT spell out "Emergency Department" in either position.

VISIT TYPE RULES:
- Use the visit type as named in the source records, with title case. Match what the source document calls the visit.
- Examples of acceptable visit type labels: Initial Report, Progress Note, Established Patient Visit, New Patient Visit, ED Provider Note, Discharge Summary, Operative Report, Chiropractic Initial Report, Chiropractic Re-Evaluation Note, Chiropractic Final Report, Physical Therapy Initial Evaluation, Physical Therapy Progress Note, Physical Therapy Discharge Summary, Follow-up, Telehealth, Office Visit (only when source does not specify more specific type), Imaging, Procedure Note.
- For imaging visits, include the specific study in the visit type label when known from verified facts (e.g., "MRI Cervical Spine without Contrast", "X-ray of Cervical Spine 5 Views", "MRI Brain without Contrast").
- Abbreviations within visit type labels should preserve fully-capitalized abbreviations exactly as commonly written (BP not Bp, ED not Ed, MRI not Mri, CT not Ct, EKG not Ekg, IV not Iv). Apply title case only to non-abbreviation words. Examples: "BP Check Telehealth" (not "Bp Check Telehealth"), "CT Chest" (not "Ct Chest"), "IV Push Procedure" (not "Iv Push Procedure").

DATE FORMATTING IN ENTRY BODY:
- Any specific date (month + day + year) appearing in the entry body must be rewritten as MM/DD/YYYY with four-digit year and zero-padded month and day. Examples: "March 26, 2025" -> "03/26/2025", "03/06/25" -> "03/06/2025".
- Do NOT change year-only references (e.g. "in 2015") or month-year references without a day (e.g. "December 2022").
- Keep any time portion as-is (e.g. "at 10:26 am" stays).
{ortho_addendum}

OUTPUT (entry text only, no preamble, no commentary):
"""


ORTHO_ADDENDUM = (
    "- This is an orthopedic, spine, or pain management visit. Prioritize objective "
    "exam findings, imaging results, assessment, and plan. Minimize subjective history."
)


def build_assembly_prompt(
    visit_key: Dict,
    facts: List[VerifiedFact],
    *,
    known_credentials: Optional[Dict[str, str]] = None,
) -> str:
    facts_payload = [f.model_dump(mode="json") for f in facts]
    creds = known_credentials or {}
    return PROMPT_TEMPLATE.format(
        visit_key=json.dumps(visit_key, indent=2),
        facts_json=json.dumps(facts_payload, indent=2),
        ortho_addendum=ORTHO_ADDENDUM if _is_ortho_visit(visit_key, facts) else "",
        known_credentials_json=json.dumps(creds, indent=2) if creds else "(none)",
    )


__all__ = ["build_assembly_prompt"]
