"""Async client for the Instagram Graph API (an Instagram professional account connected to a
Facebook Page, used through Facebook Login for Business).

Publishing follows Meta's container flow: create a media container from a public media URL, wait
until Instagram has processed it, then publish the container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

import aiohttp

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,is_comment_enabled"
)
COMMENT_FIELDS = "id,text,username,timestamp,like_count,hidden,replies{id,text,username,timestamp}"
POST_METRICS = ("views", "reach", "likes", "comments", "saved", "shares", "total_interactions")

_RATE_LIMITED = "Meta API so'rovlar limiti oshdi — birozdan keyin qayta urinib ko'ring."
_CODE_HINTS = {
    190: "Instagram access token yaroqsiz yoki muddati tugagan — yangi token olib, IG_ACCESS_TOKEN ni yangilang.",
    10: "Tokenda bu amal uchun ruxsat yo'q — README dagi ruxsatlar ro'yxatini tekshiring.",
    4: _RATE_LIMITED,
    17: _RATE_LIMITED,
    32: _RATE_LIMITED,
    613: _RATE_LIMITED,
    9004: "Instagram faylni yuklab ola olmadi — PUBLIC_BASE_URL internetdan ochiq HTTPS manzil ekanini tekshiring.",
    36003: "Rasm tomonlari nisbati Instagram talabiga mos emas.",
}
_SUBCODE_HINTS = {
    2207010: "Caption juda uzun (ko'pi bilan 2200 belgi).",
    2207026: "Video formati mos emas (MP4, H.264 video, AAC audio tavsiya etiladi).",
    2207042: "24 soatlik post joylash limiti tugagan.",
    2207050: "Instagram akkaunt cheklangan yoki faol emas — ilovada tekshiring.",
    2207051: "Instagram bu amalni spamga o'xshatib blokladi — keyinroq urinib ko'ring.",
    2207052: "Instagram faylni URL orqali ola olmadi — PUBLIC_BASE_URL ni tekshiring.",
}


class InstagramError(Exception):
    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @classmethod
    def from_response(cls, payload: dict[str, Any], status: int) -> InstagramError:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code, subcode = error.get("code"), error.get("error_subcode")
        message = error.get("error_user_msg") or error.get("message") or f"HTTP {status}"
        hint = _SUBCODE_HINTS.get(subcode) or _CODE_HINTS.get(code)
        if hint is None and isinstance(code, int) and 200 <= code < 300:
            hint = _CODE_HINTS[10]  # 2xx codes are missing-permission errors
        text = f"Instagram xatosi: {message}" + (f" ({hint})" if hint else "")
        return cls(text, code=code, subcode=subcode)


@dataclass(frozen=True)
class MediaItem:
    kind: str  # "image" | "video"
    url: str

    @property
    def url_field(self) -> str:
        return "image_url" if self.kind == "image" else "video_url"


class InstagramClient:
    def __init__(
        self,
        access_token: str,
        ig_user_id: str | None = None,
        *,
        api_version: str = "v25.0",
        host: str = "graph.facebook.com",
        base_url: str | None = None,
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
    ) -> None:
        self._token = access_token
        self.ig_user_id = ig_user_id
        self._base_url = base_url or f"https://{host}/{api_version}"
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def user_id(self) -> str:
        if not self.ig_user_id:
            raise InstagramError(
                "Instagram akkaunt ID si aniqlanmagan — IG_USER_ID ni sozlang (`python -m igbot check` yordam beradi)."
            )
        return self.ig_user_id

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call the Graph API. POST sends the parameters as a form body, GET/DELETE as the query string."""
        fields = {key: _form_value(value) for key, value in (params or {}).items()}
        fields["access_token"] = self._token
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        location = {"data": fields} if method == "POST" else {"params": fields}
        try:
            async with self._session.request(method, f"{self._base_url}/{path}", **location) as response:
                status = response.status
                try:
                    payload = await response.json(content_type=None)
                except ValueError:
                    payload = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise InstagramError(f"Instagram API ga ulanib bo'lmadi: {exc}") from exc
        if not isinstance(payload, dict):
            payload = {}
        if status >= 400 or "error" in payload:
            raise InstagramError.from_response(payload, status)
        return payload

    # --- account and posts ---------------------------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        return await self.request(
            "GET", self.user_id, {"fields": "id,username,name,followers_count,follows_count,media_count"}
        )

    async def list_media(self, limit: int) -> list[dict[str, Any]]:
        data = await self.request("GET", f"{self.user_id}/media", {"fields": MEDIA_FIELDS, "limit": limit})
        return data.get("data", [])

    async def get_media(self, media_id: str) -> dict[str, Any]:
        return await self.request("GET", media_id, {"fields": MEDIA_FIELDS + ",children{id,media_type}"})

    async def get_permalink(self, media_id: str) -> str | None:
        return (await self.request("GET", media_id, {"fields": "permalink"})).get("permalink")

    async def publishing_limit(self) -> dict[str, Any]:
        data = await self.request(
            "GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"}
        )
        entry = (data.get("data") or [{}])[0]
        return {"used": entry.get("quota_usage"), "limit": (entry.get("config") or {}).get("quota_total")}

    async def media_insights(self, media_id: str, metrics: Iterable[str] = POST_METRICS) -> dict[str, Any]:
        metrics = list(metrics)
        try:
            return _parse_insights(await self.request("GET", f"{media_id}/insights", {"metric": ",".join(metrics)}))
        except InstagramError as first_error:
            # Instagram rejects the whole request when one metric doesn't exist for this media type.
            values: dict[str, Any] = {}
            for metric in metrics:
                try:
                    values.update(_parse_insights(await self.request("GET", f"{media_id}/insights", {"metric": metric})))
                except InstagramError:
                    continue
            if not values:
                raise first_error
            return values

    async def delete_media(self, media_id: str) -> None:
        data = await self.request("DELETE", media_id)
        if data.get("success") is False:
            raise InstagramError("Instagram postni o'chirmadi.")

    async def set_comments_enabled(self, media_id: str, enabled: bool) -> None:
        await self.request("POST", media_id, {"comment_enabled": enabled})

    # --- comments ------------------------------------------------------------------------

    async def list_comments(self, media_id: str, limit: int) -> list[dict[str, Any]]:
        data = await self.request("GET", f"{media_id}/comments", {"fields": COMMENT_FIELDS, "limit": limit})
        return data.get("data", [])

    async def get_comment(self, comment_id: str) -> dict[str, Any]:
        return await self.request("GET", comment_id, {"fields": "id,text,username,timestamp"})

    async def reply_to_comment(self, comment_id: str, message: str) -> str:
        return (await self.request("POST", f"{comment_id}/replies", {"message": message})).get("id", "")

    async def hide_comment(self, comment_id: str, hide: bool) -> None:
        await self.request("POST", comment_id, {"hide": hide})

    async def delete_comment(self, comment_id: str) -> None:
        await self.request("DELETE", comment_id)

    # --- publishing ----------------------------------------------------------------------

    async def create_container(self, params: dict[str, Any]) -> str:
        data = await self.request("POST", f"{self.user_id}/media", params)
        if "id" not in data:
            raise InstagramError("Instagram media konteyner yaratmadi.")
        return data["id"]

    async def wait_until_ready(self, container_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.poll_timeout
        while True:
            data = await self.request("GET", container_id, {"fields": "status_code,status"})
            status = data.get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Instagram mediani qayta ishlay olmadi: {data.get('status') or status}")
            if loop.time() >= deadline:
                raise InstagramError("Instagram media tayyor bo'lishini kutish vaqti tugadi.")
            await asyncio.sleep(self.poll_interval)

    async def publish_container(self, container_id: str) -> str:
        data = await self.request("POST", f"{self.user_id}/media_publish", {"creation_id": container_id})
        if "id" not in data:
            raise InstagramError("Instagram postni joylamadi.")
        return data["id"]

    async def publish(self, post_type: str, items: list[MediaItem], caption: str = "") -> str:
        """Publish a feed post, carousel, reel or story and return the new Instagram media id."""
        if not items:
            raise ValueError("publish() needs at least one media item")
        captioned = {"caption": caption} if caption else {}
        first = items[0]
        if post_type == "story":
            container = await self.create_container({"media_type": "STORIES", first.url_field: first.url})
        elif len(items) == 1 and first.kind == "video":  # a single video is always published as a reel
            container = await self.create_container(
                {"media_type": "REELS", "video_url": first.url, "share_to_feed": True, **captioned}
            )
        elif len(items) == 1:
            container = await self.create_container({"image_url": first.url, **captioned})
        else:
            children = []
            for item in items:
                params: dict[str, Any] = {"is_carousel_item": True, item.url_field: item.url}
                if item.kind == "video":
                    params["media_type"] = "VIDEO"
                children.append(await self.create_container(params))
            for child in children:
                await self.wait_until_ready(child)
            container = await self.create_container(
                {"media_type": "CAROUSEL", "children": ",".join(children), **captioned}
            )
        await self.wait_until_ready(container)
        return await self.publish_container(container)

    # --- setup helpers -------------------------------------------------------------------

    async def granted_permissions(self) -> list[str]:
        data = await self.request("GET", "me/permissions")
        return sorted(item["permission"] for item in data.get("data", []) if item.get("status") == "granted")

    async def discover_accounts(self) -> list[dict[str, str]]:
        """Instagram professional accounts linked to the Facebook Pages this token can access."""
        data = await self.request(
            "GET", "me/accounts", {"fields": "name,instagram_business_account{id,username}", "limit": 100}
        )
        accounts = []
        for page in data.get("data", []):
            account = page.get("instagram_business_account")
            if account:
                accounts.append({"id": account["id"], "username": account.get("username", ""), "page": page.get("name", "")})
        return accounts


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_insights(payload: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for item in payload.get("data", []):
        if item.get("values"):
            values[item["name"]] = item["values"][0].get("value")
        elif item.get("total_value"):
            values[item["name"]] = item["total_value"].get("value")
    return values
