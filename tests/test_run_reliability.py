from pathlib import Path
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from src.ocr_client import OCRClient
from src.ocr_coverage import collect_coverage, save_coverage
from src.pipeline import MedicalChronologyPipeline
from src.session_state import (SessionStore, PHASE_DOWNLOAD, PHASE_OCR, PHASE_GENERATE,
                               STATUS_COMPLETE, STATUS_FAILED)


def pipeline_at(tmp_path):
    pipeline = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    pipeline.store = SessionStore(str(tmp_path))
    pipeline.logger = logging.getLogger('synthetic-test')
    pipeline.chronology_agent = SimpleNamespace(model='configured-test-model')
    state = pipeline.store.create(session_id='synthetic_test', patient_id='synthetic',
                                  dropbox_link='', destination_folder='/test-output')
    return pipeline, state


def confirm_inventory(pipeline, state):
    import hashlib
    root = pipeline.store.input_dir(state.session_id)
    manifest = [{'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size,
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in root.rglob('*.pdf')]
    pipeline.store.update_phase_data(state, PHASE_DOWNLOAD, {'manifest_version': 1, 'manifest': manifest})


def test_ocr_distinguishes_blank_from_failed_page(monkeypatch):
    monkeypatch.setattr('pdf2image.pdf2image.pdfinfo_from_path', lambda _: {'Pages': 4})
    monkeypatch.setattr('src.ocr_client.convert_from_path', lambda *a, **k: [object()])
    client = OCRClient('not-a-real-key')
    client._image_to_base64 = lambda _: 'synthetic'
    replies = iter([{'success': True, 'text': 'Record'}, {'success': True, 'text': ''},
                    {'success': False, 'text': '', 'error': 'Service failure'},
                    {'success': True, 'text': 'Final page'}])
    client._extract_text_from_image = lambda *a, **k: next(replies)
    messages = []
    result = client.extract_text('test.pdf', progress_callback=messages.append)
    assert [p['status'] for p in result['page_results']] == ['text', 'no_text', 'error', 'text']
    assert 'SOURCE PDF PAGE 4' in result['text']
    assert messages[-1].startswith('⚠️')
    assert '2/4' in messages[-1]


def test_expected_page_conversion_failure_does_not_truncate_later_pages(monkeypatch):
    monkeypatch.setattr('pdf2image.pdf2image.pdfinfo_from_path', lambda _: {'Pages': 3})
    def convert(*a, **kw):
        if kw['first_page'] == 2:
            raise RuntimeError('invalid page conversion')
        return [object()]
    monkeypatch.setattr('src.ocr_client.convert_from_path', convert)
    client = OCRClient('not-a-real-key')
    client._image_to_base64 = lambda _: 'synthetic'
    client._extract_text_from_image = lambda *a, **k: {'success': True, 'text': 'Test text'}
    result = client.extract_text('test.pdf')
    assert result['page_results'][1]['status'] == 'error'
    assert 'SOURCE PDF PAGE 3' in result['text']


def test_unknown_page_count_does_not_guess_one_hundred(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('Unreadable PDF')
    monkeypatch.setattr('pdf2image.pdf2image.pdfinfo_from_path', broken)
    monkeypatch.setattr('src.ocr_client.convert_from_path', broken)
    result = OCRClient('not-a-real-key').extract_text('test.pdf')
    assert not result['success']
    assert 'page count' in result['error']
    assert 'page_count' not in result


def test_legacy_coverage_does_not_invent_denominator(tmp_path):
    source, extracted = tmp_path / 'source', tmp_path / 'extracted'
    source.mkdir(); extracted.mkdir()
    (source / 'test.pdf').write_bytes(b'placeholder')
    (extracted / 'test.txt').write_text('=== SOURCE PDF PAGE 1 ===\nText\n=== SOURCE PDF PAGE 17 ===\nEnd')
    report = collect_coverage(source, extracted)
    item = report['files'][0]
    assert item['text_pages'] == 2
    assert item['total_pages'] is None
    assert item['coverage_status'] == 'unknown_legacy'
    assert report['files_needing_review'] == 1
    assert report['technical_failures'] == 0


def test_blank_page_coverage_requires_review_without_calling_it_service_failure(tmp_path):
    source, extracted = tmp_path / 'source', tmp_path / 'extracted'
    source.mkdir()
    pdf = source / 'test.pdf'; pdf.write_bytes(b'placeholder')
    extracted.mkdir(exist_ok=True)
    (extracted / 'test.txt').write_text('Synthetic text')
    save_coverage({'page_count': 2, 'page_results': [{'page': 1, 'status': 'text'},
                                                    {'page': 2, 'status': 'no_text'}]},
                  pdf, source, extracted)
    report = collect_coverage(source, extracted)
    assert report['files'][0]['no_text_pages'] == [2]
    assert report['files_needing_review'] == 1
    assert report['technical_failures'] == 0


def test_failed_generation_marks_phase_failed_and_names_correct_model(tmp_path):
    pipeline, state = pipeline_at(tmp_path)
    root = pipeline.store.input_dir(state.session_id)
    extracted = pipeline.store.extracted_dir(state.session_id)
    pdf = root / 'test.pdf'; pdf.write_bytes(b'placeholder')
    (extracted / 'test.txt').write_text('Synthetic text')
    save_coverage({'page_count': 1, 'page_results': [{'page': 1, 'status': 'text'}]}, pdf, root, extracted)
    confirm_inventory(pipeline, state)
    for phase in (PHASE_DOWNLOAD, PHASE_OCR):
        pipeline.store.mark_phase(state, phase, STATUS_COMPLETE)
    async def fail(*args):
        raise RuntimeError('Evidence review required')
    pipeline._phase_generate = fail
    messages = []
    result = asyncio.run(pipeline.run(state.session_id, messages.append))
    assert result['status'] == STATUS_FAILED
    saved = pipeline.store.load(state.session_id)
    assert saved.phases[PHASE_GENERATE].status == STATUS_FAILED
    assert saved.phases[PHASE_GENERATE].error == 'Evidence review required'
    assert any('configured-test-model' in m for m in messages)
    assert not any('Claude' in m for m in messages)


def test_partial_ocr_failure_blocks_generation_and_resume_retries_only_failed_file(tmp_path):
    pipeline, state = pipeline_at(tmp_path)
    source = pipeline.store.input_dir(state.session_id)
    extracted = pipeline.store.extracted_dir(state.session_id)
    for name in ('good', 'partial'):
        (source / f'{name}.pdf').write_bytes(b'placeholder')
    confirm_inventory(pipeline, state)
    pipeline.store.mark_phase(state, PHASE_DOWNLOAD, STATUS_COMPLETE)
    calls = []
    class FakeOCR(OCRClient):
        async def batch_extract(self, paths, **kwargs):
            calls.append(paths)
            return [{'success': True, 'source_path': path, 'file_name': path.split('/')[-1],
                     'text': '=== SOURCE PDF PAGE 1 ===\nSynthetic text', 'page_count': 2,
                     'page_results': [{'page': 1, 'status': 'text'},
                                     {'page': 2, 'status': 'error' if path.endswith('partial.pdf') and len(calls) == 2 else 'text'}]}
                    for path in paths]
    pipeline.ocr_client = FakeOCR('not-a-real-key')
    result = asyncio.run(pipeline.run(state.session_id))
    assert result['status'] == STATUS_FAILED
    saved = pipeline.store.load(state.session_id)
    assert saved.phases[PHASE_OCR].status == STATUS_FAILED
    assert saved.phases[PHASE_GENERATE].status == 'pending'
    asyncio.run(pipeline._phase_ocr(saved, lambda _: None))
    assert len(calls) == 3 and calls[2][0].endswith('partial.pdf')
    assert collect_coverage(source, extracted)['technical_failures'] == 0


def test_empty_retry_cannot_reuse_stale_partial_text(tmp_path):
    pipeline, state = pipeline_at(tmp_path)
    source = pipeline.store.input_dir(state.session_id)
    extracted = pipeline.store.extracted_dir(state.session_id)
    pdf = source / 'partial.pdf'; pdf.write_bytes(b'placeholder')
    txt = extracted / 'partial.txt'; txt.write_text('Stale partial extraction')
    save_coverage({'page_count': 1, 'page_results': [{'page': 1, 'status': 'error'}]}, pdf, source, extracted)
    class FakeOCR:
        async def batch_extract(self, *a, **k):
            return [{'success': False, 'page_count': 1, 'page_results': [{'page': 1, 'status': 'no_text'}]}]
    pipeline.ocr_client = FakeOCR()
    with pytest.raises(RuntimeError, match='No text'):
        asyncio.run(pipeline._phase_ocr(state, lambda _: None))
    assert not txt.exists()


def test_ocr_checkpoints_first_file_before_interruption_on_second(tmp_path):
    pipeline, state = pipeline_at(tmp_path)
    source = pipeline.store.input_dir(state.session_id)
    extracted = pipeline.store.extracted_dir(state.session_id)
    for name in ('first', 'second'):
        (source / f'{name}.pdf').write_bytes(b'placeholder')
    class InterruptedOCR(OCRClient):
        async def batch_extract(self, paths, **kw):
            if paths[0].endswith('second.pdf'):
                raise RuntimeError('simulated interruption')
            return [{'success': True, 'source_path': paths[0], 'file_name': 'first.pdf',
                     'text': 'Synthetic text', 'page_count': 1,
                     'page_results': [{'page': 1, 'status': 'text'}]}]
    pipeline.ocr_client = InterruptedOCR('not-a-real-key')
    with pytest.raises(RuntimeError, match='interruption'):
        asyncio.run(pipeline._phase_ocr(state, lambda _: None))
    assert (extracted / 'first.txt').read_text() == 'Synthetic text'
    assert (extracted / 'first.ocr.json').exists()


def test_failed_session_ui_shows_review_downloads_and_resume(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    pipeline, state = pipeline_at(tmp_path)
    pipeline.store.mark_phase(state, PHASE_GENERATE, STATUS_FAILED, error='Evidence needs review')
    pipeline.store.update_phase_data(state, PHASE_OCR, {'coverage': {
        'files_needing_review': 1, 'technical_failures': 0, 'files': []}})
    (pipeline.store.batches_dir(state.session_id) / 'batch_002.deposition-work.json').write_text(
        json.dumps({'source_file': 'synthetic.txt', 'blocked_stage': 'summary'}))
    (pipeline.store.batches_dir(state.session_id) / 'batch_004.scope-work.json').write_text(
        json.dumps({'status': 'blocked', 'sources': [{'id': 'D001', 'filename': 'synthetic.txt'}],
                    'attempts': [{'error': 'Source classification needs review',
                                  'details': [{'id': 'D001', 'reason': 'Missing attachment'}]}]}))
    def init(self, **kwargs):
        self.store, self.logger = pipeline.store, pipeline.logger
        self.chronology_agent = pipeline.chronology_agent
    monkeypatch.setattr(MedicalChronologyPipeline, '__init__', init)
    for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN',
                'GOOGLE_CLOUD_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.setenv(key, 'synthetic-not-a-real-key')
    monkeypatch.setenv('TEAM_PASSWORD', 'synthetic-team-password')
    st.cache_resource.clear()
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    app.query_params['session_id'] = state.session_id
    app.run()
    app.text_input[0].set_value('synthetic-team-password')
    app.button[0].click().run()
    assert not app.exception
    assert any('unknown OCR coverage' in w.value for w in app.warning)
    labels = [element.proto.label for element in app.get('download_button')]
    assert 'Download page coverage report' in labels
    assert any('deposition review details' in label for label in labels)
    assert any('source-screening review details' in label for label in labels)
    assert any('Source-screening review' in e.label for e in app.expander)
    assert any('Run / Resume' in b.label and not b.disabled for b in app.button)
    st.cache_resource.clear()

