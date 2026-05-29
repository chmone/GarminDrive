from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class RenderedActivity:
    activity_id: str
    date: str
    name: str
    summary_path: Path
    raw_path: Path
    changed: bool
    distance_miles: float | None
    moving_seconds: int | None
    pace_seconds_per_mile: float | None
    avg_heartrate: float | None
    elevation_feet: float | None


def is_run(activity: dict[str, Any]) -> bool:
    sport_type = activity.get("sport_type") or activity.get("type")
    return sport_type in included_activity_sport_types()


def render_activity(activity: dict[str, Any], output_dir: Path) -> RenderedActivity:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    activity_id = str(activity["id"])
    date = local_date(activity)
    name = str(activity.get("name") or "Untitled Run")
    slug = slugify(name)

    summary_path = output_dir / f"{date}_strava_{activity_id}_{slug}.md"
    raw_path = raw_dir / f"strava_{activity_id}.json"

    raw_json = json.dumps(activity, indent=2, sort_keys=True) + "\n"
    summary = activity_to_markdown(activity)

    changed = write_if_changed(summary_path, summary)
    raw_changed = write_if_changed(raw_path, raw_json)

    distance_miles = meters_to_miles(activity.get("distance"))
    moving_seconds = int(activity["moving_time"]) if activity.get("moving_time") is not None else None
    pace = pace_seconds_per_mile(activity.get("moving_time"), activity.get("distance"))
    elevation_feet = meters_to_feet(activity.get("total_elevation_gain"))

    return RenderedActivity(
        activity_id=activity_id,
        date=date,
        name=name,
        summary_path=summary_path,
        raw_path=raw_path,
        changed=changed or raw_changed,
        distance_miles=distance_miles,
        moving_seconds=moving_seconds,
        pace_seconds_per_mile=pace,
        avg_heartrate=number_or_none(activity.get("average_heartrate")),
        elevation_feet=elevation_feet,
    )


def load_rendered_from_raw(output_dir: Path) -> list[RenderedActivity]:
    raw_dir = output_dir / "raw"
    if not raw_dir.exists():
        return []

    rendered: list[RenderedActivity] = []
    for raw_path in sorted(raw_dir.glob("strava_*.json")):
        activity = json.loads(raw_path.read_text(encoding="utf-8"))
        if not is_run(activity):
            continue
        activity_id = str(activity["id"])
        date = local_date(activity)
        name = str(activity.get("name") or "Untitled Run")
        summary_path = output_dir / f"{date}_strava_{activity_id}_{slugify(name)}.md"
        rendered.append(
            RenderedActivity(
                activity_id=activity_id,
                date=date,
                name=name,
                summary_path=summary_path,
                raw_path=raw_path,
                changed=False,
                distance_miles=meters_to_miles(activity.get("distance")),
                moving_seconds=int(activity["moving_time"]) if activity.get("moving_time") is not None else None,
                pace_seconds_per_mile=pace_seconds_per_mile(activity.get("moving_time"), activity.get("distance")),
                avg_heartrate=number_or_none(activity.get("average_heartrate")),
                elevation_feet=meters_to_feet(activity.get("total_elevation_gain")),
            )
        )
    return sorted(rendered, key=lambda item: (item.date, item.activity_id), reverse=True)


def render_index(activities: list[RenderedActivity]) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_distance = sum(item.distance_miles or 0 for item in activities)
    total_runs = len(activities)

    lines = [
        "# Run History Index",
        "",
        f"Generated: {generated}",
        "",
        "## Totals",
        "",
        f"- Runs indexed: {total_runs}",
        f"- Total distance: {total_distance:.1f} mi",
        "",
        "## Runs",
        "",
        "| Date | Activity | Distance | Moving Time | Pace | Avg HR | Elev Gain | Local File |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for item in activities:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.date,
                    escape_table(item.name),
                    format_miles(item.distance_miles),
                    format_duration(item.moving_seconds),
                    format_pace(item.pace_seconds_per_mile),
                    format_number(item.avg_heartrate, "bpm", decimals=0),
                    format_number(item.elevation_feet, "ft", decimals=0),
                    item.summary_path.name,
                ]
            )
            + " |"
        )

    lines.append("")
    return "\n".join(lines)


