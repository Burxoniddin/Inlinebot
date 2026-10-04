from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from igbot.actions import ActionService
from igbot.agent import LOCAL_IMAGE, TOO_LONG_NOTICE, Agent, AgentRefusal, assistant_content, build_user_content
from igbot.storage import Storage, utcnow
from igbot.tools import TOOLS, ToolBox

from .conftest import CHAT, IG_USER, FakeClaude, claude_message

THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def make_agent(settings, storage, ig, claude: FakeClaude) -> Agent:
    actions = ActionService(settings, storage, ig)
    return Agent(settings, storage, claude, ToolBox(settings, storage, ig, actions))


def user(text: str, media=()) -> list[dict]:
    return build_user_content(utcnow().astimezone(), media, text)


def tool_use(name: str, tool_input: dict, tool_id: str = "toolu_1") -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def assert_extends(longer: list, shorter: list) -> None:
    """Each request must replay the previous one unchanged (append-only history)."""
    assert len(longer) > len(shorter)
    assert json.dumps(longer[: len(shorter)]) == json.dumps(shorter)


async def test_tool_loop_and_append_only_history(settings, storage, ig) -> None:
    ig.overrides[("GET", f"{IG_USER}/media")] = {"data": [{"id": "m1", "caption": "Kuzgi aksiya", "media_type": "IMAGE"}]}
    claude = FakeClaude(
        claude_message([THINKING, tool_use("list_recent_posts", {"limit": 5})], "tool_use"),
        claude_message([THINKING, text("Oxirgi post: Kuzgi aksiya.")]),
        claude_message([THINKING, text("Marhamat!")]),
    )
    agent = make_agent(settings, storage, ig, claude)

    reply = await agent.respond(CHAT, user("Oxirgi postlarni ko'rsat"))
    assert reply.text == "Oxirgi post: Kuzgi aksiya." and reply.action_ids == []
    first, second = claude.requests
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "Kuzgi aksiya" in tool_result["content"]
    assert_extends(second["messages"], first["messages"])

    await agent.respond(CHAT, user("Rahmat"))
    third = claude.requests[2]
    assert_extends(third["messages"], second["messages"])
    assert third["messages"][len(second["messages"])] == {"role": "assistant", "content": [THINKING, text("Oxirgi post: Kuzgi aksiya.")]}

    for request in claude.requests:  # identical system prompt and tools on every request (prompt cache, thinking)
        assert request["system"] == agent.system_prompt and request["tools"] == TOOLS


async def test_request_parameters(settings, storage, ig) -> None:
    claude = FakeClaude(claude_message([text("Salom!")]))
    await make_agent(settings, storage, ig, claude).respond(CHAT, user("Salom"))
    [request] = claude.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert request["output_config"] == {"effort": "medium"}
    assert request["fallbacks"] == "default"
    assert set(request["betas"]) == {"thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"}
    assert "tool_choice" not in request


