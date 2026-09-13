"""Password gateway for every Streamlit route, including media and WebSockets."""
import asyncio
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlencode
from html import escape

import aiohttp
from aiohttp import web
from src.session_state import validate_session_id
from src.access_control import configured_password, password_matches, attempt_allowed, SESSION_SECONDS

COOKIE = '__Host-chronology_session'
CLIENT = web.AppKey('client', aiohttp.ClientSession)
HOP = {'connection', 'upgrade', 'keep-alive', 'transfer-encoding', 'proxy-authenticate',
       'proxy-authorization', 'te', 'trailer', 'content-length', 'sec-websocket-key',
       'sec-websocket-version', 'sec-websocket-extensions', 'sec-websocket-protocol'}
LOGIN = '''<!doctype html><html><head><meta name="viewport" content="width=device-width">
<title>Medical Chronology — Team sign-in</title></head>
<body style="font:18px system-ui;max-width:440px;margin:12vh auto;padding:24px">
<h1>Medical Chronology</h1><p>Sign in with your team password.</p>
<form method="post" action="/login"><label>Team password<br>
<input name="password" type="password" autocomplete="current-password" required style="width:100%;padding:12px;box-sizing:border-box"></label>
<button style="margin-top:16px;padding:12px" type="submit">Sign in</button></form></body></html>'''


def return_session_id(value):
    """Carry only a validated case ID through login, never a redirect URL."""
    try:
        return validate_session_id(value)
    except (ValueError, TypeError):
        return ''


def login_page(session_id='', *, rejected=False, workspace_case=None):
    body = LOGIN
    if session_id:
        hidden = '<input type="hidden" name="session_id" value="' + escape(session_id, quote=True) + '">'
        body = body.replace('</form>', hidden + '</form>')
    if workspace_case is not None:
        body=body.replace('</form>','<input type="hidden" name="view" value="workspace"><input type="hidden" name="case" value="'+escape(workspace_case,quote=True)+'"></form>')
    if rejected:
        body = body.replace('Sign in with your team password.', 'Password not accepted. Try again.')
    return body


def sign_token(password, secret, now=None):
    payload = f'{int(time.time() if now is None else now)}.{secrets.token_hex(16)}'
    key = hashlib.sha256((secret + '\0' + password).encode()).digest()
    return payload + '.' + hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def token_valid(token, password, secret, now=None):
    try:
        payload, signature = token.rsplit('.', 1)
        issued = int(payload.split('.')[0])
        age = (time.time() if now is None else now) - issued
        key = hashlib.sha256((secret + '\0' + password).encode()).digest()
        return 0 <= age < SESSION_SECONDS and hmac.compare_digest(
            signature, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest())
    except (ValueError, TypeError, AttributeError):
        return False


