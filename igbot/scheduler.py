"""Background loop that publishes scheduled posts and removes old uploads."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .actions import ActionService, PostSpec
from .config import Settings
from .storage import Storage, utcnow

log = logging.getLogger(__name__)

# notify(chat_id, message for the admin, note for the agent)
Notify = Callable[[int, str, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        actions: ActionService,
        notify: Notify,
        *,
        interval: float = 20.0,
        cleanup_every: timedelta = timedelta(hours=1),
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.actions = actions
        self.notify = notify
        self.interval = interval
        self.cleanup_every = cleanup_every
        self._last_cleanup: datetime | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                now = utcnow()
                if self._last_cleanup is None or now - self._last_cleanup >= self.cleanup_every:
                    self._last_cleanup = now
                    removed = self.cleanup_media(now)
                    if removed:
                        log.info("Removed %d old uploads", removed)
            except Exception:
                log.exception("Scheduler iteration failed")
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime | None = None) -> int:
        """Publish every scheduled post that is due; returns how many went out."""
        published = 0
        for post in self.storage.due_scheduled_posts(now or utcnow()):
            if not self.storage.claim_scheduled_post(post.id):
                continue
            try:
                media_id, permalink = await self.actions.publish(PostSpec.from_dict(post.spec))
            except Exception as exc:
                log.warning("Scheduled post #%s failed: %s", post.id, exc)
                self.storage.finish_scheduled_post(post.id, "failed", error=str(exc))
                await self.notify(
                    post.chat_id,
                    f"❌ Rejalashtirilgan post (reja #{post.id}) joylanmadi: {exc}",
                    f"Scheduled post #{post.id} failed to publish: {exc}",
                )
                continue
            self.storage.finish_scheduled_post(post.id, "published", ig_media_id=media_id, permalink=permalink)
            published += 1
            link = permalink or media_id
            await self.notify(
                post.chat_id,
                f"✅ Rejalashtirilgan post (reja #{post.id}) joylandi: {link}",
                f"Scheduled post #{post.id} was published: {link} (media id {media_id}).",
            )
        return published

    def cleanup_media(self, now: datetime | None = None) -> int:
        """Delete uploads older than MEDIA_RETENTION_DAYS that no scheduled post or pending request needs."""
        cutoff = (now or utcnow()) - timedelta(days=self.settings.media_retention_days)
        in_use = self.storage.media_ids_in_use()
        removed = 0
        for record in self.storage.media_created_before(cutoff):
            if record.id in in_use:
                continue
            (self.settings.media_dir / record.filename).unlink(missing_ok=True)
            if record.preview_filename:
                (self.settings.preview_dir / record.preview_filename).unlink(missing_ok=True)
            self.storage.delete_media(record.id)
            removed += 1
        return removed
