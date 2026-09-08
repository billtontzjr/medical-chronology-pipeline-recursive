"""Preserve billing provenance without inferring that clinical notes are absent."""
import re

LABEL = '(billing record only)'
LEGACY_LABEL = re.compile(r'\(billing record only\)|\bChiropractic Therapy Billing\b|\bBilling[- ]only\b', re.I)


def normalize_billing(text, pending_review=False):
    # Remove only negative clinical-note availability claims from billing output.
    # Billing summaries cannot establish the absence of notes in other sections.
    text = re.sub(r'\bno (?:corresponding |associated )?clinical (?:note|notes|documentation)\b[^.;\n]*(?:[.;]|$)',
                  'Clinical-note availability is not established by this billing entry. ', text, flags=re.I)
    if LABEL not in text.lower():
        text = text.rstrip() + ' ' + LABEL
    if pending_review:
        text += ' Source sections remain pending manual review; reconcile against the original records.'
    return ' '.join(text.split())


def reconcile_billing(body, pending_review=False):
    return '\n\n'.join(normalize_billing(block, pending_review) if LEGACY_LABEL.search(block.splitlines()[0])
                       else block for block in body.split('\n\n') if block.strip())


def export_records(body):
    return [{'text': block, 'record_type': 'medical_billing' if LABEL in block.lower() else 'other',
             'classification_note': 'Billing type is retained from source screening or an explicit legacy billing label.'}
            for block in body.split('\n\n') if block.strip()]
