from __future__ import annotations

import json

from igbot.actions import ActionService
from igbot.storage import Storage, utcnow
from igbot.tools import TOOLS, ToolBox

from .conftest import CHAT, IG_USER, FakeInstagram


def toolbox(settings, storage: Storage, ig: FakeInstagram) -> ToolBox:
    return ToolBox(settings, storage, ig, ActionService(settings, storage, ig))


def test_tool_schemas_are_strict() -> None:
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"])


async def test_publish_waits_for_confirmation(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    created: list[int] = []
    result = await toolbox(settings, storage, ig).run(
        CHAT, "publish_post", {"post_type": "feed", "media_ids": [photo.id], "caption": "Salom"}, created
    )
    data = json.loads(result.content)
    assert not result.is_error and data["status"] == "awaiting_admin_confirmation"
    assert created == [data["request_id"]]
    assert storage.get_action(created[0]).status == "pending"
    assert ig.calls == []  # nothing sent to Instagram yet


async def test_publish_without_confirmation(make_settings, ig, add_media) -> None:
    settings = make_settings(REQUIRE_CONFIRMATION="false")
    storage = Storage(settings.db_path)
    photo = add_media("image")
    created: list[int] = []
    result = await toolbox(settings, storage, ig).run(
        CHAT, "publish_post", {"post_type": "feed", "media_ids": [photo.id], "caption": ""}, created
    )
    assert json.loads(result.content)["status"] == "done"
    assert len(ig.posted()) == 1
    assert [storage.get_action(action_id).status for action_id in created] == ["done"]
    storage.close()


async def test_invalid_request_is_an_error_result(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    created: list[int] = []
    result = await toolbox(settings, storage, ig).run(
        CHAT, "publish_post", {"post_type": "reel", "media_ids": [photo.id], "caption": ""}, created
    )
    assert result.is_error and "video" in result.content
    assert created == []


async def test_recent_posts_in_local_time(settings, storage, ig) -> None:
    ig.overrides[("GET", f"{IG_USER}/media")] = {
        "data": [
            {
                "id": "m1",
                "caption": "Yangi kolleksiya " * 20,
                "media_type": "CAROUSEL_ALBUM",
                "media_product_type": "FEED",
                "timestamp": "2026-10-01T08:15:00+0000",
                "like_count": 12,
                "comments_count": 3,
                "permalink": "https://www.instagram.com/p/abc/",
            }
        ]
    }
    result = await toolbox(settings, storage, ig).run(CHAT, "list_recent_posts", {"limit": 99}, [])
    [post] = json.loads(result.content)
    assert post["date"] == "2026-10-01 13:15"  # Asia/Tashkent is UTC+5
    assert post["type"] == "carousel" and post["caption"].endswith("…") and len(post["caption"]) == 150
    assert ig.calls[0][2]["limit"] == 25


async def test_delete_post_shows_which_post(settings, storage, ig) -> None:
    ig.overrides[("GET", "m1")] = {
        "id": "m1",
        "caption": "Eski aksiya",
        "timestamp": "2026-09-01T10:00:00+0000",
        "permalink": "https://www.instagram.com/p/old/",
    }
    created: list[int] = []
    await toolbox(settings, storage, ig).run(CHAT, "delete_post", {"media_id": "m1"}, created)
    action = storage.get_action(created[0])
    assert action.kind == "delete_post"
    assert action.payload["permalink"] == "https://www.instagram.com/p/old/" and action.payload["caption"] == "Eski aksiya"


async def test_cancel_scheduled_post(settings, storage, ig) -> None:
    post = storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [1], "caption": ""}, utcnow())
    box = toolbox(settings, storage, ig)
    assert json.loads((await box.run(CHAT, "cancel_scheduled_post", {"schedule_id": post.id}, [])).content)["status"] == "cancelled"
    again = await box.run(CHAT, "cancel_scheduled_post", {"schedule_id": post.id}, [])
    assert again.is_error


async def test_unknown_tool(settings, storage, ig) -> None:
    result = await toolbox(settings, storage, ig).run(CHAT, "drop_database", {}, [])
    assert result.is_error
