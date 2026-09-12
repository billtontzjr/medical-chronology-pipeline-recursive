"""Fault injection uses synthetic testimony and mocked provider responses only."""
import json
import stat

import pytest

from src.deposition import DepositionReviewRequired, prepare_document, summarize, validate_identity
from src.deposition_evidence import EvidenceError, TranscriptIndex


TEXT = ('IN THE CIRCUIT COURT\nDEPOSITION OF Jamie Example\nDATE: July 16, 2026\n'
        '1 Q. Was surgery recommended?\n2 A. No, I only had therapy.\n'
        + 'Q. Any pain?\nA. Sometimes.\n' * 8)
IDENTITY = {'date': '07/16/2026', 'witness': 'Jamie Example', 'credentials': '',
            'date_refs': [[3, 3]], 'witness_refs': [[2, 2]], 'credentials_refs': []}
SUMMARY = {'statements': [{'text': 'The witness denied a surgery recommendation and reported therapy.',
                           'evidence_refs': [[4, 5]]}]}
GOOD = {'reviews': [{'statement_id': 1, 'verdict': 'supported', 'reason': 'The witness expressly denied it.'}]}
BAD = {'reviews': [{'statement_id': 1, 'verdict': 'unsupported', 'reason': 'The draft reverses the denial.'}]}


def run(responses, checkpoint=None, model='test', text=TEXT):
    iterator = iter(responses)
    return summarize(prepare_document('example.txt', text),
                     lambda *a, **k: json.dumps(next(iterator)),
                     checkpoint_path=checkpoint, model=model)


def test_server_retrieves_numbered_quote_without_model_copying():
    _, evidence = run([IDENTITY, SUMMARY, GOOD])
    statement = evidence['statements'][0]
    assert statement['evidence'] == ['1 Q. Was surgery recommended?\n2 A. No, I only had therapy.']
    location = statement['evidence_locations'][0]
    assert TEXT[location['start_char']:location['end_char']].strip() == statement['evidence'][0]
    assert evidence['protocol_version'] == 2
    assert evidence['support_review'] == GOOD['reviews']


def test_identity_accepts_separate_exact_passages_and_keeps_locations():
    text = TEXT + '\nSESSION DATE: July 16, 2026\nWITNESS: Jamie Example\n'
    index = TranscriptIndex(text)
    n = len(index.lines)
    data = {**IDENTITY, 'date_refs': [[3, 3], [n-1, n-1]],
            'witness_refs': [[2, 2], [n, n]]}
    result = validate_identity(data, index)
    assert result['date'] == '07/16/2026'
    assert len(result['date_locations']) == len(result['witness_locations']) == 2
    for field in ('date', 'witness'):
        for location in result[field+'_locations']:
            assert text[location['start_char']:location['end_char']].strip() in result[field+'_quote']


@pytest.mark.parametrize('field,value,refs,extra', [
    ('date', '07/17/2026', [[3, 3]], ''),
    ('witness', 'Other Person', [[2, 2]], ''),
    ('witness', 'Jamie Example', [[1, 1], [3, 3]], 'Jamie\nUnrelated line\nExample\n'),
    ('credentials', 'MD', [[2, 2]], ''),
])
def test_identity_requires_value_in_a_cited_passage(field, value, refs, extra):
    text = extra or TEXT
    data = {**IDENTITY, field: value, field+'_refs': refs}
    if extra:
        # Two unrelated ranges must not be stitched into a supported full name.
        text += 'DATE: July 16, 2026\n'
        data['date_refs'] = [[4, 4]]
    with pytest.raises(DepositionReviewRequired):
        validate_identity(data, TranscriptIndex(text))


@pytest.mark.parametrize('refs', [[], [[0, 4]], [[4, 999]], [[5, 4]], [['4', 5]], [[True, 5]], [[4]], None])
def test_invalid_references_cannot_be_resolved(refs):
    with pytest.raises(EvidenceError):
        TranscriptIndex(TEXT).resolve(refs)


def test_cannot_cite_unseen_section():
    index = TranscriptIndex(TEXT)
    with pytest.raises(EvidenceError, match='outside the supplied'):
        index.resolve([[4, 5]], (0, 10))


