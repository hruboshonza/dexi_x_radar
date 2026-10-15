"""Telegram: one message per queued post, edited in place as the post moves through the queue.

Buttons: ↩ opens X with the reply prefilled (the owner posts), Copy puts a draft on the
clipboard, Edit asks for an instruction, Again asks for a new round, Skip asks for a reason.
The bot answers only TELEGRAM_CHAT_ID.
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .store import Store, parse_iso

log = logging.getLogger(__name__)

MAX_MESSAGE = 4096
MAX_COPY_TEXT = 256
SKIP_REASONS = {
    "topic": "Mimo téma",
    "data": "Slabá data",
    "tone": "Tón",
    "late": "Pozdě",
    "other": "Jiné",
}
EDIT_PROMPT = "Pokyn k úpravě #{post_id}"
_EDIT_PROMPT_RE = re.compile(r"#(\d+)")


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=40)

    async def call(self, method: str, **payload) -> dict:
        resp = await self._http.post(f"{self._base}/{method}", json={k: v for k, v in payload.items() if v is not None})
        data = resp.json()
        if not data.get("ok"):
            # Editing a message to the same content is not an error worth surfacing.
            if "message is not modified" in str(data.get("description")):
                return {}
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data.get("result", {})


def _age(created_at: str) -> str:
    minutes = int((datetime.now(timezone.utc) - parse_iso(created_at)).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} d"


def _followers(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f" · {n / 1_000_000:.1f}M followers".replace(".0M", "M")
    if n >= 1_000:
        return f" · {n / 1_000:.1f}K followers".replace(".0K", "K")
    return f" · {n} followers"


def intent_url(post_id: str, text: str) -> str:
    return f"https://x.com/intent/tweet?in_reply_to={post_id}&text={quote(text, safe='')}"


def render(post: dict, drafts: list[dict], reply: dict | None = None) -> tuple[str, dict | None]:
    """Message text (HTML) and inline keyboard for the post's current state."""
    e = html.escape
    kind = "💬 zmínka" if post["source"] == "mention" else "🔔"
    head = f"{kind} <b>@{e(post['author'])}</b>{_followers(post.get('author_followers'))} · {_age(post['created_at'])}"
    media = f" · {len(post['media'])} média" if post.get("media") else ""
    lines = [head + media]

    body_budget = 900
    text = post["text"] if len(post["text"]) <= body_budget else post["text"][:body_budget] + "…"
    lines.append(f"<blockquote>{e(text)}</blockquote>")

    status = post["status"]
    if post.get("note"):
        lines.append(f"<i>{e(post['note'])}</i>")
    if drafts and status in ("drafted", "sent"):
        for d in drafts:
            lines.append(f"<b>{d['label']}</b>  {e(d['text'])}")
    if status == "pending":
        lines.append("⏳ <i>návrh čeká na Claude</i>")
    elif status == "skipped":
        who = "Claude" if post.get("skip_by") == "claude" else "ty"
        lines.append(f"⏭ <i>přeskočeno ({who}): {e(post.get('skip_reason') or '')}</i>")
    elif status == "ignored":
        lines.append("💤 <i>bez odpovědi, vyřazeno</i>")
    elif status == "sent" and reply:
        label = reply["matched"].replace("merged:", "spojeno ").replace("~", " upraveno")
        lines.append(f"✅ <b>odesláno</b> ({e(label)}) · <a href=\"https://x.com/i/status/{reply['id']}\">tvoje reply</a>")

    message = "\n\n".join(lines)
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE - 1] + "…"

    open_btn = {"text": "Otevřít post", "url": post["url"]}
    pid = post["id"]
    if status == "drafted" and drafts:
        reply_row = [{"text": f"↩ {d['label']}", "url": intent_url(pid, d["text"])} for d in drafts]
        copy_row = [{"text": f"📋 {d['label']}", "copy_text": {"text": d["text"]}}
                    for d in drafts if len(d["text"]) <= MAX_COPY_TEXT]
        actions = [{"text": "✏️ Upravit", "callback_data": f"e:{pid}"},
                   {"text": "🔄 Znovu", "callback_data": f"r:{pid}"},
                   {"text": "⏭ Skip", "callback_data": f"s:{pid}"}]
        rows = [reply_row] + ([copy_row] if copy_row else []) + [actions, [open_btn]]
        return message, {"inline_keyboard": rows}
    if status == "pending":
        return message, {"inline_keyboard": [[open_btn, {"text": "⏭ Skip", "callback_data": f"s:{pid}"}]]}
    if status == "skipped" and post.get("skip_by") == "claude":
        return message, {"inline_keyboard": [[open_btn, {"text": "🔄 Přesto navrhni", "callback_data": f"r:{pid}"}]]}
    return message, {"inline_keyboard": [[open_btn]]}


