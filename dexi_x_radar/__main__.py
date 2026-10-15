"""python -m dexi_x_radar serve | poll-once | chat-id | status | bridge"""
import asyncio
import json
import logging
import sys

import httpx

from .app import build, quiet_http_logs, serve
from .config import load_settings
from .store import Store
from .telegram import TelegramClient


async def _poll_once(settings) -> None:
    async with httpx.AsyncClient(timeout=40) as http:
        parts = build(settings, http)
        print(json.dumps(await parts["poller"].cycle()))


async def _chat_id(settings) -> None:
    """Lists the chats that wrote to the bot. Stop the service first: Telegram allows one reader."""
    async with httpx.AsyncClient(timeout=40) as http:
        updates = await TelegramClient(settings.telegram_bot_token, http).call("getUpdates")
    chats = {}
    for u in updates or []:
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        chat = msg.get("chat") or {}
        if chat:
            chats[chat["id"]] = f"{chat.get('type')} @{chat.get('username')}"
    if not chats:
        print("No messages yet. Send /start to the bot, then run this again.")
    for cid, who in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    # {who}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    settings = load_settings()
    if command == "bridge":
        # stdout carries the MCP protocol here: no prints, logs go to stderr.
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        from .bridge import run_bridge
        if not settings.radar_url or not settings.mcp_token:
            sys.exit("bridge needs RADAR_URL (e.g. https://<host>/mcp) and MCP_TOKEN in .env")
        asyncio.run(run_bridge(settings.radar_url, settings.mcp_token))
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()
    if command == "serve":
        asyncio.run(serve(settings))
    elif command == "poll-once":
        asyncio.run(_poll_once(settings))
    elif command == "chat-id":
        asyncio.run(_chat_id(settings))
    elif command == "status":
        print(json.dumps(Store(settings.db_path).status_counts(), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
