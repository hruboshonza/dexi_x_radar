"""One poll cycle every POLL_INTERVAL_SECONDS: new posts in, own replies matched, undrafted stale posts out.

Quiet hours pause the polling. Watched posts from the night are never read: after the pause the
watch searches start again at the end of quiet hours. Mentions and the owner's own replies pick
up where they stopped.
"""
import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .matching import match_reply
from .store import Store
from .telegram import Notifier
from .xapi import XClient, iso_hours_ago, normalize, replied_to, watch_queries

log = logging.getLogger(__name__)
USERS_REFRESH_HOURS = 24 * 7


class Poller:
    def __init__(self, settings: Settings, store: Store, x: XClient, notifier: Notifier | None):
        self.s = settings
        self.store = store
        self.x = x
        self.notifier = notifier
        self.quiet = settings.quiet()

    async def run_forever(self) -> None:
        paused = False
        while True:
            if self.quiet and self.quiet.active():
                if not paused:
                    log.info("quiet hours: polling paused for %.0f min", self.quiet.seconds_left() / 60)
                    paused = True
                await asyncio.sleep(min(self.s.poll_interval, self.quiet.seconds_left() + 1))
                continue
            if paused:
                self.resume_after_quiet()
                paused = False
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll cycle")
            await asyncio.sleep(self.s.poll_interval)

    def resume_after_quiet(self) -> None:
        """Watch searches restart from the end of quiet hours instead of reading the night."""
        cleared = self.store.clear_cursors("search:")
        log.info("quiet hours over: %d watch cursors reset", cleared)

    def window_start(self) -> str:
        """Where a source without a cursor starts reading: LOOKBACK_HOURS back, never into quiet hours."""
        start = iso_hours_ago(self.s.lookback_hours)
        if self.quiet:
            quiet_end = self.quiet.last_end(datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
            start = max(start, quiet_end)
        return start

    async def ensure_users(self, handles: list[str]) -> dict:
        missing = [h for h in handles if not self.store.user(h)]
        if missing:
            for u in await self.x.users_by_handles(missing):
                self.store.put_user(u["id"], u["username"], u["followers"])
        return {h.lower(): self.store.user(h) for h in handles if self.store.user(h)}

    async def cycle(self) -> dict:
        before = self.x.cost()
        own_handle = self.s.x_own_handle
        users = await self.ensure_users(self.s.watchlist() + [own_handle])
        own = users.get(own_handle.lower())
        start = self.window_start()
        new_ids: list[str] = []

        for query in watch_queries(self.s.watchlist()):
            new_ids += await self._ingest(f"search:{query}", "watch", self.x.search_recent, query, start)

        if own:
            new_ids += await self._ingest("mentions", "mention", self.x.mentions, own["id"], start,
                                          skip_author=own["id"])
            sent = await self._match_own_replies(own["id"], start)
        else:
            sent = []

        ignored = self._ignore_stale()
        revived = self.store.revive_ignored_drafts()
        await self._outcomes()

        # Drafted posts whose message failed to send get another try.
        for post_id in self.store.unshown_open_posts():
            await self._show(post_id)
        # A message for a post without drafts is a leftover of the old "draft pending" notice.
        if self.notifier:
            for post in self.store.posts(status="pending", limit=500):
                if post.get("tg_message_id") and not self.store.latest_drafts(post["id"]):
                    await self.notifier.drop(post["id"])
        for post_id in sent + revived:
            await self._show(post_id)
        # Only posts without drafts go stale, and they leave no trace.
        if self.notifier:
            for post_id in ignored:
                await self.notifier.drop(post_id)

        summary = {"new": len(new_ids), "sent": len(sent), "ignored": len(ignored),
                   "cost_usd": round(self.x.cost() - before, 4)}
        log.info("cycle %s", summary)
        return summary

    async def _ingest(self, cursor_name: str, source: str, fetch, arg: str, start: str,
                      skip_author: str | None = None) -> list[str]:
        posts, inc = await fetch(arg, self.store.cursor(cursor_name), start, self.s.max_posts_per_poll)
        if not posts:
            return []
        self.store.set_cursor(cursor_name, max(posts, key=lambda p: int(p["id"]))["id"])
        fresh = []
        for raw in sorted(posts, key=lambda p: int(p["id"])):
            if skip_author and raw.get("author_id") == skip_author:
                continue
            if self.store.insert_post(normalize(raw, inc, source)):
                fresh.append(raw["id"])
        return fresh

    async def _match_own_replies(self, own_id: str, start: str) -> list[str]:
        """The owner's new replies: mark the queued post sent and record which draft was used."""
        posts, _ = await self.x.own_posts(own_id, self.store.cursor("own"), start, self.s.max_posts_per_poll)
        if posts:
            self.store.set_cursor("own", max(posts, key=lambda p: int(p["id"]))["id"])
        sent = []
        for raw in sorted(posts, key=lambda p: int(p["id"])):
            target = replied_to(raw)
            if not target:
                continue
            queued = self.store.post(target)
            if queued:
                result = match_reply(raw.get("text", ""), self.store.latest_drafts(target))
            else:
                result = {"matched": "unqueued", "similarity": None, "diff": None}
            created = raw["created_at"].replace(".000Z", "Z")
            if self.store.insert_reply({"id": raw["id"], "post_id": target, "text": raw.get("text", ""),
                                        "created_at": created, **result}) and queued:
                self.store.set_status(target, "sent")
                sent.append(target)
        return sent

    def _ignore_stale(self) -> list[str]:
        stale = self.store.stale_open_posts(self.s.ignore_after_hours)
        for post in stale:
            self.store.set_status(post["id"], "ignored")
        return [p["id"] for p in stale]

    async def _outcomes(self) -> None:
        """24 h after a reply: its metrics, and whether the original author answered it."""
        due = self.store.replies_due_for_outcome(self.s.outcome_after_hours)
        if not due:
            return
        metrics = {t["id"]: t.get("public_metrics", {}) for t in await self.x.tweets_by_ids([r["id"] for r in due])}
        for reply in due:
            post = self.store.post(reply["post_id"])
            author_replied = None
            if post and post.get("author_id"):
                try:
                    answers = await self.x.replies_to(reply["id"])
                    author_replied = any(a.get("author_id") == post["author_id"] for a in answers)
                except Exception:
                    log.warning("author-reply check failed for %s", reply["id"])
            self.store.set_outcome(reply["id"], metrics.get(reply["id"], {}), author_replied)

    async def _show(self, post_id: str) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.show(post_id)
        except Exception:
            log.exception("telegram show %s", post_id)
