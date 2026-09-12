"""Separate unresolved source sections from the supported chronology draft."""
import hashlib
import copy
import json
import re
from pathlib import Path

from .chronology_scope import ScopeReviewRequired, parse_scoped_response, INCLUDED, EXCLUDED
from .source_fidelity import (DiagnosticEvidenceError, DiagnosticStructureError,
                              render_diagnostic, validate_diagnostic_structure)
from .source_pages import page_units
from .ocr_review import ocr_manual_reviews

DRAFT_LABEL = 'Draft complete—manual review required'


def parse_draft_response(raw, documents):
    """Defer explicitly uncertain sources; keep all other validation gates.

    An uncertain source section and every entry citing it are withheld together.
    We do not infer which statements inside that section might still be reliable.
    Original responses remain in the private source-screening checkpoint.
    """
    try:
        text, exclusions = parse_scoped_response(raw, documents)
        return text, exclusions, []
    except ScopeReviewRequired as error:
        if error.code not in ('review_required', 'diagnostic_source_review'):
            raise
        original_error = error
    data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()))
    if (not isinstance(data.get('sources'), list) or not isinstance(data.get('entries'), list)
            or data.get('review_required')):
        # Without source IDs and a complete inventory there is no safe partition.
        raise original_error
    lookup = {f'D{i:03d}': d for i, d in enumerate(documents, 1)}
    source_ids = [s.get('id') if isinstance(s, dict) else None for s in data['sources']]
    if (any(not isinstance(sid, str) or sid not in lookup for sid in source_ids)
            or len(source_ids) != len(set(source_ids)) or set(source_ids) != set(lookup)):
        raise ScopeReviewRequired('Uncertain source response has incomplete or conflicting source IDs.')
    pending_reasons, findings = {}, {}
    dispositions = {source['id']: source for source in data['sources']}
    for source in data['sources']:
        scope = source.get('scope')
        if scope not in ('medical', 'mixed', 'excluded', 'review_required'):
            raise original_error
        if scope == 'excluded' and (not isinstance(source.get('category'), str)
                                   or source['category'] not in EXCLUDED
                                   or not isinstance(source.get('reason'), str) or not source['reason'].strip()):
            raise original_error
        if scope != 'review_required':
            if source.get('review_required'):
                raise original_error
            continue
        reason = source.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ScopeReviewRequired('An uncertain source needs a reason for the review queue.')
        pending_reasons[source['id']] = reason

    # Validate all source relationships, including withheld candidates. A later
    # malformed entry must not be hidden by an earlier deferrable evidence error.
    for entry in data['entries']:
        if not isinstance(entry, dict):
            raise ScopeReviewRequired('An entry cannot be assigned to a source for manual review.')
        ids, category = entry.get('source_ids'), entry.get('record_type')
        if (not isinstance(category, str) or category not in INCLUDED | EXCLUDED
                or not isinstance(ids, list) or not ids
                or any(not isinstance(sid, str) or sid not in lookup for sid in ids)
                or len(ids) != len(set(ids))):
            raise ScopeReviewRequired('An entry has invalid or unknown source references or record type.')
        if category in INCLUDED and any(dispositions[sid]['scope'] == 'excluded' for sid in ids):
            raise ScopeReviewRequired('A medical entry cites an excluded source.')
        if category == 'diagnostic_test':
            try:
                validate_diagnostic_structure(entry, lookup)
            except DiagnosticStructureError as exc:
                raise ScopeReviewRequired(str(exc), code='diagnostic_source_review',
                                          details=[{'source_ids': ids, 'check': exc.code}]) from exc
        elif not isinstance(entry.get('text'), str) or not entry['text'].strip():
            raise ScopeReviewRequired('An entry has invalid text.')

    model_pending = set(pending_reasons)
    for number, entry in enumerate(data['entries'], 1):
        if entry['record_type'] != 'diagnostic_test':
            continue
        if set(entry['source_ids']).intersection(model_pending):
            continue  # The model has already withheld this uncertain material.
        try:
            render_diagnostic(entry, lookup)
        except DiagnosticEvidenceError as exc:
            for sid in entry['source_ids']:
                pending_reasons.setdefault(sid, str(exc))
                findings.setdefault(sid, []).append({
                    'entry_number': number, 'check': exc.code, 'failed_checks': exc.checks,
                    'reason': str(exc)})

    # Entries citing both uncertain and otherwise clear sources are inseparable.
    # Propagate the review boundary rather than silently orphaning either source.
    pending_ids = set(pending_reasons)
    changed = True
    while changed:
        changed = False
        for entry in data['entries']:
            if entry['record_type'] not in INCLUDED or not pending_ids.intersection(entry['source_ids']):
                continue
            for sid in entry['source_ids']:
                if sid not in pending_ids:
                    pending_ids.add(sid)
                    pending_reasons[sid] = 'Shares an entry with an uncertain source; review these source sections together.'
                    changed = True
    reviews = []
    for sid in lookup:
        if sid not in pending_ids:
            continue
        doc, reason = lookup[sid], pending_reasons[sid]
        physical_pages = doc.get('source_pages', sorted({page for page, _ in page_units(doc['content'])
                                                        if page is not None}))
        reviews.append({'id': hashlib.sha256((doc['filename'] + '\n' + doc['content'] + '\n' + reason).encode()).hexdigest()[:16],
            'status': 'pending', 'source_id': sid, 'source_file': doc.get('source_file', doc['filename']),
            'source_section': doc['filename'],
            'source_pages': physical_pages,
            'diagnostic_findings': findings.get(sid, []),
            'reported_pages': sorted(set(int(n) for n in re.findall(r'\bpage\s+(\d+)\b', reason, re.I))),
            'page_reference_note': 'Page references come from the model review reason; confirm against the original PDF.',
            'reason': reason, 'handling': 'Entire uncertain source section withheld from the dated draft, including otherwise legible entries in this section.',
            'withheld_proposed_entries': [], 'reviewer': '', 'review_date': '', 'resolution': ''})
    if not pending_ids:
        raise original_error
    kept_entries = []
    for entry in data['entries']:
        ids = entry['source_ids']
        if pending_ids.intersection(ids):
            for review in reviews:
                if review['source_id'] in ids:
                    review['withheld_proposed_entries'].append(entry)
        else:
            kept_entries.append(entry)
    # Reindex only the retained source IDs for the existing strict validator.
    retained_ids = [sid for sid in lookup if sid not in pending_ids]
    mapping = {sid: f'D{i:03d}' for i, sid in enumerate(retained_ids, 1)}
    retained_sources = [dict(s, id=mapping[s['id']]) for s in data['sources'] if s['id'] in mapping]
    retained_entries = []
    for entry in kept_entries:
        retained = copy.deepcopy(entry)
        retained['source_ids'] = [mapping[sid] for sid in entry['source_ids']]
        result = retained.get('diagnostic_result')
        if isinstance(result, dict) and isinstance(result.get('evidence'), list):
            for item in result['evidence']:
                if not isinstance(item, dict) or item.get('source_id') not in entry['source_ids']:
                    raise ScopeReviewRequired('Diagnostic evidence references an unrelated source.')
                item['source_id'] = mapping[item['source_id']]
        retained_entries.append(retained)
    text, exclusions = parse_scoped_response(json.dumps({'sources': retained_sources, 'entries': retained_entries}),
                                             [lookup[sid] for sid in retained_ids])
    return text, exclusions, reviews


