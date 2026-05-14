"""Assembly prompt builder.

The assembler hands Claude a group of verified facts that all share the
same visit (date + facility + provider) and asks for a single-paragraph
chronology entry in Dr. Tontz's established format. The prompt forbids
the model from adding any clinical content not present in the verified
facts.
"""

from __future__ import annotations

import json
from typing import Dict, List

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
{ortho_addendum}

OUTPUT (entry text only, no preamble, no commentary):
"""


ORTHO_ADDENDUM = (
    "- This is an orthopedic, spine, or pain management visit. Prioritize objective "
    "exam findings, imaging results, assessment, and plan. Minimize subjective history."
)


def build_assembly_prompt(visit_key: Dict, facts: List[VerifiedFact]) -> str:
    facts_payload = [f.model_dump(mode="json") for f in facts]
    return PROMPT_TEMPLATE.format(
        visit_key=json.dumps(visit_key, indent=2),
        facts_json=json.dumps(facts_payload, indent=2),
        ortho_addendum=ORTHO_ADDENDUM if _is_ortho_visit(visit_key, facts) else "",
    )


__all__ = ["build_assembly_prompt"]
