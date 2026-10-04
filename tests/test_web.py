from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from igbot.web import create_app

NAME = "0123456789abcdef0123456789abcdef.jpg"


async def test_serves_only_bot_media(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / NAME).write_bytes(b"jpeg bytes")
    (media_dir / "notes.txt").write_text("private")
    (tmp_path / "igbot.sqlite3").write_text("private")

    async with TestClient(TestServer(create_app(media_dir))) as client:
        response = await client.get(f"/media/{NAME}")
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/jpeg"
        assert await response.read() == b"jpeg bytes"
        assert (await client.head(f"/media/{NAME}")).status == 200

        for path in ("/media/notes.txt", "/media/..%2Figbot.sqlite3", "/media/" + "f" * 32 + ".jpg", "/"):
            assert (await client.get(path)).status == 404, path

        assert (await client.get("/health")).status == 200
