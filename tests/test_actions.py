from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from igbot.actions import ActionError, ActionService, PostSpec
from igbot.instagram import InstagramError
from igbot.storage import Storage, to_iso, utcnow

from .conftest import CHAT, IG_USER, FakeInstagram


def test_post_rules(actions: ActionService, add_media) -> None:
    photo, video = add_media("image"), add_media("video")
    cases = {
        PostSpec("reel", (photo.id,)): "Reels",
        PostSpec("story", (photo.id, video.id)): "Story",
        PostSpec("feed", ()): "kamida",
        PostSpec("feed", (photo.id, photo.id)): "ikki marta",
        PostSpec("feed", (999,)): "#999",
        PostSpec("feed", (photo.id,), "x" * 2201): "2200",
        PostSpec("feed", (photo.id,), " ".join(f"#tag{i}" for i in range(31))): "heshteg",
    }
    for spec, message in cases.items():
        with pytest.raises(ActionError, match=message):
            actions.check_post(spec)
    many = tuple(add_media("image").id for _ in range(11))
    with pytest.raises(ActionError, match="10"):
        actions.check_post(PostSpec("feed", many))
    actions.check_post(PostSpec("reel", (video.id,), "ok"))
    actions.check_post(PostSpec("feed", (photo.id, video.id), "info@shop.uz #yangi"))


def test_publish_time_is_local_and_in_the_future(actions: ActionService) -> None:
    tomorrow = (utcnow() + timedelta(days=1)).astimezone(actions.settings.timezone)
    parsed = actions.parse_publish_time(tomorrow.strftime("%Y-%m-%d %H:%M"))
    assert parsed.tzinfo == timezone.utc
    assert abs(parsed - tomorrow) < timedelta(minutes=1)
    with pytest.raises(ActionError, match="kelajakda"):
        actions.parse_publish_time("2020-01-01 09:00")
    with pytest.raises(ActionError, match="YYYY-MM-DD"):
        actions.parse_publish_time("ertaga 9 da")


async def test_publish_request_runs_once(actions: ActionService, ig: FakeInstagram, add_media) -> None:
    photo = add_media("image", (1080, 1080))
    action = actions.request_publish(CHAT, PostSpec("feed", (photo.id,), "Yangi mahsulot!"))
    assert "Lenta posti" in actions.describe(action) and "Yangi mahsulot!" in actions.describe(action)

    outcome = await actions.run(action.id)
    assert outcome.ok and "instagram.com/p/media1" in outcome.message
    assert ig.posted() == [{"image_url": f"https://bot.example.com/media/{photo.filename}", "caption": "Yangi mahsulot!"}]

    again = await actions.run(action.id)
    assert again.status == "stale"
    assert len(ig.posted()) == 1


async def test_tall_photo_is_published_from_a_padded_copy(actions: ActionService, ig: FakeInstagram, add_media) -> None:
    photo = add_media("image", (1080, 1920))
    await actions.publish(PostSpec("feed", (photo.id,), ""))
    url = ig.posted()[0]["image_url"]
    assert url != f"https://bot.example.com/media/{photo.filename}"
    padded = actions.settings.media_dir / url.rsplit("/", 1)[1]
    assert not padded.exists()  # temporary copy removed after publishing
    assert (actions.settings.media_dir / photo.filename).exists()


async def test_failed_publish_is_reported(actions: ActionService, ig: FakeInstagram, add_media) -> None:
    ig.overrides[("POST", f"{IG_USER}/media")] = InstagramError("Instagram xatosi: media download failed", code=9004)
    action = actions.request_publish(CHAT, PostSpec("feed", (add_media("image").id,), ""))
    outcome = await actions.run(action.id)
    assert outcome.status == "failed" and "download failed" in outcome.message
    assert actions.storage.get_action(action.id).status == "failed"


async def test_old_requests_expire(actions: ActionService, storage: Storage) -> None:
    action = actions.request(CHAT, "delete_post", {"media_id": "m1"})
    storage._execute("UPDATE actions SET created_at = ? WHERE id = ?", (to_iso(utcnow() - timedelta(days=2)), action.id))
    outcome = await actions.run(action.id)
    assert outcome.status == "expired"
    assert storage.get_action(action.id).status == "expired"


async def test_schedule_request(actions: ActionService, storage: Storage, add_media) -> None:
    photo = add_media("image")
    when = (utcnow() + timedelta(hours=3)).astimezone(actions.settings.timezone).strftime("%Y-%m-%d %H:%M")
    action = actions.request_schedule(CHAT, PostSpec("feed", (photo.id,), "Ertaga!"), when)
    assert f"Vaqt: {when}" in actions.describe(action)

    outcome = await actions.run(action.id)
    assert outcome.ok
    [post] = storage.list_scheduled_posts()
    assert post.spec["caption"] == "Ertaga!" and actions.local_time(post.publish_at) == when


async def test_schedule_confirmed_too_late_is_refused(actions: ActionService, storage: Storage, add_media) -> None:
    spec = PostSpec("feed", (add_media("image").id,), "")
    action = storage.create_action(CHAT, "schedule", {"spec": spec.to_dict(), "publish_at": to_iso(utcnow() - timedelta(minutes=5))})
    outcome = await actions.run(action.id)
    assert outcome.status == "failed" and "o'tib ketdi" in outcome.message
    assert storage.list_scheduled_posts() == []


async def test_other_actions(actions: ActionService, ig: FakeInstagram) -> None:
    ig.overrides[("DELETE", "m1")] = {"success": True}
    ig.overrides[("POST", "c1/replies")] = {"id": "c2"}
    ig.overrides[("POST", "c1")] = {"success": True}
    ig.overrides[("DELETE", "c1")] = {"success": True}
    ig.overrides[("POST", "m1")] = {"success": True}
    requests = [
        ("delete_post", {"media_id": "m1", "permalink": "https://instagram.com/p/x"}),
        ("reply_comment", {"comment_id": "c1", "message": "Rahmat!", "username": "ali", "comment_text": "Zo'r"}),
        ("hide_comment", {"comment_id": "c1", "hide": True}),
        ("delete_comment", {"comment_id": "c1"}),
        ("set_comments", {"media_id": "m1", "enabled": False}),
    ]
    for kind, payload in requests:
        action = actions.request(CHAT, kind, payload)
        assert f"#{action.id}" in actions.describe(action)
        assert (await actions.run(action.id)).ok, kind
    assert ("POST", "c1/replies", {"message": "Rahmat!"}) in ig.calls
    assert ("POST", "c1", {"hide": True}) in ig.calls
    assert ("POST", "m1", {"comment_enabled": False}) in ig.calls


def test_delete_card_warns(actions: ActionService) -> None:
    action = actions.request(CHAT, "delete_post", {"media_id": "m1", "caption": "", "date": "2026-10-01 10:00"})
    card = actions.describe(action)
    assert "qaytarib bo'lmaydi" in card and "(bo'sh)" in card


def test_post_spec_round_trip() -> None:
    spec = PostSpec("feed", (3, 1, 2), "Matn")
    assert PostSpec.from_dict(spec.to_dict()) == spec
