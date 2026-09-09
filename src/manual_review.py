"""Separate unresolved source sections from the supported chronology draft."""
import hashlib
import copy
import json
import re
from pathlib import Path

from .chronology_scope import ScopeReviewRequired, parse_scoped_response

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
        if error.code != 'review_required':
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
    reviews = []
    pending_ids = set()
    for source in data['sources']:
        if source.get('scope') != 'review_required':
            if source.get('review_required'):
                raise original_error
            continue
        reason = source.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ScopeReviewRequired('An uncertain source needs a reason for the review queue.')
        sid = source['id']; doc = lookup[sid]; pending_ids.add(sid)
        reviews.append({'id': hashlib.sha256((doc['filename'] + '\n' + doc['content'] + '\n' + reason).encode()).hexdigest()[:16],
            'status': 'pending', 'source_id': sid, 'source_file': doc.get('source_file', doc['filename']),
            'source_section': doc['filename'],
            'reported_pages': sorted(set(int(n) for n in re.findall(r'\bpage\s+(\d+)\b', reason, re.I))),
            'page_reference_note': 'Page references come from the model review reason; confirm against the original PDF.',
            'reason': reason, 'handling': 'Entire uncertain source section withheld from the dated draft, including otherwise legible entries in this section.',
            'withheld_proposed_entries': [], 'reviewer': '', 'review_date': '', 'resolution': ''})
    if not pending_ids:
        raise original_error
    kept_entries = []
    for entry in data['entries']:
        if not isinstance(entry, dict):
            raise ScopeReviewRequired('An entry cannot be assigned to a source for manual review.')
        ids = entry.get('source_ids')
        if (not isinstance(ids, list) or not ids or
                any(not isinstance(sid, str) or sid not in lookup for sid in ids)):
            raise ScopeReviewRequired('An entry has unknown source references; review before saving the draft.')
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
    reviews = {}
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
        if item['withheld_proposed_entries']:
            paragraphs.append('WITHHELD DRAFT CANDIDATES — NOT VERIFIED. Compare each with the original source '
                              'before adding it to the chronology.')
            for entry in item['withheld_proposed_entries']:
                text = entry.get('text')
                if isinstance(text, str):
                    paragraphs.append('Unverified candidate: ' + text)
    return '\n\n'.join(paragraphs)