def activity_to_markdown(activity: dict[str, Any]) -> str:
    activity_id = str(activity["id"])
    name = str(activity.get("name") or "Untitled Run")
    start_local = str(activity.get("start_date_local") or activity.get("start_date") or "")
    date = local_date(activity)
    sport_type = activity.get("sport_type") or activity.get("type") or "Run"

    distance_miles = meters_to_miles(activity.get("distance"))
    distance_km = meters_to_km(activity.get("distance"))
    moving_seconds = int(activity["moving_time"]) if activity.get("moving_time") is not None else None
    elapsed_seconds = int(activity["elapsed_time"]) if activity.get("elapsed_time") is not None else None
    pace = pace_seconds_per_mile(activity.get("moving_time"), activity.get("distance"))
    elev_feet = meters_to_feet(activity.get("total_elevation_gain"))
    elev_meters = number_or_none(activity.get("total_elevation_gain"))

    lines = [
        f"# {date} - {name}",
        "",
        "## Identity",
        "",
        "- Source: Strava",
        f"- Strava activity ID: {activity_id}",
        f"- Sport type: {sport_type}",
        f"- Start time local: {format_strava_datetime(start_local)}",
        f"- Visibility: {activity.get('visibility') or activity.get('private') or 'unknown'}",
        "",
        "## Summary",
        "",
        f"- Distance: {format_miles(distance_miles)} ({format_km(distance_km)})",
        f"- Moving time: {format_duration(moving_seconds)}",
        f"- Elapsed time: {format_duration(elapsed_seconds)}",
        f"- Average pace: {format_pace(pace)}",
        f"- Elevation gain: {format_number(elev_feet, 'ft', decimals=0)} ({format_number(elev_meters, 'm', decimals=0)})",
        f"- Average heart rate: {format_number(activity.get('average_heartrate'), 'bpm', decimals=0)}",
        f"- Max heart rate: {format_number(activity.get('max_heartrate'), 'bpm', decimals=0)}",
        f"- Calories: {format_number(activity.get('calories'), 'cal', decimals=0)}",
        "",
        "## Context For ChatGPT",
        "",
        "Use this file as one run in the athlete's training history. Prefer the structured values above when comparing weekly mileage, pace trends, heart-rate trends, long-run progression, and training consistency.",
    ]

    splits = activity.get("splits_standard") or activity.get("splits_metric")
    if isinstance(splits, list) and splits:
        lines.extend(["", "## Splits", "", "| Split | Distance | Moving Time | Pace | Elevation Diff |", "| ---: | ---: | ---: | ---: | ---: |"])
        for index, split in enumerate(splits, start=1):
            split_distance_miles = meters_to_miles(split.get("distance"))
            split_time = int(split["moving_time"]) if split.get("moving_time") is not None else None
            split_pace = pace_seconds_per_mile(split.get("moving_time"), split.get("distance"))
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(index),
                        format_miles(split_distance_miles),
                        format_duration(split_time),
                        format_pace(split_pace),
                        format_number(meters_to_feet(split.get("elevation_difference")), "ft", decimals=0),
                    ]
                )
                + " |"
            )

    useful_raw = {
        key: activity.get(key)
        for key in [
            "id",
            "name",
            "sport_type",
            "type",
            "start_date",
            "start_date_local",
            "timezone",
            "distance",
            "moving_time",
            "elapsed_time",
            "total_elevation_gain",
            "average_speed",
            "max_speed",
            "average_heartrate",
            "max_heartrate",
            "calories",
            "suffer_score",
            "perceived_exertion",
            "average_cadence",
        ]
        if activity.get(key) is not None
    }
    lines.extend(
        [
            "",
            "## Useful Raw Fields",
            "",
            "```json",
            json.dumps(useful_raw, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


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


def format_strava_datetime(value: str) -> str:
    if not value:
        return "unknown"
    return value.replace("T", " ").replace("Z", "")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "run"


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


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")