def create_app(upstream='http://127.0.0.1:8502', secret=None, workspace=None):
    secret = secret or secrets.token_urlsafe(48)
    app = web.Application(client_max_size=200 * 1024 * 1024)
    revoked = {}

    def valid(token):
        return token not in revoked and token_valid(token, configured_password() or '', secret)

    async def session_context(app):
        async with aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
                                        timeout=aiohttp.ClientTimeout(total=None, connect=10)) as client:
            app[CLIENT] = client
            yield
    app.cleanup_ctx.append(session_context)

    async def handle(request):
        # no-referrer makes basic browser form POSTs send Origin: null.
        # Keep the login form same-origin without allowing opaque origins.
        referrer_policy = 'same-origin' if request.path == '/login' else 'no-referrer'
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': referrer_policy, 'X-Frame-Options': 'DENY'}
        password = configured_password()
        if request.path == '/healthz':
            try:
                async with app[CLIENT].get(upstream + '/_stcore/health', timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return web.Response(text='ok' if response.status == 200 else 'starting', status=response.status, headers=headers)
            except (aiohttp.ClientError, TimeoutError):
                return web.Response(text='starting', status=503, headers=headers)
        if not password:
            return web.Response(text='Team access is not configured. Ask the administrator to set TEAM_PASSWORD (at least 16 characters) in Render.', status=503, headers=headers)
        # Streamlit's own XSRF protection is retained. Also reject foreign-origin
        # login, mutation and WebSocket requests at the gateway.
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return web.Response(text='Origin not allowed.', status=403, headers=headers)
        if request.path == '/login' and request.method == 'POST':
            if not attempt_allowed():
                return web.Response(text='Too many attempts. Wait one minute.', status=429, headers=headers)
            form = await request.post()
            session_id = return_session_id(form.get('session_id', ''))
            workspace_case=return_session_id(form.get('case','')) if form.get('view')=='workspace' else None
            if not password_matches(str(form.get('password', '')), password):
                return web.Response(text=login_page(session_id, rejected=True,workspace_case=workspace_case), content_type='text/html', status=401, headers=headers)
            target = '/?' + urlencode({'session_id': session_id}) if session_id else '/'
            if workspace_case is not None:
                target='/workspace'+('?' + urlencode({'case':workspace_case}) if workspace_case else '')
            response = web.HTTPSeeOther(target, headers=headers)
            response.set_cookie(COOKIE, sign_token(password, secret), secure=True, httponly=True,
                                samesite='Strict', max_age=SESSION_SECONDS, path='/')
            raise response
        if request.path == '/logout':
            revoked[request.cookies.get(COOKIE, '')] = time.time()
            for key, stamp in list(revoked.items()):
                if time.time() - stamp >= SESSION_SECONDS:
                    revoked.pop(key, None)
            response = web.HTTPSeeOther('/login', headers=headers)
            response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
            raise response
        if request.path == '/login':
            workspace_case=return_session_id(request.query.get('case','')) if request.query.get('view')=='workspace' else None
            return web.Response(text=login_page(return_session_id(request.query.get('session_id', '')),workspace_case=workspace_case), content_type='text/html', headers=headers)
        token = request.cookies.get(COOKIE, '')
        if not valid(token):
            if request.path in ('/','/workspace','/workspace/'):
                session_id = return_session_id(request.query.get('session_id', ''))
                target = '/login?' + urlencode({'session_id': session_id}) if session_id else '/login'
                if request.path in ('/workspace','/workspace/'):
                    target='/login?'+urlencode({'view':'workspace','case':return_session_id(request.query.get('case',''))})
                raise web.HTTPSeeOther(target, headers=headers)
            return web.Response(text='Sign in to access this resource.', status=401, headers=headers)
        workspace_route=(request.path.startswith('/workspace') or request.path.startswith('/api/workspace/'))
        workspace_home=(request.path=='/' and request.query.get('legacy')!='1'
                        and not request.query.get('session_id')
                        and os.getenv('CASE_WORKSPACE_ENABLED','false').lower() in ('1','true','yes'))
        if workspace is not None and (workspace_route or workspace_home):
            response=await workspace.handle(request)
            # Only authenticated same-origin PDF embedding is allowed.
            framed=response.headers.get('X-Frame-Options')=='SAMEORIGIN'
            response.headers.update(headers)
            if framed:response.headers['X-Frame-Options']='SAMEORIGIN'
            return response
        forwarded = {k: v for k, v in request.headers.items()
                     if k.lower() not in HOP and k.lower() not in ('x-chronology-gateway',)}
        forwarded['X-Chronology-Gateway'] = secret
        # Use a fixed upstream and relative path; never act as an open proxy.
        target = upstream + request.rel_url.path_qs
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            protocols = [p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
            try:
                async with app[CLIENT].ws_connect(target, headers=forwarded, protocols=protocols, max_msg_size=200 * 1024 * 1024) as backend:
                    ws = web.WebSocketResponse(protocols=[backend.protocol] if backend.protocol else [], max_msg_size=200 * 1024 * 1024)
                    await ws.prepare(request)
                    async def relay(source, destination):
                        async for msg in source:
                            if not valid(token):
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await destination.send_str(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await destination.send_bytes(msg.data)
                            else:
                                break
                    async def expiry():
                        while valid(token):
                            await asyncio.sleep(1)
                    tasks = [asyncio.create_task(relay(ws, backend)), asyncio.create_task(relay(backend, ws)), asyncio.create_task(expiry())]
                    remaining = max(0, SESSION_SECONDS - (time.time() - int(token.split('.')[0])))
                    try:
                        await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await ws.close()
                    return ws
            except aiohttp.ClientError:
                return web.Response(text='App is starting. Refresh shortly.', status=503, headers=headers)
        try:
            async with app[CLIENT].request(request.method, target, headers=forwarded,
                                            data=request.content, allow_redirects=False) as backend:
                response_headers = backend.headers.copy()
                for key in list(response_headers):
                    if key.lower() in HOP:
                        del response_headers[key]
                response_headers.update(headers)
                response = web.StreamResponse(status=backend.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in backend.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientError:
            return web.Response(text='App is starting. Refresh shortly.', status=503, headers=headers)
    app.router.add_route('*', '/{path:.*}', handle)
    return app


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    secret = secrets.token_urlsafe(48)
    env = dict(os.environ, APP_GATEWAY_SECRET=secret)
    child = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', 'app.py',
        '--server.port=8502', '--server.address=127.0.0.1', '--server.headless=true'], env=env)
    from src.workspace_api import WorkspaceAPI
    workspace=WorkspaceAPI(os.path.dirname(os.path.abspath(__file__)))
    worker=subprocess.Popen([sys.executable,'-m','src.workspace_worker'],env=env)
    async def supervise(app):
        nonlocal_holder={'process':worker}
        async def keep_running():
            while True:
                await asyncio.sleep(5)
                if nonlocal_holder['process'].poll() is not None:
                    nonlocal_holder['process']=subprocess.Popen([sys.executable,'-m','src.workspace_worker'],env=env)
        task=asyncio.create_task(keep_running())
        yield
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        process=nonlocal_holder['process'];process.terminate()
        try:await asyncio.to_thread(process.wait,30)
        except subprocess.TimeoutExpired:process.kill()
    application=create_app(secret=secret,workspace=workspace)
    application.cleanup_ctx.append(supervise)
    try:
        web.run_app(application, host='0.0.0.0', port=int(os.getenv('PORT', '8501')), access_log=None)
    finally:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