def collect_manual_reviews(batches_dir):
    reviews = {item['id']: item for item in ocr_manual_reviews(batches_dir)}
    for path in sorted(Path(batches_dir).glob('batch_*.scope.json')):
        for item in json.loads(path.read_text()).get('manual_reviews', []):
            reviews.setdefault(item['id'], dict(item, batch=path.name.split('.')[0]))
    return list(reviews.values())


def review_markdown(reviews):
    if not reviews:
        return ''
    paragraphs = ['NEEDS MANUAL REVIEW', DRAFT_LABEL + '. ' + str(len(reviews)) +
        ' unresolved source section(s). These sections were withheld from the dated chronology; '
        'the draft is incomplete until reviewed. Do not treat this list or automated verification as approval.']
    for item in reviews:
        pages = ', '.join(map(str, item['reported_pages'])) or 'Not reliably identified; review the named source section'
        paragraphs.append(f"Review {item['id']} — PENDING\nSource: {item['source_file']}\n"
            f"Section: {item['source_section']}\nReported pages: {pages}\n"
            f"Issue: {item['reason']}\nHandling: {item['handling']}\n"
            'Team action: Compare the original pages, confirm dates/provider/treatment, and add supported encounters '
            'to the reviewed chronology. Record reviewer, date and resolution.\nReviewer: __________\nReview date: __________\nResolution: __________')
        if item.get('reviewer'):
            paragraphs.append(f"Recorded team decision: {item.get('resolution', 'Deferred, not verified')}\n"
                              f"Reviewer: {item['reviewer']}\nDecision date: {item.get('review_date', '')}\n"
                              'This item remains unresolved. Reopen its document review panel to reconsider it.')
        if item.get('source_pages'):
            paragraphs.append('Physical PDF pages in this source section: ' +
                              ', '.join(map(str, item['source_pages'])) +
                              '. These identify the section, not a verified location for every candidate.')
        for finding in item.get('diagnostic_findings', []):
            paragraphs.append(f"Candidate {finding['entry_number']} — failed evidence checks: " +
                              ', '.join(finding['failed_checks']) + '.\n' + finding['reason'])
        if item['withheld_proposed_entries']:
            paragraphs.append('WITHHELD DRAFT CANDIDATES — NOT VERIFIED. Compare each with the original source '
                              'before adding it to the chronology.')
            for entry in item['withheld_proposed_entries']:
                text = entry.get('text')
                if isinstance(text, str):
                    paragraphs.append('Unverified candidate: ' + text)
                result = entry.get('diagnostic_result')
                if isinstance(result, dict):
                    paragraphs.append('Unverified diagnostic candidate — NOT APPROVED:\n' +
                                      '\n'.join(f'{field.capitalize()}: {result[field]}'
                                                for field in ('date', 'facility', 'provider', 'study')))
                    for evidence in result['evidence']:
                        paragraphs.append(f"Proposed source: {evidence['source_id']}\n"
                                          f"Proposed date quote: {evidence['date_quote']}\n"
                                          f"Proposed result quote: {evidence['quote']}")
    return '\n\n'.join(paragraphs)
