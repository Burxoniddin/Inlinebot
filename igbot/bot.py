"""Telegram side (aiogram 3): an admin-only chat with the agent, media intake and confirmation buttons."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

import anthropic
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from . import media as media_utils
from . import texts
from .actions import ActionService, PostSpec
from .agent import Agent, AgentRefusal, build_user_content
from .config import Settings
from .instagram import InstagramClient, InstagramError
from .storage import MediaRecord, Storage, utcnow

log = logging.getLogger(__name__)

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # bots can't download bigger files
ALBUM_WAIT = 1.5  # seconds to wait for the rest of an album
MESSAGE_LIMIT = 4096
VIDEO_EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class _Album:
    """Photos/videos sent together arrive as separate messages; they are collected into one agent turn."""

    items: list[tuple[int, MediaRecord]] = field(default_factory=list)
    caption: str = ""
    errors: list[str] = field(default_factory=list)
    downloading: int = 0
    flush: asyncio.Task[None] | None = None


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def confirm_keyboard(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.CONFIRM, callback_data=f"act:{action_id}:ok"),
                InlineKeyboardButton(text=texts.DECLINE, callback_data=f"act:{action_id}:no"),
            ]
        ]
    )


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        agent: Agent,
        actions: ActionService,
        ig: InstagramClient,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.agent = agent
        self.actions = actions
        self.ig = ig
        self.bot = bot
        self._albums: dict[tuple[int, str], _Album] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def routers(self) -> list[Router]:
        is_admin = F.from_user.id.in_(self.settings.admin_ids)
        admin = Router(name="admin")
        admin.message.filter(is_admin)
        admin.callback_query.filter(is_admin)
        admin.message.register(self.on_start, CommandStart())
        admin.message.register(self.on_start, Command("help"))
        admin.message.register(self.on_new, Command("new"))
        admin.message.register(self.on_status, Command("status"))
        admin.message.register(self.on_media, F.photo | F.video | F.animation | F.document)
        admin.message.register(self.on_text, F.text)
        admin.message.register(self.on_unsupported)
        admin.callback_query.register(self.on_button, F.data.startswith("act:"))

        guest = Router(name="guest")
        guest.message.filter(~is_admin)
        guest.callback_query.filter(~is_admin)
        guest.message.register(self.on_guest)
        guest.callback_query.register(self.on_guest_button)
        return [admin, guest]

    # --- commands ------------------------------------------------------------------------

    async def on_start(self, message: Message) -> None:
        await message.answer(texts.START)

    async def on_new(self, message: Message) -> None:
        await self.agent.reset(message.chat.id)
        await message.answer(texts.NEW_CONVERSATION)

    async def on_status(self, message: Message) -> None:
        lines = []
        try:
            account = await self.ig.get_account()
            lines.append(
                f"📷 Instagram: @{account.get('username')} — {account.get('followers_count')} obunachi, "
                f"{account.get('media_count')} ta post"
            )
            quota = await self.ig.publishing_limit()
            lines.append(f"📊 Oxirgi 24 soatda API orqali joylangan: {quota['used']}/{quota['limit'] or 100}")
        except InstagramError as exc:
            lines.append(f"❌ {exc}")
        scheduled = self.storage.list_scheduled_posts()
        lines.append(f"🗓 Rejalashtirilgan postlar: {len(scheduled)} ta")
        for post in scheduled[:10]:
            lines.append(f"   • reja #{post.id} — {self.actions.local_time(post.publish_at)}")
        lines.append(f"🤖 Model: {self.settings.claude_model} (effort: {self.settings.claude_effort})")
        lines.append("✅ Tasdiqlash tugmalari yoqilgan" if self.settings.require_confirmation else "⚠️ Tasdiqlash o'chirilgan")
        if not self.settings.public_base_url:
            lines.append("⚠️ PUBLIC_BASE_URL sozlanmagan — post joylab bo'lmaydi.")
        await message.answer("\n".join(lines), link_preview_options=NO_PREVIEW)

    # --- messages and media --------------------------------------------------------------

    async def on_text(self, message: Message) -> None:
        await self.ask_agent(message.chat.id, message.text or "", [])

    async def on_unsupported(self, message: Message) -> None:
        await message.answer(texts.UNSUPPORTED_MESSAGE)

    async def on_media(self, message: Message) -> None:
        if message.media_group_id is None:
            try:
                record = await self._save_media(message)
            except media_utils.MediaError as exc:
                await message.answer(f"❌ {exc}")
                return
            except Exception:
                log.exception("Could not save media")
                await message.answer(f"❌ {texts.SAVE_FAILED}")
                return
            await self.ask_agent(message.chat.id, message.caption or "", [record])
            return

        key = (message.chat.id, message.media_group_id)
        album = self._albums.setdefault(key, _Album())
        album.downloading += 1
        if album.flush is not None:
            album.flush.cancel()
            album.flush = None
        try:
            album.items.append((message.message_id, await self._save_media(message)))
        except media_utils.MediaError as exc:
            album.errors.append(str(exc))
        except Exception:
            log.exception("Could not save media")
            album.errors.append(texts.SAVE_FAILED)
        finally:
            album.downloading -= 1
        if message.caption:
            album.caption = message.caption
        if album.downloading == 0:
            album.flush = self._spawn(self._flush_album(key))

    async def _flush_album(self, key: tuple[int, str]) -> None:
        await asyncio.sleep(ALBUM_WAIT)
        album = self._albums.pop(key, None)
        if album is None:
            return
        chat_id = key[0]
        if album.errors:
            await self.bot.send_message(chat_id, "❌ " + "\n".join(album.errors))
        records = [record for _, record in sorted(album.items, key=lambda item: item[0])]
        if records:
            await self.ask_agent(chat_id, album.caption, records)

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _save_media(self, message: Message) -> MediaRecord:
        """Download an attached photo/video, store it and return its record."""
        document = message.document
        if message.photo:
            kind, source, mime = "image", message.photo[-1], "image/jpeg"
        elif message.video:
            kind, source, mime = "video", message.video, message.video.mime_type or "video/mp4"
        elif message.animation:
            kind, source, mime = "video", message.animation, message.animation.mime_type or "video/mp4"
        elif document is not None and (document.mime_type or "").startswith(("image/", "video/")):
            mime = document.mime_type or ""
            kind, source = ("image" if mime.startswith("image/") else "video"), document
        else:
            raise media_utils.MediaError(texts.UNSUPPORTED_FILE)
        if kind == "video" and mime not in VIDEO_EXTENSIONS:
            raise media_utils.MediaError(texts.UNSUPPORTED_VIDEO)
        if (source.file_size or 0) > TELEGRAM_DOWNLOAD_LIMIT:
            raise media_utils.MediaError(texts.FILE_TOO_BIG)

        token = secrets.token_hex(16)
        download = self.settings.media_dir / f"{token}.part"
        filename = f"{token}.jpg" if kind == "image" else f"{token}{VIDEO_EXTENSIONS[mime]}"
        target = self.settings.media_dir / filename
        preview: str | None = f"{token}.jpg"
        duration = None
        try:
            await self.bot.download(source, destination=download)
            if kind == "image":
                width, height = await asyncio.to_thread(media_utils.normalize_image, download, target)
                await asyncio.to_thread(media_utils.make_preview, target, self.settings.preview_dir / preview)
            else:
                download.rename(target)
                width, height = getattr(source, "width", None), getattr(source, "height", None)
                duration = getattr(source, "duration", None)
                preview = await self._video_preview(getattr(source, "thumbnail", None), preview)
            return self.storage.add_media(message.chat.id, kind, filename, preview, width, height, duration)
        except BaseException as exc:
            target.unlink(missing_ok=True)
            (self.settings.preview_dir / f"{token}.jpg").unlink(missing_ok=True)
            if isinstance(exc, TelegramAPIError):
                log.warning("Download failed: %s", exc)
                raise media_utils.MediaError(texts.SAVE_FAILED) from exc
            raise
        finally:
            download.unlink(missing_ok=True)

    async def _video_preview(self, thumbnail: Any, preview: str) -> str | None:
        """Claude can't watch videos; it gets Telegram's thumbnail instead (when there is one)."""
        if thumbnail is None:
            return None
        download = self.settings.preview_dir / f"{preview}.part"
        try:
            await self.bot.download(thumbnail, destination=download)
            await asyncio.to_thread(media_utils.make_preview, download, self.settings.preview_dir / preview)
            return preview
        except (TelegramAPIError, media_utils.MediaError) as exc:
            log.warning("No preview for video: %s", exc)
            return None
        finally:
            download.unlink(missing_ok=True)

    # --- the agent -----------------------------------------------------------------------

    async def ask_agent(self, chat_id: int, text: str, media: list[MediaRecord]) -> None:
        content = build_user_content(utcnow().astimezone(self.settings.timezone), media, text)
        try:
            async with ChatActionSender.typing(bot=self.bot, chat_id=chat_id):
                reply = await self.agent.respond(chat_id, content)
        except AgentRefusal:
            await self.send_text(chat_id, texts.REFUSED)
            return
        except anthropic.AuthenticationError:
            await self.send_text(chat_id, texts.AI_AUTH)
            return
        except anthropic.RateLimitError:
            await self.send_text(chat_id, texts.AI_BUSY)
            return
        except anthropic.APIStatusError as exc:
            log.warning("Claude API error %s: %s", exc.status_code, exc.message)
            busy = exc.status_code in (500, 502, 503, 529)
            await self.send_text(chat_id, texts.AI_BUSY if busy else texts.AI_ERROR.format(error=exc.message))
            return
        except anthropic.APIConnectionError:
            await self.send_text(chat_id, texts.AI_UNREACHABLE)
            return
        except Exception:
            log.exception("Agent turn failed")
            await self.send_text(chat_id, texts.UNEXPECTED)
            return
        if reply.notice:
            await self.send_text(chat_id, reply.notice)
        await self.send_text(chat_id, reply.text)
        for action_id in reply.action_ids:
            await self.send_action_card(chat_id, action_id)

    async def send_text(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            await self.bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW)

    async def notify(self, chat_id: int, text: str, note: str) -> None:
        """Report something that happened in the background to the admin and to the agent."""
        try:
            await self.send_text(chat_id, text)
        except TelegramAPIError as exc:
            log.warning("Could not notify chat %s: %s", chat_id, exc)
        await self.agent.add_note(chat_id, note)

    async def report_interrupted(self) -> None:
        """After a restart: tell admins about work that was cut off mid-way."""
        for post in self.storage.fail_interrupted_posts():
            await self.notify(
                post.chat_id,
                texts.INTERRUPTED_POST.format(id=post.id),
                f"Scheduled post #{post.id} was interrupted while publishing; it may or may not be on Instagram.",
            )
        for action in self.storage.fail_interrupted_actions():
            await self.notify(
                action.chat_id,
                texts.INTERRUPTED_ACTION.format(id=action.id),
                f"Request #{action.id} was interrupted while running; its result on Instagram is unknown.",
            )

    # --- confirmation cards --------------------------------------------------------------

    async def send_action_card(self, chat_id: int, action_id: int) -> None:
        action = self.storage.get_action(action_id)
        if action is None or action.status != "pending":
            return
        if action.kind in ("publish", "schedule"):
            await self._send_preview(chat_id, PostSpec.from_dict(action.payload["spec"]))
        await self.bot.send_message(
            chat_id,
            self.actions.describe(action),
            reply_markup=confirm_keyboard(action_id),
            link_preview_options=NO_PREVIEW,
        )

    async def _send_preview(self, chat_id: int, spec: PostSpec) -> None:
        files = []
        for media_id in spec.media_ids:
            record = self.storage.get_media(media_id)
            if record is not None:
                files.append((record.kind, FSInputFile(self.settings.media_dir / record.filename)))
        try:
            if len(files) == 1:
                kind, file = files[0]
                if kind == "image":
                    await self.bot.send_photo(chat_id, file)
                else:
                    await self.bot.send_video(chat_id, file)
            elif files:
                await self.bot.send_media_group(
                    chat_id,
                    [InputMediaPhoto(media=file) if kind == "image" else InputMediaVideo(media=file) for kind, file in files],
                )
        except TelegramAPIError as exc:
            log.warning("Could not send post preview: %s", exc)

    async def on_button(self, callback: CallbackQuery) -> None:
        try:
            _, raw_id, decision = (callback.data or "").split(":")
            action_id = int(raw_id)
        except ValueError:
            await callback.answer()
            return
        action = self.storage.get_action(action_id)
        chat_id = callback.message.chat.id if callback.message else None
        if action is None or action.chat_id != chat_id:
            await callback.answer(texts.NOT_FOUND, show_alert=True)
            return
        if action.status != "pending":
            await callback.answer(texts.ALREADY_HANDLED, show_alert=True)
            return
        message_id = callback.message.message_id
        card = self.actions.describe(action)

        if decision != "ok":
            if not self.storage.set_action_status(action_id, "pending", "cancelled"):
                await callback.answer(texts.ALREADY_HANDLED, show_alert=True)
                return
            await callback.answer(texts.CANCELLED)
            await self._edit_card(chat_id, message_id, f"{card}\n\n{texts.CANCELLED}")
            await self.agent.add_note(chat_id, f"The admin declined request #{action_id}; nothing changed on Instagram.")
            return

        await callback.answer(texts.RUNNING)
        await self._edit_card(chat_id, message_id, f"{card}\n\n{texts.RUNNING}")
        outcome = await self.actions.run(action_id)
        if outcome.status == "stale":  # a second tap raced the first one, which reports the result
            return
        mark = {"done": "✅", "expired": "⌛"}.get(outcome.status, "❌")
        await self._edit_card(chat_id, message_id, f"{card}\n\n{mark} {outcome.message}")
        state = {"done": "was approved and done", "expired": "had expired and was not carried out"}.get(
            outcome.status, "was approved but failed"
        )
        await self.agent.add_note(chat_id, f"Request #{action_id} {state}: {outcome.message}")

    async def _edit_card(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                text=text[:MESSAGE_LIMIT], chat_id=chat_id, message_id=message_id, reply_markup=None,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError as exc:
            log.warning("Could not update confirmation card: %s", exc)

    # --- everyone else -------------------------------------------------------------------

    async def on_guest(self, message: Message) -> None:
        if message.chat.type == "private" and message.from_user is not None:
            await message.answer(texts.NOT_ADMIN.format(user_id=message.from_user.id))

    async def on_guest_button(self, callback: CallbackQuery) -> None:
        await callback.answer("⛔", show_alert=True)
