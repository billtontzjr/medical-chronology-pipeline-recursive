"""Exercise the actual gateway and case API with fictional files only."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from serve import create_app, sign_token, COOKIE
from src.workspace_api import WorkspaceAPI

PASSWORD = "workspace-synthetic-password"
SECRET = "workspace-test-secret"


def test_gateway_case_api_authentication_sources_jobs_and_archive(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEAM_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUIRE_PERSISTENT_STORAGE", "false")
    monkeypatch.setenv("CASE_WORKSPACE_ENABLED", "true")
    api = WorkspaceAPI(Path(__file__).resolve().parents[1])

    def factory(model):
        return SimpleNamespace(
            create_session=lambda source, name: api.store.sessions.create(
                "fictional-api-case",
                name,
                source,
                "/Medical chronology pipeline outputs/fictional-api-case",
            )
        )

    api.pipeline_factory = factory
    auth = {"Cookie": COOKIE + "=" + sign_token(PASSWORD, SECRET)}
    write = {**auth, "X-Workspace-Request": "1"}
    base = "/api/workspace/cases"

    async def scenario():
        async def legacy(_):
            return web.Response(text="legacy route")

        upstream = web.Application()
        upstream.router.add_route("*", "/{p:.*}", legacy)
        async with TestServer(upstream) as backend:
            async with TestClient(
                TestServer(
                    create_app(str(backend.make_url("")).rstrip("/"), SECRET, api)
                )
            ) as client:
                assert (await client.get(base)).status == 401
                assert (await client.get(base + "/fake/sources/doc")).status == 401
                assert (await client.get("/workspace/assets/app.js")).status == 401
                response = await client.get(
                    "/workspace?case=fictional-api-case", allow_redirects=False
                )
                assert (
                    response.headers["Location"]
                    == "/login?view=workspace&case=fictional-api-case"
                )
                page = await client.get(response.headers["Location"])
                body = await page.text()
                assert (
                    'name="view" value="workspace"' in body
                    and 'name="case" value="fictional-api-case"' in body
                )
                response = await client.post(
                    "/login",
                    data={
                        "password": PASSWORD,
                        "view": "workspace",
                        "case": "fictional-api-case",
                    },
                    allow_redirects=False,
                )
                assert (
                    response.headers["Location"] == "/workspace?case=fictional-api-case"
                )
                assert response.cookies[COOKIE]["secure"]
                response = await client.post(
                    "/login",
                    data={
                        "password": PASSWORD,
                        "view": "workspace",
                        "case": "//outside.invalid",
                    },
                    allow_redirects=False,
                )
                assert response.headers["Location"] == "/workspace"
                response = await client.get("/", headers=auth)
                assert "Case workspace" in await response.text()
                assert (
                    await (
                        await client.get("/?session_id=old-case", headers=auth)
                    ).text()
                    == "legacy route"
                )
                assert (
                    await (await client.get("/?legacy=1", headers=auth)).text()
                    == "legacy route"
                )
                data = {
                    "name": "Alex Fictional",
                    "dob": "04/14/1980",
                    "source": "https://www.dropbox.com/fictional",
                    "model": "synthetic-model",
                }
                assert (await client.post(base, json=data, headers=auth)).status == 403
                assert (
                    await client.post(
                        base,
                        json=data,
                        headers={**write, "Origin": "https://foreign.invalid"},
                    )
                ).status == 403
                result = await client.post(base, json=data, headers=write)
                assert result.status == 201
                case = await result.json()
                assert case["policy"]["depositions"] is False
                source = api.store.sessions.input_dir(case["id"]) / "source.pdf"
                source.write_bytes(b"%PDF-fictional-source")
                api.store.put(
                    case["id"],
                    "documents",
                    "doc1",
                    {"id": "doc1", "path": "source.pdf", "status": "pending"},
                )
                response = await client.get(
                    base + "/" + case["id"] + "/sources/doc1", headers=auth
                )
                assert (
                    response.status == 200
                    and await response.read() == b"%PDF-fictional-source"
                )
                assert (
                    response.headers["X-Frame-Options"] == "SAMEORIGIN"
                    and response.headers["Cache-Control"] == "no-store"
                )
                assert (
                    await client.get(base + "/other-case/sources/doc1", headers=auth)
                ).status == 404
                assert (
                    await client.get(
                        base + "/" + case["id"] + "/sources/missing", headers=auth
                    )
                ).status == 404
                for _ in range(2):
                    response = await client.post(
                        base + "/" + case["id"] + "/run", json={}, headers=write
                    )
                    assert response.status == 200
                    job = await response.json()
                    if _ == 0:
                        first = job["id"]
                    else:
                        assert job["id"] == first
                assert (
                    await client.post(
                        base + "/" + case["id"] + "/archive", json={}, headers=write
                    )
                ).status == 400
                assert (
                    await client.post(
                        base + "/" + case["id"] + "/pause", json={}, headers=write
                    )
                ).status == 200
                assert api.store.job(case["id"])["status"] == "paused"
                assert (
                    await client.post(
                        base + "/" + case["id"] + "/archive", json={}, headers=write
                    )
                ).status == 200
                assert (await (await client.get(base, headers=auth)).json())[
                    "cases"
                ] == []
                assert (
                    await client.post(
                        base + "/" + case["id"] + "/restore", json={}, headers=write
                    )
                ).status == 200
                assert source.read_bytes() == b"%PDF-fictional-source"
                assert (
                    await client.get(base + "/" + case["id"], headers=auth)
                ).status == 200

    asyncio.run(scenario())
