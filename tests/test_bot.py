from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from multidict import CIMultiDict, CIMultiDictProxy
from PIL import Image
from yarl import URL

from igbot import bot as bot_module
from igbot.actions import ActionService, PostSpec
from igbot.agent import AgentReply
from igbot.bot import TelegramBot, split_message
from igbot.media import MediaError

from .conftest import CHAT


class FakeAgent:
    def __init__(self) -> None:
        self.notes: list[tuple[int, str]] = []
        self.reply = AgentReply(text="Tayyor")
        self.busy = asyncio.Event()  # set = no agent turn in progress
        self.busy.set()

    async def add_note(self, chat_id: int, text: str) -> None:
        await self.busy.wait()  # like the real agent, notes wait for a running turn
        self.notes.append((chat_id, text))

    async def respond(self, chat_id: int, content: list) -> AgentReply:
        return self.reply


def make_bot(settings, storage, ig) -> tuple[TelegramBot, SimpleNamespace, FakeAgent]:
    telegram_api = SimpleNamespace(
        id=42,  # aiogram's ChatActionSender reads bot.id
        send_message=AsyncMock(),
        edit_message_text=AsyncMock(),
        send_photo=AsyncMock(),
        send_media_group=AsyncMock(),
        send_chat_action=AsyncMock(),
    )
    agent = FakeAgent()
    actions = ActionService(settings, storage, ig)
    return TelegramBot(settings, storage, agent, actions, ig, telegram_api), telegram_api, agent


def sent_cards(api: SimpleNamespace) -> list[str]:
    return [call.args[1] for call in api.send_message.await_args_list if call.kwargs.get("reply_markup") is not None]


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


async def fake_download(source, destination, **kwargs) -> None:
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


async def test_download_errors_do_not_leak_the_bot_token(settings, storage, ig, caplog) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)
    url = URL("https://api.telegram.org/file/bot123456:SECRET-TOKEN/photos/file_1.jpg")
    request_info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    api.download = AsyncMock(side_effect=aiohttp.ClientResponseError(request_info, (), status=502, message="Bad Gateway"))
    with pytest.raises(MediaError):
        await telegram._save_media(upload(photo=[SimpleNamespace(file_size=1000)]))
    assert "SECRET-TOKEN" not in caplog.text
    assert list(settings.media_dir.iterdir()) == []


async def test_video_is_kept_when_its_thumbnail_fails(settings, storage, ig) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)

    async def download(source, destination, **kwargs):
        if source.kind == "thumbnail":
            raise TimeoutError
        destination.write_bytes(b"video bytes")

    api.download = AsyncMock(side_effect=download)
    video = SimpleNamespace(kind="video", mime_type="video/mp4", file_size=10, width=720, height=1280, duration=9,
                            thumbnail=SimpleNamespace(kind="thumbnail"))
    record = await telegram._save_media(upload(video=video))
    assert record.preview_filename is None and (settings.media_dir / record.filename).exists()


async def test_a_failed_message_does_not_lose_the_cards(settings, storage, ig, add_media) -> None:
    telegram, api, agent = make_bot(settings, storage, ig)
    first = telegram.actions.request_publish(CHAT, PostSpec("feed", (add_media("image").id,), "Bir"))
    second = telegram.actions.request(CHAT, "delete_post", {"media_id": "m1"})
    agent.reply = AgentReply(text="Ikkita so'rov tayyor", action_ids=[first.id, second.id])
    api.send_message.side_effect = [
        TelegramBadRequest(method=None, message="Bad Request: message is too long"),  # the reply text
        TelegramRetryAfter(method=None, message="Flood", retry_after=0),  # first card: flood control...
        None,  # ...retried
        None,  # second card
    ]
    await telegram.ask_agent(CHAT, "Joyla va o'chir", [])
    cards = sent_cards(api)  # the first card is attempted twice (flood control, then the retry)
    assert len(cards) == 3 and cards[0] == cards[1]
    assert f"#{first.id}" in cards[1] and f"#{second.id}" in cards[2]


async def test_pending_command_shows_waiting_cards(settings, storage, ig) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)
    waiting = telegram.actions.request(CHAT, "delete_post", {"media_id": "m1"})
    done = telegram.actions.request(CHAT, "delete_post", {"media_id": "m2"})
    storage.set_action_status(done.id, "pending", "cancelled")
    message = SimpleNamespace(chat=SimpleNamespace(id=CHAT), answer=AsyncMock())
    await telegram.on_pending(message)
    assert [f"#{waiting.id}" in card for card in sent_cards(api)] == [True]
    message.answer.assert_not_awaited()


async def test_approve_and_decline_at_once(settings, storage, ig, add_media) -> None:
    telegram, api, _ = make_bot(settings, storage, ig)
    action = telegram.actions.request_publish(CHAT, PostSpec("feed", (add_media("image").id,), ""))
    approve, decline = button(f"act:{action.id}:ok"), button(f"act:{action.id}:no")
    await asyncio.gather(telegram.on_button(approve), telegram.on_button(decline))
    assert storage.get_action(action.id).status == "done" and len(ig.posted()) == 1
    assert decline.answer.await_args.kwargs.get("show_alert") is True
    assert "✅" in api.edit_message_text.await_args_list[-1].kwargs["text"]


async def test_background_notices_do_not_wait_for_the_agent(settings, storage, ig) -> None:
    telegram, api, agent = make_bot(settings, storage, ig)
    agent.busy.clear()  # an agent turn is running
    await asyncio.wait_for(telegram.notify(CHAT, "✅ Joylandi", "Scheduled post #1 was published"), timeout=1)
    api.send_message.assert_awaited()
    assert agent.notes == []
    agent.busy.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert agent.notes == [(CHAT, "Scheduled post #1 was published")]
