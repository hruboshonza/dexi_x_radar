"""Phase-0 reply radar test: pull new posts from watched accounts and @DexiScan mentions
through the official X API (pay-per-use) and store them for drafting in Claude Code.

Read-only. Nothing is ever posted. Every reply is sent by a human from the X app.

Usage:
    python radar_poll.py                  # new posts since the last run
    python radar_poll.py --dry-run        # show queries and the worst-case cost, no calls
    python radar_poll.py --lookback-hours 6 --max-posts 60
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
INBOX = ROOT / "inbox"
API = "https://api.x.com/2"
XBRIEF_URLS = {
    "xbrief": "https://dexiscan.com/api/x-brief",
    "xbrief_xyz": "https://dexiscan.com/api/x-brief/xyz",
}
LENS_URL = "https://dexiscan.com/api/wallet/{address}/lens"
LENS_MAX_PER_RUN = 5
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
OWN_HANDLE = "DexiScan"

# docs.x.com/x-api/getting-started/pricing, checked 2026-10-14
PRICE_POST_READ = 0.005
PRICE_USER_READ = 0.010

TWEET_FIELDS = "created_at,public_metrics,author_id,conversation_id,entities,attachments,lang"
EXPANSIONS = "author_id,attachments.media_keys"
MEDIA_FIELDS = "type,url,preview_image_url,alt_text"
USER_FIELDS = "username,name,public_metrics,verified"
MAX_QUERY_LEN = 512


def load_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_watchlist() -> list[str]:
    handles = []
    for line in (ROOT / "watchlist.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("@")
        if line and not line.startswith("#"):
            handles.append(line)
    return handles


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"users": {}, "since": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


class XApi:
    def __init__(self, bearer: str):
        self.bearer = bearer
        self.posts_read = 0
        self.users_read = 0

    def get(self, path: str, params: dict) -> dict:
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.bearer}",
            "User-Agent": "dexi_x_radar/0.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            hint = {
                401: "token is wrong or revoked",
                402: "out of credits",
                403: "the app has no access to this endpoint, or no credits",
                429: f"rate limited, reset at {e.headers.get('x-rate-limit-reset')}",
            }.get(e.code, "")
            sys.exit(f"X API {e.code} on {path} ({hint}): {body[:500]}")


def chunk_queries(handles: list[str]) -> list[str]:
    """Split `from:` handles into search queries that fit the 512-char limit."""
    suffix = " -is:reply -is:retweet"
    queries, current = [], []
    for h in handles:
        candidate = current + [f"from:{h}"]
        if len("(" + " OR ".join(candidate) + ")" + suffix) > MAX_QUERY_LEN and current:
            queries.append("(" + " OR ".join(current) + ")" + suffix)
            candidate = [f"from:{h}"]
        current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + ")" + suffix)
    return queries


def resolve_users(api: XApi, state: dict, handles: list[str]) -> None:
    missing = [h for h in handles if h.lower() not in state["users"]]
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        data = api.get("/users/by", {"usernames": ",".join(batch), "user.fields": USER_FIELDS})
        for u in data.get("data", []):
            api.users_read += 1
            state["users"][u["username"].lower()] = {
                "id": u["id"],
                "username": u["username"],
                "followers": u.get("public_metrics", {}).get("followers_count"),
            }
        for err in data.get("errors", []):
            print(f"  ! user lookup: {err.get('detail') or err}")


def fetch_paged(api: XApi, path: str, params: dict, max_posts: int) -> tuple[list, dict]:
    is_mentions = "/mentions" in path
    min_results = 5 if is_mentions else 10  # recent search rejects max_results < 10
    posts, users, media = [], {}, {}
    next_token = None
    while len(posts) < max_posts:
        page = dict(params)
        page["max_results"] = max(min_results, min(100, max_posts - len(posts)))
        if next_token:
            page["pagination_token" if is_mentions else "next_token"] = next_token
        data = api.get(path, page)
        batch = data.get("data", [])
        api.posts_read += len(batch)
        posts.extend(batch)
        inc = data.get("includes", {})
        users.update({u["id"]: u for u in inc.get("users", [])})
        media.update({m["media_key"]: m for m in inc.get("media", [])})
        next_token = data.get("meta", {}).get("next_token")
        if not next_token or not batch:
            break
    return posts, {"users": users, "media": media}


def normalize(post: dict, inc: dict, source: str, now: datetime) -> dict:
    author = inc["users"].get(post.get("author_id"), {})
    handle = author.get("username", "?")
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    media = [inc["media"].get(k, {}) for k in post.get("attachments", {}).get("media_keys", [])]
    urls = [u.get("expanded_url") or u.get("url") for u in post.get("entities", {}).get("urls", [])]
    # Full addresses only: a truncated one (0x8E8b…) cannot be looked up.
    addresses = sorted({a.lower() for a in ADDRESS_RE.findall(" ".join([post.get("text", "")] + urls))})
    return {
        "id": post["id"],
        "source": source,
        "url": f"https://x.com/{handle}/status/{post['id']}",
        "author": handle,
        "author_followers": author.get("public_metrics", {}).get("followers_count"),
        "created_at": post["created_at"],
        "age_min": round((now - created).total_seconds() / 60),
        "text": post.get("text", ""),
        "lang": post.get("lang"),
        "metrics": post.get("public_metrics", {}),
        "cashtags": [c["tag"].upper() for c in post.get("entities", {}).get("cashtags", [])],
        "urls": urls,
        "addresses": addresses,
        "media": [
            {"type": m.get("type"), "url": m.get("url") or m.get("preview_image_url"), "alt": m.get("alt_text")}
            for m in media if m
        ],
        "reply_intent": f"https://x.com/intent/tweet?in_reply_to={post['id']}",
    }


def save_xbrief(env: dict) -> None:
    key = env.get("X_BRIEF_KEY")
    if not key:
        print("x-brief: skipped (no X_BRIEF_KEY in .env)")
        return
    # Same headers as main/discord/x_brief_ping.py: prod also runs the API_READ_KEY guard
    # in front of every /api route, which wants X-API-Key.
    headers = dict(dexiscan_headers(env), **{"X-Admin-Key": key})
    for name, url in XBRIEF_URLS.items():
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"x-brief {name}: HTTP {e.code} {e.read().decode('utf-8', errors='replace')[:200]}")
            continue
        (INBOX / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"x-brief {name}: saved, snapshot {payload.get('snapshot')}")


def dexiscan_headers(env: dict) -> dict:
    headers = {"User-Agent": "dexi_x_radar/0.1"}
    if env.get("API_READ_KEY"):
        headers["X-API-Key"] = env["API_READ_KEY"]
    return headers


def save_lenses(env: dict, posts: list[dict]) -> None:
    """Whale Radar (wallet lens) for every full address found in the posts."""
    addresses = list(dict.fromkeys(a for p in posts for a in p["addresses"]))[:LENS_MAX_PER_RUN]
    if not addresses:
        return
    if not env.get("API_READ_KEY"):
        print(f"whale radar: {len(addresses)} address(es) found, skipped (no API_READ_KEY in .env)")
        return
    for address in addresses:
        req = urllib.request.Request(LENS_URL.format(address=address), headers=dexiscan_headers(env))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"whale radar {address}: HTTP {e.code} {e.read().decode('utf-8', errors='replace')[:200]}")
            continue
        (INBOX / f"lens_{address}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"whale radar {address}: saved")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-hours", type=float, default=3.0,
                    help="first run only: how far back to read")
    ap.add_argument("--max-posts", type=int, default=40,
                    help="cap on posts read per source per run (cost guard)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    handles = load_watchlist()
    state = load_state()
    queries = chunk_queries(handles)
    worst = 2 * args.max_posts * len(queries) * PRICE_POST_READ + (len(handles) + 1) * PRICE_USER_READ
    print(f"watchlist: {len(handles)} accounts, {len(queries)} search quer{'y' if len(queries) == 1 else 'ies'}")
    print(f"worst-case cost this run: ${worst:.2f}")
    if args.dry_run:
        for q in queries:
            print(f"  query: {q}")
        return
    bearer = env.get("X_BEARER_TOKEN")
    if not bearer:
        sys.exit(f"Missing X_BEARER_TOKEN in {ROOT / '.env'} (see .env.example)")

    INBOX.mkdir(exist_ok=True)
    api = XApi(bearer)
    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(hours=args.lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    common = {"tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS,
              "media.fields": MEDIA_FIELDS, "user.fields": USER_FIELDS}

    resolve_users(api, state, handles + [OWN_HANDLE])
    found = []

    for i, query in enumerate(queries):
        key = f"search_{i}:{query}"
        params = dict(common, query=query)
        if state["since"].get(key):
            params["since_id"] = state["since"][key]
        else:
            params["start_time"] = start_time
        posts, inc = fetch_paged(api, "/tweets/search/recent", params, args.max_posts)
        if posts:
            state["since"][key] = max(posts, key=lambda p: int(p["id"]))["id"]
        found += [normalize(p, inc, "watch", now) for p in posts]

    own = state["users"].get(OWN_HANDLE.lower())
    if own:
        params = dict(common)
        if state["since"].get("mentions"):
            params["since_id"] = state["since"]["mentions"]
        else:
            params["start_time"] = start_time
        posts, inc = fetch_paged(api, f"/users/{own['id']}/mentions", params, args.max_posts)
        if posts:
            state["since"]["mentions"] = max(posts, key=lambda p: int(p["id"]))["id"]
        found += [normalize(p, inc, "mention", now) for p in posts]

    found.sort(key=lambda p: p["created_at"], reverse=True)
    (INBOX / "latest.json").write_text(json.dumps(found, indent=2, ensure_ascii=False), encoding="utf-8")
    with (INBOX / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
        for p in found:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    save_state(state)
    save_xbrief(env)
    save_lenses(env, found)

    cost = api.posts_read * PRICE_POST_READ + api.users_read * PRICE_USER_READ
    print(f"\n{len(found)} new posts · read {api.posts_read} posts + {api.users_read} users ≈ ${cost:.3f}\n")
    for p in found:
        text = " ".join(p["text"].split())
        media = f" [{len(p['media'])} media]" if p["media"] else ""
        print(f"[{p['source']}] @{p['author']} · {p['age_min']} min · {p['metrics'].get('like_count', 0)} likes{media}")
        print(f"   {text[:220]}")
        print(f"   {p['url']}")


if __name__ == "__main__":
    main()
