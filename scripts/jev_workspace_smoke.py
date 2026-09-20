"""Isolated fictional workspace smoke test: real Jev; fixture generation and upload.

Requires development dependencies. Never accepts an existing case or patient path.
The live API is called only with --live; the private environment file is not changed.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def run(root):
    import subprocess
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from src.case_store import CaseStore
    from src.medical_run import MedicalRun
    from src.workspace_api import WorkspaceAPI
    from serve import create_app, COOKIE, sign_token
    from test_medical_workspace import prepared_pipeline, TEXT

    os.environ.update(SESSION_DATA_DIR=str(root / 'data'), REQUIRE_PERSISTENT_STORAGE='false',
                      CASE_WORKSPACE_ENABLED='true', JEV_ENABLED='false',
                      TEAM_PASSWORD='fictional-local-smoke-password')
    store = CaseStore(root)
    case, pipeline, calls = prepared_pipeline(store)
    original = store.sessions.input_dir(case['id']) / 'record.pdf'
    # Minimal text PDF fixture; uses no additional Python PDF dependency.
    lines = TEXT.splitlines()[1:]
    stream = 'BT /F1 10 Tf 50 790 Td 14 TL ' + ' '.join(
        '(' + line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ') Tj T*'
        for line in lines) + ' ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               ('<< /Length ' + str(len(stream.encode())) + ' >>\nstream\n' + stream + '\nendstream').encode()]
    data, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    original.write_bytes(data)
    state = store.sessions.load(case['id'])
    state.phases['download'].data['manifest'][0].update(
        size=original.stat().st_size, sha256=hashlib.sha256(original.read_bytes()).hexdigest())
    store.sessions.save(state)
    # Read the generated PDF with a real text extractor; no OCR provider is called.
    def extract(path, **kwargs):
        extracted = subprocess.run(['pdftotext', str(path), '-'], check=True, capture_output=True).stdout.decode()
        text = '=== SOURCE PDF PAGE 1 ===\n' + extracted.strip() + '\n'
        assert text == TEXT
        return dict(success=True, text=text, page_count=1,
                    page_results=[dict(page=1, status='text')], source_path=path, file_name='record.pdf')
    pipeline.ocr_client.extract_text = extract
    runner = MedicalRun(store, pipeline, case['id'])
    runner.run()
    before = {p: p.read_bytes() for p in (runner.path / 'versions').rglob('*') if p.is_file()}
    entries = copy.deepcopy(store.all(case['id'], 'entries'))
    os.environ['JEV_ENABLED'] = 'true'
    runner.run('export')
    report = json.loads((runner.path / 'output/jev_review.json').read_text())
    assert report['entries_checked'] == 1 and report['entries_unchecked'] == 0
    assert entries == store.all(case['id'], 'entries')
    assert all(p.read_bytes() == data for p, data in before.items())
    cache = {p: p.read_bytes() for p in (runner.work / 'jev').glob('*.json')}
    assert cache
    # A repeat export must reuse the successful response, not call the provider.
    from unittest.mock import patch
    with patch('src.jev_client.JevClient.evaluate', side_effect=AssertionError('Unexpected repeat API call')):
        runner.run('export')
    assert all(p.read_bytes() == data for p, data in cache.items())
    api = WorkspaceAPI(ROOT)
    api.store = store
    detail = api.detail(case['id'])
    assert detail['entries'] and detail['artifacts']
    if report['results'][0]['flags']:
        assert any(i['kind'] == 'jev_review' for i in detail['issues'])
    async def check_routes():
        backend = web.Application()
        backend.router.add_get('/', lambda request: web.Response(text='fictional backend'))
        secret = 'fictional-local-smoke-secret'
        auth = {'Cookie': COOKIE + '=' + sign_token(os.environ['TEAM_PASSWORD'], secret)}
        async with TestServer(backend) as upstream:
            async with TestClient(TestServer(create_app(str(upstream.make_url('')).rstrip('/'), secret, api))) as client:
                base = '/api/workspace/cases/' + case['id']
                assert (await client.get(base)).status == 401
                result = await client.get(base, headers=auth)
                assert result.status == 200 and (await result.json())['entries']
                page = await client.get('/workspace', headers=auth)
                assert page.status == 200
                doc_id = detail['documents'][0]['id']
                for suffix in ('', '/text?page=1', '/pages/1.png'):
                    response = await client.get(base + '/sources/' + doc_id + suffix, headers=auth)
                    assert response.status == 200 and await response.read()
                downloaded = []
                for artifact in detail['artifacts']:
                    response = await client.get(base + '/artifacts/' + artifact['id'], headers=auth)
                    assert response.status == 200 and await response.read()
                    downloaded.append(artifact['name'])
                return downloaded
    downloads = asyncio.run(check_routes())
    return dict(synthetic_only=True, real_jev=True, real_pdf_text_extraction=True,
                fixture_generation=True, fixture_cloud_upload=True, deployed=False,
                browser_visual_test=False, entries_checked=report['entries_checked'],
                review_flags=report['results'][0]['flags'], downloads_verified=downloads,
                prior_versions_unchanged=True, narrative_unchanged=True,
                cached_repeat_without_api=True, unauthenticated_access_rejected=True,
                source_pdf_text_and_preview_verified=True, report=report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path('/tmp/jev-workspace-smoke.json'))
    parser.add_argument('--preview-port', type=int, help='Keep the isolated fictional workspace open on loopback for browser testing.')
    args = parser.parse_args()
    if not args.live:
        print('Use --live to test a new isolated fictional case with the real Jev API.')
        return
    from dotenv import load_dotenv
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    with tempfile.TemporaryDirectory(prefix='jev-fictional-workspace-') as temp:
        result = run(Path(temp))
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print('Fictional workspace smoke passed: real Jev, source preview, authenticated downloads, cache and immutable history.', flush=True)
        if args.preview_port:
            from aiohttp import web
            from src.workspace_api import WorkspaceAPI
            from src.case_store import CaseStore
            from serve import create_app
            api = WorkspaceAPI(ROOT)
            api.store = CaseStore(Path(temp))
            app = create_app('http://127.0.0.1:1', 'fictional-local-smoke-secret', api)
            web.run_app(app, host='127.0.0.1', port=args.preview_port, access_log=None)


if __name__ == '__main__':
    main()
