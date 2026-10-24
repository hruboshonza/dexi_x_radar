"""MCP server: how Claude reads the queue, fetches DexiScan data and saves drafts.

There is deliberately no tool that posts to X.
"""
import hmac
import re
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .config import Settings
from .dexiscan import DexiScan
from .matching import strip_mentions
from .store import Store, now_iso, parse_iso
from .telegram import Notifier

MAX_DRAFT = 280
OWNER_EXAMPLES = 10
_DASHES = ("—", "–")
# "77 Hyperliquid whales we track", "214 wallets we track": the owner never states the cohort size.
_COHORT_SIZE = re.compile(r"\b\d{2,4}\s+(?:tracked\s+)?(?:hyperliquid\s+)?(?:whales|wallets)\s+(?:that\s+)?we\s+track",
                          re.IGNORECASE)
# The strongest tells from the humanizer skill; a draft with one never reaches the phone.
_HUMANIZER_TELLS = (
    ("'not X but Y' contrast (humanizer §1)", re.compile(
        r"\bnot (?:just|only|merely)\b"
        r"|\b(?:it|this|that)(?:'s| is) not\b[^.!?]*,\s*(?:it|this|that)(?:'s| is)\b"
        r"|,\s+not\s+[^,.;:!?]{1,40}[.!?]", re.IGNORECASE)),
    ("staged line (humanizer §2-4)", re.compile(
        r"\b(?:let that sink in|read that again|here's the thing|the real question|at its core"
        r"|make no mistake|what (?:really )?matters)\b", re.IGNORECASE)),
    ("stock AI word (humanizer §12)", re.compile(
        r"\b(?:delve\w*|crucial|pivotal|testament|tapestry|vibrant|showcas\w*|underscor\w*)\b", re.IGNORECASE)),
    ("chatbot residue (humanizer §22)", re.compile(
        r"\b(?:great question|hope this helps|let me know)\b", re.IGNORECASE)),
)
# The owner writes as "we" (DexiScan); "I" is the occasional exception.
_SINGULAR = re.compile(r"\b(?:I|I'm|I'd|I've|I'll|my|me)\b")
_PLURAL = re.compile(r"\b(?:we|we're|we'd|we've|we'll|our|us)\b", re.IGNORECASE)

INSTRUCTIONS = """Reply radar for the @DexiScan X account. Posts arrive in a queue; you write reply
drafts, the owner posts them by hand. Workflow: radar_pending -> for each post either radar_skip
(our data adds nothing) or fetch data (dexiscan_xbrief, dexiscan_whale_radar for full wallet
addresses) and radar_save_drafts (1-3 drafts, each built on DexiScan data). The humanizer skill
decides how drafts are written: write them, run its process, save only the final text.
`owner_examples` in radar_pending are the owner's own writing next to our drafts (owner_ideal: a 💡
rewrite; owner_posted: what the owner actually posted after changing them): humanizer's writing sample.
radar_playbook says what a reply must carry: a number the thread doesn't have, readable in one
pass. Use only numbers from the post or from the DexiScan tools. Write as "we" (DexiScan). Never
state the total number of tracked wallets."""


