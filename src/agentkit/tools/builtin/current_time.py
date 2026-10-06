from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from agentkit.tools.base import ToolFailure, tool


class TimeResult(BaseModel):
    iso: str
    timezone: str
    weekday: str


@tool(side_effects="none", tags=["time"])
def current_time(timezone: str = "UTC") -> TimeResult:
    """Return the current date and time in a time zone.

    Use this whenever the answer depends on today's date or the time now.
    Do not guess the date from memory.

    Args:
        timezone: IANA time zone name such as "UTC", "Europe/Berlin" or "Asia/Kolkata".
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolFailure(
            "invalid_input",
            f"'{timezone}' is not a known time zone. Use an IANA name such as 'Europe/Berlin'.",
        ) from exc
    now = datetime.now(zone)
    return TimeResult(
        iso=now.isoformat(timespec="seconds"), timezone=timezone, weekday=now.strftime("%A")
    )