async def test_changes_wait_for_the_admin(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    claude = FakeClaude(
        claude_message(
            [THINKING, tool_use("publish_post", {"post_type": "feed", "media_ids": [photo.id], "caption": "Yangi! #kuz"})],
            "tool_use",
        ),
        claude_message([text("Post tayyor, tasdiqlang.")]),
    )
    reply = await make_agent(settings, storage, ig, claude).respond(CHAT, user("Joyla", [photo]))
    [action_id] = reply.action_ids
    assert storage.get_action(action_id).status == "pending"
    assert "awaiting_admin_confirmation" in claude.requests[1]["messages"][-1]["content"][0]["content"]
    assert ig.calls == []


async def test_photos_are_sent_but_stored_as_placeholders(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    claude = FakeClaude(claude_message([text("Chiroyli rasm!")]))
    await make_agent(settings, storage, ig, claude).respond(CHAT, user("", [photo]))

    sent = claude.requests[0]["messages"][0]["content"]
    image = next(block for block in sent if block["type"] == "image")
    expected = base64.b64encode((settings.preview_dir / photo.preview_filename).read_bytes()).decode()
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": expected}
    assert any(f"media #{photo.id}" in block.get("text", "") for block in sent)

    stored = storage.get_conversation(CHAT).messages[0]["content"]
    assert {"type": LOCAL_IMAGE, "media_id": photo.id} in stored


async def test_refusal_discards_the_turn(settings, storage, ig, add_media) -> None:
    photo = add_media("image")
    claude = FakeClaude(
        claude_message([text("Salom!")]),
        claude_message([tool_use("publish_post", {"post_type": "feed", "media_ids": [photo.id], "caption": ""})], "tool_use"),
        claude_message([], "refusal"),
    )
    agent = make_agent(settings, storage, ig, claude)
    await agent.respond(CHAT, user("Salom"))
    saved = storage.get_conversation(CHAT).messages

    with pytest.raises(AgentRefusal):
        await agent.respond(CHAT, user("..."))
    assert storage.get_conversation(CHAT).messages == saved
    assert storage.get_action(1).status == "cancelled"  # the request prepared in the refused turn is withdrawn


async def test_work_done_before_a_failure_is_remembered(make_settings, ig, add_media) -> None:
    """Without confirmation buttons a post can go out before the turn fails; the agent must not repeat it."""
    settings = make_settings(REQUIRE_CONFIRMATION="false")
    storage = Storage(settings.db_path)
    photo = add_media("image")
    claude = FakeClaude(
        claude_message([tool_use("publish_post", {"post_type": "feed", "media_ids": [photo.id], "caption": ""})], "tool_use"),
        RuntimeError("overloaded"),
    )
    with pytest.raises(RuntimeError):
        await make_agent(settings, storage, ig, claude).respond(CHAT, user("Joyla"))
    assert len(ig.posted()) == 1
    [note] = storage.get_conversation(CHAT).messages
    assert note["content"][0]["text"].startswith("[Bot notice] Request #1 (publish) done")
    storage.close()


async def test_api_error_keeps_history(settings, storage, ig) -> None:
    claude = FakeClaude(claude_message([text("Salom!")]), RuntimeError("network down"))
    agent = make_agent(settings, storage, ig, claude)
    await agent.respond(CHAT, user("Salom"))
    saved = storage.get_conversation(CHAT).messages
    with pytest.raises(RuntimeError):
        await agent.respond(CHAT, user("Yana"))
    assert storage.get_conversation(CHAT).messages == saved


async def test_cut_off_tool_call_is_not_run(settings, storage, ig) -> None:
    claude = FakeClaude(
        claude_message([tool_use("delete_post", {"media_id": "m1"})], "max_tokens"),
        claude_message([text("Qayta urinaman.")]),
    )
    await make_agent(settings, storage, ig, claude).respond(CHAT, user("O'chir"))
    result = claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and ig.calls == []


async def test_new_conversation_when_idle_too_long_or_changed(settings, make_settings, storage, ig) -> None:
    claude = FakeClaude(*[claude_message([text("ok")]) for _ in range(5)])
    agent = make_agent(settings, storage, ig, claude)
    await agent.respond(CHAT, user("1"))
    await agent.respond(CHAT, user("2"))
    assert len(claude.requests[1]["messages"]) == 3

    conversation = storage.get_conversation(CHAT)
    conversation.updated_at = utcnow() - timedelta(hours=13)
    storage.save_conversation(conversation)
    await agent.respond(CHAT, user("3"))
    assert len(claude.requests[2]["messages"]) == 1  # idle: fresh start

    conversation = storage.get_conversation(CHAT)
    conversation.context_tokens = 10**6
    storage.save_conversation(conversation)
    reply = await agent.respond(CHAT, user("4"))
    assert reply.notice == TOO_LONG_NOTICE and len(claude.requests[3]["messages"]) == 1

    other = make_agent(make_settings(BRAND_GUIDE="Kofe do'koni"), storage, ig, claude)  # different system prompt
    await other.respond(CHAT, user("5"))
    assert len(claude.requests[4]["messages"]) == 1


async def test_notes_are_appended(settings, storage, ig) -> None:
    claude = FakeClaude(claude_message([text("Salom!")]), claude_message([text("Ha, joylandi.")]))
    agent = make_agent(settings, storage, ig, claude)
    await agent.respond(CHAT, user("Salom"))
    await agent.add_note(CHAT, "Request #1 was approved and done: Post joylandi")
    await agent.respond(CHAT, user("Joylandimi?"))
    first, second = claude.requests
    assert_extends(second["messages"], first["messages"])
    assert second["messages"][2]["content"][0]["text"].startswith("[Bot notice] Request #1")


def test_fallback_switch_keeps_only_valid_blocks() -> None:
    message = claude_message(
        [
            text("Boshlayman"),
            THINKING,
            {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}, "trigger": {"type": "refusal"}},
            {"type": "thinking", "thinking": "", "signature": "sig-fallback"},
            text("Tayyor"),
        ]
    )
    kept = assistant_content(message.content)
    assert [(block["type"], block.get("text") or block.get("signature")) for block in kept] == [
        ("text", "Boshlayman"),
        ("thinking", "sig-fallback"),
        ("text", "Tayyor"),
    ]


def test_user_content_header(settings) -> None:
    now = utcnow().astimezone(settings.timezone)
    blocks = build_user_content(now, [], "  ertaga soat 9 da joyla ")
    assert blocks[0]["text"] == f"[{now:%Y-%m-%d %H:%M} Asia/Tashkent, {now:%A}]"
    assert blocks[1] == {"type": "text", "text": "ertaga soat 9 da joyla"}
