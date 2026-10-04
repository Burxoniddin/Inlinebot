"""Public HTTP endpoint Instagram downloads our media from (Instagram only accepts media by URL)."""

from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

# Only the random names the bot itself creates are served; nothing else in the folder is reachable.
_MEDIA_NAME = re.compile(r"[0-9a-f]{32}\.(?:jpg|mp4|mov)")


def create_app(media_dir: Path) -> web.Application:
    async def media(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = media_dir / name
        if not _MEDIA_NAME.fullmatch(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/media/{name}", media)
    app.router.add_get("/health", health)
    return app


async def start_web_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
