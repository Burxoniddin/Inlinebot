from __future__ import annotations

import os
from datetime import timedelta

from igbot.actions import ActionService
from igbot.instagram import InstagramError
from igbot.scheduler import Scheduler
from igbot.storage import utcnow

from .conftest import CHAT, IG_USER


def make_scheduler(settings, storage, ig, notes: list) -> Scheduler:
    async def notify(chat_id: int, text: str, note: str) -> None:
        notes.append((chat_id, text, note))

    return Scheduler(settings, storage, ActionService(settings, storage, ig), notify)


async def test_due_posts_are_published(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    spec = {"post_type": "feed", "media_ids": [photo.id], "caption": "Vaqti keldi"}
    due = storage.add_scheduled_post(CHAT, spec, utcnow() - timedelta(seconds=5))
    later = storage.add_scheduled_post(CHAT, spec, utcnow() + timedelta(hours=1))
    notes: list = []

    assert await make_scheduler(settings, storage, ig, notes).tick() == 1
    assert storage.get_scheduled_post(due.id).status == "published"
    assert storage.get_scheduled_post(later.id).status == "scheduled"
    assert ig.posted()[0]["caption"] == "Vaqti keldi"
    [(chat_id, text, note)] = notes
    assert chat_id == CHAT and text.startswith("✅") and "media1" in note


async def test_failures_are_reported(settings, storage, ig, add_media) -> None:
    ig.overrides[("POST", f"{IG_USER}/media")] = InstagramError("Instagram xatosi: token expired", code=190)
    post = storage.add_scheduled_post(
        CHAT, {"post_type": "feed", "media_ids": [add_media("image").id], "caption": ""}, utcnow()
    )
    notes: list = []
    assert await make_scheduler(settings, storage, ig, notes).tick() == 0
    failed = storage.get_scheduled_post(post.id)
    assert failed.status == "failed" and "token expired" in failed.error
    assert notes[0][1].startswith("❌")


def test_unanswered_requests_expire_and_strays_are_swept(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    old_request = storage.create_action(CHAT, "publish", {"spec": {"post_type": "feed", "media_ids": [photo.id], "caption": ""}})
    storage._execute("UPDATE actions SET created_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", old_request.id))
    fresh_request = storage.create_action(CHAT, "delete_post", {"media_id": "m1"})
    stray, fresh_stray = settings.media_dir / ("e" * 32 + ".jpg"), settings.media_dir / ("f" * 32 + ".part")
    stray.write_bytes(b"left over")
    fresh_stray.write_bytes(b"still downloading")
    os.utime(stray, (0, 0))

    make_scheduler(settings, storage, ig, []).cleanup_media()
    assert storage.get_action(old_request.id).status == "expired"
    assert storage.get_action(fresh_request.id).status == "pending"
    assert not stray.exists() and fresh_stray.exists()
    assert (settings.media_dir / photo.filename).exists()


def test_old_uploads_are_removed_unless_needed(settings, storage, ig, add_media) -> None:
    old, needed, recent = add_media("image"), add_media("image"), add_media("image")
    storage._execute("UPDATE media SET created_at = ? WHERE id IN (?, ?)", ("2000-01-01T00:00:00+00:00", old.id, needed.id))
    storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [needed.id], "caption": ""}, utcnow())

    assert make_scheduler(settings, storage, ig, []).cleanup_media() == 1
    assert storage.get_media(old.id) is None
    assert not (settings.media_dir / old.filename).exists() and not (settings.preview_dir / old.preview_filename).exists()
    assert storage.get_media(needed.id) is not None and storage.get_media(recent.id) is not None
