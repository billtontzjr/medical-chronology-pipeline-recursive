import asyncio
import hashlib
import io
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
from docx import Document
from dropbox.files import FileMetadata, FolderMetadata
from src.download_snapshot import download_snapshot
from src.tools.dropbox_tool import DropboxTool
from src.output_safety import validate_destination, OUTPUT_ROOT
from src.session_lock import session_lock
from src.pipeline import MedicalChronologyPipeline
from src.session_state import SessionStore, PHASE_DOWNLOAD, STATUS_COMPLETE
from src.billing import reconcile_billing, export_records
from src.word_export import chronology_docx
from src.chronology_scope import parse_scoped_response
from src.chronology_agent import ChronologyAgent


def metadata(name, data=b'%PDF synthetic', path=None):
    content_hash = hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()
    return FileMetadata(name=name, id='id:' + name, client_modified=datetime(2026, 1, 1),
                        server_modified=datetime(2026, 1, 1), rev='123456789', size=len(data),
                        path_display=path or '/Records/' + name, content_hash=content_hash)


class FakeDropbox:
    def __init__(self):
        self.calls = []
        self.fail_folder = False
        self.fail_download = None
        self.corrupt = False
    def sharing_get_shared_link_metadata(self, source):
        return SimpleNamespace(path_lower='/records')
    def files_list_folder(self, path, **kwargs):
        if path in ('', '/Records'):
            entries = [metadata('a.pdf'), FolderMetadata(name='Nested', id='id:nested')]
        else:
            if self.fail_folder:
                raise RuntimeError('nested listing failed')
            entries = [metadata('b.PdF')]
        return SimpleNamespace(entries=entries, has_more=False)
    def sharing_get_shared_link_file(self, source, path=''):
        self.calls.append(path)
        if self.fail_download and path.endswith(self.fail_download):
            raise RuntimeError('interrupted')
        return None, SimpleNamespace(content=b'bad' if self.corrupt else b'%PDF synthetic')
    def files_download(self, path):
        return self.sharing_get_shared_link_file('', path)


def test_nested_failure_blocks_before_any_download(tmp_path):
    dbx = FakeDropbox(); dbx.fail_folder = True
    with pytest.raises(RuntimeError, match='nested listing'):
        download_snapshot(SimpleNamespace(dbx=dbx), 'https://www.dropbox.com/scl/fo/test', tmp_path)
    assert dbx.calls == []


def test_interrupted_download_resumes_verified_files_and_preserves_nested_case(tmp_path):
    dbx = FakeDropbox(); dbx.fail_download = 'a.pdf'
    tool = SimpleNamespace(dbx=dbx)
    with pytest.raises(RuntimeError, match='interrupted'):
        download_snapshot(tool, '/Records', tmp_path)
    assert (tmp_path / 'Nested/b.PdF').exists()
    dbx.fail_download = None
    result = download_snapshot(tool, '/Records', tmp_path)
    assert [f['path'] for f in result] == ['Nested/b.PdF', 'a.pdf']
    assert dbx.calls.count('/Records/Nested/b.PdF') == 1
    assert json.loads((tmp_path / 'source-inventory.json').read_text())['complete']


def test_corrupt_download_never_commits_snapshot(tmp_path):
    dbx = FakeDropbox(); dbx.corrupt = True
    with pytest.raises(RuntimeError, match='size differs'):
        download_snapshot(SimpleNamespace(dbx=dbx), '/Records', tmp_path)
    assert not json.loads((tmp_path / 'source-inventory.json').read_text())['complete']


def test_remote_inventory_change_blocks_completion(tmp_path):
    dbx = FakeDropbox()
    original = dbx.files_list_folder
    def changing(path, **kw):
        result = original(path, **kw)
        if len(dbx.calls) >= 2 and path == '/Records':
            result.entries.append(metadata('new.pdf'))
        return result
    dbx.files_list_folder = changing
    with pytest.raises(RuntimeError, match='changed during download'):
        download_snapshot(SimpleNamespace(dbx=dbx), '/Records', tmp_path)