def skip_keyboard(post_id: str) -> dict:
    buttons = [{"text": label, "callback_data": f"sr:{post_id}:{code}"} for code, label in SKIP_REASONS.items()]
    return {"inline_keyboard": [buttons[:3], buttons[3:]]}


class Notifier:
    """Keeps the one Telegram message per post in sync with the store.

    A new post arrives silently. Only finished drafts ping the phone: Telegram never
    notifies on an edit, so `ping=True` replaces the message with a fresh one.
    """

    def __init__(self, store: Store, tg: TelegramClient, chat_id: str):
        self.store = store
        self.tg = tg
        self.chat_id = chat_id

    async def show(self, post_id: str, ping: bool = False) -> None:
        post = self.store.post(post_id)
        if not post:
            return
        drafts = self.store.latest_drafts(post_id)
        replies = self.store.replies(post_id=post_id)
        text, keyboard = render(post, drafts, replies[-1] if replies else None)
        common = {"text": text, "parse_mode": "HTML", "reply_markup": keyboard,
                  "link_preview_options": {"is_disabled": True}}
        old = post.get("tg_message_id")
        if old and not ping:
            await self.tg.call("editMessageText", chat_id=self.chat_id, message_id=old, **common)
            return
        if old:
            try:
                await self.tg.call("deleteMessage", chat_id=self.chat_id, message_id=old)
            except Exception:
                log.warning("could not delete message %s", old)
        sent = await self.tg.call("sendMessage", chat_id=self.chat_id, disable_notification=not ping, **common)
        self.store.set_message_id(post_id, sent["message_id"])

    async def say(self, text: str, **extra) -> dict:
        return await self.tg.call("sendMessage", chat_id=self.chat_id, text=text, **extra)


class BotLoop:
    """Long-polls Telegram for button presses and edit instructions."""

    def __init__(self, store: Store, tg: TelegramClient, notifier: Notifier, chat_id: str):
        self.store = store
        self.tg = tg
        self.notifier = notifier
        self.chat_id = str(chat_id)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once(timeout=25)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram loop")
                await asyncio.sleep(5)

    async def poll_once(self, timeout: int = 0) -> None:
        offset = int(self.store.cursor("telegram") or 0)
        updates = await self.tg.call("getUpdates", offset=offset or None, timeout=timeout,
                                     allowed_updates=["message", "callback_query"])
        for update in updates or []:
            self.store.set_cursor("telegram", str(update["update_id"] + 1))
            try:
                await self.handle(update)
            except Exception:
                log.exception("telegram update %s", update.get("update_id"))

    def _allowed(self, chat: dict) -> bool:
        return str(chat.get("id")) == self.chat_id

    async def handle(self, update: dict) -> None:
        if cb := update.get("callback_query"):
            if not self._allowed(cb.get("message", {}).get("chat", {})):
                return
            await self.on_button(cb)
        elif msg := update.get("message"):
            if not self._allowed(msg.get("chat", {})):
                return
            await self.on_message(msg)

    async def on_button(self, cb: dict) -> None:
        data = cb.get("data", "")
        kind, _, rest = data.partition(":")
        await self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])
        if kind == "s":
            await self.tg.call("editMessageReplyMarkup", chat_id=self.chat_id,
                               message_id=cb["message"]["message_id"], reply_markup=skip_keyboard(rest))
        elif kind == "sr":
            post_id, _, code = rest.partition(":")
            reason = SKIP_REASONS.get(code, code)
            self.store.add_feedback(post_id, "skip", code)
            self.store.set_status(post_id, "skipped", skip_by="owner", skip_reason=reason)
            await self.notifier.show(post_id)
        elif kind == "r":
            self.store.add_feedback(rest, "regen")
            self.store.set_status(rest, "pending", skip_by=None, skip_reason=None)
            await self.notifier.show(rest)
        elif kind == "e":
            await self.notifier.say(
                EDIT_PROMPT.format(post_id=rest),
                reply_markup={"force_reply": True, "input_field_placeholder": "např. kratší, přidej závěr"},
            )

    async def on_message(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        prompt = (msg.get("reply_to_message") or {}).get("text", "")
        if prompt.startswith(EDIT_PROMPT.split("#")[0]) and (m := _EDIT_PROMPT_RE.search(prompt)) and text:
            post_id = m.group(1)
            if self.store.post(post_id):
                self.store.add_feedback(post_id, "edit", text)
                self.store.set_status(post_id, "pending")
                await self.notifier.show(post_id)
                await self.notifier.say("Zapsáno, Claude to přepíše v dalším kole.")
            return
        if text.startswith("/start") or text.startswith("/status"):
            counts = self.store.status_counts()
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "fronta je prázdná"
            await self.notifier.say(f"Radar běží. {summary}")
