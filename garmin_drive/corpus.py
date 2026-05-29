from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from .deep_archive import compact_run_for_export, recent_mile_split_rows
from .render import (
    format_duration,
    format_km,
    format_miles,
    format_number,
    format_pace,
    is_run,
    local_date,
    meters_to_feet,
    meters_to_km,
    meters_to_miles,
    number_or_none,
    pace_seconds_per_mile,
    write_if_changed,
)


@dataclass(frozen=True)
class GeneratedFile:
    path: Path
    remote_name: str
    mime_type: str
    as_google_doc: bool
    changed: bool
    remote_folder_parts: tuple[str, ...] = ()


CSV_FIELDS = [
    "local_date",
    "name",
    "distance_miles",
    "moving_time",
    "pace_per_mile",
    "elevation_gain_feet",
    "average_heartrate",
    "max_heartrate",
    "calories",
    "enriched",
    "mile_split_count",
    "route_available",
    "raw_data_path",
    "route_geojson_path",
    "sport_type",
    "source_activity_id",
    "strava_activity_url",
]

MILE_SPLIT_CSV_FIELDS = [
    "local_date",
    "name",
    "source_activity_id",
    "split_index",
    "split_type",
    "source",
    "distance_miles",
    "moving_time",
    "pace_per_mile",
    "average_heartrate",
    "max_heartrate",
    "elevation_gain_feet",
    "elevation_loss_feet",
    "net_elevation_change_feet",
    "average_cadence",
    "average_grade",
    "route_available",
    "strava_activity_url",
]


def normalize_runs(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_activity(activity) for activity in activities if is_run(activity)]


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(activity["id"])
    distance_miles = meters_to_miles(activity.get("distance"))
    distance_km = meters_to_km(activity.get("distance"))
    moving_seconds = int_or_none(activity.get("moving_time"))
    elapsed_seconds = int_or_none(activity.get("elapsed_time"))
    pace = pace_seconds_per_mile(activity.get("moving_time"), activity.get("distance"))
    elevation_feet = meters_to_feet(activity.get("total_elevation_gain"))
    elevation_meters = number_or_none(activity.get("total_elevation_gain"))

    return {
        "source": "strava",
        "source_activity_id": activity_id,
        "strava_activity_url": f"https://www.strava.com/activities/{activity_id}",
        "name": str(activity.get("name") or "Untitled Run"),
        "sport_type": activity.get("sport_type") or activity.get("type") or "Run",
        "start_date": activity.get("start_date"),
        "start_date_local": activity.get("start_date_local"),
        "local_date": local_date(activity),
        "timezone": activity.get("timezone"),
        "distance_miles": round_or_none(distance_miles, 4),
        "distance_kilometers": round_or_none(distance_km, 4),
        "moving_time_seconds": moving_seconds,
        "moving_time": format_duration(moving_seconds),
        "elapsed_time_seconds": elapsed_seconds,
        "elapsed_time": format_duration(elapsed_seconds),
        "pace_seconds_per_mile": round_or_none(pace, 2),
        "pace_per_mile": format_pace(pace),
        "elevation_gain_feet": round_or_none(elevation_feet, 1),
        "elevation_gain_meters": round_or_none(elevation_meters, 1),
        "average_heartrate": round_or_none(number_or_none(activity.get("average_heartrate")), 1),
        "max_heartrate": round_or_none(number_or_none(activity.get("max_heartrate")), 1),
        "calories": round_or_none(number_or_none(activity.get("calories")), 1),
        "average_cadence": round_or_none(number_or_none(activity.get("average_cadence")), 1),
        "average_speed_mps": round_or_none(number_or_none(activity.get("average_speed")), 4),
        "max_speed_mps": round_or_none(number_or_none(activity.get("max_speed")), 4),
        "suffer_score": activity.get("suffer_score"),
        "perceived_exertion": activity.get("perceived_exertion"),
        "visibility": activity.get("visibility"),
        "private": activity.get("private"),
        "commute": activity.get("commute"),
        "manual": activity.get("manual"),
        "trainer": activity.get("trainer"),
    }


