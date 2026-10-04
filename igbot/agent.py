"""The Claude agent behind the bot.

Each Telegram chat has one conversation that is only ever appended to: earlier turns are sent again
unchanged on every request. Claude's thinking blocks are bound to the exact conversation before them,
and an unchanged prefix also keeps the prompt cache warm. So instead of trimming old turns, the agent
starts a fresh conversation when the old one has gone idle, grown too long, or the prompt/tools changed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

import anthropic

from .config import Settings
from .prompts import build_system_prompt
from .storage import Conversation, MediaRecord, Storage, utcnow
from .tools import TOOLS, ToolBox

log = logging.getLogger(__name__)

MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 12
# drop_block: if a replayed thinking block ever fails the conversation check, the API drops it instead of
# rejecting the request.
BETAS = ["thinking-binding-controls-2026-08-01"]
# fallbacks="default": if a safety classifier declines a request, the API retries it on a fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
LOCAL_IMAGE = "local_image"  # stored placeholder for an uploaded photo, expanded to base64 per request
TOO_LONG_NOTICE = "ℹ️ Suhbat juda uzun bo'lib ketdi, shuning uchun yangisi boshlandi (oldingi xabarlar unutildi)."
TOO_MANY_STEPS = "Bu so'rov juda ko'p qadam talab qildi, shuning uchun to'xtadim. Iltimos, uni qismlarga bo'lib yozing."


class AgentRefusal(Exception):
    """Claude declined the request (stop_reason "refusal")."""


@dataclass
class AgentReply:
    text: str
    action_ids: list[int] = field(default_factory=list)
    notice: str | None = None


def build_user_content(now: datetime, media: Iterable[MediaRecord], text: str) -> list[dict[str, Any]]:
    """One admin message: a local date/time header (for "tomorrow at 9"), attached media, then the text."""
    zone = getattr(now.tzinfo, "key", None) or now.tzname()
    blocks: list[dict[str, Any]] = [{"type": "text", "text": f"[{now:%Y-%m-%d %H:%M} {zone}, {now:%A}]"}]
    media = list(media)
    for record in media:
        details = ["photo" if record.kind == "image" else "video"]
        if record.width and record.height:
            details.append(f"{record.width}x{record.height}")
        if record.duration:
            details.append(f"{record.duration:.0f}s")
        blocks.append({"type": "text", "text": f"[media #{record.id}: {', '.join(details)}]"})
        if record.preview_filename:
            blocks.append({"type": LOCAL_IMAGE, "media_id": record.id})
    if text.strip():
        blocks.append({"type": "text", "text": text.strip()})
    elif media:
        blocks.append({"type": "text", "text": "(The admin sent media without a message.)"})
    return blocks


def assistant_content(blocks: Iterable[Any]) -> list[dict[str, Any]]:
    """Response content as stored in the history. After a server-side fallback, content that the declining
    model produced before the last `fallback` marker is dropped except text, as the API requires; the markers
    themselves are informational and dropped too."""
    dumped = [block.to_dict(mode="json") for block in blocks]
    boundary = max((index for index, block in enumerate(dumped) if block.get("type") == "fallback"), default=-1)
    return [
        block
        for index, block in enumerate(dumped)
        if block.get("type") != "fallback" and (index > boundary or block.get("type") == "text")
    ]


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        result["is_error"] = True
    return result


class Agent:
    def __init__(self, settings: Settings, storage: Storage, client: anthropic.AsyncAnthropic, toolbox: ToolBox) -> None:
        self.settings = settings
        self.storage = storage
        self.client = client
        self.toolbox = toolbox
        self.system_prompt = build_system_prompt(
            timezone_name=settings.timezone.key,
            require_confirmation=settings.require_confirmation,
            brand_guide=settings.brand_guide,
        )
        identity = json.dumps(
            {"model": settings.claude_model, "system": self.system_prompt, "tools": TOOLS}, sort_keys=True
        )
        self.fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def reset(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            self.storage.delete_conversation(chat_id)

    async def add_note(self, chat_id: int, text: str) -> None:
        """Tell the agent about something that happened outside the chat (a button press, a scheduled post)."""
        async with self._lock(chat_id):
            conversation, _ = self._conversation(chat_id)
            self._append_notes(conversation, [text])

    async def respond(self, chat_id: int, content: list[dict[str, Any]]) -> AgentReply:
        async with self._lock(chat_id):
            conversation, notice = self._conversation(chat_id)
            messages = conversation.messages + [{"role": "user", "content": content}]
            requests: list[int] = []
            try:
                text, context_tokens = await self._run(chat_id, messages, requests)
            except BaseException:
                self._abandon_turn(conversation, requests)
                raise
            conversation.messages = messages
            self._save(conversation, context_tokens)
            return AgentReply(text=text, action_ids=requests, notice=notice)

    def _abandon_turn(self, conversation: Conversation, requests: list[int]) -> None:
        """Nothing from a failed turn is kept, so the next request continues from the last saved turn. Requests
        the turn prepared are withdrawn (the admin never saw them explained); anything it already carried out
        (REQUIRE_CONFIRMATION=false) is recorded as a note so the agent doesn't do it a second time."""
        notes = []
        for action_id in requests:
            if self.storage.set_action_status(action_id, "pending", "cancelled", "AI javobi yakunlanmadi"):
                continue
            action = self.storage.get_action(action_id)
            if action is not None and action.status in ("done", "failed"):
                notes.append(f"Request #{action_id} ({action.kind}) {action.status} in a reply that was cut off: {action.result}")
        if notes:
            self._append_notes(conversation, notes)

    def _append_notes(self, conversation: Conversation, notes: list[str]) -> None:
        for note in notes:
            conversation.messages.append({"role": "user", "content": [{"type": "text", "text": f"[Bot notice] {note}"}]})
        self._save(conversation, conversation.context_tokens)

    # --- conversation lifecycle ----------------------------------------------------------

    def _conversation(self, chat_id: int) -> tuple[Conversation, str | None]:
        current = self.storage.get_conversation(chat_id)
        notice = None
        if current is not None and current.fingerprint == self.fingerprint:
            idle = utcnow() - current.updated_at > timedelta(hours=self.settings.conversation_idle_hours)
            if current.context_tokens > self.settings.max_context_tokens:
                notice = TOO_LONG_NOTICE
            elif not idle:
                return current, None
        return Conversation(chat_id, self.fingerprint, [], 0, utcnow()), notice

    def _save(self, conversation: Conversation, context_tokens: int) -> None:
        conversation.context_tokens = context_tokens
        conversation.updated_at = utcnow()
        self.storage.save_conversation(conversation)

    # --- the tool loop -------------------------------------------------------------------

    async def _run(self, chat_id: int, messages: list[dict[str, Any]], created: list[int]) -> tuple[str, int]:
        context_tokens = 0
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._create(messages)
            usage = response.usage
            context_tokens = (
                usage.input_tokens
                + (usage.cache_read_input_tokens or 0)
                + (usage.cache_creation_input_tokens or 0)
                + usage.output_tokens
            )
            if response.stop_reason == "refusal":
                raise AgentRefusal(getattr(response.stop_details, "category", None) or "refusal")
            content = assistant_content(response.content)
            tool_uses = [block for block in content if block.get("type") == "tool_use"]
            if not tool_uses:
                text = "\n\n".join(
                    block["text"].strip() for block in content if block.get("type") == "text" and block["text"].strip()
                )
                if text:  # a reply with no text at all isn't kept: an empty assistant turn can't be sent back
                    messages.append({"role": "assistant", "content": content})
                return text or "✅", context_tokens
            messages.append({"role": "assistant", "content": content})
            cut_off = response.stop_reason == "max_tokens"
            results = []
            for block in tool_uses:
                if cut_off:  # the call's input may be incomplete: don't run it
                    results.append(_tool_result(block["id"], "Your response was cut off before this tool call was complete. Call it again.", True))
                    continue
                outcome = await self.toolbox.run(chat_id, block["name"], block["input"], created)
                results.append(_tool_result(block["id"], outcome.content, outcome.is_error))
            messages.append({"role": "user", "content": results})
        return TOO_MANY_STEPS, context_tokens

    async def _create(self, messages: list[dict[str, Any]]) -> Any:
        params: dict[str, Any] = {
            "model": self.settings.claude_model,
            "max_tokens": MAX_TOKENS,
            "system": self.system_prompt,
            "tools": TOOLS,
            "messages": self._expand(messages),
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            "output_config": {"effort": self.settings.claude_effort},
            "cache_control": {"type": "ephemeral"},
            "betas": list(BETAS),
        }
        if self.settings.claude_fallbacks:
            params["betas"].append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        return await self.client.beta.messages.create(**params)

    def _expand(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace stored photo placeholders with the actual image data. The preview file never changes,
        so every request carries exactly the same bytes for an earlier turn."""
        expanded = []
        for message in messages:
            content = message["content"]
            if isinstance(content, list) and any(block.get("type") == LOCAL_IMAGE for block in content):
                content = [self._image_block(block) if block.get("type") == LOCAL_IMAGE else block for block in content]
                message = {**message, "content": content}
            expanded.append(message)
        return expanded

    def _image_block(self, placeholder: dict[str, Any]) -> dict[str, Any]:
        media_id = placeholder["media_id"]
        record = self.storage.get_media(media_id)
        try:
            if record is None or record.preview_filename is None:
                raise FileNotFoundError(media_id)
            data = (self.settings.preview_dir / record.preview_filename).read_bytes()
        except OSError:
            return {"type": "text", "text": f"[the preview of media #{media_id} is no longer available]"}
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode("ascii")},
        }
