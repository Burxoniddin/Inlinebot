from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from igbot import bot as bot_module
from igbot.actions import ActionService, PostSpec
from igbot.bot import TelegramBot, split_message
from igbot.media import MediaError

from .conftest import CHAT


class FakeAgent:
    def __init__(self) -> None:
        self.notes: list[tuple[int, str]] = []

    async def add_note(self, chat_id: int, text: str) -> None:
        self.notes.append((chat_id, text))


def make_bot(settings, storage, ig) -> tuple[TelegramBot, SimpleNamespace, FakeAgent]:
    telegram_api = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    agent = FakeAgent()
    actions = ActionService(settings, storage, ig)
    return TelegramBot(settings, storage, agent, actions, ig, telegram_api), telegram_api, agent


def button(data: str, chat_id: int = CHAT) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=77),
        answer=AsyncMock(),
    )


def test_split_message() -> None:
    text = "\n".join(f"qator {i}" for i in range(2000))
    chunks = split_message(text)
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "\n".join(chunks) == text
    assert split_message("x" * 5000) == ["x" * 4096, "x" * 904]


async def test_confirm_button_publishes_and_tells_the_agent(settings, storage, ig, add_media) -> None:
    telegram, api, agent = make_bot(settings, storage, ig)
    action = telegram.actions.request_publish(CHAT, PostSpec("feed", (add_media("image").id,), "Salom"))

    callback = button(f"act:{action.id}:ok")
    await telegram.on_button(callback)

    assert storage.get_action(action.id).status == "done"
    assert len(ig.posted()) == 1
    final_card = api.edit_message_text.await_args_list[-1].kwargs["text"]
    assert "✅ Post joylandi" in final_card
    assert agent.notes == [(CHAT, f"Request #{action.id} was approved and done: {storage.get_action(action.id).result}")]

    second = button(f"act:{action.id}:ok")  # pressing again does nothing
    await telegram.on_button(second)
    assert second.answer.await_args.kwargs.get("show_alert") is True
    assert len(ig.posted()) == 1


async def test_decline_button(settings, storage, ig) -> None:
    telegram, api, agent = make_bot(settings, storage, ig)
    action = telegram.actions.request(CHAT, "delete_post", {"media_id": "m1"})
    await telegram.on_button(button(f"act:{action.id}:no"))
    assert storage.get_action(action.id).status == "cancelled"
    assert ig.calls == []
    assert "declined" in agent.notes[0][1]


async def test_button_from_another_chat_is_ignored(settings, storage, ig) -> None:
    telegram, _, _ = make_bot(settings, storage, ig)
    action = telegram.actions.request(CHAT, "delete_post", {"media_id": "m1"})
    callback = button(f"act:{action.id}:ok", chat_id=999)
    await telegram.on_button(callback)
    assert storage.get_action(action.id).status == "pending"
    assert ig.calls == []


def upload(**attachments) -> SimpleNamespace:
    fields = {"photo": None, "video": None, "animation": None, "document": None, **attachments}
    return SimpleNamespace(chat=SimpleNamespace(id=CHAT), **fields)


async def fake_download(source, destination) -> None:
    """Telegram download: a PNG for photos/thumbnails, some bytes for videos."""
    if getattr(source, "kind", "image") == "image":
        Image.new("RGBA", (1600, 900), (255, 0, 0, 255)).save(destination, "PNG")
    else:
        destination.write_bytes(b"video bytes")


async def test_uploads_are_stored(settings, storage, ig) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)
    api.download = AsyncMock(side_effect=fake_download)

    photo = await telegram._save_media(upload(photo=[SimpleNamespace(file_size=10), SimpleNamespace(file_size=1000)]))
    assert (photo.kind, photo.width, photo.height) == ("image", 1600, 900)
    with Image.open(settings.media_dir / photo.filename) as stored:
        assert stored.format == "JPEG"
    assert (settings.preview_dir / photo.preview_filename).exists()

    thumbnail = SimpleNamespace(kind="image")
    video_file = SimpleNamespace(
        kind="video", mime_type="video/quicktime", file_size=5000, width=1080, height=1920, duration=15, thumbnail=thumbnail
    )
    video = await telegram._save_media(upload(document=video_file))
    assert video.kind == "video" and video.filename.endswith(".mov") and video.duration == 15
    assert (settings.preview_dir / video.preview_filename).exists()
    assert [path.suffix for path in settings.media_dir.iterdir()].count(".part") == 0


async def test_unusable_uploads_are_refused(settings, storage, ig) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)
    api.download = AsyncMock(side_effect=fake_download)
    refused = [
        upload(document=SimpleNamespace(mime_type="application/pdf", file_size=10)),
        upload(document=SimpleNamespace(mime_type="video/webm", file_size=10)),
        upload(video=SimpleNamespace(mime_type="video/mp4", file_size=25 * 1024 * 1024)),
    ]
    for message in refused:
        with pytest.raises(MediaError):
            await telegram._save_media(message)
    api.download.assert_not_awaited()
    assert list(settings.media_dir.iterdir()) == []


async def test_album_becomes_one_agent_turn(settings, storage, ig, add_media, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "ALBUM_WAIT", 0.05)
    telegram, _, _ = make_bot(settings, storage, ig)
    first, second = add_media("image"), add_media("image")
    delays = {1: 0.15, 2: 0.0}  # the first photo downloads slower than the second

    async def save_media(message):
        await asyncio.sleep(delays[message.message_id])
        return {1: first, 2: second}[message.message_id]

    turns = []

    async def ask_agent(chat_id, text, media):
        turns.append((chat_id, text, [record.id for record in media]))

    telegram._save_media = save_media
    telegram.ask_agent = ask_agent
    messages = [
        SimpleNamespace(chat=SimpleNamespace(id=CHAT), media_group_id="g1", message_id=1, caption="Karusel qil"),
        SimpleNamespace(chat=SimpleNamespace(id=CHAT), media_group_id="g1", message_id=2, caption=None),
    ]
    await asyncio.gather(*(telegram.on_media(message) for message in messages))
    await asyncio.sleep(0.2)
    assert turns == [(CHAT, "Karusel qil", [first.id, second.id])]
