"""The service: HTTP (MCP + /health), the X poller, the Telegram loop and, with an API key, the drafter."""
import asyncio
import logging

import httpx
import uvicorn

from .config import Settings
from .dexiscan import DexiScan
from .drafter import Drafter
from .mcp_server import RadarTools, build_app
from .poller import Poller
from .store import Store
from .telegram import BotLoop, Notifier, TelegramClient
from .xapi import XClient


def build(settings: Settings, http: httpx.AsyncClient):
    store = Store(settings.db_path)
    tg = TelegramClient(settings.telegram_bot_token, http)
    notifier = Notifier(store, tg, settings.telegram_chat_id)
    x = XClient(settings.x_bearer_token, http)
    dexi = DexiScan(settings.dexiscan_base_url, settings.x_brief_key, settings.api_read_key, http)
    tools = RadarTools(settings, store, notifier, dexi)
    return {
        "store": store,
        "notifier": notifier,
        "poller": Poller(settings, store, x, notifier),
        "bot": BotLoop(store, tg, notifier, settings.telegram_chat_id),
        "tools": tools,
        "drafter": Drafter(settings, store, tools, notifier) if settings.anthropic_api_key else None,
    }


def quiet_http_logs() -> None:
    """httpx logs every request URL at INFO, and Telegram puts the bot token in the URL."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def serve(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()
    if missing := settings.missing():
        raise SystemExit(f"Missing settings: {', '.join(missing)} (see .env.example)")
    async with httpx.AsyncClient(timeout=40) as http:
        parts = build(settings, http)
        app = build_app(parts["tools"], settings.mcp_token, settings.mcp_path)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.port, log_level="info"))
        tasks = [asyncio.create_task(parts["poller"].run_forever()), asyncio.create_task(parts["bot"].run_forever())]
        if parts["drafter"]:
            tasks.append(asyncio.create_task(parts["drafter"].run_forever()))
        else:
            logging.getLogger(__name__).info("no ANTHROPIC_API_KEY: drafts come only from MCP clients")
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()
