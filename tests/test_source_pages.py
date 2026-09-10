"""Large fabricated records regress source boundaries without patient fixtures."""
import json
import logging

import pytest

from src.chronology_agent import ChronologyAgent
from src.session_model import batch_signature, saved_model, GENERATION_VERSION
from src.source_fidelity import render_diagnostic, DiagnosticEvidenceError
from src.source_pages import chunk_source, page_units, source_prompt_text


def agent():
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger = logging.getLogger('synthetic-page-test')
    a.model = 'synthetic-model'
    a._call_api_with_retry = lambda *a, **k: pytest.fail('No provider calls')
    return a


def page(number, date, *, padding=True, attribution=True):
    return (f'=== SOURCE PDF PAGE {number} ===\nDate of service: {date}\n' +
            ('Facility: Synthetic Imaging\nProvider: Example, MD\n' if attribution else '') +
            'Study: Lumbar MRI\nHistory: ' +
            ('Synthetic filler only. ' * 600 if padding else 'Synthetic record.') +
            '\nImpression: No acute fracture.\n')


def entry(date='01/12/2026'):
    return {'record_type': 'diagnostic_test', 'source_ids': ['D001'],
            'diagnostic_result': {'date': date, 'facility': 'Synthetic Imaging',
                'provider': 'Example, MD', 'study': 'Lumbar MRI',
                'evidence': [{'source_id': 'D001', 'date_quote': f'Date of service: {date}',
                              'quote': 'Impression: No acute fracture.'}]}}


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_large_record_keeps_exact_text_whole_pages_and_diagnostic_support(tmp_path, newline):
    source = (page(1, '01/12/2026') + page(2, '01/19/2026')).replace('\n', newline)
    assert len(source) > 20000
    chunks = agent()._chunk_large_document('nested/synthetic.txt', source, max_chunk_chars=20000)
    assert len(chunks) == 2
    assert ''.join(c['content'] for c in chunks) == source
    for number, (chunk, date) in enumerate(zip(chunks, ['01/12/2026', '01/19/2026']), 1):
        assert chunk['source_pages'] == [number]
        assert not chunk['incomplete_page']
        assert len(chunk['content']) <= 20000
        assert len(chunk['content'].splitlines()) > 3
        assert render_diagnostic(entry(date), {'D001': chunk}).startswith(date)
    # Also exercise the actual extracted-file reader's 20,000-character limit.
    (tmp_path / 'synthetic.txt').write_text(source)
    loaded = agent()._read_extracted_files(str(tmp_path))
    assert len(loaded) == 2 and all(c['source_file'] == 'synthetic.txt' for c in loaded)
    assert [c['source_pages'] for c in loaded] == [[1], [2]]


def test_oversized_page_fragments_are_bounded_and_never_complete_evidence():
    source = page(8, '01/12/2026', padding=False) + 'Notes: ' + 'x' * 45000
    chunks = chunk_source('oversized.txt', source, 20000)
    assert len(chunks) > 1
    assert ''.join(c['content'] for c in chunks) == source
    assert all(c['incomplete_page'] and c['source_pages'] == [8] for c in chunks)
    assert all(len(c['content']) <= 20000 for c in chunks)
    # Even a fragment containing a plausible full result cannot establish the
    # completeness of the original oversized page.
    for chunk in chunks:
        with pytest.raises(DiagnosticEvidenceError, match='part of a page'):
            render_diagnostic(entry(), {'D001': chunk})


def test_carried_header_can_attribute_but_never_supply_a_date_or_result():
    source = page(1, '01/12/2026') + page(2, '01/19/2026', attribution=False)
    continuation = chunk_source('synthetic.txt', source, 20000)[1]
    assert 'Date of service: 01/12/2026' in continuation['reference_context']
    assert render_diagnostic(entry('01/19/2026'), {'D001': continuation})
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry('01/12/2026'), {'D001': continuation})
    continuation['content'] = continuation['content'].replace('Impression: No acute fracture.', '')
    continuation['reference_context'] += '\nImpression: No acute fracture.'
    with pytest.raises(DiagnosticEvidenceError):
        render_diagnostic(entry('01/19/2026'), {'D001': continuation})


