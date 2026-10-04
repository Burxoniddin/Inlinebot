"""`python -m igbot` runs the bot; `python -m igbot check` tests the configuration."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from .app import check, run
from .config import ConfigError, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m igbot", description="Instagram AI kontent menejeri (Telegram bot)")
    parser.add_argument("command", nargs="?", choices=["run", "check"], default="run")
    command = parser.parse_args().command

    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(f"Sozlamalarda xato: {exc}")
    if command == "check":
        sys.exit(1 if asyncio.run(check(settings)) else 0)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
