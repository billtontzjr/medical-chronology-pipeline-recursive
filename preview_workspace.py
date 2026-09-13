"""Local, fictional-data preview. Never connects to Dropbox or model providers."""

import argparse
import os
from pathlib import Path
from aiohttp import web
from src.workspace_api import WorkspaceAPI


def create_preview(base_dir):
    workspace = WorkspaceAPI(base_dir, preview=True)

    async def handle(request):
        if request.method == "POST":
            return web.json_response(
                {
                    "error": "This fictional preview is read-only. Processing and review changes are disabled here; no external services are connected."
                },
                status=409,
            )
        return await workspace.handle(request)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    os.environ["SESSION_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ["REQUIRE_PERSISTENT_STORAGE"] = "false"
    web.run_app(
        create_preview(Path(__file__).parent),
        host="127.0.0.1",
        port=args.port,
        access_log=None,
    )
