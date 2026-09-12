"""Synthetic large deposition through consolidation, source audits and export."""
import copy
import json
from zipfile import ZipFile

import pytest

from tests.test_depositions import agent
from src.deposition import prepare_document, source_chunks, resolve_statements, validate_identity, validate_support_review, DepositionReviewRequired
from src.deposition_evidence import StageRunner, TranscriptIndex, EvidenceError
from src.deposition_synthesis import evidence_packs, validate_audit

TEXT = ('IN THE CIRCUIT COURT\nDEPOSITION OF Jamie Example\nDATE: July 16, 2026\n' +
        'Q. Did you have surgery or pain?\nA. No surgery. I sometimes recall pain.\n' * 1900)
IDENTITY = {'date': '07/16/2026', 'witness': 'Jamie Example', 'credentials': '',
            'date_refs': [[3, 3]], 'witness_refs': [[2, 2]], 'credentials_refs': []}
SENTENCE = 'The witness denied surgery and recalled pain sometimes.'


def saved_large_run(tmp_path, empty=False):
    a = agent()
    source, batches, output = [tmp_path / n for n in ('source', 'batches', 'output')]
    source.mkdir()
    path = source / 'witness.txt'
    path.write_text(TEXT)
    document = prepare_document('witness.txt', TEXT)
    index = TranscriptIndex(document['content'])
    runner = StageRunner(document, a.model, None, batches / 'batch_001.deposition-work.json')
    runner.state['stages']['identity'] = validate_identity(copy.deepcopy(IDENTITY), index)
    offset, all_notes = 0, []
    chunks = source_chunks(document)
    for number, part in enumerate(chunks, 1):
        end = offset + len(part['content'])
        first = next(i+1 for i, pos in enumerate(index.offsets) if pos >= offset) + 4
        data = {'statements': [] if empty else [
            {'text': SENTENCE, 'evidence_refs': [[first, first+90]]} for _ in range(27)]}
        resolve_statements(data, index, (offset, end), allow_empty=True)
        runner.state['stages'][f'section {number} of {len(chunks)}'] = data
        all_notes.extend(data['statements'])
        offset = end
    runner.save()
    return a, source, batches, output, copy.deepcopy(runner.state['stages']), all_notes


def model(calls, missing=False, interrupt_at=None):
    def call(prompt, **kwargs):
        calls.append(prompt)
        assert len(prompt) < 160000
        assert 'Extract relevant testimony from this slice' not in prompt
        assert "Extract the deposition's actual session date" not in prompt
        pack = json.loads(prompt.split('ORIGINAL EVIDENCE GROUP:\n')[1])
        if interrupt_at and len(calls) == interrupt_at:
            raise RuntimeError('simulated interruption')
        if 'BOUNDED DEPOSITION CONSOLIDATION' in prompt:
            return json.dumps({'statements': [{'text': SENTENCE, 'evidence_refs': pack[0]['evidence_refs']}]})
        assert 'FINAL DEPOSITION EVIDENCE AND COVERAGE AUDIT' in prompt
        return json.dumps({'reviews': [{'statement_id': 1, 'verdict': 'supported', 'reason': 'Denial and uncertainty preserved.'}],
            'coverage': [{'source_note_id': n['source_note_id'], 'verdict': 'missing' if missing else 'covered',
                         'summary_statement_ids': [] if missing else [1],
                         'reason': 'Material qualifier omitted.' if missing else 'Same qualified testimony retained.'} for n in pack]})
    return call


