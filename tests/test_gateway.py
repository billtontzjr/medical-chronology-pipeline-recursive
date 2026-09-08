import asyncio
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import pytest
from serve import create_app, sign_token, token_valid, COOKIE
from src.access_control import SESSION_SECONDS

PASSWORD = 'synthetic-password-for-tests'
SECRET = 'synthetic-gateway-secret'


def test_signed_cookie_expiry_tampering_and_password_rotation():
    token = sign_token(PASSWORD, SECRET, now=1000)
    assert token_valid(token, PASSWORD, SECRET, now=1001)
    assert not token_valid(token + 'x', PASSWORD, SECRET, now=1001)
    assert not token_valid(token, PASSWORD, SECRET, now=999)
    assert not token_valid(token, PASSWORD, SECRET, now=1000 + SESSION_SECONDS)
    assert not token_valid(token, 'rotated-password', SECRET, now=1001)


def test_gateway_protects_downloads_websockets_and_logout(monkeypatch):
    monkeypatch.setenv('TEAM_PASSWORD', PASSWORD)
    async def scenario():
        upstream = web.Application()
        async def endpoint(request):
            assert request.headers.get('X-Chronology-Gateway') == SECRET
            if request.path == '/_stcore/stream':
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws.send_str('echo:' + msg.data)
                return ws
            return web.Response(body=b'synthetic-private-download')
        upstream.router.add_route('*', '/{path:.*}', endpoint)
        async with TestServer(upstream) as backend:
            async with TestClient(TestServer(create_app(str(backend.make_url('')).rstrip('/'), SECRET))) as client:
                for path in ('/media/known-file.docx', '/_stcore/stream', '/_stcore/upload_file'):
                    response = await client.get(path, allow_redirects=False)
                    assert response.status == 401
                    assert 'private-download' not in await response.text()
                assert (await client.get('/', allow_redirects=False)).status == 303
                # A forged internal header is never authentication.
                assert (await client.get('/media/file', headers={'X-Chronology-Gateway': SECRET})).status == 401
                response = await client.post('/login', data={'password': PASSWORD}, allow_redirects=False)
                assert response.status == 303
                cookie = response.cookies[COOKIE]
                assert cookie['secure'] and cookie['httponly'] and cookie['samesite'] == 'Strict'
                token = cookie.value
                auth = {'Cookie': COOKIE + '=' + token}
                response = await client.get('/media/known-file.docx', headers=auth)
                assert await response.read() == b'synthetic-private-download'
                assert response.headers['Cache-Control'] == 'no-store'
                foreign = dict(auth, Origin='https://foreign.invalid')
                assert (await client.post('/anything', headers=foreign)).status == 403
                ws = await client.ws_connect('/_stcore/stream', headers=auth)
                await ws.send_str('test')
                assert (await ws.receive()).data == 'echo:test'
                await client.get('/logout', headers=auth, allow_redirects=False)
                assert (await client.get('/media/known-file.docx', headers=auth)).status == 401
                assert (await ws.receive(timeout=3)).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    asyncio.run(scenario())


def test_gateway_without_password_fails_closed(monkeypatch):
    monkeypatch.delenv('TEAM_PASSWORD', raising=False)
    async def scenario():
        async with TestClient(TestServer(create_app(secret=SECRET))) as client:
            for path in ('/', '/media/file.docx', '/_stcore/stream'):
                assert (await client.get(path)).status == 503
    asyncio.run(scenario())
