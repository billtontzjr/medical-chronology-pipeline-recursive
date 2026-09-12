"""Document-level OCR deferral and retry; never certify unreadable text."""
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .deposition_evidence import atomic_json
from .session_state import PhaseState, STATUS_PENDING


def _decisions(session):
    path = Path(session) / 'review' / 'ocr.json'
    return json.loads(path.read_text()) if path.exists() else {}


def _source(root, name):
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file() or path.suffix.lower() != '.pdf':
        raise ValueError('The OCR source is unavailable or outside this case.')
    return path


def ocr_items(store, state):
    decisions = _decisions(store.session_dir(state.session_id))
    items = []
    for report in state.phases['ocr'].data.get('coverage', {}).get('files', []):
        decision = decisions.get(report['source_file'])
        if report.get('needs_review') or (decision and decision['status'] == 'deferred'):
            fingerprint = hashlib.sha256(json.dumps({'report': report, 'decision': decision}, sort_keys=True).encode()).hexdigest()
            items.append({'report': report, 'decision': decision, 'fingerprint': fingerprint})
    return items


def save_ocr_decision(store, session_id, source_file, *, fingerprint, action, reviewer, reason):
    if action not in ('deferred', 'retry_requested'):
        raise ValueError('Choose defer this document or retry its extraction.')
    if not reviewer.strip() or not reason.strip():
        raise ValueError('Enter the reviewer name and a brief decision note.')
    state = store.load(session_id)
    item = next((x for x in ocr_items(store, state) if x['report']['source_file'] == source_file), None)
    if not item or item['fingerprint'] != fingerprint:
        raise ValueError('The OCR review changed. Reload the current item.')
    source = _source(store.input_dir(session_id), source_file)
    decisions = _decisions(store.session_dir(session_id))
    prior = decisions.get(source_file, {})
    txt = store.extracted_dir(session_id) / Path(source_file).with_suffix('.txt')
    decisions[source_file] = {'revision': uuid.uuid4().hex, 'source_file': source_file,
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'text_sha256': hashlib.sha256(txt.read_bytes()).hexdigest() if txt.exists() else None,
        'status': action, 'reviewer': reviewer.strip(), 'reason': reason.strip(),
        'reviewed_at': datetime.now(timezone.utc).isoformat(), 'coverage': item['report'],
        'history': prior.get('history', []) + ([{k:v for k,v in prior.items() if k != 'history'}] if prior else [])}
    atomic_json(store.session_dir(session_id) / 'review' / 'ocr.json', decisions)
    apply_ocr_updates(store, session_id)
    return decisions[source_file]


def apply_ocr_updates(store, session_id):
    state = store.load(session_id)
    applied = dict(state.phases['ocr'].data.get('review_revisions', {}))
    for name, decision in _decisions(store.session_dir(session_id)).items():
        if applied.get(name) == decision['revision']:
            continue
        source = _source(store.input_dir(session_id), name)
        if hashlib.sha256(source.read_bytes()).hexdigest() != decision['source_sha256']:
            raise ValueError('The OCR source changed; review the current version before continuing.')
        if decision['status'] not in ('deferred', 'retry_requested'):
            raise ValueError('Unknown OCR review decision.')
        history = store.session_dir(session_id) / 'review-history' / decision['revision']
        history.mkdir(parents=True, exist_ok=True)
        state.status = STATUS_PENDING
        state.last_error = None
        for phase in ('generate', 'header', 'summary', 'upload'):
            state.phases[phase] = PhaseState()
        if decision['status'] == 'retry_requested':
            state.phases['ocr'].status = STATUS_PENDING
        store.save(state)
        # Source removal/re-extraction can change batch composition and numbering.
        # Preserve the old revision and rebuild generation; keep every other OCR file.
        batches = store.batches_dir(session_id)
        if batches.exists() and not (history / 'batches').exists():
            batches.rename(history / 'batches')
        batches.mkdir(exist_ok=True)
        model = history / 'batches' / 'run_model.json'
        if model.exists() and not (batches / 'run_model.json').exists():
            shutil.copy2(model, batches / 'run_model.json')
        out = store.output_dir(session_id)
        if out.exists() and any(out.iterdir()) and not (history / 'output').exists():
            out.rename(history / 'output')
        out.mkdir(exist_ok=True)
        if decision['status'] == 'retry_requested':
            for suffix in ('.txt', '.ocr.json'):
                path = store.extracted_dir(session_id) / Path(name).with_suffix(suffix)
                if path.exists():
                    path.rename(history / path.name)
        applied[name] = decision['revision']
        state.phases['ocr'].data['review_revisions'] = applied
        store.save(state)


def deferred_sources(input_dir, extracted_dir):
    """Hash-bound choices only. Changed evidence reopens review, never stays excluded."""
    deferred = {}
    for name, decision in _decisions(Path(extracted_dir).parent).items():
        if decision['status'] != 'deferred':
            continue
        source = _source(input_dir, name)
        txt = Path(extracted_dir) / Path(name).with_suffix('.txt')
        text_hash = hashlib.sha256(txt.read_bytes()).hexdigest() if txt.exists() else None
        if (hashlib.sha256(source.read_bytes()).hexdigest() != decision['source_sha256']
                or text_hash != decision['text_sha256']):
            raise ValueError('A deferred OCR source changed. Review its current version before continuing.')
        deferred[name] = decision
    return deferred


def blocking_coverage(coverage, deferred):
    return [r for r in coverage['files'] if r['source_file'] not in deferred
            and (r.get('technical_failure') or r.get('coverage_status') == 'unknown_legacy')]


def selected_retries(session, completed):
    decisions = _decisions(session)
    if not decisions:
        return None  # Ordinary initial extraction/resume retains its existing behavior.
    return {name for name, decision in decisions.items()
            if decision['status'] == 'retry_requested'
            and completed.get(name) != decision['revision']}


def retry_revision(session, name):
    return _decisions(session)[name]['revision']


def ocr_manual_reviews(batches_dir):
    reviews = []
    for name, d in _decisions(Path(batches_dir).parent).items():
        if d['status'] != 'deferred':
            continue
        report = d['coverage']
        reviews.append({'id': d['revision'][:16], 'status': 'deferred', 'source_file': name,
            'source_section': str(Path(name).with_suffix('.txt')), 'reported_pages': [],
            'source_pages': sorted(set(report.get('error_pages', []) + report.get('no_text_pages', []))),
            'reason': 'OCR coverage needs review. ' + d['reason'],
            'handling': 'Entire document withheld by a human reviewer because extraction is uncertain. No missing text or negative medical finding inferred.',
            'reviewer': d['reviewer'], 'review_date': d['reviewed_at'],
            'resolution': 'Deferred, not verified', 'withheld_proposed_entries': []})
    return reviews