def test_large_saved_deposition_reaches_word_export_with_every_note_audited(tmp_path):
    a, source, batches, output, original, notes = saved_large_run(tmp_path)
    assert len(json.dumps(notes)) > 160000
    calls = []
    a._call_api_with_retry = model(calls)
    assert a.generate_batches(str(source), str(batches))['success']
    checked = json.loads((batches / 'batch_001.deposition-work.json').read_text())
    assert all(checked['stages'][k] == v for k, v in original.items())
    evidence = json.loads((batches / 'batch_001.deposition.json').read_text())
    coverage = [c['source_note_id'] for audit in evidence['aggregation']['audits'] for c in audit['coverage']]
    assert sorted(coverage) == list(range(1, len(notes)+1))
    assert len(calls) == 2 * evidence['aggregation']['source_groups']
    assert evidence['aggregation']['source_groups'] > 1
    a.extract_header = lambda *args: {'patient_name': 'JAMIE EXAMPLE', 'date_of_birth': '[See Records]', 'date_of_injury': '[See Records]'}
    a.generate_summary_and_gaps = lambda *args: {'summary_md': 'Summary.', 'gaps_md': 'Source review remains distinct.'}
    a.assemble_outputs(str(source), str(batches), str(output))
    data = json.loads((output / 'chronology.json').read_text())
    assert data['chronology_markdown'].count('07/16/2026. Jamie Example, Deposition.') == 1
    assert SENTENCE in data['chronology_markdown']
    assert data['deposition_evidence'][0]['aggregation']['source_notes'] == len(notes)
    with ZipFile(output / 'chronology.docx') as word:
        assert SENTENCE in word.read('word/document.xml').decode()
    a._call_api_with_retry = lambda *args, **kwargs: pytest.fail('Completed batch must be reused')
    assert a.generate_batches(str(source), str(batches))['batches_skipped_from_disk'] == 1
    assert (source / 'witness.txt').read_text() == TEXT


def test_aggregation_resume_reuses_completed_groups(tmp_path):
    a, source, batches, output, original, notes = saved_large_run(tmp_path)
    calls = []
    a._call_api_with_retry = model(calls, interrupt_at=2)
    with pytest.raises(RuntimeError, match='interruption'):
        a.generate_batches(str(source), str(batches))
    resumed = []
    a._call_api_with_retry = model(resumed)
    a.generate_batches(str(source), str(batches))
    assert calls[0] not in resumed
    checked = json.loads((batches / 'batch_001.deposition-work.json').read_text())
    assert all(checked['stages'][k] == v for k, v in original.items())


def test_missing_material_gets_one_repair_then_requires_reviewer(tmp_path):
    a, source, batches, output, original, notes = saved_large_run(tmp_path)
    calls = []
    a._call_api_with_retry = model(calls, missing=True)
    with pytest.raises(DepositionReviewRequired, match='reviewer must resolve'):
        a.generate_batches(str(source), str(batches))
    assert not (batches / 'batch_001.md').exists()
    checked = json.loads((batches / 'batch_001.deposition-work.json').read_text())
    assert checked['blocked_stage'] == 'aggregation evidence and coverage'
    assert len(calls) == 4 * checked['aggregation']['source_groups']
    a._call_api_with_retry = lambda *args, **kwargs: pytest.fail('No unchanged substantive retry')
    with pytest.raises(DepositionReviewRequired, match='Needs review'):
        a.generate_batches(str(source), str(batches))


def test_empty_extraction_is_distinct_from_size_limit(tmp_path):
    a, source, batches, output, original, notes = saved_large_run(tmp_path, empty=True)
    a._call_api_with_retry = lambda *args, **kwargs: pytest.fail('No API call for empty extraction')
    with pytest.raises(DepositionReviewRequired, match='No substantive testimony'):
        a.generate_batches(str(source), str(batches))
    checked = json.loads((batches / 'batch_001.deposition-work.json').read_text())
    assert checked['blocked_stage'] == 'empty testimony extraction'


def test_pack_construction_is_lossless_and_audit_cannot_skip_or_duplicate_notes():
    notes = [{'text': 'Denied surgery; pain only sometimes.', 'evidence_refs': [[1, 2]],
              'evidence': ['Q. Surgery? A. No; pain sometimes.']} for _ in range(8)]
    packs = evidence_packs(notes, limit=500)
    assert [n['source_note_id'] for p in packs for n in p] == list(range(1, 9))
    assert all(n['evidence'] == notes[0]['evidence'] for p in packs for n in p)
    pack = packs[0]
    result = {'reviews': [{'statement_id': 1, 'verdict': 'supported', 'reason': 'Matched.'}],
              'coverage': [{'source_note_id': pack[0]['source_note_id'], 'verdict': 'covered',
                            'summary_statement_ids': [1], 'reason': 'Matched.'} for _ in pack]}
    with pytest.raises(EvidenceError, match='repeated or omitted'):
        validate_audit(result, [{'text': 'summary'}], pack, validate_support_review)
