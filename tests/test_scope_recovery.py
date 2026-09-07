"""Synthetic failures exercise bounded correction, private retention, and resume."""
import json
import logging

import pytest

from src.chronology_agent import ChronologyAgent
from src.chronology_scope import ScopeReviewRequired, parse_scoped_response
from src.scope_recovery import screen_batch
from src.session_model import batch_signature, saved_model

DOCS = [{'filename': 'synthetic.txt', 'content': 'Assessment: Back pain. Plan: Therapy.'}]
ENTRY = '06/04/2026. Clinic. Physician, MD. Office Visit. Assessment: Back pain. Plan: Therapy.'
VALID = {'sources': [{'id': 'D001', 'scope': 'medical'}], 'entries': [
    {'record_type': 'clinical_care', 'source_ids': ['D001'], 'text': ENTRY}]}


def agent():
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.model = 'gpt-6-astra'
    a.logger = logging.getLogger('synthetic')
    return a


def test_unknown_source_is_corrected_once_and_both_responses_retained(tmp_path):
    bad = json.loads(json.dumps(VALID)); bad['sources'][0]['id'] = 'D099'
    responses = iter([json.dumps(bad), json.dumps(VALID)])
    prompts = []
    path = tmp_path / 'batch.scope-work.json'
    def call(prompt, **kwargs):
        prompts.append(prompt)
        return next(responses)
    assert screen_batch('prompt', DOCS, call, checkpoint=path)[0] == ENTRY
    state = json.loads(path.read_text())
    assert state['status'] == 'complete' and len(state['attempts']) == 2
    assert state['attempts'][0]['code'] == 'unknown_source_id'
    assert 'RESPONSE FORMAT CORRECTION' in prompts[1]
    assert path.stat().st_mode & 0o077 == 0
    assert screen_batch('prompt', DOCS, lambda *a, **k: pytest.fail('no new calls'), checkpoint=path)[0] == ENTRY


@pytest.mark.parametrize('data', [
    {'review_required': 'Missing attachment'},
    {'sources': [{'id': 'D001', 'scope': 'review_required', 'reason': 'Unclear attachment'}]},
    {'sources': [None, {'id': 'D001', 'scope': 'review_required', 'reason': 'Unclear'}], 'entries': []},
    {'sources': [{'id': 'D001', 'scope': 'medical'}, {'id': 'D001', 'scope': 'excluded'}], 'entries': []},
])
def test_substantive_uncertainty_never_retried_or_bypassed_on_resume(tmp_path, data):
    calls = []
    path = tmp_path / 'batch.scope-work.json'
    def call(*a, **k):
        calls.append(1); return json.dumps(data)
    for _ in range(2):
        with pytest.raises(ScopeReviewRequired):
            screen_batch('prompt', DOCS, call, checkpoint=path)
    assert calls == [1]
    assert json.loads(path.read_text())['status'] == 'blocked'


def test_two_format_failures_block_and_preserve_private_responses(tmp_path):
    calls = []
    path = tmp_path / 'batch.scope-work.json'
    def call(*a, **k):
        calls.append(1); return 'not JSON'
    for _ in range(2):
        with pytest.raises(ScopeReviewRequired):
            screen_batch('prompt', DOCS, call, checkpoint=path)
    assert calls == [1, 1]
    assert len(json.loads(path.read_text())['attempts']) == 2


def test_interrupted_format_correction_resumes_at_second_attempt(tmp_path):
    path = tmp_path / 'batch.scope-work.json'
    responses = iter(['bad JSON', RuntimeError('network interrupted')])
    def interrupted(*a, **k):
        response = next(responses)
        if isinstance(response, Exception): raise response
        return response
    with pytest.raises(RuntimeError):
        screen_batch('prompt', DOCS, interrupted, checkpoint=path)
    assert len(json.loads(path.read_text())['attempts']) == 1
    assert screen_batch('prompt', DOCS, lambda *a, **k: json.dumps(VALID), checkpoint=path)[0] == ENTRY


def test_checkpoint_model_change_is_blocked_without_overwrite(tmp_path):
    path = tmp_path / 'batch.scope-work.json'
    screen_batch('prompt', DOCS, lambda *a, **k: json.dumps(VALID), model='astra', checkpoint=path)
    before = path.read_bytes()
    with pytest.raises(ScopeReviewRequired, match='model changed'):
        screen_batch('prompt', DOCS, lambda *a, **k: pytest.fail('no call'), model='claude', checkpoint=path)
    assert path.read_bytes() == before


def test_prior_batches_preserved_and_failed_batch_not_published(tmp_path, monkeypatch):
    a = agent(); batches = tmp_path / 'batches'; source = tmp_path / 'source'; source.mkdir()
    a._read_extracted_files = lambda _: DOCS * 2
    a._plan_batches = lambda _: [DOCS, DOCS]
    replies = iter([json.dumps(VALID), json.dumps({'review_required': 'missing report'})])
    a._call_api_with_retry = lambda *a, **k: next(replies)
    monkeypatch.setattr('src.chronology_agent.time.sleep', lambda _: None)
    with pytest.raises(ScopeReviewRequired): a.generate_batches(str(source), str(batches))
    assert (batches / 'batch_001.md').read_text() == ENTRY
    assert not (batches / 'batch_002.md').exists()
    first = (batches / 'batch_001.md').read_bytes()
    a._call_api_with_retry = lambda *a, **k: pytest.fail('Do not repeat completed or blocked batches')
    with pytest.raises(ScopeReviewRequired): a.generate_batches(str(source), str(batches))
    assert (batches / 'batch_001.md').read_bytes() == first


def test_legacy_model_recovered_by_exact_fingerprint_not_sidebar(tmp_path):
    a = agent(); a.model = 'claude-opus-5'
    a._read_extracted_files = lambda _: DOCS
    a._plan_batches = lambda _: [DOCS]
    manifest = {'signature': batch_signature([DOCS], [], 'gpt-6-astra'), 'total_batches': 1}
    path = tmp_path / 'batch_manifest.json'; path.write_text(json.dumps(manifest))
    assert saved_model(a, tmp_path, tmp_path, ['claude-opus-5', 'gpt-6-astra']) == 'gpt-6-astra'
    assert json.loads(path.read_text()) == manifest
    a._read_extracted_files = lambda _: pytest.fail('model is now saved')
    assert saved_model(a, tmp_path, tmp_path, []) == 'gpt-6-astra'


def test_unmatched_legacy_fingerprint_does_not_guess_or_write(tmp_path):
    a = agent(); a._read_extracted_files = lambda _: DOCS; a._plan_batches = lambda _: [DOCS]
    (tmp_path / 'batch_manifest.json').write_text(json.dumps({'signature': 'unknown'}))
    with pytest.raises(ValueError, match='could not be confirmed'):
        saved_model(a, tmp_path, tmp_path, ['gpt-6-astra'])
    assert not (tmp_path / 'run_model.json').exists()


@pytest.mark.parametrize('value', [[], {}, None, 17])
def test_malformed_source_id_is_handled_as_validation_failure(value):
    invalid = {'sources': [{'id': value, 'scope': 'medical'}], 'entries': []}
    with pytest.raises(ScopeReviewRequired): parse_scoped_response(json.dumps(invalid), DOCS)
