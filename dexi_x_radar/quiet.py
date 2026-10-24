"""Quiet hours: no X polling and no drafting inside QUIET_HOURS, local time in TIMEZONE.

"22-6" means from 22:00 to 06:00 the next morning. The zone follows daylight saving time.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class QuietHours:
    def __init__(self, start_hour: int, end_hour: int, tz: str):
        self.start = start_hour
        self.end = end_hour
        self.tz = ZoneInfo(tz)

    @classmethod
    def parse(cls, value: str, tz: str) -> "QuietHours | None":
        """'22-6' -> 22:00 to 06:00. 'off' turns quiet hours off."""
        value = (value or "").strip().lower()
        if value in ("", "off", "none"):
            return None
        start, _, end = value.partition("-")
        return cls(int(start), int(end), tz)

    def _local(self, now: datetime | None) -> datetime:
        return (now or datetime.now(timezone.utc)).astimezone(self.tz)

    def active(self, now: datetime | None = None) -> bool:
        hour = self._local(now).hour
        if self.start > self.end:
            return hour >= self.start or hour < self.end
        return self.start <= hour < self.end

    def last_end(self, now: datetime | None = None) -> datetime:
        """The latest end of quiet hours at or before `now`, in UTC."""
        local = self._local(now)
        end = local.replace(hour=self.end, minute=0, second=0, microsecond=0)
        if end > local:
            end -= timedelta(days=1)
        return end.astimezone(timezone.utc)

    def seconds_left(self, now: datetime | None = None) -> float:
        """Seconds until quiet hours end (0 outside them)."""
        if not self.active(now):
            return 0.0
        local = self._local(now)
        end = local.replace(hour=self.end, minute=0, second=0, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
        return (end - local).total_seconds()
