"""Changes to Instagram: validation, the text of the confirmation card, and execution.

The agent never changes Instagram directly. Its tools create a request (an "action"); the admin
approves it with a button and only then does `ActionService.run` carry it out.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import media as media_utils
from .config import ConfigError, Settings
from .instagram import InstagramClient, InstagramError, MediaItem
from .storage import ActionRecord, MediaRecord, Storage, to_iso, utcnow

log = logging.getLogger(__name__)

POST_TYPES = ("feed", "reel", "story")
MAX_CAROUSEL_ITEMS = 10
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_MENTIONS = 20
REQUEST_TTL = timedelta(hours=24)

_HASHTAG = re.compile(r"#\w+")
_MENTION = re.compile(r"(?<![\w.])@[\w.]+")


class ActionError(Exception):
    """A request the bot won't carry out; the message (Uzbek) explains why."""


@dataclass(frozen=True)
class PostSpec:
    post_type: str
    media_ids: tuple[int, ...]
    caption: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"post_type": self.post_type, "media_ids": list(self.media_ids), "caption": self.caption}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostSpec:
        return cls(data["post_type"], tuple(int(media_id) for media_id in data["media_ids"]), data.get("caption", ""))


@dataclass(frozen=True)
class Outcome:
    status: str  # "done" | "failed" | "expired" | "stale" (already handled elsewhere)
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "done"


def check_caption(caption: str) -> None:
    if len(caption) > MAX_CAPTION_CHARS:
        raise ActionError(f"Caption {len(caption)} belgidan iborat — Instagram ko'pi bilan {MAX_CAPTION_CHARS} ta ruxsat beradi.")
    if len(_HASHTAG.findall(caption)) > MAX_HASHTAGS:
        raise ActionError(f"Captionda {MAX_HASHTAGS} tadan ko'p heshteg bor.")
    if len(_MENTION.findall(caption)) > MAX_MENTIONS:
        raise ActionError(f"Captionda {MAX_MENTIONS} tadan ko'p @belgi bor.")


