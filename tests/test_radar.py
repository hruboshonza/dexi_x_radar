import asyncio
import json
import tempfile
import types
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

    def test_drafted_posts_never_go_stale(self):
        for pid, status in (("drafted", "drafted"), ("redraft", "pending")):
            self.store.insert_post(make_post(pid, minutes_ago=3 * 24 * 60))
            self.store.save_drafts(pid, ["Funding cost him $1.59M."])
            self.store.set_status(pid, status)
        self.assertEqual(self.store.stale_open_posts(6), [])

    def test_ignored_posts_with_drafts_are_revived(self):
        self.store.insert_post(make_post("1"))
        self.store.insert_post(make_post("2"))
        self.store.save_drafts("1", ["Funding cost him $1.59M."])
        self.store.set_status("1", "ignored")
        self.store.set_status("2", "ignored")
        self.assertEqual(self.store.revive_ignored_drafts(), ["1"])
        self.assertEqual(self.store.post("1")["status"], "drafted")
        self.assertEqual(self.store.post("2")["status"], "ignored")


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
        self.dropped = []

    async def show(self, post_id, ping=False):
        self.shown.append(post_id)

    async def drop(self, post_id):
        self.dropped.append(post_id)


class FakeTg:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {}


class DeliveryTest(unittest.TestCase):
    def notifier(self, store):
        from dexi_x_radar.telegram import Notifier
        tg = FakeTg()

        async def call(method, **payload):
            tg.calls.append((method, payload))
            return {"message_id": 77} if method == "sendMessage" else {}
        tg.call = call
        return Notifier(store, tg, chat_id="1"), tg

    def test_nothing_reaches_the_phone_before_drafts(self):
        store = Store(":memory:")
        store.insert_post(make_post("1"))
        notifier, tg = self.notifier(store)
        asyncio.run(notifier.show("1"))
        self.assertEqual(tg.calls, [])

    def test_drafts_arrive_as_one_message_that_pings(self):
        store = Store(":memory:")
        store.insert_post(make_post("1"))
        store.save_drafts("1", ["Funding cost him $1.59M."])
        store.set_status("1", "drafted")
        notifier, tg = self.notifier(store)
        asyncio.run(notifier.show("1", ping=True))
        method, payload = tg.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertFalse(payload["disable_notification"])
        self.assertLess(payload["text"].index("Someone opened"), payload["text"].index("Funding cost him"),
                        "the author's post comes first, the drafts right under it")
        self.assertEqual(store.post("1")["tg_message_id"], 77)

    def test_a_late_post_arrives_silently_and_marked(self):
        store = Store(":memory:")
        store.insert_post(make_post("1", minutes_ago=75))
        store.save_drafts("1", ["Funding cost him $1.59M."])
        store.set_status("1", "drafted")
        notifier, tg = self.notifier(store)
        asyncio.run(notifier.show("1", ping=True))
        payload = tg.calls[-1][1]
        self.assertTrue(payload["disable_notification"])
        self.assertIn("pozdě", payload["text"])

    def test_a_skipped_post_leaves_no_message(self):
        store = Store(":memory:")
        store.insert_post(make_post("1"))
        store.set_message_id("1", 55)
        notifier, tg = self.notifier(store)
        tools = RadarTools(load_settings({"DB_PATH": ":memory:"}), store, notifier, dexi=None)
        asyncio.run(tools.skip("1", "meme"))
        self.assertEqual(tg.calls[-1][0], "deleteMessage")
        self.assertIsNone(store.post("1")["tg_message_id"])

    def test_stale_and_leftover_pending_messages_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl = Path(tmp) / "watchlist.txt"
            wl.write_text("lookonchain\n", encoding="utf-8")
            settings = load_settings({"WATCHLIST_FILE": str(wl), "DB_PATH": ":memory:"})
            store = Store(":memory:")
            store.insert_post(make_post("old", minutes_ago=7 * 60))
            store.insert_post(make_post("old-drafted", minutes_ago=7 * 60))
            store.save_drafts("old-drafted", ["Funding cost him $1.59M."])
            store.set_status("old-drafted", "drafted")
            store.insert_post(make_post("leftover", minutes_ago=30))
            store.set_message_id("leftover", 9)
            notifier = RecordingNotifier()

            class QuietOwner(FakeX):
                async def own_posts(self, *args):
                    return [], {}
            asyncio.run(Poller(settings, store, QuietOwner(), notifier).cycle())
            self.assertEqual(store.post("old")["status"], "ignored")
            self.assertEqual(store.post("old-drafted")["status"], "drafted")
            self.assertIn("old", notifier.dropped)
            self.assertNotIn("old-drafted", notifier.dropped)
            self.assertIn("leftover", notifier.dropped)

    def bot(self, store):
        from dexi_x_radar.telegram import BotLoop
        notifier, tg = self.notifier(store)
        return BotLoop(store, tg, notifier, chat_id="1"), tg

    def press(self, bot, data):
        asyncio.run(bot.handle({"callback_query": {"id": "c", "data": data,
                                                   "message": {"chat": {"id": 1}, "message_id": 55}}}))

    def test_ideal_wording_is_feedback_only(self):
        store = Store(":memory:")
        store.insert_post(make_post("1"))
        store.save_drafts("1", ["Funding cost him $1.59M."])
        store.set_status("1", "sent")
        bot, tg = self.bot(store)
        self.press(bot, "i:1")
        self.assertTrue(tg.calls[-1][1]["text"].startswith("Ideální znění #1"))
        asyncio.run(bot.handle({"message": {"chat": {"id": 1}, "text": "We'd wait for funding to flip.",
                                            "reply_to_message": {"text": "Ideální znění #1"}}}))
        self.assertEqual([(f["kind"], f["value"]) for f in store.feedback(post_id="1")],
                         [("ideal", "We'd wait for funding to flip.")])
        self.assertEqual(store.post("1")["status"], "sent", "no new round")

    def test_delete_needs_a_confirmation(self):
        store = Store(":memory:")
        store.insert_post(make_post("1"))
        store.save_drafts("1", ["Funding cost him $1.59M."])
        store.set_status("1", "drafted")
        store.set_message_id("1", 55)
        bot, tg = self.bot(store)
        self.press(bot, "d:1")
        self.assertEqual(tg.calls[-1][0], "editMessageReplyMarkup")
        self.press(bot, "dn:1")
        self.assertEqual(store.post("1")["tg_message_id"], 55)
        self.press(bot, "dd:1")
        self.assertEqual(tg.calls[-1][0], "deleteMessage")
        self.assertEqual(store.post("1")["status"], "dismissed")
        self.assertIsNone(store.post("1")["tg_message_id"])
        self.assertEqual(store.unshown_open_posts(), [], "a deleted post is not sent again")

    def test_old_drafts_keep_their_buttons(self):
        post = {**make_post("1"), "status": "sent"}
        _, kb = render(post, DRAFTS[:1])
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("💡 Ideál", labels)
        self.assertIn("🗑 Smazat", labels)

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

    def save(self, drafts, general="", insight=""):
        return asyncio.run(self.tools.save_drafts("1", drafts, "note", general, insight))

    def test_rejects_dashes_length_and_cohort_size(self):
        r = self.save(["Whales — short.", "x" * 281], general="The 77 Hyperliquid whales we track went short.")
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["errors"]), 3)
        self.assertEqual(self.store.post("1")["status"], "pending")

    def test_one_or_two_drafts(self):
        r = self.save(["Funding cost him $1.59M.", "Size tells you conviction", "A third one."])
        self.assertFalse(r["ok"])
        self.assertIn("1 or 2 drafts", r["error"])

    def test_saves_clean_drafts_in_order(self):
        r = self.save(["Funding cost him $1.59M.", "Size tells you conviction :)"],
                      general="At 14x, a 7% move ends it.",
                      insight="Strategy pays for its buys by selling stock, so the bid lasts while MSTR trades at a premium.")
        self.assertTrue(r["ok"])
        self.assertEqual(self.store.post("1")["status"], "drafted")
        self.assertEqual([d["label"] for d in self.store.latest_drafts("1")], ["A", "B", "N", "G"])
        text, kb = render(self.store.post("1"), self.store.latest_drafts("1"))
        self.assertIn("🔎 N", text)
        self.assertIn("🌐 G", text)
        self.assertEqual([b["text"] for b in kb["inline_keyboard"][0]], ["↩ A", "↩ B", "↩ N", "↩ G"])

    def test_general_reply_is_validated(self):
        bad = self.save(["Funding cost him $1.59M."], general="Rates matter, not the chart.")
        self.assertFalse(bad["ok"])
        self.assertIn("G: 'not X but Y' contrast", " ".join(bad["errors"]))

    def test_insight_is_validated(self):
        bad = self.save(["Funding cost him $1.59M."], insight="Here's the thing: ETFs sold.")
        self.assertIn("N: staged line", " ".join(bad["errors"]))

    def test_missing_general_and_question_are_warnings(self):
        r = self.save(["Funding cost him $1.59M."])
        joined = " ".join(r["warnings"])
        self.assertIn("no general reply (G)", joined)
        self.assertIn("no draft asks the author anything", joined)
        asked = self.save(["Funding cost him $1.59M. Is that in your plan?"])
        self.assertNotIn("asks the author", " ".join(asked["warnings"]))

    def test_rejects_humanizer_tells(self):
        r = self.save(["We'd expect grind, not fireworks.", "It's not just leverage. Let that sink in."],
                      general="Great question! This is a pivotal moment.")
        self.assertFalse(r["ok"])
        joined = " ".join(r["errors"])
        self.assertIn('A: \'not X but Y\' contrast (humanizer §1): ", not fireworks."', r["errors"])
        self.assertIn("B: staged line", joined)
        self.assertIn("G: stock AI word", joined)
        self.assertIn("G: chatbot residue", joined)
        self.assertEqual(self.store.post("1")["status"], "pending")

    def test_rejects_playbook_forms(self):
        r = self.save(["$SOXL went the other way, 10 bearish vs 3 bullish.",
                       "Biggest move: 10 wallets bullish on $NVDA."],
                      general="Profit-taking on one chip ticker, dip-buying on another. We'll see if they hold.")
        self.assertFalse(r["ok"])
        joined = " ".join(r["errors"])
        self.assertIn("A: dramatized turn", joined)
        self.assertIn("B: label then reveal", joined)
        self.assertIn("G: mirrored pair", joined)
        self.assertIn("G: closing forecast", joined)

    def test_rejects_we_would_closers(self):
        for closer in ["We'd watch whether those shorts start covering.",
                       "So we size for the drawdown first, then think about targets.",
                       "We'd rather be a bit late with a clean invalidation than early.",
                       "We do the same."]:
            r = self.save([f"Whales added $4.8M of $BTC shorts in 24h.\n{closer}"])
            self.assertIn("ending (playbook, endings)", " ".join(r.get("errors", [])), closer)

    def test_we_closer_only_counts_at_the_end(self):
        r = self.save(["We'd size down before FOMC. Whales already cut $2.9M of $BTC longs."])
        self.assertTrue(r["ok"], r.get("errors"))

    def test_rejects_the_bot_opener_and_figure_stacks(self):
        r = self.save(["The Hyperliquid whales we track are net short $1.63M on $SOL.",
                       "$RKLB down 14.1%: +$649K. $META down 2.5%: +$728K. $GOOGL down 2.7%: +$569K."])
        joined = " ".join(r["errors"])
        self.assertIn('A: opens with "The Hyperliquid whales we track"', joined)
        self.assertIn("B: 6 figures", joined)
        ok = self.save(["HL whales we track are net short $SOL by $1.63M, funding still positive at 8.6% APR."])
        self.assertTrue(ok["ok"], ok.get("errors"))

    def test_plain_facts_without_forms_pass(self):
        r = self.save(["10 wallets cut $SOXL longs after a +23% day, 8 of them in profit, $241k realized.",
                       "$NVDA: 10 wallets bullish vs 2, $934k in, 8 fresh longs."],
                      general="We'll check those 8 $NVDA longs again tomorrow.")
        self.assertTrue(r["ok"], r.get("errors"))

    def test_plain_contrast_with_information_passes(self):
        r = self.save(["Low open interest means less forced selling on the next dip, but also less fuel "
                       "for a squeeze up.",
                       "Not for everyone. Whales stayed net short and are up $1.26M."])
        self.assertTrue(r["ok"], r.get("errors"))

    def test_we_over_i(self):
        warn = "more drafts in 'I' than in 'we'"
        mostly_i = self.save(["I'd stay out of this one.", "My read: funding decides it."])
        self.assertIn(warn, " ".join(mostly_i["warnings"]))
        mostly_we = self.save(["Our data says funding decides it.", "We're out of this one."])
        self.assertNotIn(warn, " ".join(mostly_we["warnings"]))

    def test_pending_carries_owner_instructions(self):
        self.store.add_feedback("1", "edit", "kratší")
        view = self.tools.pending()["pending"][0]
        self.assertEqual(view["owner_instructions"], ["kratší"])

    def test_ideal_rewrites_reach_the_drafting_session(self):
        self.store.insert_post(make_post("2", text="Friday is for booking profits"))
        self.store.save_drafts("2", ["Whales are adding, not booking."])
        self.store.add_feedback("2", "ideal", "Our whales are adding into Friday. We'd stay long.")
        example = self.tools.pending()["owner_examples"][0]
        self.assertEqual(example["our_drafts"], {"A": "Whales are adding, not booking."})
        self.assertEqual(example["owner_ideal"], "Our whales are adding into Friday. We'd stay long.")
        self.assertEqual(len(self.tools.feedback_summary()["ideal_rewrites"]), 1)

    def test_owner_replies_reach_the_drafting_session_with_their_reach(self):
        self.store.insert_post(make_post("3"))
        self.store.save_drafts("3", ["Hyperliquid whales stayed net short and are up $1.26M on it."])
        self.store.insert_post(make_post("4", source="mention", text="@DexiScan Longs closing everywhere lol"))
        for rid, pid, matched, text in [
                ("r1", "3", "A~", "@AshCrypto Wallets we're tracking remain net short and are up $1.26M."),
                ("r2", "3", "A", "@AshCrypto Hyperliquid whales stayed net short and are up $1.26M on it."),
                ("r3", "4", "own", "@soonmource Could be smart risk before a period of uncertainty :)")]:
            self.store.insert_reply({"id": rid, "post_id": pid, "text": text, "created_at": iso(1), "matched": matched})
        self.store.set_outcome("r1", {"impression_count": 35, "like_count": 0, "reply_count": 0}, False)
        examples = self.tools.pending()["owner_examples"]
        posted = {e["owner_posted"]: e for e in examples if "owner_posted" in e}
        self.assertEqual(set(posted), {"Wallets we're tracking remain net short and are up $1.26M.",
                                       "Could be smart risk before a period of uncertainty :)"},
                         "a draft posted unchanged is not the owner's writing; an own reply without drafts is")
        edited = posted["Wallets we're tracking remain net short and are up $1.26M."]
        self.assertEqual(edited["our_drafts"], {"A": "Hyperliquid whales stayed net short and are up $1.26M on it."})
        self.assertEqual(edited["reach_24h"]["impressions"], 35)
        self.assertEqual(posted["Could be smart risk before a period of uncertainty :)"]["our_drafts"], {})

    def test_top_replies_rank_by_reach_and_skip_banned_forms(self):
        for pid, rid, text, metrics, answered in [
                ("5", "t1", "@a Do you see the band at 77K as fresh longs?", {"impression_count": 506, "like_count": 1}, 0),
                ("6", "t2", "@a Whales bought it. We'd want the selling to stop first.", {"impression_count": 900}, 0),
                ("7", "t3", "@a Sell the news 🙈", {"impression_count": 64, "like_count": 2}, 1)]:
            self.store.insert_post(make_post(pid))
            self.store.insert_reply({"id": rid, "post_id": pid, "text": text, "created_at": iso(1), "matched": "own"})
            self.store.set_outcome(rid, metrics, bool(answered))
        top = self.tools.top_replies()
        self.assertEqual([t["reply"] for t in top], ["Do you see the band at 77K as fresh longs?", "Sell the news 🙈"])
        self.assertTrue(top[1]["author_replied"])


