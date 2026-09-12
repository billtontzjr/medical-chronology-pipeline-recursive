"""Synthetic team decisions only; no production identities or records."""
import asyncio
import json
import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.chronology_agent import ChronologyAgent
from src.deposition import prepare_document, summarize
from src.deposition_evidence import StageRunner, atomic_json
from src.deposition_review import load_item, save_decision, parse_ranges
from src.manual_review import collect_manual_reviews, review_markdown
from src.ocr_coverage import save_coverage, collect_coverage
from src.ocr_review import (ocr_items, save_ocr_decision, deferred_sources, blocking_coverage)
from src.pipeline import MedicalChronologyPipeline
from src.review_ui import review_count
from src.session_lock import session_lock
from src.session_state import SessionStore, SessionState

TEXT = ('IN THE CIRCUIT COURT\nDEPOSITION OF Jamie Example\nDATE: July 16, 2026\n'
        + 'Q. Any pain?\nA. I recall pain.\n' * 8)
IDENTITY = {'date': '07/16/2026', 'witness': 'Jamie Example', 'credentials': '',
            'date_refs': [[3, 3]], 'witness_refs': [[2, 2]], 'credentials_refs': []}


def fixture(tmp_path):
    store = SessionStore(str(tmp_path))
    state = SessionState.new('test_case', 'Example', 'https://example.test/source', '/Medical chronology pipeline outputs/test_case')
    store.save(state)
    batches, extracted = store.batches_dir(state.session_id), store.extracted_dir(state.session_id)
    batches.mkdir(parents=True, exist_ok=True)
    extracted.mkdir(parents=True, exist_ok=True)
    store.input_dir(state.session_id).mkdir(parents=True, exist_ok=True)
    (extracted / 'Witness.txt').write_text(TEXT)
    atomic_json(batches / 'run_model.json', {'model': 'test'})
    doc = prepare_document('Witness.txt', TEXT)
    work = StageRunner(doc, 'test', None, batches / 'batch_001.deposition-work.json')
    work.state['blocked_stage'] = 'identity'
    work.state['rejections'] = [{'stage': 'identity', 'error': 'Evidence quote did not match', 'response': json.dumps(IDENTITY), 'attempt': 2}]
    work.save()
    return store, state, doc


def decide(store, state, action='deferred', **overrides):
    item = load_item(store.extracted_dir(state.session_id), store.batches_dir(state.session_id), 'batch_001')
    args = dict(fingerprint=item['fingerprint'], action=action, reviewer='Reviewer Example', reason='Checked source; defer pending review',
                metadata=IDENTITY, confirmed=action == 'approved_identity')
    args.update(overrides)
    return save_decision(store, state.session_id, 'batch_001', **args)


def test_confirm_is_human_source_bound_and_preserves_other_work(tmp_path):
    store, state, doc = fixture(tmp_path)
    batches = store.batches_dir(state.session_id)
    (batches / 'batch_002.md').write_text('Unrelated completed encounter')
    out = store.output_dir(state.session_id)
    out.mkdir(exist_ok=True)
    (out / 'chronology.md').write_text('Earlier draft')
    with pytest.raises(ValueError, match='Confirm'):
        decide(store, state, 'approved_identity', confirmed=False)
    with pytest.raises(ValueError):
        decide(store, state, 'approved_identity', metadata={**IDENTITY, 'witness': 'Different Person'})
    decision = decide(store, state, 'approved_identity')
    assert decision['reviewer'] == 'Reviewer Example'
    assert decision['case_association_confirmed']
    assert (batches / 'batch_002.md').read_text() == 'Unrelated completed encounter'
    assert (store.session_dir(state.session_id) / 'review-history' / decision['revision'] / 'output/chronology.md').read_text() == 'Earlier draft'
    responses = iter([
        {'statements': [{'text': 'The witness recalled pain.', 'evidence_refs': [[4, 5]]}]},
        {'reviews': [{'statement_id': 1, 'verdict': 'supported', 'reason': 'Expressly recalled.'}]}])
    entry, evidence = summarize(doc, lambda *a, **k: json.dumps(next(responses)),
        checkpoint_path=batches / 'batch_001.deposition-work.json', model='test', identity_review=decision)
    assert entry.startswith('07/16/2026. Jamie Example, Deposition.')
    assert evidence['human_identity_review']['reviewer'] == 'Reviewer Example'
    assert json.loads((batches / 'batch_001.deposition-work.json').read_text())['rejections']