def test_home_url_preserves_top_level_folder(tmp_path):
    dbx = FakeDropbox()
    result = download_snapshot(SimpleNamespace(dbx=dbx), 'https://www.dropbox.com/home/Records', tmp_path)
    assert len(result) == 2
    assert '/Records/a.pdf' in dbx.calls


@pytest.mark.parametrize('destination', ['/Records', OUTPUT_ROOT, OUTPUT_ROOT + '/../Records',
                                         'https://attacker.invalid/home' + OUTPUT_ROOT + '/case'])
def test_output_destination_rejects_source_and_escape(destination):
    with pytest.raises(ValueError):
        validate_destination(destination, '/Records')


def test_upload_preserves_different_existing_file(tmp_path):
    path = tmp_path / 'test.md'; path.write_text('new output')
    tool = DropboxTool.__new__(DropboxTool)
    tool.dbx = SimpleNamespace(files_get_metadata=lambda _: metadata('test.md', b'original'),
                              files_upload=lambda *a, **k: pytest.fail('must not overwrite'))
    result = tool.upload_file(str(path), OUTPUT_ROOT + '/case/test.md', max_retries=1)
    assert not result['success'] and 'preserved' in result['error']


def test_same_output_upload_is_idempotent(tmp_path):
    path = tmp_path / 'test.md'; path.write_bytes(b'output')
    tool = DropboxTool.__new__(DropboxTool)
    tool.dbx = SimpleNamespace(files_get_metadata=lambda _: metadata('test.md', b'output'),
                              files_upload=lambda *a, **k: pytest.fail('no rewrite needed'))
    assert tool.upload_file(str(path), OUTPUT_ROOT + '/case/test.md')['verified']


def test_legacy_resume_and_concurrent_mutation_fail_closed(tmp_path):
    p = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    p.store = SessionStore(str(tmp_path)); p.logger = logging.getLogger('test')
    state = p.store.create(session_id='legacy', patient_id='synthetic', dropbox_link='/Records', destination_folder=OUTPUT_ROOT + '/case')
    p.store.mark_phase(state, PHASE_DOWNLOAD, STATUS_COMPLETE)
    result = asyncio.run(p.run('legacy'))
    assert 'no confirmed source inventory' in result['error']
    with session_lock(p._lock_path('legacy')):
        assert 'already being processed' in asyncio.run(p.run('legacy'))['error']
        with pytest.raises(RuntimeError, match='already being processed'):
            p.archive_session('legacy')
    p.archive_session('legacy')
    assert not p.list_sessions()
    assert (p.store.sessions_root.parent / 'archive/legacy/state.json').exists()


def test_structured_billing_survives_unknown_service_label_and_withheld_notes():
    data = {'sources': [{'id': 'D001', 'scope': 'medical'}], 'entries': [
        {'record_type': 'medical_billing', 'source_ids': ['D001'],
         'text': '07/15/2024. Clinic. Service XYZ. No clinical note available in records.'}]}
    body, _ = parse_scoped_response(json.dumps(data), [{'filename': 'billing.txt'}])
    body = reconcile_billing(body, True)
    assert 'no clinical note' not in body.lower()
    assert 'pending manual review' in body
    records = export_records(body)
    assert records[0]['record_type'] == 'medical_billing'
    # Structured metadata controls relocation even without a familiar label.
    text = '07/15/2024. Clinic. Service XYZ. Billed service.'
    doc = Document(io.BytesIO(chronology_docx(text, separate_billing=True,
                   records=[{'text': text, 'record_type': 'medical_billing'}])))
    paragraphs = [p.text for p in doc.paragraphs]
    assert paragraphs.index('Billing-only appendix') < paragraphs.index(text)