class RadarTools:
    def __init__(self, settings: Settings, store: Store, notifier: Notifier | None, dexi: DexiScan):
        self.s = settings
        self.store = store
        self.notifier = notifier
        self.dexi = dexi

    def _post_view(self, post: dict) -> dict:
        age = int((datetime.now(timezone.utc) - parse_iso(post["created_at"])).total_seconds() // 60)
        return {
            "id": post["id"], "url": post["url"], "source": post["source"], "status": post["status"],
            "author": post["author"], "author_followers": post.get("author_followers"),
            "age_min": age, "created_at": post["created_at"], "text": post["text"],
            "media": post["media"], "urls": post["urls"], "addresses": post["addresses"],
            "metrics": post["metrics"], "note": post.get("note"),
        }

    def ideal_rewrites(self, since: str = "", limit: int | None = None) -> list[dict]:
        """The owner's "this is how it should have sounded", newest first, next to our drafts."""
        ideals = [f for f in self.store.feedback(since=since) if f["kind"] == "ideal"][::-1][:limit]
        out = []
        for f in ideals:
            post = self.store.post(f["post_id"]) or {}
            out.append({"post_id": f["post_id"], "author": post.get("author"), "post": (post.get("text") or "")[:500],
                        "our_drafts": {d["label"]: d["text"] for d in self.store.latest_drafts(f["post_id"])},
                        "owner_ideal": f["value"], "at": f["created_at"]})
        return out

    def posted_rewrites(self, limit: int | None = None) -> list[dict]:
        """Replies the owner posted after changing our drafts (edited, merged or own), newest first."""
        out = []
        for r in self.store.replies():
            matched = r["matched"]
            if not (matched.endswith("~") or matched.startswith("merged") or matched == "own"):
                continue
            drafts = self.store.latest_drafts(r["post_id"])
            if not drafts:
                continue
            post = self.store.post(r["post_id"]) or {}
            out.append({"post_id": r["post_id"], "author": post.get("author"), "post": (post.get("text") or "")[:500],
                        "our_drafts": {d["label"]: d["text"] for d in drafts},
                        "owner_posted": strip_mentions(r["text"]), "matched": matched, "at": r["created_at"]})
        return out[:limit]

    def owner_examples(self, limit: int = OWNER_EXAMPLES) -> list[dict]:
        """Humanizer's writing sample: the owner's 💡 rewrites and edited posted replies, newest first."""
        examples = self.ideal_rewrites(limit=limit) + self.posted_rewrites(limit=limit)
        return sorted(examples, key=lambda e: e["at"], reverse=True)[:limit]

    def pending(self, limit: int = 5) -> dict:
        posts = self.store.posts(status="pending", limit=limit)
        out = []
        for p in posts:
            view = self._post_view(p)
            feedback = self.store.feedback(post_id=p["id"])
            view["owner_instructions"] = [f["value"] for f in feedback if f["kind"] == "edit"]
            view["regenerate"] = any(f["kind"] == "regen" for f in feedback)
            view["previous_drafts"] = self.store.all_drafts(p["id"])
            out.append(view)
        return {"pending": out, "count": len(out),
                "owner_examples": self.owner_examples() if out else []}

    async def save_drafts(self, post_id: str, drafts: list[str], note: str = "") -> dict:
        post = self.store.post(post_id)
        if not post:
            return {"ok": False, "error": f"unknown post {post_id}"}
        texts = [d.strip() for d in drafts if d and d.strip()]
        if not 1 <= len(texts) <= 3:
            return {"ok": False, "error": "send 1 to 3 drafts, the full reply first"}
        errors, warnings = [], []
        for label, text in zip("ABC", texts):
            if any(ch in text for ch in _DASHES):
                errors.append(f"{label}: contains an em or en dash")
            if len(text) > MAX_DRAFT:
                errors.append(f"{label}: {len(text)} chars, max {MAX_DRAFT}")
            if _COHORT_SIZE.search(text):
                errors.append(f"{label}: states the total number of tracked wallets")
            plain = text.replace("’", "'")
            for name, pattern in _HUMANIZER_TELLS:
                if m := pattern.search(plain):
                    errors.append(f'{label}: {name}: "{m.group(0).strip()}"')
        singular = sum(1 for t in texts if _SINGULAR.search(t))
        plural = sum(1 for t in texts if _PLURAL.search(t))
        if singular > plural:
            warnings.append("more drafts in 'I' than in 'we': the owner prefers 'we' (DexiScan)")
        if errors:
            return {"ok": False, "errors": errors}
        rnd = self.store.save_drafts(post_id, texts)
        self.store.set_status(post_id, "drafted", note=note or None, skip_by=None, skip_reason=None)
        if self.notifier:
            await self.notifier.show(post_id, ping=True)
        return {"ok": True, "round": rnd, "warnings": warnings}

    async def skip(self, post_id: str, reason: str) -> dict:
        if not self.store.post(post_id):
            return {"ok": False, "error": f"unknown post {post_id}"}
        self.store.set_status(post_id, "skipped", skip_by="claude", skip_reason=reason)
        if self.notifier:
            await self.notifier.drop(post_id)
        return {"ok": True}

    def queue(self, status: str | None = None, limit: int = 20) -> dict:
        return {"posts": [self._post_view(p) for p in self.store.posts(status=status, limit=limit)],
                "counts": self.store.status_counts()}

    def post_detail(self, post_id: str) -> dict:
        post = self.store.post(post_id)
        if not post:
            return {"error": f"unknown post {post_id}"}
        return {**self._post_view(post), "drafts": self.store.all_drafts(post_id),
                "replies": self.store.replies(post_id=post_id), "feedback": self.store.feedback(post_id=post_id),
                "skip": {"by": post.get("skip_by"), "reason": post.get("skip_reason")}}

    def feedback_summary(self, days: int = 7) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        replies = [r for r in self.store.replies(since=since) if r["matched"] != "unqueued"]
        feedback = self.store.feedback(since=since)
        matched: dict[str, int] = {}
        sent = []
        for r in replies:
            key = "merged" if r["matched"].startswith("merged") else r["matched"].rstrip("~") + (
                " edited" if r["matched"].endswith("~") else "")
            matched[key] = matched.get(key, 0) + 1
            post = self.store.post(r["post_id"]) or {}
            sent.append({"to": post.get("author"), "posted": r["text"], "matched": r["matched"],
                         "similarity": r["similarity"], "diff": r["diff"], "metrics": r["metrics"],
                         "author_replied": r["author_replied"]})
        skips: dict[str, int] = {}
        for f in feedback:
            if f["kind"] == "skip":
                skips[f["value"]] = skips.get(f["value"], 0) + 1
        per_author: dict[str, dict] = {}
        for p in self.store.posts(limit=1000):
            if p["fetched_at"] < since:
                continue
            a = per_author.setdefault(p["author"], {"posts": 0, "sent": 0, "skipped": 0, "ignored": 0, "dismissed": 0})
            a["posts"] += 1
            if p["status"] in ("sent", "skipped", "ignored", "dismissed"):
                a[p["status"]] += 1
        return {
            "since": since,
            "queue": self.store.status_counts(since),
            "drafts_used": matched,
            "sent": sent,
            "owner_skip_reasons": skips,
            "edit_instructions": [f["value"] for f in feedback if f["kind"] == "edit"],
            "ideal_rewrites": self.ideal_rewrites(since=since),
            "dismissed": sum(1 for f in feedback if f["kind"] == "dismiss"),
            "regenerate_requests": sum(1 for f in feedback if f["kind"] == "regen"),
            "per_author": per_author,
        }

    def playbook(self) -> str:
        return self.s.playbook_file.read_text(encoding="utf-8")


def build_mcp(tools: RadarTools, path: str = "/mcp") -> FastMCP:
    # host 0.0.0.0 keeps FastMCP's localhost-only Host check off; the bearer token guards the path.
    mcp = FastMCP("dexi-x-radar", instructions=INSTRUCTIONS, host="0.0.0.0", stateless_http=True,
                  json_response=True, streamable_http_path=path)

    @mcp.tool()
    def radar_pending(limit: int = 5) -> dict:
        """Posts waiting for drafts, newest first, with owner instructions and earlier drafts.
        `owner_examples`: the owner's latest 💡 rewrites and edited posted replies next to our drafts
        (how a reply should sound; the humanizer writing sample)."""
        return tools.pending(limit)

    @mcp.tool()
    async def radar_save_drafts(post_id: str, drafts: list[str], note: str = "") -> dict:
        """Save the drafts and push them to the owner's phone as one message under the post.
        `drafts`: 1-3 replies (A, B, C), the full reply first, each built on DexiScan data and
        passed through the humanizer skill (final text only).
        `note`: the angle and the data used, e.g. "Whale flip vs ETF week · xbrief BTC book".
        Rejected: em/en dashes, over 280 chars, stating the total number of tracked wallets,
        the strongest humanizer tells (not X but Y, staged lines, stock AI words, chatbot residue)."""
        return await tools.save_drafts(post_id, drafts, note)

    @mcp.tool()
    async def radar_skip(post_id: str, reason: str) -> dict:
        """Skip a post that is not worth a reply (memes, airdrops, off-topic). The owner can override."""
        return await tools.skip(post_id, reason)

    @mcp.tool()
    def radar_queue(status: str | None = None, limit: int = 20) -> dict:
        """The queue, optionally by status: pending | drafted | sent | skipped | ignored | dismissed."""
        return tools.queue(status, limit)

    @mcp.tool()
    def radar_get_post(post_id: str) -> dict:
        """Read one queued post with every draft round, the owner's reply and all feedback (read-only)."""
        return tools.post_detail(post_id)

    @mcp.tool()
    def radar_feedback(days: int = 7) -> dict:
        """What the owner posted vs the drafts, skip reasons, edit instructions, the owner's ideal
        rewrites, outcomes, per account."""
        return tools.feedback_summary(days)

    @mcp.tool()
    def radar_playbook() -> str:
        """The reply rules (PLAYBOOK.md). Read before drafting."""
        return tools.playbook()

    @mcp.tool()
    async def dexiscan_xbrief(market: str = "crypto") -> dict:
        """Current whale positioning from DexiScan (bias, accumulation, crowd, leverage…). market: crypto | xyz."""
        return await tools.dexi.xbrief(market)

    @mcp.tool()
    async def dexiscan_whale_radar(address: str) -> dict:
        """Whale Radar for one full wallet address: PnL, win rate with n, avg win/loss, open positions."""
        return await tools.dexi.whale_radar(address)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse(f"ok {now_iso()}")

    return mcp


class BearerAuth:
    """Pure ASGI guard: every request under `prefix` needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str, prefix: str = "/mcp"):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.enabled = bool(token)
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not (self.enabled and hmac.compare_digest(got, self.expected)):
                # Read the request body first: answering with it unread makes uvicorn drop the connection.
                more = True
                while more:
                    message = await receive()
                    more = message.get("type") == "http.request" and message.get("more_body", False)
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(tools: RadarTools, token: str, path: str = "/mcp"):
    return BearerAuth(build_mcp(tools, path).streamable_http_app(), token, prefix=path)