def test_invalid_ref_corrected_once_and_diagnostic_kept(tmp_path):
    path = tmp_path / 'review.json'
    bad = {'statements': [{'text': 'The witness reported therapy.', 'evidence_refs': [[500, 501]]}]}
    run([IDENTITY, bad, SUMMARY, GOOD], path)
    state = json.loads(path.read_text())
    assert len(state['rejections']) == 1
    assert state['rejections'][0]['stage'] == 'summary'
    assert '500' in state['rejections'][0]['response']
    assert 'blocked_stage' not in state
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bad_ref_blocks_then_resume_reuses_identity(tmp_path):
    path = tmp_path / 'review.json'
    bad = {'statements': [{'text': 'The witness reported therapy.', 'evidence_refs': [[500, 501]]}]}
    with pytest.raises(DepositionReviewRequired, match='after two attempts'):
        run([IDENTITY, bad, bad], path)
    state = json.loads(path.read_text())
    assert state['blocked_stage'] == 'summary'
    assert list(state['stages']) == ['identity']
    # No identity response is supplied: a restart must use its checked checkpoint.
    run([SUMMARY, GOOD], path)
    run([], path)  # Completed stages also survive another restart.


def test_model_or_source_change_invalidates_stage_cache(tmp_path):
    path = tmp_path / 'review.json'
    run([IDENTITY, SUMMARY, GOOD], path)
    before = json.loads(path.read_text())['signature']
    run([IDENTITY, SUMMARY, GOOD], path, model='other-model')
    after = json.loads(path.read_text())['signature']
    assert before != after
    run([IDENTITY, SUMMARY, GOOD], path, model='other-model', text=TEXT+'\nEND')
    assert json.loads(path.read_text())['signature'] != after


def test_real_quote_does_not_allow_reversed_meaning(tmp_path):
    path = tmp_path / 'review.json'
    false = {'statements': [{'text': 'The witness confirmed surgery was recommended.', 'evidence_refs': [[4, 5]]}]}
    with pytest.raises(DepositionReviewRequired, match='still has unsupported'):
        run([IDENTITY, false, BAD, false, BAD], path)
    data = json.loads(path.read_text())
    assert data['blocked_stage'] == 'support review 2'
    assert data['support_issues'] == BAD['reviews']
    # A repeat run must not ask the model repeatedly for a favorable verdict.
    with pytest.raises(DepositionReviewRequired, match='still has unsupported'):
        run([], path)


def test_support_failure_repairs_summary_then_checks_repair():
    false = {'statements': [{'text': 'Surgery was recommended.', 'evidence_refs': [[4, 5]]}]}
    entry, evidence = run([IDENTITY, false, BAD, SUMMARY, GOOD])
    assert 'denied a surgery recommendation' in entry
    assert evidence['support_review'] == GOOD['reviews']


def test_incomplete_support_review_cannot_pass():
    missing = {'reviews': []}
    with pytest.raises(DepositionReviewRequired, match='support review 1'):
        run([IDENTITY, SUMMARY, missing, missing])


