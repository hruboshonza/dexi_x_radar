"""DexiScan data for drafts, over HTTPS with read-only keys. Never a database connection."""
import time

import httpx

XBRIEF_CARDS = ("bias", "bias24", "accumulation", "adds", "swing", "crowd", "leverage", "liquidation",
                "signals24", "outcomes", "price_whales", "book")


class DexiScanError(RuntimeError):
    pass


class DexiScan:
    def __init__(self, base_url: str, x_brief_key: str, api_read_key: str, http: httpx.AsyncClient | None = None):
        self._base = base_url
        self._http = http or httpx.AsyncClient(timeout=30)
        self._headers = {"User-Agent": "dexi_x_radar/1.0"}
        if api_read_key:
            self._headers["X-API-Key"] = api_read_key
        self._brief_key = x_brief_key
        self._cache: dict[str, tuple[float, dict]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._brief_key and "X-API-Key" in self._headers)

    async def _get(self, path: str, extra: dict | None = None, ttl: int = 0) -> dict:
        hit = self._cache.get(path)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        resp = await self._http.get(f"{self._base}{path}", headers={**self._headers, **(extra or {})})
        if resp.status_code != 200:
            raise DexiScanError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if ttl:
            self._cache[path] = (time.monotonic(), data)
        return data

    async def xbrief(self, market: str = "crypto", cards: list[str] | None = None) -> dict:
        """The x-brief payload, trimmed to the cards that matter for a reply."""
        path = "/api/x-brief/xyz" if market == "xyz" else "/api/x-brief"
        data = await self._get(path, {"X-Admin-Key": self._brief_key}, ttl=300)
        wanted = cards or list(XBRIEF_CARDS)
        trimmed = {}
        for name in wanted:
            card = (data.get("cards") or {}).get(name)
            if isinstance(card, dict) and isinstance(card.get("points"), list) and len(card["points"]) > 2:
                card = {**card, "points": [card["points"][0], card["points"][-1]], "points_note": "first and last only"}
            if isinstance(card, dict) and isinstance(card.get("list"), list):
                card = {**card, "list": card["list"][:8]}
            trimmed[name] = card
        return {
            "market": market,
            "snapshot": data.get("snapshot"),
            "compared_to": data.get("compared_to"),
            "main_tweet": (data.get("tweets") or {}).get("main"),
            "cards": trimmed,
            "rule": "Never state the total number of tracked wallets in a reply.",
        }

    async def whale_radar(self, address: str) -> dict:
        """Whale Radar (wallet lens) for one wallet, without the long curves."""
        data = await self._get(f"/api/wallet/{address}/lens", ttl=60)
        history = data.get("history") or {}
        return {
            "address": data.get("address"),
            "wallet": data.get("wallet"),
            "pnl": data.get("pnl"),
            "accountValue": data.get("accountValue"),
            "stats": data.get("stats"),
            "window": data.get("window"),
            "open": data.get("open"),
            "closed_last_10": (data.get("closed") or [])[:10],
            "daily_pnl_last_7": (history.get("daily") or [])[-7:],
        }
