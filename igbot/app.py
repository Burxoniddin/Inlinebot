"""Wiring: `run` starts the bot, web server and scheduler; `check` tests the configuration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets

import aiohttp
import anthropic
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError
from PIL import Image

from .actions import ActionService
from .agent import Agent
from .bot import TelegramBot
from .config import Settings
from .instagram import InstagramClient, InstagramError
from .scheduler import Scheduler
from .storage import Storage
from .tools import ToolBox
from .web import create_app, start_web_server

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = (
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_contents",
    "instagram_manage_comments",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
)


def _instagram(settings: Settings) -> InstagramClient:
    return InstagramClient(
        settings.ig_access_token,
        settings.ig_user_id,
        api_version=settings.graph_api_version,
        host=settings.graph_host,
    )


async def _find_account(ig: InstagramClient) -> None:
    """Fill in the Instagram account id when IG_USER_ID isn't set and the token sees exactly one account."""
    try:
        accounts = await ig.discover_accounts()
    except InstagramError as exc:
        log.error("Instagram akkauntini aniqlab bo'lmadi: %s", exc)
        return
    if len(accounts) == 1:
        ig.ig_user_id = accounts[0]["id"]
        log.info("Instagram akkaunt: @%s (id %s)", accounts[0]["username"], ig.ig_user_id)
    elif not accounts:
        log.error("Token Facebook sahifasiga ulangan Instagram professional akkauntni ko'rmayapti (README ga qarang).")
    else:
        names = ", ".join(f"@{account['username']} (id {account['id']})" for account in accounts)
        log.error("Bir nechta Instagram akkaunt topildi, IG_USER_ID ni tanlang: %s", names)


async def run(settings: Settings) -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.warning("ANTHROPIC_API_KEY sozlanmagan — AI javob bera olmaydi (python -m igbot check bilan tekshiring).")
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.preview_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(settings.db_path)
    ig = _instagram(settings)
    claude = anthropic.AsyncAnthropic()
    bot = Bot(settings.telegram_token)
    try:
        if not ig.ig_user_id:
            await _find_account(ig)
        actions = ActionService(settings, storage, ig)
        agent = Agent(settings, storage, claude, ToolBox(settings, storage, ig, actions))
        telegram = TelegramBot(settings, storage, agent, actions, ig, bot)
        await telegram.report_interrupted()
        runner = await start_web_server(create_app(settings.media_dir), settings.web_host, settings.web_port)
        scheduler = asyncio.create_task(Scheduler(settings, storage, actions, telegram.notify).run())
        dispatcher = Dispatcher()
        dispatcher.include_routers(*telegram.routers())
        log.info("Bot ishga tushdi (media server: %s:%s)", settings.web_host, settings.web_port)
        try:
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            await runner.cleanup()
    finally:
        await bot.session.close()
        await ig.close()
        await claude.close()
        storage.close()


async def check(settings: Settings) -> int:
    """Check every external dependency and print what is wrong; returns the number of problems."""
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(("✅ " if ok else "❌ ") + text)

    try:
        bot = Bot(settings.telegram_token)
    except TokenValidationError:
        report(False, "Telegram: TELEGRAM_BOT_TOKEN noto'g'ri formatda")
    else:
        try:
            me = await bot.get_me()
            report(True, f"Telegram bot: @{me.username}")
        except TelegramAPIError as exc:
            report(False, f"Telegram: {exc}")
        finally:
            await bot.session.close()

    ig = _instagram(settings)
    try:
        try:
            granted = set(await ig.granted_permissions())
            missing = [permission for permission in REQUIRED_PERMISSIONS if permission not in granted]
            report(not missing, "Token ruxsatlari joyida" if not missing else f"Tokenda ruxsatlar yetishmaydi: {', '.join(missing)}")
        except InstagramError as exc:
            report(False, f"Token ruxsatlarini tekshirib bo'lmadi: {exc}")
        if not ig.ig_user_id:
            accounts = await ig.discover_accounts()
            for account in accounts:
                print(f"   • @{account['username']} — IG_USER_ID={account['id']} (sahifa: {account['page']})")
            if len(accounts) != 1:
                report(False, "IG_USER_ID ni yuqoridagi ro'yxatdan tanlab .env ga yozing" if accounts
                       else "Token orqali hech qanday Instagram professional akkaunt topilmadi")
            else:
                ig.ig_user_id = accounts[0]["id"]
        if ig.ig_user_id:
            account = await ig.get_account()
            report(True, f"Instagram: @{account.get('username')} — {account.get('followers_count')} obunachi")
            quota = await ig.publishing_limit()
            report(True, f"Joylash limiti (24 soat): {quota['used']}/{quota['limit'] or 100}")
    except InstagramError as exc:
        report(False, str(exc))
    finally:
        await ig.close()

    claude = anthropic.AsyncAnthropic()
    try:
        model = await claude.models.retrieve(settings.claude_model)
        report(True, f"Claude: {model.display_name} ({model.id})")
    except (anthropic.AuthenticationError, TypeError):  # TypeError: the SDK found no credentials at all
        report(False, "Claude: ANTHROPIC_API_KEY noto'g'ri yoki sozlanmagan")
    except anthropic.NotFoundError:
        report(False, f"Claude: {settings.claude_model} modeli topilmadi")
    except anthropic.APIError as exc:
        report(False, f"Claude: {exc}")
    finally:
        await claude.close()

    if not settings.public_base_url:
        report(False, "PUBLIC_BASE_URL sozlanmagan — Instagram media fayllarni yuklab ololmaydi")
    else:
        await _check_public_url(settings, report)
    return problems


async def _check_public_url(settings: Settings, report) -> None:
    """Serve a test image and fetch it through PUBLIC_BASE_URL, the way Instagram will. If the port is
    taken (the bot is probably running), check that PUBLIC_BASE_URL reaches it via /health instead."""
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.jpg"
    path = settings.media_dir / name
    Image.new("RGB", (8, 8), (200, 50, 50)).save(path, "JPEG")
    runner = None
    try:
        try:
            runner = await start_web_server(create_app(settings.media_dir), settings.web_host, settings.web_port)
            url = settings.media_url(name)
        except OSError:
            url = f"{settings.public_base_url}/health"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url) as response:
                ok = response.status == 200
        report(ok, f"PUBLIC_BASE_URL ochiladi: {url}" if ok else f"PUBLIC_BASE_URL dan javob {response.status}: {url}")
    except aiohttp.ClientError as exc:
        report(False, f"PUBLIC_BASE_URL ga ulanib bo'lmadi: {exc}")
    finally:
        path.unlink(missing_ok=True)
        if runner is not None:
            await runner.cleanup()
