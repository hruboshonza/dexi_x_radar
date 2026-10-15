"""Settings from the environment, with `.env` in the repo root as a fallback for local runs."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    x_bearer_token: str
    x_own_handle: str
    poll_interval: int
    max_posts_per_poll: int
    lookback_hours: float
    watchlist_file: Path
    telegram_bot_token: str
    telegram_chat_id: str
    mcp_token: str
    mcp_path: str
    port: int
    db_path: Path
    dexiscan_base_url: str
    x_brief_key: str
    api_read_key: str
    playbook_file: Path
    ignore_after_hours: float = 6.0
    outcome_after_hours: float = 24.0

    def missing(self) -> list[str]:
        required = {
            "X_BEARER_TOKEN": self.x_bearer_token,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "MCP_TOKEN": self.mcp_token,
        }
        return [name for name, value in required.items() if not value]

    def watchlist(self) -> list[str]:
        handles = []
        for line in self.watchlist_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("@")
            if line and not line.startswith("#"):
                handles.append(line)
        return handles


def load_settings(env: dict | None = None) -> Settings:
    """Process environment wins over `.env`, so Railway variables override a local file."""
    merged = {**_read_dotenv(ROOT / ".env"), **os.environ, **(env or {})}

    def get(name: str, default: str = "") -> str:
        return (merged.get(name) or default).strip()

    def path(name: str, default: str) -> Path:
        p = Path(get(name, default))
        return p if p.is_absolute() else ROOT / p

    return Settings(
        x_bearer_token=get("X_BEARER_TOKEN"),
        x_own_handle=get("X_OWN_HANDLE", "DexiScan").lstrip("@"),
        poll_interval=int(get("POLL_INTERVAL_SECONDS", "300")),
        max_posts_per_poll=int(get("MAX_POSTS_PER_POLL", "40")),
        lookback_hours=float(get("LOOKBACK_HOURS", "2")),
        watchlist_file=path("WATCHLIST_FILE", "watchlist.txt"),
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        mcp_token=get("MCP_TOKEN"),
        mcp_path="/" + get("MCP_PATH", "/mcp").strip("/"),
        port=int(get("PORT", "8080")),
        db_path=path("DB_PATH", "data/radar.db"),
        dexiscan_base_url=get("DEXISCAN_BASE_URL", "https://dexiscan.com").rstrip("/"),
        x_brief_key=get("X_BRIEF_KEY"),
        api_read_key=get("API_READ_KEY"),
        playbook_file=path("PLAYBOOK_FILE", "PLAYBOOK.md"),
    )