def test_verification_resumes_checkpoint_after_interruption_and_invalidates_changes(tmp_path):
    a = ChronologyAgent.__new__(ChronologyAgent); a.model = 'synthetic'; a.logger = logging.getLogger('test')
    p = tmp_path / 'draft.md'; p.write_text('01/01/2026. Clinic. Visit.\n\n01/02/2026. Clinic. Visit.')
    a._read_extracted_files = lambda _: [{'filename': 'one', 'content': '01/01/2026 record'},
                                        {'filename': 'two', 'content': '01/02/2026 record'}]
    calls = []
    def verify(entries, docs):
        calls.append(entries[0])
        if len(calls) == 2:
            raise RuntimeError('interrupted')
        return 'No issues found.'
    a._verify_entry_batch = verify
    checkpoint = tmp_path / 'verification-work.json'
    assert not a.verify_chronology(str(p), str(tmp_path), checkpoint_path=checkpoint)['success']
    assert len(json.loads(checkpoint.read_text())['results']) == 1
    result = a.verify_chronology(str(p), str(tmp_path), checkpoint_path=checkpoint)
    assert result['entries_reviewed'] == 2 and len(calls) == 3
    p.write_text(p.read_text().replace('Visit.', 'Changed visit.'))
    assert a.verify_chronology(str(p), str(tmp_path), checkpoint_path=checkpoint)['success']
    assert len(calls) == 5


def test_complete_pipeline_download_ocr_generate_export_upload_and_resume(tmp_path):
    from src.ocr_client import OCRClient
    p = MedicalChronologyPipeline.__new__(MedicalChronologyPipeline)
    p.store = SessionStore(str(tmp_path)); p.logger = logging.getLogger('full-test')
    dbx = FakeDropbox()
    tool = DropboxTool.__new__(DropboxTool); tool.dbx = dbx
    uploaded = []
    def upload_folder(local_dir, dropbox_folder, **kwargs):
        from pathlib import Path
        files = list(Path(local_dir).iterdir())
        uploaded.extend(f.name for f in files)
        return {'success': True, 'uploaded': [{'name': f.name, 'verified': True} for f in files]}
    tool.upload_folder = upload_folder
    p.dropbox_tool = tool
    class SyntheticOCR(OCRClient):
        async def batch_extract(self, paths, **kwargs):
            return [{'success': True, 'source_path': paths[0], 'file_name': paths[0].split('/')[-1],
                     'text': '=== SOURCE PDF PAGE 1 ===\n01/01/2026\nAssessment: Synthetic pain. Plan: Follow up.',
                     'page_count': 1, 'page_results': [{'page': 1, 'status': 'text'}]}]
    p.ocr_client = SyntheticOCR('synthetic-not-a-real-key')
    a = ChronologyAgent.__new__(ChronologyAgent); a.model = 'synthetic'; a.logger = p.logger
    a.batch_size = 5
    calls = []
    def model(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({'sources': [{'id': 'D001', 'scope': 'medical'}, {'id': 'D002', 'scope': 'medical'}],
            'entries': [{'record_type': 'clinical_care', 'source_ids': ['D001'],
                         'text': '01/01/2026. Clinic A. Jane Alpha, MD. Visit. Assessment: Synthetic pain. Plan: Follow up.'},
                        {'record_type': 'diagnostic_test', 'source_ids': ['D002'],
                         'text': '01/01/2026. Clinic B. John Beta, MD. Imaging. Impression: Synthetic result.'}]})
    a._call_api_with_retry = model
    a.extract_header = lambda *args: {'patient_name': 'SYNTHETIC', 'date_of_birth': '[See Records]', 'date_of_injury': '[See Records]'}
    a.generate_summary_and_gaps = lambda *args: {'summary_md': 'Synthetic summary.', 'gaps_md': 'Synthetic gaps.'}
    p.chronology_agent = a
    state = p.create_session('/Records', 'synthetic')
    result = asyncio.run(p.run(state.session_id))
    assert result['status'] == 'complete', result
    assert {'chronology.docx', 'chronology.json', 'ocr_coverage.json'} <= set(uploaded)
    report = json.loads((p.store.output_dir(state.session_id) / 'ocr_coverage.json').read_text())
    assert len(report['files']) == 2 and report['technical_failures'] == 0
    assert len(calls) == 1
    assert asyncio.run(p.run(state.session_id))['status'] == 'complete'
    assert len(calls) == 1 and len(dbx.calls) == 2
