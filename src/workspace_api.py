"""Authenticated case workspace endpoints; the gateway owns authentication."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from aiohttp import web
from .case_store import CaseStore, digest
from .source_pages import page_units
from .source_preview import render_source_page


class WorkspaceAPI:
    def __init__(self, base_dir, *, pipeline_factory=None, preview=False):
        self.base = Path(base_dir)
        self.store = CaseStore(base_dir)
        self.preview = preview
        self.pipeline_factory = pipeline_factory
        self.preview_slots = asyncio.Semaphore(2)

    def pipeline(self, model=None):
        if self.pipeline_factory:
            return self.pipeline_factory(model)
        from .pipeline import MedicalChronologyPipeline

        return MedicalChronologyPipeline(
            base_dir=str(self.base),
            model=model,
            google_api_key=os.getenv("GOOGLE_CLOUD_API_KEY"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
        )

    def create_case(self, data):
        model = data.get("model") or "claude-opus-5"
        return self.store.create(
            self.pipeline(model),
            name=data.get("name", ""),
            dob=data.get("dob", ""),
            doi=data.get("doi", ""),
            source=data.get("source", ""),
            model=model,
            policy=data.get("policy"),
        )

    def detail(self, case_id):
        case = self.store.overview(case_id)
        docs = self.store.all(case_id, "documents")
        if case["legacy"] and not docs:
            # Read-only inventory. Never manufacture provenance for older OCR.
            root = self.store.directory(case_id) / "input"
            if root.exists():
                for p in sorted(root.rglob("*.pdf")):
                    if not p.resolve().is_relative_to(root.resolve()):
                        continue
                    relative = str(p.relative_to(root))
                    docs.append(
                        {
                            "id": digest([case_id, relative])[:24],
                            "path": relative,
                            "status": "legacy",
                            "reason": "Historical source; original run rules retained.",
                        }
                    )
        entries = self.store.all(case_id, "entries")
        export = self.store.get(case_id, "case", "export") or {}
        if (
            export
            and case.get("phases", {}).get("header", {}).get("status") == "complete"
        ):
            path = (
                self.store.directory(case_id)
                / "versions"
                / export["version"]
                / "chronology.json"
            )
            if path.exists():
                entries = json.loads(path.read_text()).get("records", entries)
        if case["legacy"] and not entries:
            path = self.store.directory(case_id) / "output" / "chronology.json"
            if path.exists():
                try:
                    entries = [
                        {"id": f"legacy-{i}", **r}
                        for i, r in enumerate(
                            json.loads(path.read_text()).get("records", [])
                        )
                    ]
                except (ValueError, TypeError):
                    pass
        artifacts = self.store.all(case_id, "artifacts")
        if not artifacts:
            root = self.store.directory(case_id) / "output"
            if root.exists():
                artifacts = [
                    {
                        "id": p.name,
                        "name": p.name,
                        "bytes": p.stat().st_size,
                        "section": "output",
                        "path": p.name,
                    }
                    for p in sorted(root.iterdir())
                    if p.is_file() and p.suffix in (".docx", ".md", ".json", ".zip")
                ]
        safe_docs = [
            {
                **d,
                "pages": [
                    {k: v for k, v in p.items() if k != "text"}
                    for p in d.get("pages", [])
                ],
            }
            for d in docs
        ]
        for artifact in artifacts:
            artifact["current"] = bool(
                artifact.get("version") == export.get("version")
                and case.get("phases", {}).get("header", {}).get("status") == "complete"
            )
            if not artifact["current"] and artifact.get("version"):
                artifact["label"] = "Previous version · " + artifact["name"]
        artifacts.sort(key=lambda a: (not a["current"], a["name"]))
        return {
            **case,
            "documents": safe_docs,
            "entries": sorted(
                entries, key=lambda e: e.get("sort_date", e.get("date", ""))
            ),
            "issues": self.store.all(case_id, "issues"),
            "history": self.store.history(case_id),
            "artifacts": artifacts,
            "preview": self.preview,
        }

    def document(self, case_id, document_id):
        doc = self.store.get(case_id, "documents", document_id)
        if not doc:
            doc = next(
                (
                    d
                    for d in self.detail(case_id)["documents"]
                    if d["id"] == document_id
                ),
                None,
            )
        if not doc:
            raise FileNotFoundError("Document not found in this case.")
        return doc

    def source_text(self, case_id, doc, page):
        textpath = Path(doc["path"]).with_suffix(".txt")
        try:
            text = self.store.file(case_id, "extracted", str(textpath)).read_text()
        except FileNotFoundError:
            return {"page": page, "text": "", "provenance": "unknown"}
        found = next((t for n, t in page_units(text) if n == page), "")
        override = self.store.get(case_id, "overrides", doc["id"]) or {}
        return {
            "page": page,
            "text": found,
            "provenance": "physical_pdf_page" if found else "unknown",
            "correction": override.get("corrections", {}).get(str(page)),
            "correction_revision": override.get("revision"),
        }

    async def handle(self, request):
        try:
            path = request.path
            if path in ("/workspace", "/workspace/") or (
                path == "/" and request.query.get("legacy") != "1"
            ):
                return web.FileResponse(self.base / "web/workspace/index.html")
            if path.startswith("/workspace/assets/"):
                name = path.removeprefix("/workspace/assets/")
                if name not in ("app.js", "style.css"):
                    raise FileNotFoundError("Asset not found.")
                return web.FileResponse(self.base / "web/workspace" / name)
            parts = path.removeprefix("/api/workspace/").split("/")
            if not parts or parts[0] != "cases":
                raise FileNotFoundError("Resource not found.")
            if request.method not in ("GET", "POST"):
                raise ValueError("Unsupported request method.")
            if request.method == "POST":
                # A custom header forces cross-origin callers through CORS preflight.
                # No CORS permission is granted; the gateway also validates Origin.
                if (
                    request.headers.get("X-Workspace-Request") != "1"
                    or request.content_type != "application/json"
                ):
                    return web.json_response(
                        {"error": "Use the case workspace to make changes."}, status=403
                    )
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid request.")
            else:
                data = {}
            if len(parts) == 1:
                if request.method == "GET":
                    return web.json_response(
                        {
                            "cases": await asyncio.to_thread(
                                self.store.list_cases,
                                request.query.get("archived") == "1",
                            ),
                            "preview": self.preview,
                        }
                    )
                c = await asyncio.to_thread(self.create_case, data)
                return web.json_response(c, status=201)
            case_id = parts[1]
            if len(parts) == 2 and request.method == "GET":
                return web.json_response(await asyncio.to_thread(self.detail, case_id))
            if len(parts) < 3:
                raise FileNotFoundError("Action not found.")
            action = parts[2]
            if request.method == "GET" and action == "sources" and len(parts) >= 4:
                doc = await asyncio.to_thread(self.document, case_id, parts[3])
                if (
                    len(parts) == 6
                    and parts[4] == "pages"
                    and parts[5].endswith(".png")
                ):
                    original = self.store.file(case_id, "input", doc["path"])
                    async with self.preview_slots:
                        image = await asyncio.to_thread(
                            render_source_page,
                            original,
                            int(parts[5].removesuffix(".png")),
                            doc.get("sha256"),
                        )
                    return web.Response(
                        body=image,
                        content_type="image/png",
                        headers={"Cache-Control": "no-store"},
                    )
                if len(parts) == 5 and parts[4] == "text":
                    return web.json_response(
                        await asyncio.to_thread(
                            self.source_text,
                            case_id,
                            doc,
                            int(request.query.get("page", "1")),
                        )
                    )
                if len(parts) != 4:
                    raise FileNotFoundError("Source route not found.")
                original = self.store.file(case_id, "input", doc["path"])
                return web.FileResponse(
                    original,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Disposition": "inline",
                        "X-Frame-Options": "SAMEORIGIN",
                    },
                )
            if request.method == "GET" and action == "artifacts" and len(parts) == 4:
                artifacts = (await asyncio.to_thread(self.detail, case_id))["artifacts"]
                item = next((a for a in artifacts if a["id"] == parts[3]), None)
                if not item:
                    raise FileNotFoundError("Saved export not found.")
                path = self.store.file(
                    case_id,
                    item.get("section", "output"),
                    item.get("path", item["name"]),
                )
                return web.FileResponse(
                    path,
                    headers={
                        "Content-Disposition": 'attachment; filename="'
                        + Path(item["name"]).name.replace('"', "")
                        + '"'
                    },
                )
            if request.method != "POST" or len(parts) != 3:
                raise FileNotFoundError("Action not found.")
            if action in ("archive", "restore"):
                result = await asyncio.to_thread(
                    self.store.archive, case_id, action == "restore"
                )
            elif action == "pause":
                (self.store.directory(case_id) / "PAUSE").touch()
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status='paused' WHERE case_id=? AND status='queued'",
                        (case_id,),
                    )
                result = {"status": "pause_requested"}
            elif action in ("run", "export"):
                case = self.store.overview(case_id)
                if case["legacy"]:
                    raise ValueError(
                        "Continue this historical run in the legacy workspace."
                    )
                result = await asyncio.to_thread(self.store.enqueue, case_id, action)
            elif action == "review":
                from .medical_run import review_source

                result = await asyncio.to_thread(
                    review_source, self.store, case_id, data
                )
            else:
                raise FileNotFoundError("Action not found.")
            return web.json_response(result)
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