def test_long_transcript_resume_retains_completed_sections(tmp_path):
    text = TEXT + ('Q. How do you feel?\nA. Sometimes sore.\n' * 4000)
    doc = prepare_document('example.txt', text)
    path, calls = tmp_path / 'review.json', []
    def fail(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps(IDENTITY)
        if len(calls) == 2:
            return json.dumps(SUMMARY)
        raise RuntimeError('simulated network interruption')
    with pytest.raises(RuntimeError, match='network interruption'):
        summarize(doc, fail, checkpoint_path=path, model='test')
    assert len(json.loads(path.read_text())['stages']) == 2
    resumed = []
    def resume(prompt, **kwargs):
        resumed.append(prompt)
        if 'Extract relevant testimony from this slice' in prompt:
            return json.dumps({'statements': []})
        if 'Audit each summary sentence' in prompt:
            return json.dumps(GOOD)
        return json.dumps(SUMMARY)
    summarize(doc, resume, checkpoint_path=path, model='test')
    assert 'Extract relevant testimony from this slice' in resumed[0]
    assert 'L000001:' not in resumed[0]


def test_synthesis_cannot_cite_lines_not_in_its_evidence():
    from src.deposition import resolve_statements
    data = {'statements': [{'text': 'The witness reported pain.', 'evidence_refs': [[6, 7]]}]}
    with pytest.raises(DepositionReviewRequired, match='not included'):
        resolve_statements(data, TranscriptIndex(TEXT), available={4, 5})


def test_old_deposition_rechecked_without_repeating_clinical_batch(tmp_path, monkeypatch):
    import logging
    from src.chronology_agent import ChronologyAgent
    monkeypatch.setattr('src.chronology_agent.time.sleep', lambda _: None)
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger, a.model = logging.getLogger('synthetic'), 'test'
    a._read_extracted_files = lambda _: [
        {'filename': 'clinic.txt', 'content': '01/01/2026. Clinic. Follow-up. Back pain.'},
        prepare_document('example.txt', TEXT)]
    a._process_scoped_batch = lambda *a, **k: ('01/01/2026. Clinic. Follow-up. Back pain.', [])
    replies = iter([IDENTITY, SUMMARY, GOOD])
    a._call_api_with_retry = lambda *a, **k: json.dumps(next(replies))
    a.generate_batches('', str(tmp_path))
    clinical = (tmp_path / 'batch_001.md').read_bytes()
    evidence = tmp_path / 'batch_002.deposition.json'
    saved = json.loads(evidence.read_text()); saved.pop('protocol_version')
    evidence.write_text(json.dumps(saved))
    (tmp_path / 'batch_002.deposition-work.json').unlink()
    (tmp_path / 'batch_002.md').write_text('OLD DEPOSITION')
    a._process_scoped_batch = lambda *a: pytest.fail('Clinical batch must be preserved')
    replies = iter([IDENTITY, SUMMARY, GOOD])
    result = a.generate_batches('', str(tmp_path))
    assert result['batches_skipped_from_disk'] == 1
    assert (tmp_path / 'batch_001.md').read_bytes() == clinical
    assert 'OLD DEPOSITION' not in (tmp_path / 'batch_002.md').read_text()
    assert json.loads(evidence.read_text())['protocol_version'] == 2


def test_response_limit_preserves_stages_and_private_diagnostics_on_resume(tmp_path):
    from tests.test_model_adapters import agent, claude_response
    from src.deposition_evidence import StageRunner
    from src.response_recovery import IncompleteResponseError
    a = agent('anthropic', claude_response('max_tokens', '{"cut":'))
    a.model = 'claude-opus-5'
    doc = prepare_document('example.txt', TEXT)
    path = tmp_path / 'checkpoint.json'
    runner = StageRunner(doc, a.model, a._call_api_with_retry, path)
    runner.state['stages'] = {'identity': IDENTITY, 'section 1 of 7': {'statements': []}}
    runner.save()
    with pytest.raises(IncompleteResponseError):
        runner.ask('section 2 of 7', 'synthetic source', lambda data: data, 6000)
    saved = json.loads(path.read_text())
    assert list(saved['stages']) == ['identity', 'section 1 of 7']
    assert 'blocked_stage' not in saved
    assert saved['response_error']['stage'] == 'section 2 of 7'
    assert len(saved['response_diagnostics']) == 2
    assert all(d['stage'] == 'section 2 of 7' for d in saved['response_diagnostics'])
    assert path.stat().st_mode & 0o777 == 0o600
    a.client.messages.create.return_value = claude_response('end_turn', '{"statements": []}')
    runner = StageRunner(doc, a.model, a._call_api_with_retry, path)
    runner.ask('section 2 of 7', 'synthetic source', lambda data: data, 6000)
    saved = json.loads(path.read_text())
    assert 'response_error' not in saved
    assert len(saved['stages']) == 3
    assert len(saved['response_diagnostics']) == 2


def test_recovered_response_still_must_pass_evidence_validation(tmp_path):
    from tests.test_model_adapters import agent, claude_response
    from src.deposition_evidence import StageRunner
    a = agent('anthropic', None)
    a.client.messages.create.side_effect = [claude_response('max_tokens'),
        claude_response('end_turn', '{"date": "unsupported"}'),
        claude_response('end_turn', '{"date": "unsupported"}')]
    runner = StageRunner(prepare_document('example.txt', TEXT), a.model,
        a._call_api_with_retry, tmp_path / 'checkpoint.json')
    with pytest.raises(EvidenceError):
        runner.ask('identity', 'source', lambda data: validate_identity(data, TranscriptIndex(TEXT)), 2000)
    assert runner.state['blocked_stage'] == 'identity'
    assert not runner.state['stages']
    assert len(runner.state['rejections']) == 2