def merge_run_history(existing: Any, fetched_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(existing, dict):
        existing_runs = existing.get("runs", [])
    elif isinstance(existing, list):
        existing_runs = existing
    else:
        existing_runs = []

    merged = {
        str(run["source_activity_id"]): run
        for run in existing_runs
        if isinstance(run, dict) and run.get("source_activity_id")
    }
    for run in fetched_runs:
        merged[str(run["source_activity_id"])] = run

    return sorted(
        merged.values(),
        key=lambda run: (str(run.get("start_date_local") or run.get("local_date") or ""), str(run.get("source_activity_id"))),
        reverse=True,
    )


def run_history_payload(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(runs),
        "runs": runs,
    }


def render_corpus(
    runs: list[dict[str, Any]],
    output_dir: Path,
    *,
    markdown_as_google_docs: bool,
    recent_mile_days: int = 14,
) -> list[GeneratedFile]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[GeneratedFile] = []
    compact_runs = [compact_run_for_export(run) for run in runs]
    recent_split_rows = recent_mile_split_rows(runs, recent_days=recent_mile_days)

    generated.append(
        write_generated(
            output_dir / "Run History Index.md",
            render_index(runs),
            remote_name="Run History Index",
            mime_type="text/markdown",
            as_google_doc=markdown_as_google_docs,
        )
    )
    generated.append(
        write_generated(
            output_dir / "Run History Data.json",
            json.dumps(run_history_payload(compact_runs), indent=2, sort_keys=True) + "\n",
            remote_name="Run History Data.json",
            mime_type="application/json",
            as_google_doc=False,
        )
    )
    generated.append(
        write_generated(
            output_dir / "Run History Data.csv",
            render_csv(compact_runs),
            remote_name="Run History Data.csv",
            mime_type="text/csv",
            as_google_doc=False,
        )
    )
    generated.append(
        write_generated(
            output_dir / "Recent Mile Splits.csv",
            render_mile_split_csv(recent_split_rows),
            remote_name="Recent Mile Splits.csv",
            mime_type="text/csv",
            as_google_doc=False,
        )
    )
    generated.append(
        write_generated(
            output_dir / "Recent Mile Splits.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "recent_days": recent_mile_days,
                    "split_count": len(recent_split_rows),
                    "mile_splits": recent_split_rows,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            remote_name="Recent Mile Splits.json",
            mime_type="application/json",
            as_google_doc=False,
        )
    )
    for year, year_runs in group_by_year(runs).items():
        generated.append(
            write_generated(
                output_dir / f"Runs {year}.md",
                render_year(year, year_runs),
                remote_name=f"Runs {year}",
                mime_type="text/markdown",
                as_google_doc=markdown_as_google_docs,
            )
        )

    return generated


def render_index(runs: list[dict[str, Any]]) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_distance = sum_float(run.get("distance_miles") for run in runs)
    total_time = sum_int(run.get("moving_time_seconds") for run in runs)
    recent = runs[:20]
    weeks = recent_weeks(runs, week_count=12)

    lines = [
        "# Run History Index",
        "",
        f"Generated: {generated}",
        "",
        "This folder is generated for ChatGPT analysis of running history. Use the yearly run documents for readable detail and `Run History Data.json` or `Run History Data.csv` for structured calculations.",
        "",
        "## All-Time Totals",
        "",
        f"- Runs indexed: {len(runs)}",
        f"- Total distance: {total_distance:.1f} mi",
        f"- Total moving time: {format_duration(total_time)}",
        f"- Latest run: {latest_run_label(runs)}",
        "",
        "## Last 12 Weeks",
        "",
        "| Week | Runs | Distance | Moving Time |",
        "| --- | ---: | ---: | ---: |",
    ]
    for week in weeks:
        lines.append(
            f"| {week['label']} | {week['runs']} | {week['distance_miles']:.1f} mi | {format_duration(week['moving_time_seconds'])} |"
        )

    lines.extend(
        [
            "",
            "## Most Recent Runs",
            "",
            "| Date | Activity | Distance | Moving Time | Pace | Avg HR | Elev Gain |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in recent:
        lines.append(run_table_row(run))

    lines.append("")
    return "\n".join(lines)


def render_year(year: int, runs: list[dict[str, Any]]) -> str:
    total_distance = sum_float(run.get("distance_miles") for run in runs)
    total_time = sum_int(run.get("moving_time_seconds") for run in runs)

    lines = [
        f"# Runs {year}",
        "",
        "## Year Summary",
        "",
        f"- Runs: {len(runs)}",
        f"- Distance: {total_distance:.1f} mi",
        f"- Moving time: {format_duration(total_time)}",
        "",
        "## Monthly Summary",
        "",
        "| Month | Runs | Distance | Moving Time |",
        "| --- | ---: | ---: | ---: |",
    ]

    for month, month_runs in group_by_month(runs).items():
        lines.append(
            f"| {month} | {len(month_runs)} | {sum_float(run.get('distance_miles') for run in month_runs):.1f} mi | {format_duration(sum_int(run.get('moving_time_seconds') for run in month_runs))} |"
        )

    lines.extend(
        [
            "",
            "## Runs",
            "",
            "| Date | Activity | Distance | Moving Time | Pace | Avg HR | Elev Gain | Strava |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for run in runs:
        lines.append(run_table_row(run, include_link=True))

    lines.append("")
    return "\n".join(lines)


def render_csv(runs: list[dict[str, Any]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for run in runs:
        writer.writerow(run)
    return buffer.getvalue()


def render_mile_split_csv(rows: list[dict[str, Any]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=MILE_SPLIT_CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def write_generated(
    path: Path,
    content: str,
    *,
    remote_name: str,
    mime_type: str,
    as_google_doc: bool,
    remote_folder_parts: tuple[str, ...] = (),
) -> GeneratedFile:
    changed = write_if_changed(path, content)
    return GeneratedFile(
        path=path,
        remote_name=remote_name,
        mime_type=mime_type,
        as_google_doc=as_google_doc,
        changed=changed,
        remote_folder_parts=remote_folder_parts,
    )


def run_table_row(run: dict[str, Any], *, include_link: bool = False) -> str:
    cells = [
        str(run.get("local_date") or "unknown"),
        escape_table(str(run.get("name") or "Untitled Run")),
        format_miles(number_or_none(run.get("distance_miles"))),
        format_duration(int_or_none(run.get("moving_time_seconds"))),
        str(run.get("pace_per_mile") or "unknown"),
        format_number(run.get("average_heartrate"), "bpm", decimals=0),
        format_number(run.get("elevation_gain_feet"), "ft", decimals=0),
    ]
    if include_link:
        url = str(run.get("strava_activity_url") or "")
        cells.append(f"[Open]({url})" if url else "")
    return "| " + " | ".join(cells) + " |"


def group_by_year(runs: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        parsed = parse_local_date(run)
        if parsed:
            grouped[parsed.year].append(run)
    return dict(sorted(grouped.items(), reverse=True))


def group_by_month(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        parsed = parse_local_date(run)
        if parsed:
            grouped[parsed.strftime("%Y-%m")].append(run)
    return dict(sorted(grouped.items(), reverse=True))


def recent_weeks(runs: list[dict[str, Any]], *, week_count: int) -> list[dict[str, Any]]:
    today = date.today()
    current_monday = today.fromordinal(today.toordinal() - today.weekday())
    buckets = []
    for offset in range(week_count):
        start = current_monday.fromordinal(current_monday.toordinal() - 7 * offset)
        end = start.fromordinal(start.toordinal() + 6)
        week_runs = [run for run in runs if (parsed := parse_local_date(run)) and start <= parsed <= end]
        buckets.append(
            {
                "label": f"{start.isoformat()}",
                "runs": len(week_runs),
                "distance_miles": sum_float(run.get("distance_miles") for run in week_runs),
                "moving_time_seconds": sum_int(run.get("moving_time_seconds") for run in week_runs),
            }
        )
    return buckets


def parse_local_date(run: dict[str, Any]) -> date | None:
    value = str(run.get("local_date") or "")
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def latest_run_label(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "none"
    latest = runs[0]
    return f"{latest.get('local_date')} - {latest.get('name')} ({format_miles(number_or_none(latest.get('distance_miles')))})"


def sum_float(values: Any) -> float:
    total = 0.0
    for value in values:
        number = number_or_none(value)
        if number is not None:
            total += number
    return total


def sum_int(values: Any) -> int:
    total = 0
    for value in values:
        integer = int_or_none(value)
        if integer is not None:
            total += integer
    return total


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")
