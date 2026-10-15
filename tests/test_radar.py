import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from dexi_x_radar.config import load_settings
from dexi_x_radar.matching import match_reply, strip_mentions
from dexi_x_radar.mcp_server import BearerAuth, RadarTools, build_mcp
from dexi_x_radar.poller import Poller
from dexi_x_radar.store import Store
from dexi_x_radar.telegram import MAX_MESSAGE, render


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_post(pid: str, minutes_ago: float = 5, **over) -> dict:
    return {"id": pid, "source": "watch", "author": "AshCrypto", "author_id": "9", "author_followers": 2_155_580,
            "created_at": iso(minutes_ago), "text": "Someone opened a $92M $ETH long", "url": f"https://x.com/AshCrypto/status/{pid}",
            "media": [], "urls": [], "addresses": [], "metrics": {}, **over}


DRAFTS = [
    {"label": "A", "text": "He's paid $1.59M in funding just to hold it. Not a trade I'd want to manage."},
    {"label": "B", "text": "$92M on $ETH and the funding bill alone is $1.59M. Size tells you conviction."},
    {"label": "C", "text": "At what point does a 14x long stop being a trade and become hope?"},
]


class MatchingTest(unittest.TestCase):
    def test_strips_the_reply_prefix(self):
        self.assertEqual(strip_mentions("@AshCrypto @lookonchain hello there."), "hello there.")

    def test_exact_draft(self):
        self.assertEqual(match_reply("@AshCrypto " + DRAFTS[1]["text"], DRAFTS)["matched"], "B")

    def test_merged_drafts(self):
        final = "@AshCrypto " + DRAFTS[0]["text"] + " " + DRAFTS[2]["text"]
        self.assertEqual(match_reply(final, DRAFTS)["matched"], "merged:AC")

    def test_edited_draft_records_the_diff(self):
        final = DRAFTS[0]["text"].replace("Not a trade I'd want to manage.", "Considering the data, I'd stay out.")
        r = match_reply(final, DRAFTS)
        self.assertEqual(r["matched"], "A~")
        self.assertIn("Considering the data,", " ".join(r["diff"]["added"]))
        self.assertIn("manage.", " ".join(r["diff"]["removed"]))

    def test_own_text(self):
        self.assertEqual(match_reply("gm, nice chart", DRAFTS)["matched"], "own")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_a_post_is_queued_once(self):
        self.assertTrue(self.store.insert_post(make_post("1")))
        self.assertFalse(self.store.insert_post(make_post("1")))
        self.assertEqual(len(self.store.posts()), 1)

    def test_draft_rounds(self):
        self.store.insert_post(make_post("1"))
        self.assertEqual(self.store.save_drafts("1", ["a.", "b."]), 1)
        self.assertEqual(self.store.save_drafts("1", ["c."]), 2)
        self.assertEqual([d["text"] for d in self.store.latest_drafts("1")], ["c."])
        self.assertEqual(len(self.store.all_drafts("1")), 3)

    def test_stale_open_posts(self):
        self.store.insert_post(make_post("old", minutes_ago=7 * 60))
        self.store.insert_post(make_post("new", minutes_ago=10))
        self.assertEqual([p["id"] for p in self.store.stale_open_posts(6)], ["old"])


class RenderTest(unittest.TestCase):
    def test_drafted_message_fits_telegram(self):
        post = {**make_post("2110630134416015655", text="x" * 5000), "status": "drafted", "note": "Report card"}
        long_draft = {"label": "B", "text": "y" * 270}
        text, kb = render(post, [DRAFTS[0], long_draft])
        self.assertLessEqual(len(text), MAX_MESSAGE)
        for row in kb["inline_keyboard"]:
            for button in row:
                if "callback_data" in button:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)
        copy_labels = [b["text"] for b in kb["inline_keyboard"][1]]
        self.assertEqual(copy_labels, ["📋 A"], "a draft over 256 chars gets no copy button")
        self.assertTrue(kb["inline_keyboard"][0][0]["url"].startswith("https://x.com/intent/tweet?in_reply_to="))

    def test_text_is_escaped(self):
        post = {**make_post("1", text="<b>bold</b> & co"), "status": "pending"}
        text, _ = render(post, [])
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", text)


class FakeX:
    """Canned X API: one watch post, one mention by the owner (ignored), one own reply."""

    def __init__(self):
        self.calls = 0

    def cost(self):
        return 0.0

    async def users_by_handles(self, handles):
        return [{"id": "100" if h.lower() == "dexiscan" else f"u{i}", "username": h, "followers": 10}
                for i, h in enumerate(handles)]

    async def search_recent(self, query, since_id, start, max_posts):
        self.calls += 1
        if since_id:
            return [], {"users": {}, "media": {}}
        return [{"id": "500", "author_id": "9", "created_at": iso(3) , "text": "whale post"}], \
            {"users": {"9": {"username": "lookonchain", "public_metrics": {"followers_count": 710000}}}, "media": {}}

    async def mentions(self, user_id, since_id, start, max_posts):
        return [{"id": "501", "author_id": "100", "created_at": iso(2), "text": "@DexiScan self"}], \
            {"users": {}, "media": {}}

    async def own_posts(self, user_id, since_id, start, max_posts):
        if since_id:
            return [], {}
        return [{"id": "900", "created_at": iso(1), "text": "@lookonchain " + DRAFTS[1]["text"],
                 "referenced_tweets": [{"type": "replied_to", "id": "500"}]},
                {"id": "901", "created_at": iso(1), "text": "@someone hi.",
                 "referenced_tweets": [{"type": "replied_to", "id": "777"}]}], {}

    async def tweets_by_ids(self, ids):
        return []

    async def replies_to(self, tweet_id):
        return []


