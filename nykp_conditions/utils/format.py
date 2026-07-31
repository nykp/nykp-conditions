"""Text formatting shared by the posted messages."""

from datetime import datetime


def format_hour(when: datetime) -> str:
    """Render an hour as '8am Mon' -- lowercase meridiem, capitalized weekday."""
    return f"{when.strftime('%-I')}{when.strftime('%p').lower()} {when.strftime('%a')}"
