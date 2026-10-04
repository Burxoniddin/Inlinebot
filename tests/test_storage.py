from __future__ import annotations

from datetime import timedelta

from igbot.storage import Conversation, Storage, utcnow

from .conftest import CHAT


def test_request_is_handled_once(storage: Storage) -> None:
    action = storage.create_action(CHAT, "delete_post", {"media_id": "m1"})
    assert action.status == "pending" and action.payload == {"media_id": "m1"}
    assert storage.set_action_status(action.id, "pending", "running")
    assert not storage.set_action_status(action.id, "pending", "running")
    assert not storage.set_action_status(action.id, "pending", "cancelled")
    assert storage.set_action_status(action.id, "running", "done", "ok")
    assert storage.get_action(action.id).result == "ok"


def test_scheduled_posts_lifecycle(storage: Storage) -> None:
    now = utcnow()
    due = storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [1], "caption": ""}, now - timedelta(minutes=1))
    later = storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [2], "caption": ""}, now + timedelta(hours=1))
    assert [post.id for post in storage.due_scheduled_posts(now)] == [due.id]
    assert [post.id for post in storage.list_scheduled_posts()] == [due.id, later.id]

    assert storage.claim_scheduled_post(due.id)
    assert not storage.claim_scheduled_post(due.id)
    storage.finish_scheduled_post(due.id, "published", ig_media_id="m9", permalink="https://instagram.com/p/x")
    published = storage.get_scheduled_post(due.id)
    assert (published.status, published.ig_media_id) == ("published", "m9")

    assert storage.cancel_scheduled_post(later.id)
    assert storage.list_scheduled_posts() == []


def test_interrupted_work_is_marked_failed(storage: Storage) -> None:
    post = storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [1], "caption": ""}, utcnow())
    storage.claim_scheduled_post(post.id)
    action = storage.create_action(CHAT, "publish", {"spec": {"post_type": "feed", "media_ids": [1], "caption": ""}})
    storage.set_action_status(action.id, "pending", "running")

    assert [p.id for p in storage.fail_interrupted_posts()] == [post.id]
    assert [a.id for a in storage.fail_interrupted_actions()] == [action.id]
    assert storage.get_scheduled_post(post.id).status == "failed"
    assert storage.get_action(action.id).status == "failed"


def test_media_in_use(storage: Storage) -> None:
    storage.add_scheduled_post(CHAT, {"post_type": "feed", "media_ids": [1, 2], "caption": ""}, utcnow())
    storage.create_action(CHAT, "publish", {"spec": {"post_type": "feed", "media_ids": [3], "caption": ""}})
    done = storage.create_action(CHAT, "publish", {"spec": {"post_type": "feed", "media_ids": [4], "caption": ""}})
    storage.set_action_status(done.id, "pending", "cancelled")
    assert storage.media_ids_in_use() == {1, 2, 3}


def test_conversation_round_trip(storage: Storage) -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "Salom 👋"}]}]
    storage.save_conversation(Conversation(CHAT, "fp", messages, 1234, utcnow()))
    loaded = storage.get_conversation(CHAT)
    assert (loaded.messages, loaded.context_tokens, loaded.fingerprint) == (messages, 1234, "fp")
    storage.delete_conversation(CHAT)
    assert storage.get_conversation(CHAT) is None
