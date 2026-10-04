"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

T = TypeVar("T")


class ConfigError(Exception):
    """A required setting is missing or has an invalid value."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_ids: frozenset[int]
    ig_access_token: str
    ig_user_id: str | None
    graph_api_version: str
    graph_host: str
    public_base_url: str | None
    web_host: str
    web_port: int
    data_dir: Path
    timezone: ZoneInfo
    claude_model: str
    claude_effort: str
    claude_fallbacks: bool
    require_confirmation: bool
    brand_guide: str
    conversation_idle_hours: float
    max_context_tokens: int
    media_retention_days: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "igbot.sqlite3"

    @property
    def media_dir(self) -> Path:
        """Files Instagram downloads when we publish; served publicly under /media/."""
        return self.data_dir / "media"

    @property
    def preview_dir(self) -> Path:
        """Small JPEG previews that are shown to Claude; never served."""
        return self.data_dir / "previews"

    def media_url(self, filename: str) -> str:
        if not self.public_base_url:
            raise ConfigError(
                "PUBLIC_BASE_URL sozlanmagan: Instagram rasm va videoni internetdan ochiq "
                "HTTPS manzildan yuklab oladi, shuning uchun post joylab bo'lmaydi."
            )
        return f"{self.public_base_url}/media/{filename}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return (env.get(name) or default).strip()

    def required(name: str) -> str:
        value = get(name)
        if not value:
            raise ConfigError(f"{name} sozlanmagan (.env.example ga qarang)")
        return value

    def number(name: str, default: str, cast: Callable[[str], T]) -> T:
        raw = get(name, default)
        try:
            return cast(raw)
        except ValueError:
            raise ConfigError(f"{name} son bo'lishi kerak, berilgan: {raw!r}") from None

    def flag(name: str, default: bool) -> bool:
        raw = get(name).lower()
        if not raw:
            return default
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name} true yoki false bo'lishi kerak, berilgan: {raw!r}")

    try:
        admin_ids = frozenset(int(part) for part in required("ADMIN_IDS").replace(" ", "").split(",") if part)
    except ValueError:
        admin_ids = frozenset()
    if not admin_ids:
        raise ConfigError("ADMIN_IDS vergul bilan ajratilgan Telegram ID raqamlari bo'lishi kerak, masalan 12345,67890")

    public_base_url = get("PUBLIC_BASE_URL").rstrip("/") or None
    if public_base_url and not public_base_url.startswith(("https://", "http://")):
        raise ConfigError("PUBLIC_BASE_URL https:// bilan boshlanishi kerak")

    timezone_name = get("TIMEZONE", "Asia/Tashkent")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Noma'lum TIMEZONE: {timezone_name!r}") from None

    effort = get("CLAUDE_EFFORT", "medium").lower()
    if effort not in EFFORT_LEVELS:
        raise ConfigError(f"CLAUDE_EFFORT quyidagilardan biri bo'lishi kerak: {', '.join(EFFORT_LEVELS)}")

    brand_guide = get("BRAND_GUIDE")
    brand_guide_file = get("BRAND_GUIDE_FILE")
    if brand_guide_file:
        try:
            brand_guide = Path(brand_guide_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"BRAND_GUIDE_FILE ni o'qib bo'lmadi: {exc}") from None

    return Settings(
        telegram_token=required("TELEGRAM_BOT_TOKEN"),
        admin_ids=admin_ids,
        ig_access_token=required("IG_ACCESS_TOKEN"),
        ig_user_id=get("IG_USER_ID") or None,
        graph_api_version=get("GRAPH_API_VERSION", "v25.0"),
        graph_host=get("GRAPH_API_HOST", "graph.facebook.com"),
        public_base_url=public_base_url,
        web_host=get("WEB_HOST", "0.0.0.0"),
        web_port=number("WEB_PORT", get("PORT", "8080"), int),
        data_dir=Path(get("DATA_DIR", "data")),
        timezone=timezone,
        claude_model=get("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=effort,
        claude_fallbacks=flag("CLAUDE_FALLBACKS", True),
        require_confirmation=flag("REQUIRE_CONFIRMATION", True),
        brand_guide=brand_guide,
        conversation_idle_hours=number("CONVERSATION_IDLE_HOURS", "12", float),
        max_context_tokens=number("MAX_CONTEXT_TOKENS", "150000", int),
        media_retention_days=number("MEDIA_RETENTION_DAYS", "14", int),
    )