class VoiceTest(unittest.TestCase):
    def test_the_voice_copy_matches_the_owners_skill(self):
        from dexi_x_radar.voice import is_current, source_path
        source = source_path()
        if not source:
            self.skipTest("the synced rudejohn-x-voice skill isn't on this machine")
        copy = load_settings({"DB_PATH": ":memory:"}).voice_file
        self.assertTrue(is_current(copy, source),
                        "prompts/voice/SKILL.md is older than your rudejohn-x-voice skill: "
                        "run `python -m dexi_x_radar sync-voice`, commit and deploy")

    def test_sync_copies_the_source(self):
        import os
        from dexi_x_radar.voice import sync
        with tempfile.TemporaryDirectory() as tmp:
            src, copy = Path(tmp) / "SKILL.md", Path(tmp) / "out" / "SKILL.md"
            src.write_text("voice v2", encoding="utf-8")
            os.environ["VOICE_SKILL_SOURCE"] = str(src)
            try:
                self.assertIn("copied", sync(copy))
                self.assertEqual(copy.read_text(encoding="utf-8"), "voice v2")
                self.assertIn("up to date", sync(copy))
            finally:
                del os.environ["VOICE_SKILL_SOURCE"]


class LoggingTest(unittest.TestCase):
    def test_http_client_logs_never_carry_request_urls(self):
        import logging
        from dexi_x_radar.app import quiet_http_logs
        quiet_http_logs()
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO))