def test_defer_continues_unrelated_work_and_exports_unresolved_review(tmp_path, monkeypatch):
    store, state, doc = fixture(tmp_path)
    decision = decide(store, state)
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger, a.model = logging.getLogger('review-test'), 'test'
    a._read_extracted_files = lambda _: [doc, {'filename': 'Clinic.txt', 'content': 'Office visit'}]
    a._source_exclusions = []
    a._plan_batches = lambda docs: [[d] for d in docs]
    a._process_scoped_batch = lambda *args, **kwargs: ('07/17/2026. Clinic. Office Visit. Pain.', [])
    a._call_api_with_retry = lambda *args, **kwargs: pytest.fail('Deferred deposition must not call the model')
    monkeypatch.setattr('src.chronology_agent.time.sleep', lambda _: None)
    result = a.generate_batches(str(store.extracted_dir(state.session_id)), str(store.batches_dir(state.session_id)))
    assert result['success']
    assert (store.batches_dir(state.session_id) / 'batch_002.md').read_text()
    reviews = collect_manual_reviews(store.batches_dir(state.session_id))
    assert reviews[0]['status'] == 'deferred'
    assert 'Reviewer Example' in review_markdown(reviews)
    assert 'Entire deposition withheld' in review_markdown(reviews)
    assert review_count(store, store.load(state.session_id)) == 1
    # Later reconsideration is durable and preserves the earlier decision.
    approved = decide(store, state, 'approved_identity')
    assert approved['history'][0]['revision'] == decision['revision']
    assert not (store.batches_dir(state.session_id) / 'batch_001.md').exists()
    assert (store.batches_dir(state.session_id) / 'batch_002.md').exists()


def test_stale_form_or_changed_source_cannot_approve(tmp_path):
    store, state, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='changed'):
        decide(store, state, fingerprint='old-page')
    (store.extracted_dir(state.session_id) / 'Witness.txt').write_text(TEXT + 'Changed source')
    with pytest.raises(ValueError, match='changed'):
        decide(store, state)


def test_blocked_identity_is_not_retried_without_a_team_decision(tmp_path):
    store, state, doc = fixture(tmp_path)
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger, a.model = logging.getLogger('review-test'), 'test'
    a._read_extracted_files = lambda _: [doc]
    a._source_exclusions = []
    a._call_api_with_retry = lambda *a, **k: pytest.fail('Unchanged rejected evidence must not be retried')
    with pytest.raises(ValueError, match='Review documents'):
        a.generate_batches(str(store.extracted_dir(state.session_id)), str(store.batches_dir(state.session_id)))


def test_review_methods_cannot_race_worker(tmp_path):
    store, state, _ = fixture(tmp_path)
    pipeline = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    pipeline.store = store
    with session_lock(pipeline._lock_path(state.session_id)):
        with pytest.raises(RuntimeError):
            pipeline.review_deposition(state.session_id, 'batch_001')


def ocr_fixture(tmp_path):
    store, state, _ = fixture(tmp_path)
    pdf = store.input_dir(state.session_id) / 'Poor.pdf'
    pdf.write_bytes(b'synthetic PDF bytes')
    (store.extracted_dir(state.session_id) / 'Poor.txt').write_text('Partial text')
    save_coverage({'page_count': 2, 'page_results': [{'page': 1, 'status': 'text'}, {'page': 2, 'status': 'error'}]},
                  pdf, store.input_dir(state.session_id), store.extracted_dir(state.session_id))
    state.phases['ocr'].data['coverage'] = collect_coverage(store.input_dir(state.session_id), store.extracted_dir(state.session_id))
    store.save(state)
    return store, state, pdf


def test_ocr_defer_discloses_original_and_retry_only_invalidates_selected_extraction(tmp_path):
    store, state, pdf = ocr_fixture(tmp_path)
    extracted = store.extracted_dir(state.session_id)
    (extracted / 'Good.txt').write_text('Completed OCR')
    def choose(action):
        current = store.load(state.session_id)
        item = ocr_items(store, current)[0]
        return save_ocr_decision(store, state.session_id, 'Poor.pdf', fingerprint=item['fingerprint'],
            action=action, reviewer='Reviewer Example', reason='Page 2 unreadable')
    decision = choose('deferred')
    deferred = deferred_sources(store.input_dir(state.session_id), extracted)
    assert list(deferred) == ['Poor.pdf']
    assert not blocking_coverage(state.phases['ocr'].data['coverage'], deferred)
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger = logging.getLogger('test')
    assert all(d['filename'] != 'Poor.txt' for d in a._read_extracted_files(str(extracted)))
    assert (extracted / 'Poor.txt').read_text() == 'Partial text'
    assert pdf.read_bytes() == b'synthetic PDF bytes'
    assert 'Entire document withheld' in review_markdown(collect_manual_reviews(store.batches_dir(state.session_id)))
    assert review_count(store, store.load(state.session_id)) == 1
    retry = choose('retry_requested')
    assert retry['history'][0]['revision'] == decision['revision']
    assert not (extracted / 'Poor.txt').exists()
    assert (extracted / 'Good.txt').read_text() == 'Completed OCR'
    assert pdf.exists()
    assert not deferred_sources(store.input_dir(state.session_id), extracted)
    assert store.load(state.session_id).phases['ocr'].status == 'pending'
    assert (store.batches_dir(state.session_id) / 'run_model.json').exists()