@pytest.mark.parametrize('source', ['x' * 301, ('line\n' * 101), 'a\f' + 'b' * 150, ''])
def test_unmarked_or_long_line_chunks_never_drop_or_reorder_text(source):
    chunks = chunk_source('synthetic.txt', source, 100)
    assert ''.join(c['content'] for c in chunks) == source
    assert all(len(c['content']) <= 100 for c in chunks)
    assert all(c['source_pages'] == [] for c in chunks)


def test_small_formfeed_pages_remain_separate_without_invented_page_numbers():
    source = 'First page.\fSecond page.'
    units = list(page_units(source))
    assert len(units) == 2 and all(number is None for number, _ in units)
    assert ''.join(text for _, text in units) == source


@pytest.mark.parametrize('version', ['source-bound-diagnostics-v4', 'same-day-care-v3'])
def test_real_legacy_large_file_model_recovery_does_not_reuse_or_modify_batches(tmp_path, version):
    sources, batches = tmp_path / 'sources', tmp_path / 'batches'
    sources.mkdir(); batches.mkdir()
    (sources / 'synthetic.txt').write_text(page(1, '01/12/2026') + page(2, '01/19/2026'))
    a = agent()
    old = a._read_legacy_extracted_files(str(sources))
    signature = batch_signature(a._plan_batches(old), a._source_exclusions, 'saved-synthetic-model', version=version)
    (batches / 'batch_manifest.json').write_text(json.dumps({'signature': signature,
                                                           'model': 'saved-synthetic-model'}))
    (batches / 'batch_001.md').write_text('Preserved old draft')
    before = {p.name: p.read_bytes() for p in batches.iterdir()}
    assert saved_model(a, sources, batches, [], persist=False) == 'saved-synthetic-model'
    assert {p.name: p.read_bytes() for p in batches.iterdir()} == before
    with pytest.raises(ValueError, match='summary rules'):
        a.generate_batches(str(sources), str(batches))
    assert {p.name: p.read_bytes() for p in batches.iterdir()} == before
    new = a._read_extracted_files(str(sources))
    assert signature != batch_signature(a._plan_batches(new), a._source_exclusions,
                                        'saved-synthetic-model', version=GENERATION_VERSION)


def test_pinned_model_recovery_never_reinterprets_source_layout(tmp_path):
    (tmp_path / 'run_model.json').write_text(json.dumps({'model': 'saved-synthetic-model'}))
    a = agent()
    a._read_extracted_files = lambda *a: pytest.fail('Pin is already saved')
    a._read_legacy_extracted_files = a._read_extracted_files
    assert saved_model(a, tmp_path, tmp_path, [], persist=False) == 'saved-synthetic-model'


def test_generation_and_verification_keep_attribution_separate_from_evidence(monkeypatch):
    doc = {'filename': 'synthetic.txt (part 2)', 'content': 'Partial synthetic page.',
           'reference_context': 'Facility: Synthetic Imaging\nDate: 01/12/2026',
           'incomplete_page': True}
    a, prompts = agent(), []
    def screen(prompt, *args, **kwargs):
        prompts.append(prompt)
        return '', []
    monkeypatch.setattr('src.chronology_agent.screen_batch', screen)
    a._process_scoped_batch([doc], 1, 1)
    def verify(prompt, *args, **kwargs):
        prompts.append(prompt)
        return 'Synthetic review.'
    a._call_api_with_retry = verify
    a._verify_entry_batch(['Synthetic entry.'], [doc])
    assert len(prompts) == 2
    for prompt in prompts:
        assert source_prompt_text(doc) in prompt
        assert 'ATTRIBUTION ONLY' in prompt and 'Not date/result evidence' in prompt
        assert 'PARTIAL SOURCE PAGE' in prompt


def test_batch_budget_includes_carried_reference_context():
    docs = [{'filename': f'synthetic-{i}.txt', 'content': 'x' * 20000,
             'reference_context': 'y' * 1500} for i in range(12)]
    batches = agent()._plan_batches(docs)
    assert [len(batch) for batch in batches] == [11, 1]
    assert all(sum(len(source_prompt_text(doc)) // 4 for doc in batch) <= 60000 for batch in batches)


def test_verification_date_mapping_uses_source_body_not_carried_context():
    doc = {'filename': 'synthetic.txt',
           'content': 'Study Date: 2026-01-19T09:00:00Z\nExam Date: Jan 20, 2026',
           'reference_context': 'Study Date: 01/12/2026'}
    dates = agent()._map_dates_to_documents([doc])
    assert dates == {'01/19/2026': [doc], '01/20/2026': [doc]}