class BridgeTest(unittest.TestCase):
    def test_rpc_sends_no_protocol_header_and_unwraps_results(self):
        from dexi_x_radar.bridge import RemoteError, rpc
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = __import__("json").loads(request.content)
            if body["params"].get("name") == "boom":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"message": "nope"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": body["method"]}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                ok = await rpc(http, "https://radar/x", "tok", "tools/call", {"name": "radar_skip", "arguments": {}})
                with self.assertRaises(RemoteError):
                    await rpc(http, "https://radar/x", "tok", "tools/call", {"name": "boom", "arguments": {}})
                return ok
        self.assertEqual(asyncio.run(run()), {"ok": "tools/call"})
        self.assertNotIn("mcp-protocol-version", seen[0].headers)
        self.assertEqual(seen[0].headers["authorization"], "Bearer tok")


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


def prague(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Prague"))


class QuietHoursTest(unittest.TestCase):
    def setUp(self):
        from dexi_x_radar.quiet import QuietHours
        self.q = QuietHours.parse("22-6", "Europe/Prague")

    def test_night_is_quiet_in_summer_and_winter_time(self):
        for month, day in [(7, 15), (12, 15)]:
            self.assertTrue(self.q.active(prague(month, day, 22)))
            self.assertTrue(self.q.active(prague(month, day, 5, 59)))
            self.assertFalse(self.q.active(prague(month, day, 6)))
            self.assertFalse(self.q.active(prague(month, day, 21, 59)))
        self.assertTrue(self.q.active(datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)), "22:30 CEST")
        self.assertFalse(self.q.active(datetime(2026, 12, 15, 20, 30, tzinfo=timezone.utc)), "21:30 CET")

    def test_end_and_time_left(self):
        self.assertEqual(self.q.last_end(prague(10, 24, 9)), prague(10, 24, 6).astimezone(timezone.utc))
        self.assertEqual(self.q.last_end(prague(10, 24, 3)), prague(10, 23, 6).astimezone(timezone.utc))
        self.assertEqual(self.q.seconds_left(prague(10, 24, 5, 30)), 1800)
        self.assertEqual(self.q.seconds_left(prague(10, 24, 23)), 7 * 3600)
        self.assertEqual(self.q.seconds_left(prague(10, 24, 12)), 0)

    def test_off(self):
        from dexi_x_radar.quiet import QuietHours
        self.assertIsNone(QuietHours.parse("off", "Europe/Prague"))
        self.assertIsNone(load_settings({"QUIET_HOURS": "off"}).quiet())

    def test_poller_skips_the_night_after_the_pause(self):
        store = Store(":memory:")
        for name in ("search:from:lookonchain", "mentions", "own"):
            store.set_cursor(name, "123")
        poller = Poller(load_settings({"DB_PATH": ":memory:"}), store, FakeX(), notifier=None)
        poller.resume_after_quiet()
        self.assertIsNone(store.cursor("search:from:lookonchain"), "the watch search restarts after the night")
        self.assertEqual(store.cursor("mentions"), "123", "mentions from the night are still read")
        self.assertEqual(store.cursor("own"), "123")
        quiet_end = poller.quiet.last_end().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertGreaterEqual(poller.window_start(), quiet_end)