def test_ocr_changed_evidence_reopens_review(tmp_path):
    store, state, pdf = ocr_fixture(tmp_path)
    item = ocr_items(store, state)[0]
    save_ocr_decision(store, state.session_id, 'Poor.pdf', fingerprint=item['fingerprint'], action='deferred', reviewer='Reviewer', reason='Unreadable')
    pdf.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        deferred_sources(store.input_dir(state.session_id), store.extracted_dir(state.session_id))


def test_review_retry_does_not_reextract_other_legacy_files(tmp_path):
    from types import SimpleNamespace
    store, state, _ = ocr_fixture(tmp_path)
    other = store.input_dir(state.session_id) / 'Legacy.pdf'
    other.write_bytes(b'other synthetic PDF')
    extracted = store.extracted_dir(state.session_id)
    (extracted / 'Legacy.txt').write_text('Preserved legacy OCR')
    state.phases['ocr'].data['coverage'] = collect_coverage(store.input_dir(state.session_id), extracted)
    store.save(state)
    item = next(i for i in ocr_items(store, state) if i['report']['source_file'] == 'Poor.pdf')
    save_ocr_decision(store, state.session_id, 'Poor.pdf', fingerprint=item['fingerprint'],
        action='retry_requested', reviewer='Reviewer', reason='Retry selected file')
    calls = []
    async def extract(paths, **kwargs):
        calls.extend(paths)
        return [{'success': True, 'page_count': 1, 'page_results': [{'page': 1, 'status': 'text'}]}]
    def save(*args, **kwargs):
        (extracted / 'Poor.txt').write_text('Recovered selected text')
    pipeline = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    pipeline.store = store
    pipeline.logger = logging.getLogger('selected-retry')
    pipeline.ocr_client = SimpleNamespace(batch_extract=extract, save_extracted_text=save)
    with pytest.raises(RuntimeError, match='OCR has failed'):
        asyncio.run(pipeline._phase_ocr(store.load(state.session_id), lambda _: None))
    assert [Path(p).name for p in calls] == ['Poor.pdf']
    assert (extracted / 'Legacy.txt').read_text() == 'Preserved legacy OCR'
    assert store.load(state.session_id).phases['ocr'].data['coverage']['files_needing_review'] == 1
    asyncio.run(pipeline._phase_ocr(store.load(state.session_id), lambda _: None))
    assert len(calls) == 1  # No unchanged retry on a later resume.


def test_evidence_line_input():
    assert parse_ranges('27; 84-85') == [[27, 27], [84, 85]]
    with pytest.raises(ValueError):
        parse_ranges('page twenty')


def test_review_panel_returns_after_navigation_and_ocr_has_no_approve(tmp_path):
    store, state, _ = ocr_fixture(tmp_path)
    script = '''
import streamlit as st
from src.session_state import SessionStore
from src.review_ui import render_review_panel, review_count
from types import SimpleNamespace
store = SessionStore(BASE)
state = store.load('test_case')
st.write('Needs review count:', review_count(store, state))
render_review_panel(SimpleNamespace(store=store), state, 'sessions', lambda *a, **k: None)
'''.replace('BASE', repr(str(tmp_path)))
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert 'Needs review: 2' in app.warning[0].value
    next(b for b in app.button if b.label.startswith('Review documents')).click().run()
    assert not app.exception
    assert any(b.label == 'Confirm identity and continue' for b in app.button)
    assert any(b.label == 'Retry extraction and continue' for b in app.button)
    assert not any('Approve OCR' in b.label for b in app.button)
    app.run()  # Navigation/rerun does not lose the durable alert or issue.
    assert 'Needs review: 2' in app.warning[0].value
    fresh = AppTest.from_string(script).run()
    assert 'Needs review: 2' in fresh.warning[0].value


def test_incomplete_response_is_reviewable_and_deferrable_but_not_identity_approval(tmp_path):
    from src.deposition_review import review_items
    store, state, doc = fixture(tmp_path)
    path = store.batches_dir(state.session_id) / 'batch_001.deposition-work.json'
    work = json.loads(path.read_text())
    work.pop('blocked_stage')
    work['response_error'] = {'stage': 'section 2 of 7', 'error': 'max_tokens'}
    atomic_json(path, work)
    assert review_count(store, state) == 1
    assert len(review_items(store.extracted_dir(state.session_id), store.batches_dir(state.session_id))) == 1
    with pytest.raises(ValueError, match='Confirm the source identity'):
        decide(store, state, action='approved_identity')
    assert decide(store, state)['status'] == 'deferred'
