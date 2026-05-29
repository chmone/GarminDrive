from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DEFAULT_ACTIVITY_SPORT_TYPES = {
    "Run",
    "TrailRun",
    "VirtualRun",
    "Treadmill",
    "TrackRun",
    "Ride",
    "VirtualRide",
    "MountainBikeRide",
    "GravelRide",
    "EBikeRide",
    "EMountainBikeRide",
}


def included_activity_sport_types() -> set[str]:
    configured = os.getenv("STRAVA_ACTIVITY_SPORT_TYPES")
    if not configured:
        return DEFAULT_ACTIVITY_SPORT_TYPES
    return {item.strip() for item in configured.split(",") if item.strip()}


def is_run(activity: dict[str, Any]) -> bool:
    sport_type = activity.get("sport_type") or activity.get("type")
    return sport_type in included_activity_sport_types()


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def local_date(activity: dict[str, Any]) -> str:
    value = str(activity.get("start_date_local") or activity.get("start_date") or "")
    if not value:
        return "unknown-date"
    return value[:10]


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def meters_to_miles(value: Any) -> float | None:
    meters = number_or_none(value)
    if meters is None:
        return None
    return meters / 1609.344


def meters_to_km(value: Any) -> float | None:
    meters = number_or_none(value)
    if meters is None:
        return None
    return meters / 1000


def meters_to_feet(value: Any) -> float | None:
    meters = number_or_none(value)
    if meters is None:
        return None
    return meters * 3.28084


def pace_seconds_per_mile(seconds: Any, meters: Any) -> float | None:
    duration = number_or_none(seconds)
    miles = meters_to_miles(meters)
    if duration is None or miles is None or miles <= 0:
        return None
    return duration / miles


def format_miles(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} mi"


def format_km(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} km"


def format_number(value: Any, unit: str, *, decimals: int = 1) -> str:
    number = number_or_none(value)
    if number is None:
        return "unknown"
    return f"{number:.{decimals}f} {unit}"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_pace(seconds_per_mile: float | None) -> str:
    if seconds_per_mile is None:
        return "unknown"
    total = int(round(seconds_per_mile))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}/mi"
