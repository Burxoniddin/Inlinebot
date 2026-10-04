from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from anthropic.types.beta import BetaMessage
from PIL import Image

from igbot.actions import ActionService
from igbot.config import Settings, load_settings
from igbot.instagram import InstagramClient
from igbot.storage import MediaRecord, Storage

IG_USER = "ig1"
BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "ADMIN_IDS": "1001",
    "IG_ACCESS_TOKEN": "ig-token",
    "IG_USER_ID": IG_USER,
    "PUBLIC_BASE_URL": "https://bot.example.com",
}
CHAT = 1001


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def make(**overrides: str) -> Settings:
        settings = load_settings({**BASE_ENV, "DATA_DIR": str(tmp_path / "data"), **overrides})
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        settings.preview_dir.mkdir(parents=True, exist_ok=True)
        return settings

    return make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def storage(settings: Settings):
    store = Storage(settings.db_path)
    yield store
    store.close()


class FakeInstagram(InstagramClient):
    """Stands in for the Graph API: records every call and answers like Instagram would.

    `overrides[(method, path)]` replaces the default answer; a list is consumed one item per call and
    an exception instance is raised.
    """

    def __init__(self) -> None:
        super().__init__("ig-token", IG_USER, poll_interval=0, poll_timeout=5)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.overrides: dict[tuple[str, str], Any] = {}
        self._containers = 0

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, path, params))
        if (method, path) in self.overrides:
            answer = self.overrides[(method, path)]
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return copy.deepcopy(answer)
        if method == "POST" and path == f"{IG_USER}/media":
            self._containers += 1
            return {"id": f"container{self._containers}"}
        if method == "GET" and path.startswith("container"):
            return {"status_code": "FINISHED"}
        if method == "POST" and path == f"{IG_USER}/media_publish":
            return {"id": "media1"}
        if method == "GET" and params.get("fields") == "permalink":
            return {"permalink": f"https://www.instagram.com/p/{path}/"}
        raise AssertionError(f"unexpected Graph API call: {method} {path} {params}")

    def posted(self) -> list[dict[str, Any]]:
        """Parameters of every media container that was created."""
        return [params for method, path, params in self.calls if method == "POST" and path == f"{IG_USER}/media"]


@pytest.fixture
def ig() -> FakeInstagram:
    return FakeInstagram()


@pytest.fixture
def actions(settings: Settings, storage: Storage, ig: FakeInstagram) -> ActionService:
    return ActionService(settings, storage, ig)


@pytest.fixture
def add_media(settings: Settings, storage: Storage) -> Callable[..., MediaRecord]:
    """Create an uploaded media file (with preview) as the bot would after a Telegram upload."""
    counter = iter(range(1, 1000))

    def add(kind: str = "image", size: tuple[int, int] = (1080, 1080), chat_id: int = CHAT) -> MediaRecord:
        token = f"{next(counter):032x}"
        preview = f"{token}.jpg"
        if kind == "image":
            filename = f"{token}.jpg"
            Image.new("RGB", size, (30, 120, 200)).save(settings.media_dir / filename, "JPEG")
        else:
            filename = f"{token}.mp4"
            (settings.media_dir / filename).write_bytes(b"fake video")
        Image.new("RGB", (64, 64), (30, 120, 200)).save(settings.preview_dir / preview, "JPEG")
        return storage.add_media(chat_id, kind, filename, preview, size[0], size[1], 12.0 if kind == "video" else None)

    return add


def claude_message(content: list[dict[str, Any]], stop_reason: str = "end_turn", input_tokens: int = 1000) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 50},
        }
    )


class FakeClaude:
    """Mimics `AsyncAnthropic().beta.messages.create`, returning scripted responses in order."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> Any:
        self.requests.append(copy.deepcopy(params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
