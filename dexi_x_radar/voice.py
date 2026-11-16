"""The owner's voice skill (rudejohn-x-voice), copied into the repo.

The skill lives in the owner's Claude account and syncs to ~/.claude/skills/synced/<id>/. The
service on Railway can't read that folder, so the repo keeps a copy in prompts/voice/SKILL.md.
`python -m dexi_x_radar sync-voice` refreshes the copy; a test on the owner's PC fails while the
copy differs from the source, so an outdated copy is noticed on the next test run.
"""
import os
from pathlib import Path

SKILL_NAME = "rudejohn-x-voice"


def source_path() -> Path | None:
    """The synced skill on this machine, or None (on Railway, in CI)."""
    explicit = os.environ.get("VOICE_SKILL_SOURCE", "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    synced = Path.home() / ".claude" / "skills" / "synced"
    if not synced.is_dir():
        return None
    found = sorted(synced.glob(f"*/{SKILL_NAME}/SKILL.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def _text(path: Path) -> bytes:
    # git on Windows may check the copy out with CRLF line endings; that is no difference.
    return path.read_bytes().replace(b"\r\n", b"\n")


def is_current(copy: Path, source: Path) -> bool:
    return copy.is_file() and _text(copy) == _text(source)


def sync(copy: Path) -> str:
    source = source_path()
    if not source:
        return f"{SKILL_NAME} not found under ~/.claude/skills/synced (or VOICE_SKILL_SOURCE); nothing copied"
    if is_current(copy, source):
        return f"{copy} is up to date"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(source.read_bytes())
    return f"copied {source} -> {copy}; commit it and deploy"
