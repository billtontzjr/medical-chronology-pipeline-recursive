"""Durable, source-bound human decisions for one deposition at a time."""
import copy
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .deposition import prepare_document, validate_identity, decode
from .deposition_evidence import TranscriptIndex, StageRunner, atomic_json
from .session_state import PhaseState, STATUS_PENDING


def _batch_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'batch_\d{3,}', value):
        raise ValueError('Invalid review item.')
    return value


def load_item(input_dir, batches_dir, batch):
    batch = _batch_name(batch)
    root, batches = Path(input_dir).resolve(), Path(batches_dir)
    work = json.loads((batches / (batch + '.deposition-work.json')).read_text())
    source = (root / work['source_file']).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError('The review source is unavailable or outside this case.')
    doc = prepare_document(work['source_file'], source.read_text())
    if doc is None:
        raise ValueError('This source no longer matches the saved deposition. Review its extraction.')
    model = json.loads((batches / 'run_model.json').read_text())['model']
    signature = StageRunner(doc, model, None).signature
    if signature != work.get('signature'):
        raise ValueError('The source or model changed. The saved review cannot approve this version.')
    index = TranscriptIndex(doc['content'])
    proposal = work.get('stages', {}).get('identity', {})
    for rejected in reversed(work.get('rejections', [])):
        if rejected.get('stage') == 'identity':
            try:
                candidate = decode(rejected['response'])
                if isinstance(candidate, dict):
                    proposal = candidate
                    break
            except ValueError:
                pass
    path = batches / (batch + '.deposition-review.json')
    decision = json.loads(path.read_text()) if path.exists() else None
    if decision and decision.get('signature') != signature:
        raise ValueError('The saved human decision belongs to another source version.')
    fingerprint = hashlib.sha256(json.dumps({'work': work, 'decision': decision}, sort_keys=True).encode()).hexdigest()
    return {'batch': batch, 'work': work, 'document': doc, 'index': index,
            'signature': signature, 'fingerprint': fingerprint, 'proposal': proposal,
            'decision': decision}


def review_items(input_dir, batches_dir):
    items = []
    for path in sorted(Path(batches_dir).glob('batch_*.deposition-work.json')):
        work = json.loads(path.read_text())
        decision_path = path.with_name(path.name.split('.')[0] + '.deposition-review.json')
        if work.get('blocked_stage') or decision_path.exists():
            items.append(load_item(input_dir, batches_dir, path.name.split('.')[0]))
    return items


def parse_ranges(text):
    """Team-facing source line ranges, e.g. 27; 84-85."""
    refs = []
    for part in text.split(';'):
        if not part.strip():
            continue
        match = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+))?\s*', part)
        if not match:
            raise ValueError('Use source line numbers such as 27; 84-85.')
        refs.append([int(match[1]), int(match[2] or match[1])])
    return refs


def save_decision(store, session_id, batch, *, fingerprint, action, reviewer,
                  reason, metadata=None, confirmed=False):
    """Caller holds the session operation lock; no automatic reviewer approvals."""
    item = load_item(store.extracted_dir(session_id), store.batches_dir(session_id), batch)
    if fingerprint != item['fingerprint']:
        raise ValueError('This item changed. Reload it and review the current evidence.')
    if action not in ('approved_identity', 'deferred'):
        raise ValueError('Choose confirm identity or defer document.')
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError('Enter the reviewer name for the decision record.')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Record a brief decision note.')
    identity = None
    if action == 'approved_identity':
        if not confirmed or item['work'].get('blocked_stage') != 'identity':
            raise ValueError('Confirm the source identity and case association before approval. Other testimony issues cannot be approved here.')
        identity = validate_identity(copy.deepcopy(metadata), item['index'])
    prior = item['decision'] or {}
    decision = {'revision': uuid.uuid4().hex, 'signature': item['signature'],
                'source_file': item['document']['filename'],
                'transcript_sha256': item['document']['transcript_sha256'],
                'status': action, 'reviewer': reviewer.strip(),
                'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reason': reason.strip(),
                'case_association_confirmed': bool(confirmed and identity), 'identity': identity,
                'history': prior.get('history', []) + ([{k: v for k, v in prior.items() if k != 'history'}] if prior else [])}
    atomic_json(store.batches_dir(session_id) / (batch + '.deposition-review.json'), decision)
    apply_review_updates(store, session_id)
    return decision


def apply_review_updates(store, session_id):
    """Idempotent invalidation after a durable decision; preserve all replaced work."""
    state = store.load(session_id)
    applied = dict(state.phases['generate'].data.get('review_revisions', {}))
    batches = store.batches_dir(session_id)
    for path in sorted(batches.glob('batch_*.deposition-review.json')):
        decision = json.loads(path.read_text())
        batch = _batch_name(path.name.split('.')[0])
        if applied.get(batch) == decision['revision']:
            continue
        # Fail closed on stale decisions before invalidating any output.
        item = load_item(store.extracted_dir(session_id), batches, batch)
        if decision['status'] not in ('approved_identity', 'deferred'):
            raise ValueError('Unknown deposition decision; source review is required.')
        history = store.session_dir(session_id) / 'review-history' / decision['revision']
        history.mkdir(parents=True, exist_ok=True)
        # Mark outputs stale before moving them; an interrupted update resumes here.
        state.status = STATUS_PENDING
        state.last_error = None
        for phase in ('generate', 'header', 'summary', 'upload'):
            state.phases[phase] = PhaseState()
        state.phases['generate'].data['review_revisions'] = applied
        store.save(state)
        for suffix in ('.md', '.scope.json', '.deposition.json'):
            artifact = batches / (batch + suffix)
            if artifact.exists():
                artifact.rename(history / artifact.name)
        out = store.output_dir(session_id)
        if out.exists() and any(out.iterdir()):
            out.rename(history / 'output')
        out.mkdir(exist_ok=True)
        consolidation = batches / 'encounter_consolidation.json'
        if consolidation.exists():
            consolidation.rename(history / consolidation.name)
        if decision['status'] == 'approved_identity':
            work_path = batches / (batch + '.deposition-work.json')
            backup = history / work_path.name
            if not backup.exists():
                shutil.copy2(work_path, backup)
            work = item['work']
            # Identity conditions all later testimony prompts; recheck this document
            # while every unrelated completed batch stays available for reuse.
            work['stages'] = {}
            work['blocked_stage'] = 'identity'
            atomic_json(work_path, work)
        applied[batch] = decision['revision']
        state.phases['generate'].data['review_revisions'] = applied
        store.save(state)


def decision_for(document, model, work_path):
    path = Path(work_path).with_name(Path(work_path).name.replace('.deposition-work.json', '.deposition-review.json'))
    if not path.exists():
        return None
    decision = json.loads(path.read_text())
    if decision.get('signature') != StageRunner(document, model, None).signature:
        raise ValueError('The deposition decision does not match this source and model.')
    if decision.get('status') not in ('approved_identity', 'deferred'):
        raise ValueError('Unknown deposition review decision.')
    return decision


def deferred_review(document, decision):
    return {'id': decision['revision'][:16], 'status': 'deferred',
            'source_file': document['filename'], 'source_section': document['filename'],
            'reported_pages': [], 'source_pages': [],
            'reason': decision['reason'],
            'handling': 'Entire deposition withheld by a human reviewer. Unresolved; no testimony or negative finding inferred. Resolve this document from its review panel to include it later.',
            'reviewer': decision['reviewer'], 'review_date': decision['reviewed_at'],
            'resolution': 'Deferred, not verified', 'withheld_proposed_entries': []}
