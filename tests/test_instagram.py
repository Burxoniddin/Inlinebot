from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from igbot.instagram import InstagramClient, InstagramError, MediaItem

from .conftest import IG_USER, FakeInstagram

PHOTO = MediaItem("image", "https://bot.example.com/media/a.jpg")
PHOTO2 = MediaItem("image", "https://bot.example.com/media/b.jpg")
VIDEO = MediaItem("video", "https://bot.example.com/media/c.mp4")


async def test_single_photo(ig: FakeInstagram) -> None:
    media_id = await ig.publish("feed", [PHOTO], "Salom!")
    assert media_id == "media1"
    assert ig.calls == [
        ("POST", f"{IG_USER}/media", {"image_url": PHOTO.url, "caption": "Salom!"}),
        ("GET", "container1", {"fields": "status_code,status"}),
        ("POST", f"{IG_USER}/media_publish", {"creation_id": "container1"}),
    ]


async def test_carousel_with_video(ig: FakeInstagram) -> None:
    await ig.publish("feed", [PHOTO, VIDEO], "Karusel")
    assert ig.posted() == [
        {"is_carousel_item": True, "image_url": PHOTO.url},
        {"is_carousel_item": True, "video_url": VIDEO.url, "media_type": "VIDEO"},
        {"media_type": "CAROUSEL", "children": "container1,container2", "caption": "Karusel"},
    ]
    assert ig.calls[-1] == ("POST", f"{IG_USER}/media_publish", {"creation_id": "container3"})


async def test_single_video_is_published_as_reel(ig: FakeInstagram) -> None:
    await ig.publish("feed", [VIDEO], "Video")
    assert ig.posted() == [{"media_type": "REELS", "video_url": VIDEO.url, "share_to_feed": True, "caption": "Video"}]


async def test_story_has_no_caption(ig: FakeInstagram) -> None:
    await ig.publish("story", [PHOTO], "")
    assert ig.posted() == [{"media_type": "STORIES", "image_url": PHOTO.url}]


async def test_waits_while_instagram_processes(ig: FakeInstagram) -> None:
    ig.overrides[("GET", "container1")] = [
        {"status_code": "IN_PROGRESS"},
        {"status_code": "IN_PROGRESS"},
        {"status_code": "FINISHED"},
    ]
    await ig.publish("reel", [VIDEO], "")
    assert sum(1 for call in ig.calls if call[1] == "container1") == 3


async def test_processing_error_stops_publishing(ig: FakeInstagram) -> None:
    ig.overrides[("GET", "container1")] = {"status_code": "ERROR", "status": "Error: video too short"}
    with pytest.raises(InstagramError, match="video too short"):
        await ig.publish("reel", [VIDEO], "")
    assert not any(path.endswith("media_publish") for _, path, _ in ig.calls)


async def test_insights_fall_back_to_single_metrics(ig: FakeInstagram) -> None:
    unsupported = InstagramError("metric not supported", code=100)
    ig.overrides[("GET", "m1/insights")] = [
        unsupported,
        {"data": [{"name": "views", "values": [{"value": 120}]}]},
        unsupported,
        {"data": [{"name": "likes", "total_value": {"value": 7}}]},
    ]
    assert await ig.media_insights("m1", ("views", "reach", "likes")) == {"views": 120, "likes": 7}


def test_error_messages_explain_the_fix() -> None:
    error = InstagramError.from_response({"error": {"message": "Invalid OAuth access token.", "code": 190}}, 400)
    assert error.code == 190
    assert "Invalid OAuth access token." in str(error) and "IG_ACCESS_TOKEN" in str(error)
    limit = InstagramError.from_response({"error": {"message": "x", "code": 9, "error_subcode": 2207042}}, 400)
    assert "limit" in str(limit)
    permission = InstagramError.from_response({"error": {"message": "(#200) Permissions error", "code": 200}}, 403)
    assert "ruxsat" in str(permission)


async def test_http_layer() -> None:
    """The real request(): token and parameters go in the form body for POST and the query for GET/DELETE."""
    seen = []

    async def graph(request: web.Request) -> web.Response:
        form = dict(await request.post()) if request.method == "POST" else {}
        seen.append((request.method, request.match_info["path"], dict(request.query), form))
        if request.match_info["path"] == "broken":
            return web.Response(status=500, text="<html>oops</html>")
        if request.match_info["path"] == "denied":
            return web.json_response({"error": {"message": "No permission", "code": 10}}, status=403)
        return web.json_response({"id": "123", "success": True})

    app = web.Application()
    app.router.add_route("*", "/v25.0/{path:.*}", graph)
    async with TestServer(app) as server:
        client = InstagramClient("secret", IG_USER, base_url=str(server.make_url("/v25.0")))
        try:
            assert await client.create_container({"image_url": "https://x/y.jpg", "is_carousel_item": True}) == "123"
            await client.delete_media("m1")
            with pytest.raises(InstagramError, match="No permission") as denied:
                await client.request("GET", "denied")
            assert denied.value.code == 10
            with pytest.raises(InstagramError, match="HTTP 500"):
                await client.request("GET", "broken")
        finally:
            await client.close()

    assert seen[0] == (
        "POST", f"{IG_USER}/media", {}, {"image_url": "https://x/y.jpg", "is_carousel_item": "true", "access_token": "secret"}
    )
    assert seen[1] == ("DELETE", "m1", {"access_token": "secret"}, {})