class FakeAnthropic:
    """Answers each create() with the next canned JSON and records the request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self
        self.beta = types.SimpleNamespace(messages=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        searches = types.SimpleNamespace(web_search_requests=2) if kwargs.get("tools") else None
        usage = types.SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0, server_tool_use=searches)
        content = [types.SimpleNamespace(type="thinking", thinking=""),
                   types.SimpleNamespace(type="text", text=json.dumps(answer))]
        return types.SimpleNamespace(content=content, stop_reason="end_turn", usage=usage, model=kwargs["model"])


class FakeDexi:
    def __init__(self):
        self.markets = []

    async def xbrief(self, market="crypto"):
        self.markets.append(market)
        return {"market": market, "cards": {"book": {"BTC": {"long": 13.9, "short": 13.4}}}}

    async def whale_radar(self, address):
        return {"address": address, "stats": {"win_rate": 0.125, "n": 8}}


REPLY = {"reply": True, "market": "crypto", "research": False, "reason": "whale claim"}
DRAFTED = {"drafts": ["HL whales flipped net long $BTC: $13.9M vs $13.4M short.", "Dip buyers have company :)"],
           "insight": "", "insight_source": "",
           "general": "A flip this small can come from one wallet. How many wallets added matters as much as size.",
           "note": "Whale flip · xbrief BTC book", "skip_reason": ""}
FINDINGS = {"findings": [{"fact": "Strive bought $511M of BTC on Nov 3", "source": "https://example.com/strive",
                          "published": "2026-11-03", "why": "a spot buyer on the same dip"}]}


class DrafterTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.insert_post(make_post("1", text="Whales are buying the dip", addresses=["0xabc"]))
        self.settings = load_settings({"DB_PATH": ":memory:", "QUIET_HOURS": "off", "DRAFT_DAILY_BUDGET_USD": "5",
                                       "DRAFT_MODEL": "claude-opus-5-5", "TRIAGE_MODEL": "claude-haiku-5-5"})
        self.dexi = FakeDexi()
        self.tools = RadarTools(self.settings, self.store, None, dexi=self.dexi)

    def drafter(self, *answers):
        from dexi_x_radar.drafter import Drafter
        self.client = FakeAnthropic(*answers)
        return Drafter(self.settings, self.store, self.tools, None, client=self.client)

    def test_triage_then_draft_with_playbook_humanizer_and_data(self):
        drafter = self.drafter(REPLY, DRAFTED)
        done = asyncio.run(drafter.run_once())
        self.assertEqual(done, {"1": "drafted"})
        self.assertEqual([d["label"] for d in self.store.latest_drafts("1")], ["A", "B", "G"])
        triage, draft = self.client.calls
        self.assertEqual(triage["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", triage, "Haiku has no server-side fallback")
        self.assertEqual(draft["model"], "claude-opus-5-5")
        self.assertEqual(draft["fallbacks"], "default")
        system = draft["system"][0]["text"]
        self.assertIn("# Reply playbook", system)
        self.assertIn("# Humanizer", system)
        self.assertIn("<voice_skill>\n---\nname: \"rudejohn-x-voice\"", system)
        user = " ".join(b.get("text", "") for b in draft["messages"][0]["content"])
        self.assertIn("13.9", user, "the x-brief goes with the post")
        self.assertIn("0xabc", user, "Whale Radar for the wallet in the post")
        # 2 calls x (1000 in, 500 out): Haiku $0.00035 + Opus $0.014
        self.assertAlmostEqual(drafter.spent_today(), 0.01435, places=6)

    def test_research_feeds_the_insight_and_its_source_reaches_the_note(self):
        with_n = {**DRAFTED, "insight": "Strive bought $511M of $BTC on Nov 3, a spot bid under this dip.",
                  "insight_source": "https://example.com/strive"}
        drafter = self.drafter({**REPLY, "research": True}, FINDINGS, with_n)
        self.assertEqual(asyncio.run(drafter.run_once()), {"1": "drafted"})
        triage, research, draft = self.client.calls
        self.assertEqual(research["model"], "claude-sonnet-5-5")
        self.assertEqual(research["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}])
        self.assertEqual(research["output_config"], {"effort": "low"})
        self.assertIn("example.com/strive", draft["messages"][0]["content"][-1]["text"], "findings go to the drafter")
        self.assertEqual([d["label"] for d in self.store.latest_drafts("1")], ["A", "B", "N", "G"])
        self.assertTrue(self.store.post("1")["note"].endswith("· N: https://example.com/strive"))
        # Haiku $0.00035 + Sonnet $0.007 + 2 searches $0.02 + Opus $0.014
        self.assertAlmostEqual(drafter.spent_today(), 0.04135, places=6)

    def test_failed_research_still_drafts(self):
        drafter = self.drafter({**REPLY, "research": True}, "no json here", DRAFTED)
        self.assertEqual(asyncio.run(drafter.run_once()), {"1": "drafted"})
        self.assertNotIn("N", [d["label"] for d in self.store.latest_drafts("1")])

    def test_failed_brief_still_drafts_without_whale_data(self):
        async def broken(market="crypto"):
            raise RuntimeError("/api/x-brief/xyz: HTTP 500")
        self.dexi.xbrief = broken
        drafter = self.drafter({**REPLY, "market": "xyz"}, DRAFTED)
        self.assertEqual(asyncio.run(drafter.run_once()), {"1": "drafted"})
        user = self.client.calls[1]["messages"][0]["content"]
        self.assertIn("write no whale figures", " ".join(b.get("text", "") for b in user))

    def test_research_off(self):
        self.settings = load_settings({"DB_PATH": ":memory:", "QUIET_HOURS": "off", "NOTE_MODEL": "off"})
        self.tools = RadarTools(self.settings, self.store, None, dexi=self.dexi)
        drafter = self.drafter({**REPLY, "research": True}, DRAFTED)
        self.assertEqual(asyncio.run(drafter.run_once()), {"1": "drafted"})
        self.assertEqual(len(self.client.calls), 2)

    def test_triage_skip_never_drafts(self):
        done = asyncio.run(self.drafter({"reply": False, "market": "crypto", "reason": "meme"}).run_once())
        self.assertEqual(done, {"1": "skipped"})
        self.assertEqual(self.store.post("1")["skip_by"], "claude")
        self.assertEqual(len(self.client.calls), 1)

    def test_rejected_drafts_get_rewritten(self):
        bad = {**DRAFTED, "general": "Rates matter, not the chart.",
               "drafts": ["Whales added $4.8M of $BTC shorts.\nWe'd watch whether they cover."]}
        done = asyncio.run(self.drafter(REPLY, bad, DRAFTED).run_once())
        self.assertEqual(done, {"1": "drafted"})
        retry = self.client.calls[2]["messages"]
        self.assertEqual([m["role"] for m in retry], ["user", "assistant", "user"])
        self.assertIn("not X but Y", retry[2]["content"])
        self.assertIn("we'd watch / we size", retry[2]["content"])

    def test_owner_redraft_is_not_triaged_away(self):
        self.store.add_feedback("1", "regen")
        done = asyncio.run(self.drafter({"reply": False, "market": "crypto", "reason": "meh"}, DRAFTED).run_once())
        self.assertEqual(done, {"1": "drafted"})
        self.assertIn("different angle", self.client.calls[1]["messages"][0]["content"][-1]["text"])

    def test_budget_stops_drafting(self):
        drafter = self.drafter(REPLY, DRAFTED)
        self.store.set_cursor(f"anthropic_usd:{drafter._day()}", "5.01")
        self.assertEqual(asyncio.run(drafter.run_once()), {})
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.post("1")["status"], "pending")

    def test_a_post_drafted_elsewhere_meanwhile_is_left_alone(self):
        drafter = self.drafter(REPLY, DRAFTED)
        create = self.client.create

        async def local_session_wins(**kwargs):
            resp = await create(**kwargs)
            if len(self.client.calls) == 2:
                self.store.save_drafts("1", ["Drafted over MCP."])
                self.store.set_status("1", "drafted")
            return resp
        self.client.create = local_session_wins
        self.assertEqual(asyncio.run(drafter.run_once()), {"1": "taken"})
        self.assertEqual([d["text"] for d in self.store.latest_drafts("1")], ["Drafted over MCP."])

    def test_quiet_hours_stop_drafting(self):
        drafter = self.drafter(REPLY, DRAFTED)
        drafter.quiet = types.SimpleNamespace(active=lambda: True)
        self.assertEqual(asyncio.run(drafter.run_once()), {})
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