class PollerTest(unittest.TestCase):
    def test_cycle_queues_once_and_matches_the_owner_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl = Path(tmp) / "watchlist.txt"
            wl.write_text("lookonchain\n", encoding="utf-8")
            settings = load_settings({"WATCHLIST_FILE": str(wl), "DB_PATH": ":memory:", "X_OWN_HANDLE": "DexiScan"})
            store = Store(":memory:")
            poller = Poller(settings, store, FakeX(), notifier=None)

            async def run():
                # The owner's reply arrives in the same cycle as the post: drafts saved in between.
                first = await poller.cycle()
                return first
            store.insert_post(make_post("500", author="lookonchain"))
            store.save_drafts("500", [d["text"] for d in DRAFTS])
            summary = asyncio.run(run())

            self.assertEqual(summary["new"], 0, "500 was already queued; 501 is the owner's own post")
            self.assertEqual(store.post("500")["status"], "sent")
            replies = store.replies(post_id="500")
            self.assertEqual(replies[0]["matched"], "B")
            self.assertEqual(store.replies(post_id="777")[0]["matched"], "unqueued")
            self.assertIsNone(store.post("501"))
            second = asyncio.run(poller.cycle())
            self.assertEqual(second["new"], 0)


class RecordingNotifier:
    def __init__(self):
        self.shown = []

    async def show(self, post_id, ping=False):
        self.shown.append(post_id)


class FakeTg:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {}


class DeliveryTest(unittest.TestCase):
    def test_unshown_posts_are_sent_and_stale_ones_stay_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl = Path(tmp) / "watchlist.txt"
            wl.write_text("lookonchain\n", encoding="utf-8")
            settings = load_settings({"WATCHLIST_FILE": str(wl), "DB_PATH": ":memory:"})
            store = Store(":memory:")
            store.insert_post(make_post("old", minutes_ago=7 * 60))
            notifier = RecordingNotifier()

            class QuietOwner(FakeX):
                async def own_posts(self, *args):
                    return [], {}
            asyncio.run(Poller(settings, store, QuietOwner(), notifier).cycle())
            self.assertIn("500", notifier.shown, "the new watch post reaches the phone")
            self.assertNotIn("old", notifier.shown, "a never-shown post that went stale sends nothing")
            self.assertEqual(store.post("old")["status"], "ignored")

    def test_setup_mode_answers_start_with_the_chat_id(self):
        from dexi_x_radar.telegram import BotLoop
        tg = FakeTg()
        bot = BotLoop(Store(":memory:"), tg, notifier=None, chat_id="")
        asyncio.run(bot.handle({"message": {"text": "/start", "chat": {"id": 4242}}}))
        asyncio.run(bot.handle({"callback_query": {"id": "1", "data": "s:1", "message": {"chat": {"id": 4242}}}}))
        self.assertEqual(len(tg.calls), 1)
        self.assertIn("TELEGRAM_CHAT_ID=4242", tg.calls[0][1]["text"])


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.insert_post(make_post("1"))
        self.tools = RadarTools(load_settings({"DB_PATH": ":memory:"}), self.store, None, dexi=None)

    def save(self, drafts):
        return asyncio.run(self.tools.save_drafts("1", drafts, "note"))

    def test_rejects_dashes_length_and_cohort_size(self):
        r = self.save(["Whales — short.", "x" * 281, "The 77 Hyperliquid whales we track went short."])
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["errors"]), 3)
        self.assertEqual(self.store.post("1")["status"], "pending")

    def test_saves_and_warns_on_missing_punctuation(self):
        r = self.save(["Funding cost him $1.59M.", "Size tells you conviction"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["warnings"], ["B: last sentence has no closing punctuation"])
        self.assertEqual(self.store.post("1")["status"], "drafted")

    def test_pending_carries_owner_instructions(self):
        self.store.add_feedback("1", "edit", "kratší")
        view = self.tools.pending()["pending"][0]
        self.assertEqual(view["owner_instructions"], ["kratší"])


class LoggingTest(unittest.TestCase):
    def test_http_client_logs_never_carry_request_urls(self):
        import logging
        from dexi_x_radar.app import quiet_http_logs
        quiet_http_logs()
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO))


class AuthTest(unittest.TestCase):
    def test_mcp_needs_the_token_and_health_does_not(self):
        tools = RadarTools(load_settings({"DB_PATH": ":memory:"}), Store(":memory:"), None, dexi=None)
        app = BearerAuth(build_mcp(tools).streamable_http_app(), "secret")

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                return ((await c.get("/health")).status_code,
                        (await c.post("/mcp", json={})).status_code,
                        (await c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})).status_code)
        self.assertEqual(asyncio.run(run()), (200, 401, 401))


if __name__ == "__main__":
    unittest.main()
