"""Read-only X API v2 client (pay-per-use). It has no method that writes."""
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
# docs.x.com/x-api/getting-started/pricing, checked 2026-10-14
PRICE_POST_READ = 0.005
PRICE_USER_READ = 0.010

TWEET_FIELDS = "created_at,public_metrics,author_id,conversation_id,entities,attachments,lang,referenced_tweets"
EXPANSIONS = "author_id,attachments.media_keys"
MEDIA_FIELDS = "type,url,preview_image_url,alt_text"
USER_FIELDS = "username,name,public_metrics"
MAX_QUERY_LEN = 512
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class XApiError(RuntimeError):
    pass


class XClient:
    def __init__(self, bearer: str, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=30)
        self._headers = {"Authorization": f"Bearer {bearer}", "User-Agent": "dexi_x_radar/1.0"}
        self.posts_read = 0
        self.users_read = 0

    def cost(self) -> float:
        return self.posts_read * PRICE_POST_READ + self.users_read * PRICE_USER_READ

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self._http.get(f"{API}{path}", params=params, headers=self._headers)
        if resp.status_code != 200:
            raise XApiError(f"X API {resp.status_code} on {path}: {resp.text[:300]}")
        return resp.json()

    async def users_by_handles(self, handles: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(handles), 100):
            data = await self._get("/users/by", {"usernames": ",".join(handles[i:i + 100]), "user.fields": USER_FIELDS})
            for u in data.get("data", []):
                self.users_read += 1
                out.append({"id": u["id"], "username": u["username"],
                            "followers": u.get("public_metrics", {}).get("followers_count")})
            for err in data.get("errors", []):
                log.warning("user lookup: %s", err.get("detail") or err)
        return out

    async def _paged(self, path: str, params: dict, max_posts: int, min_results: int) -> tuple[list, dict]:
        posts, users, media = [], {}, {}
        token = None
        token_param = "pagination_token" if "/users/" in path else "next_token"
        while len(posts) < max_posts:
            page = dict(params, max_results=max(min_results, min(100, max_posts - len(posts))))
            if token:
                page[token_param] = token
            data = await self._get(path, page)
            batch = data.get("data", [])
            self.posts_read += len(batch)
            posts.extend(batch)
            inc = data.get("includes", {})
            users.update({u["id"]: u for u in inc.get("users", [])})
            media.update({m["media_key"]: m for m in inc.get("media", [])})
            token = data.get("meta", {}).get("next_token")
            if not token or not batch:
                break
        return posts, {"users": users, "media": media}

    def _window(self, since_id: str | None, start_time: str) -> dict:
        return {"since_id": since_id} if since_id else {"start_time": start_time}

    async def search_recent(self, query: str, since_id: str | None, start_time: str, max_posts: int):
        params = {"query": query, "tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS,
                  "media.fields": MEDIA_FIELDS, "user.fields": USER_FIELDS, **self._window(since_id, start_time)}
        return await self._paged("/tweets/search/recent", params, max_posts, min_results=10)

    async def mentions(self, user_id: str, since_id: str | None, start_time: str, max_posts: int):
        params = {"tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS, "media.fields": MEDIA_FIELDS,
                  "user.fields": USER_FIELDS, **self._window(since_id, start_time)}
        return await self._paged(f"/users/{user_id}/mentions", params, max_posts, min_results=5)

    async def own_posts(self, user_id: str, since_id: str | None, start_time: str, max_posts: int):
        """The owner's own posts and replies (retweets excluded)."""
        params = {"tweet.fields": TWEET_FIELDS, "exclude": "retweets", **self._window(since_id, start_time)}
        return await self._paged(f"/users/{user_id}/tweets", params, max_posts, min_results=5)

    async def tweets_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        data = await self._get("/tweets", {"ids": ",".join(ids[:100]), "tweet.fields": "public_metrics,author_id"})
        self.posts_read += len(data.get("data", []))
        return data.get("data", [])

    async def replies_to(self, tweet_id: str, max_posts: int = 20) -> list[dict]:
        posts, _ = await self._paged(
            "/tweets/search/recent",
            {"query": f"in_reply_to_tweet_id:{tweet_id}", "tweet.fields": "author_id,created_at"},
            max_posts, min_results=10,
        )
        return posts


def watch_queries(handles: list[str]) -> list[str]:
    """`from:` handles split into search queries under the 512-char limit."""
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


def normalize(post: dict, inc: dict, source: str) -> dict:
    author = inc["users"].get(post.get("author_id"), {})
    handle = author.get("username", "unknown")
    media = [inc["media"].get(k, {}) for k in post.get("attachments", {}).get("media_keys", [])]
    urls = [u.get("expanded_url") or u.get("url") for u in post.get("entities", {}).get("urls", [])]
    # Full addresses only: a truncated one (0x8E8b…) cannot be looked up.
    addresses = sorted({a.lower() for a in ADDRESS_RE.findall(" ".join([post.get("text", "")] + urls))})
    return {
        "id": post["id"],
        "source": source,
        "author": handle,
        "author_id": post.get("author_id"),
        "author_followers": author.get("public_metrics", {}).get("followers_count"),
        "created_at": post["created_at"].replace(".000Z", "Z"),
        "text": post.get("text", ""),
        "url": f"https://x.com/{handle}/status/{post['id']}",
        "lang": post.get("lang"),
        "media": [{"type": m.get("type"), "url": m.get("url") or m.get("preview_image_url"), "alt": m.get("alt_text")}
                  for m in media if m],
        "urls": urls,
        "addresses": addresses,
        "metrics": post.get("public_metrics", {}),
    }


def replied_to(post: dict) -> str | None:
    for ref in post.get("referenced_tweets", []) or []:
        if ref.get("type") == "replied_to":
            return ref.get("id")
    return None


def iso_hours_ago(hours: float) -> str:
    ts = datetime.now(timezone.utc).timestamp() - hours * 3600
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