class ActionService:
    def __init__(self, settings: Settings, storage: Storage, ig: InstagramClient) -> None:
        self.settings = settings
        self.storage = storage
        self.ig = ig

    def local_time(self, moment: datetime) -> str:
        return moment.astimezone(self.settings.timezone).strftime("%Y-%m-%d %H:%M")

    # --- validation ----------------------------------------------------------------------

    def check_post(self, spec: PostSpec) -> list[MediaRecord]:
        if spec.post_type not in POST_TYPES:
            raise ActionError(f"Noma'lum post turi: {spec.post_type}")
        if not spec.media_ids:
            raise ActionError("Post uchun kamida bitta rasm yoki video kerak.")
        if len(set(spec.media_ids)) != len(spec.media_ids):
            raise ActionError("Bitta media ikki marta tanlangan.")
        records = []
        for media_id in spec.media_ids:
            record = self.storage.get_media(media_id)
            if record is None or not (self.settings.media_dir / record.filename).is_file():
                raise ActionError(f"Media #{media_id} topilmadi — faylni botga qayta yuboring.")
            records.append(record)
        if spec.post_type == "reel" and (len(records) != 1 or records[0].kind != "video"):
            raise ActionError("Reels uchun aynan bitta video kerak.")
        if spec.post_type == "story" and len(records) != 1:
            raise ActionError("Story uchun aynan bitta rasm yoki video kerak.")
        if len(records) > MAX_CAROUSEL_ITEMS:
            raise ActionError(f"Karuselda ko'pi bilan {MAX_CAROUSEL_ITEMS} ta media bo'ladi.")
        check_caption(spec.caption)
        return records

    def parse_publish_time(self, text: str) -> datetime:
        try:
            local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            raise ActionError("Vaqt YYYY-MM-DD HH:MM ko'rinishida bo'lishi kerak.") from None
        moment = local.replace(tzinfo=self.settings.timezone).astimezone(timezone.utc)
        now = utcnow()
        if moment <= now + timedelta(minutes=1):
            raise ActionError(f"Vaqt kelajakda bo'lishi kerak (hozir {self.local_time(now)}).")
        if moment > now + timedelta(days=365):
            raise ActionError("Bir yildan uzoq muddatga rejalashtirib bo'lmaydi.")
        return moment

    # --- creating requests -----------------------------------------------------------------

    def request_publish(self, chat_id: int, spec: PostSpec) -> ActionRecord:
        self.check_post(spec)
        return self.storage.create_action(chat_id, "publish", {"spec": spec.to_dict()})

    def request_schedule(self, chat_id: int, spec: PostSpec, publish_at: str) -> ActionRecord:
        self.check_post(spec)
        moment = self.parse_publish_time(publish_at)
        return self.storage.create_action(chat_id, "schedule", {"spec": spec.to_dict(), "publish_at": to_iso(moment)})

    def request(self, chat_id: int, kind: str, payload: dict[str, Any]) -> ActionRecord:
        return self.storage.create_action(chat_id, kind, payload)

    # --- confirmation card -----------------------------------------------------------------

    def describe(self, action: ActionRecord) -> str:
        payload = action.payload
        if action.kind in ("publish", "schedule"):
            spec = PostSpec.from_dict(payload["spec"])
            if action.kind == "publish":
                lines = [f"📤 So'rov #{action.id}: post joylash"]
            else:
                when = datetime.fromisoformat(payload["publish_at"])
                lines = [f"🗓 So'rov #{action.id}: postni rejalashtirish", f"Vaqt: {self.local_time(when)}"]
            lines.append(f"Turi: {self._post_label(spec)}")
            lines.append("Media: " + ", ".join(f"#{media_id}" for media_id in spec.media_ids))
            if spec.post_type == "story":
                lines.append("(Story'da caption bo'lmaydi.)")
            elif spec.caption:
                lines += ["", "Caption:", spec.caption]
            else:
                lines.append("Caption: (bo'sh)")
        elif action.kind == "delete_post":
            lines = [
                f"🗑 So'rov #{action.id}: postni o'chirish",
                f"Post: {payload.get('permalink') or payload['media_id']}",
                f"Sana: {payload.get('date') or '?'}",
                "Caption: " + (payload.get("caption") or "(bo'sh)"),
                "",
                "⚠️ O'chirilgan postni qaytarib bo'lmaydi.",
            ]
        elif action.kind == "reply_comment":
            lines = [
                f"💬 So'rov #{action.id}: izohga javob",
                f"Izoh: @{payload.get('username') or '?'}: {payload.get('comment_text') or ''}",
                "",
                f"Javob: {payload['message']}",
            ]
        elif action.kind == "hide_comment":
            title = "izohni yashirish" if payload["hide"] else "izohni qayta ko'rsatish"
            lines = [f"🙈 So'rov #{action.id}: {title}", f"Izoh: @{payload.get('username') or '?'}: {payload.get('comment_text') or ''}"]
        elif action.kind == "delete_comment":
            lines = [
                f"🗑 So'rov #{action.id}: izohni o'chirish",
                f"Izoh: @{payload.get('username') or '?'}: {payload.get('comment_text') or ''}",
                "",
                "⚠️ O'chirilgan izohni qaytarib bo'lmaydi.",
            ]
        elif action.kind == "set_comments":
            title = "izohlarni yoqish" if payload["enabled"] else "izohlarni o'chirib qo'yish"
            lines = [f"💬 So'rov #{action.id}: {title}", f"Post: {payload.get('permalink') or payload['media_id']}"]
        elif action.kind == "cancel_schedule":
            when = datetime.fromisoformat(payload["publish_at"])
            lines = [
                f"🗓 So'rov #{action.id}: rejani bekor qilish",
                f"Reja #{payload['schedule_id']} — {self.local_time(when)}",
                "Caption: " + (payload.get("caption") or "(bo'sh)"),
            ]
        else:
            lines = [f"So'rov #{action.id}: {action.kind}"]
        return "\n".join(lines)

    def _post_label(self, spec: PostSpec) -> str:
        if spec.post_type == "story":
            return "Story"
        if spec.post_type == "reel":
            return "Reels"
        if len(spec.media_ids) > 1:
            return f"Karusel ({len(spec.media_ids)} ta media)"
        record = self.storage.get_media(spec.media_ids[0])
        if record is not None and record.kind == "video":
            return "Reels (bitta video Reels sifatida joylanadi)"
        return "Lenta posti (rasm)"

    # --- execution -------------------------------------------------------------------------

    async def run(self, action_id: int) -> Outcome:
        """Carry out a pending request exactly once."""
        refused = self.claim(action_id)
        if refused is not None:
            return refused
        action = self.storage.get_action(action_id)
        assert action is not None
        return await self.execute(action)

    def claim(self, action_id: int) -> Outcome | None:
        """Reserve a pending request for execution. Returns None if it is now ours to run, otherwise why not.
        It doesn't await, so two button taps can't both get the request."""
        action = self.storage.get_action(action_id)
        if action is not None and action.status == "pending" and utcnow() - action.created_at > REQUEST_TTL:
            if self.storage.set_action_status(action_id, "pending", "expired"):
                return Outcome("expired", "So'rov eskirgan (24 soatdan ko'p vaqt o'tgan) — qaytadan so'rang.")
        if action is None or not self.storage.set_action_status(action_id, "pending", "running"):
            return Outcome("stale", "Bu so'rov allaqachon ko'rib chiqilgan.")
        return None

    async def execute(self, action: ActionRecord) -> Outcome:
        """Carry out a request reserved with `claim`."""
        try:
            message = await self._carry_out(action)
        except (ActionError, ConfigError, InstagramError, media_utils.MediaError) as exc:
            self.storage.set_action_status(action.id, "running", "failed", str(exc))
            return Outcome("failed", str(exc))
        except Exception as exc:
            log.exception("Request #%s failed", action.id)
            self.storage.set_action_status(action.id, "running", "failed", f"Kutilmagan xato: {exc}")
            return Outcome("failed", f"Kutilmagan xato: {exc}")
        self.storage.set_action_status(action.id, "running", "done", message)
        return Outcome("done", message)

    async def _carry_out(self, action: ActionRecord) -> str:
        payload = action.payload
        if action.kind == "publish":
            media_id, permalink = await self.publish(PostSpec.from_dict(payload["spec"]))
            return f"Post joylandi: {permalink or media_id}"
        if action.kind == "schedule":
            spec = PostSpec.from_dict(payload["spec"])
            self.check_post(spec)
            moment = datetime.fromisoformat(payload["publish_at"])
            if moment <= utcnow():
                raise ActionError("Rejalashtirilgan vaqt o'tib ketdi — yangi vaqt bilan qayta so'rang.")
            post = self.storage.add_scheduled_post(action.chat_id, spec.to_dict(), moment)
            return f"Post {self.local_time(moment)} ga rejalashtirildi (reja #{post.id})."
        if action.kind == "cancel_schedule":
            if not self.storage.cancel_scheduled_post(payload["schedule_id"]):
                raise ActionError(f"Reja #{payload['schedule_id']} allaqachon joylangan yoki bekor qilingan.")
            return f"Reja #{payload['schedule_id']} bekor qilindi."
        if action.kind == "delete_post":
            await self.ig.delete_media(payload["media_id"])
            return "Post Instagramdan o'chirildi."
        if action.kind == "reply_comment":
            await self.ig.reply_to_comment(payload["comment_id"], payload["message"])
            return "Izohga javob yozildi."
        if action.kind == "hide_comment":
            await self.ig.hide_comment(payload["comment_id"], payload["hide"])
            return "Izoh yashirildi." if payload["hide"] else "Izoh qayta ko'rsatildi."
        if action.kind == "delete_comment":
            await self.ig.delete_comment(payload["comment_id"])
            return "Izoh o'chirildi."
        if action.kind == "set_comments":
            await self.ig.set_comments_enabled(payload["media_id"], payload["enabled"])
            return "Izohlar yoqildi." if payload["enabled"] else "Izohlar o'chirib qo'yildi."
        raise ActionError(f"Noma'lum amal turi: {action.kind}")

    async def publish(self, spec: PostSpec) -> tuple[str, str | None]:
        """Publish a post now; returns (Instagram media id, permalink)."""
        records = self.check_post(spec)
        replaced: dict[int, str] = {}
        images = [record for record in records if record.kind == "image"]
        try:
            if spec.post_type == "feed" and images:
                replaced = await asyncio.to_thread(self._fit_feed_images, images)
            items = [
                MediaItem(record.kind, self.settings.media_url(replaced.get(record.id, record.filename)))
                for record in records
            ]
            caption = "" if spec.post_type == "story" else spec.caption
            media_id = await self.ig.publish(spec.post_type, items, caption)
        finally:
            for filename in replaced.values():
                (self.settings.media_dir / filename).unlink(missing_ok=True)
        try:
            permalink = await self.ig.get_permalink(media_id)
        except InstagramError:
            permalink = None
        return media_id, permalink

    def _fit_feed_images(self, images: list[MediaRecord]) -> dict[int, str]:
        """Pad feed photos into Instagram's aspect-ratio limits. Returns media id -> temporary file for
        each photo that had to change; the others are published from their original file."""
        fitted = media_utils.prepare_feed_images([self.settings.media_dir / record.filename for record in images])
        replaced: dict[int, str] = {}
        try:
            for record, image in zip(images, fitted):
                if image.size == (record.width, record.height):
                    continue
                filename = f"{secrets.token_hex(16)}.jpg"
                replaced[record.id] = filename
                media_utils.save_jpeg(image, self.settings.media_dir / filename)
        except BaseException:
            for filename in replaced.values():
                (self.settings.media_dir / filename).unlink(missing_ok=True)
            raise
        return replaced
