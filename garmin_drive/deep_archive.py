from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .render import (
    format_duration,
    format_pace,
    local_date,
    meters_to_feet,
    meters_to_miles,
    number_or_none,
    pace_seconds_per_mile,
    write_if_changed,
)


MILE_METERS = 1609.344
RAW_DATA_DIR = "Raw Data"
RAW_RUNS_DIR = "Runs"
RAW_ROUTES_DIR = "Routes"
ALL_ROUTES_NAME = "All Run Routes.geojson"
ALL_MAP_NAME = "All Runs Map.html"
RECENT_MAP_NAME = "Recent Run Map.html"
STREAM_KEYS = [
    "time",
    "distance",
    "latlng",
    "altitude",
    "velocity_smooth",
    "heartrate",
    "cadence",
    "moving",
    "grade_smooth",
    "temp",
]


def build_raw_archive(activity: dict[str, Any], streams: dict[str, Any], *, fetched_at: str | None = None) -> dict[str, Any]:
    if not isinstance(streams, dict):
        streams = {}
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    mile_splits = derive_mile_splits(activity, streams)
    route = build_route_feature(activity, streams)
    stream_types = sorted(key for key, stream in streams.items() if stream_data(stream) is not None)
    sample_count = max((len(stream_data(stream) or []) for stream in streams.values()), default=0)

    return {
        "schema_version": 1,
        "source": "strava",
        "source_activity_id": str(activity["id"]),
        "fetched_at": fetched_at,
        "activity": activity,
        "streams": streams,
        "stream_types": stream_types,
        "stream_sample_count": sample_count,
        "mile_splits": mile_splits,
        "route": route,
    }


def archive_enrichment_fields(archive: dict[str, Any]) -> dict[str, Any]:
    activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else {}
    activity_id = str(archive.get("source_activity_id") or activity.get("id") or "")
    year = year_for_activity(activity)
    route_available = archive.get("route") is not None
    return {
        "enriched": True,
        "enriched_at": archive.get("fetched_at"),
        "stream_types": archive.get("stream_types") or [],
        "stream_sample_count": archive.get("stream_sample_count"),
        "mile_splits": archive.get("mile_splits") or [],
        "mile_split_count": len(archive.get("mile_splits") or []),
        "route_available": route_available,
        "raw_data_path": raw_run_relative_path(year, activity, activity_id),
        "route_geojson_path": route_relative_path(year, activity, activity_id) if route_available else None,
    }


def derive_mile_splits(activity: dict[str, Any], streams: dict[str, Any]) -> list[dict[str, Any]]:
    distances = stream_values(streams, "distance")
    if distances:
        return derive_stream_mile_splits(activity, streams, distances)
    return derive_strava_split_fallback(activity)


def derive_stream_mile_splits(
    activity: dict[str, Any],
    streams: dict[str, Any],
    distances: list[Any],
) -> list[dict[str, Any]]:
    numeric_distances = [number_or_none(value) for value in distances]
    valid_distances = [value for value in numeric_distances if value is not None]
    if not valid_distances:
        return derive_strava_split_fallback(activity)

    total_meters = valid_distances[-1]
    if total_meters <= 0:
        return []

    times = stream_values(streams, "time") or []
    moving = stream_values(streams, "moving") or []
    altitude = stream_values(streams, "altitude") or []
    heartrate = stream_values(streams, "heartrate") or []
    cadence = stream_values(streams, "cadence") or []
    grade = stream_values(streams, "grade_smooth") or []
    speed = stream_values(streams, "velocity_smooth") or []
    latlng = stream_values(streams, "latlng") or []

    splits: list[dict[str, Any]] = []
    start_index = 0
    start_meters = 0.0
    split_index = 1

    while start_meters < total_meters:
        end_meters = min(split_index * MILE_METERS, total_meters)
        end_index = first_index_at_or_after(numeric_distances, end_meters, start_index)
        if end_index <= start_index and end_index < len(numeric_distances) - 1:
            end_index += 1
        indices = list(range(start_index, end_index + 1))
        distance_meters = max(0.0, end_meters - start_meters)
        distance_miles = meters_to_miles(distance_meters) or 0.0
        elapsed_seconds = elapsed_between(times, start_index, end_index)
        moving_seconds = moving_between(times, moving, start_index, end_index)
        pace = pace_seconds_per_mile(moving_seconds, distance_meters) if moving_seconds else None
        gain_meters, loss_meters, net_meters = elevation_change(altitude, indices)

        splits.append(
            {
                "split_index": split_index,
                "split_type": "mile" if distance_meters >= MILE_METERS - 1 else "partial_mile",
                "source": "stream",
                "start_index": start_index,
                "end_index": end_index,
                "start_distance_meters": round(start_meters, 2),
                "end_distance_meters": round(end_meters, 2),
                "distance_meters": round(distance_meters, 2),
                "distance_miles": round(distance_miles, 4),
                "elapsed_time_seconds": elapsed_seconds,
                "elapsed_time": format_duration(elapsed_seconds),
                "moving_time_seconds": moving_seconds,
                "moving_time": format_duration(moving_seconds),
                "pace_seconds_per_mile": round_or_none(pace, 2),
                "pace_per_mile": format_pace(pace),
                "average_heartrate": round_or_none(average_at_indices(heartrate, indices), 1),
                "max_heartrate": round_or_none(max_at_indices(heartrate, indices), 1),
                "elevation_gain_feet": round_or_none(meters_to_feet(gain_meters), 1),
                "elevation_loss_feet": round_or_none(meters_to_feet(loss_meters), 1),
                "net_elevation_change_feet": round_or_none(meters_to_feet(net_meters), 1),
                "average_cadence": round_or_none(average_at_indices(cadence, indices), 1),
                "average_grade": round_or_none(average_at_indices(grade, indices), 2),
                "average_speed_mps": round_or_none(average_at_indices(speed, indices), 3),
                "route_available": bool(latlng),
            }
        )

        if end_index >= len(numeric_distances) - 1:
            break
        start_index = end_index
        start_meters = end_meters
        split_index += 1

    return splits


def derive_strava_split_fallback(activity: dict[str, Any]) -> list[dict[str, Any]]:
    splits = activity.get("splits_standard") or activity.get("splits_metric") or []
    if not isinstance(splits, list):
        return []
    derived: list[dict[str, Any]] = []
    for index, split in enumerate(splits, start=1):
        if not isinstance(split, dict):
            continue
        distance_meters = number_or_none(split.get("distance"))
        moving_seconds = int_or_none(split.get("moving_time"))
        elapsed_seconds = int_or_none(split.get("elapsed_time"))
        pace = pace_seconds_per_mile(moving_seconds, distance_meters)
        derived.append(
            {
                "split_index": index,
                "split_type": "mile",
                "source": "strava_split",
                "distance_meters": round_or_none(distance_meters, 2),
                "distance_miles": round_or_none(meters_to_miles(distance_meters), 4),
                "elapsed_time_seconds": elapsed_seconds,
                "elapsed_time": format_duration(elapsed_seconds),
                "moving_time_seconds": moving_seconds,
                "moving_time": format_duration(moving_seconds),
                "pace_seconds_per_mile": round_or_none(pace, 2),
                "pace_per_mile": format_pace(pace),
                "elevation_gain_feet": round_or_none(meters_to_feet(split.get("elevation_difference")), 1),
                "route_available": False,
            }
        )
    return derived


def build_route_feature(activity: dict[str, Any], streams: dict[str, Any]) -> dict[str, Any] | None:
    latlng = stream_values(streams, "latlng") or []
    altitude = stream_values(streams, "altitude") or []
    coordinates: list[list[float]] = []

    if latlng:
        for index, point in enumerate(latlng):
            if not isinstance(point, list) or len(point) < 2:
                continue
            lat = number_or_none(point[0])
            lng = number_or_none(point[1])
            if lat is None or lng is None:
                continue
            alt = number_or_none(altitude[index]) if index < len(altitude) else None
            coordinates.append([lng, lat, alt] if alt is not None else [lng, lat])
    else:
        polyline = route_polyline(activity)
        if polyline:
            coordinates = [[lng, lat] for lat, lng in decode_polyline(polyline)]

    if len(coordinates) < 2:
        return None

    activity_id = str(activity.get("id") or "")
    return {
        "type": "Feature",
        "id": activity_id,
        "properties": {
            "source": "strava",
            "source_activity_id": activity_id,
            "name": str(activity.get("name") or "Untitled Run"),
            "sport_type": activity.get("sport_type") or activity.get("type") or "Run",
            "local_date": local_date(activity),
            "start_date": activity.get("start_date"),
            "start_date_local": activity.get("start_date_local"),
            "distance_miles": round_or_none(meters_to_miles(activity.get("distance")), 4),
        },
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def route_polyline(activity: dict[str, Any]) -> str | None:
    map_data = activity.get("map")
    if not isinstance(map_data, dict):
        return None
    polyline = map_data.get("polyline") or map_data.get("summary_polyline")
    return str(polyline) if polyline else None


def decode_polyline(polyline: str) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(polyline):
        lat_change, index = decode_polyline_value(polyline, index)
        lng_change, index = decode_polyline_value(polyline, index)
        lat += lat_change
        lng += lng_change
        coordinates.append((lat / 1e5, lng / 1e5))
    return coordinates


def decode_polyline_value(polyline: str, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        value = ord(polyline[index]) - 63
        index += 1
        result |= (value & 0x1F) << shift
        shift += 5
        if value < 0x20:
            break
    delta = ~(result >> 1) if result & 1 else result >> 1
    return delta, index


def raw_file_stem(activity: dict[str, Any], activity_id: str | None = None) -> str:
    activity_id = activity_id or str(activity.get("id") or "")
    date = local_date(activity)
    sport_type = str(activity.get("sport_type") or activity.get("type") or "activity")
    name = str(activity.get("name") or sport_type)
    slug = slugify_filename(f"{sport_type}-{name}")
    return f"{date}_{slug}_{activity_id}"


def raw_run_relative_path(year: str, activity: dict[str, Any], activity_id: str | None = None) -> str:
    return f"{RAW_DATA_DIR}/{RAW_RUNS_DIR}/{year}/{raw_file_stem(activity, activity_id)}.json"


def route_relative_path(year: str, activity: dict[str, Any], activity_id: str | None = None) -> str:
    return f"{RAW_DATA_DIR}/{RAW_ROUTES_DIR}/{year}/{raw_file_stem(activity, activity_id)}.geojson"


def local_cache_path(data_dir: Path, activity: dict[str, Any] | str, year: str | None = None) -> Path:
    if isinstance(activity, dict):
        activity_id = str(activity["id"])
        year = year or year_for_activity(activity)
    else:
        activity_id = str(activity)
        year = year or "unknown"
    return data_dir / "raw_archive" / RAW_RUNS_DIR / year / f"{activity_id}.json"


def load_cached_archive(data_dir: Path, activity_id: str, year: str) -> dict[str, Any] | None:
    path = local_cache_path(data_dir, activity_id, year)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cached_archive(data_dir: Path, archive: dict[str, Any]) -> None:
    activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else {}
    path = local_cache_path(data_dir, activity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(archive, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_raw_archive_files(output_dir: Path, archive: dict[str, Any]) -> list[Path]:
    activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else {}
    activity_id = str(archive.get("source_activity_id") or activity.get("id") or "")
    year = year_for_activity(activity)
    written: list[Path] = []

    stem = raw_file_stem(activity, activity_id)
    raw_path = output_dir / RAW_DATA_DIR / RAW_RUNS_DIR / year / f"{stem}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(raw_path, json.dumps(archive, indent=2, sort_keys=True) + "\n")
    written.append(raw_path)

    route = archive.get("route")
    if route:
        route_path = output_dir / RAW_DATA_DIR / RAW_ROUTES_DIR / year / f"{stem}.geojson"
        route_path.parent.mkdir(parents=True, exist_ok=True)
        write_if_changed(route_path, json.dumps(feature_collection([route]), indent=2, sort_keys=True) + "\n")
        written.append(route_path)

    return written


def feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def merge_route_features(existing: dict[str, Any] | None, new_features: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(existing, dict):
        for feature in existing.get("features", []):
            if not isinstance(feature, dict):
                continue
            activity_id = feature_activity_id(feature)
            if activity_id:
                merged[activity_id] = feature
    for feature in new_features:
        activity_id = feature_activity_id(feature)
        if activity_id:
            merged[activity_id] = feature
    return feature_collection(
        sorted(
            merged.values(),
            key=lambda feature: str(feature.get("properties", {}).get("local_date") or ""),
            reverse=True,
        )
    )


def load_geojson_text(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def feature_activity_id(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    return str(properties.get("source_activity_id") or feature.get("id") or "")


def filter_routes_for_activity_ids(routes: dict[str, Any], activity_ids: set[str]) -> dict[str, Any]:
    features = [
        feature
        for feature in routes.get("features", [])
        if isinstance(feature, dict) and feature_activity_id(feature) in activity_ids
    ]
    return feature_collection(features)


def render_map_html(title: str, routes: dict[str, Any]) -> str:
    route_json = json.dumps(routes, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    body {{ font-family: Arial, sans-serif; }}
    #empty {{ padding: 24px; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const routes = {route_json};
    const map = L.map("map");
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);
    const layer = L.geoJSON(routes, {{
      style: {{ color: "#e64626", weight: 3, opacity: 0.72 }},
      onEachFeature: (feature, item) => {{
        const p = feature.properties || {{}};
        const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;"
        }}[char]));
        item.bindPopup(`<strong>${{escapeHtml(p.local_date)}}</strong><br>${{escapeHtml(p.name || "Run")}}<br>${{escapeHtml(p.distance_miles)}} mi`);
      }}
    }}).addTo(map);
    if (layer.getLayers().length) {{
      map.fitBounds(layer.getBounds(), {{ padding: [24, 24] }});
    }} else {{
      document.body.innerHTML = '<div id="empty">No route data available.</div>';
    }}
  </script>
</body>
</html>
"""


def compact_run_for_export(run: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in run.items() if key != "mile_splits"}
    if run.get("mile_splits"):
        compact["mile_split_count"] = len(run.get("mile_splits") or [])
    return compact


def recent_mile_split_rows(runs: list[dict[str, Any]], *, recent_days: int) -> list[dict[str, Any]]:
    cutoff = date.today().toordinal() - max(0, recent_days - 1)
    rows: list[dict[str, Any]] = []
    for run in runs:
        run_date = parse_date(str(run.get("local_date") or ""))
        if run_date is None or run_date.toordinal() < cutoff:
            continue
        for split in run.get("mile_splits") or []:
            if not isinstance(split, dict):
                continue
            row = {
                "local_date": run.get("local_date"),
                "name": run.get("name"),
                "source_activity_id": run.get("source_activity_id"),
                "strava_activity_url": run.get("strava_activity_url"),
            }
            row.update(split)
            rows.append(row)
    return rows


def year_for_activity(activity: dict[str, Any]) -> str:
    value = local_date(activity)
    if len(value) >= 4 and value[:4].isdigit():
        return value[:4]
    return "unknown"


def stream_values(streams: dict[str, Any], key: str) -> list[Any] | None:
    stream = streams.get(key)
    if not isinstance(stream, dict):
        return None
    return stream_data(stream)


def stream_data(stream: dict[str, Any]) -> list[Any] | None:
    data = stream.get("data")
    return data if isinstance(data, list) else None


def first_index_at_or_after(values: list[float | None], target: float, start_index: int) -> int:
    for index in range(start_index, len(values)):
        value = values[index]
        if value is not None and value >= target:
            return index
    return len(values) - 1


def elapsed_between(times: list[Any], start_index: int, end_index: int) -> int | None:
    start = number_at(times, start_index)
    end = number_at(times, end_index)
    if start is None or end is None:
        return None
    return int(max(0, end - start))


def moving_between(times: list[Any], moving: list[Any], start_index: int, end_index: int) -> int | None:
    if not times:
        return None
    total = 0.0
    for index in range(start_index + 1, end_index + 1):
        previous_time = number_at(times, index - 1)
        current_time = number_at(times, index)
        if previous_time is None or current_time is None:
            continue
        if not moving or index >= len(moving) or bool(moving[index]):
            total += max(0.0, current_time - previous_time)
    return int(round(total))


def elevation_change(values: list[Any], indices: list[int]) -> tuple[float, float, float]:
    points = [number_at(values, index) for index in indices]
    points = [point for point in points if point is not None]
    if len(points) < 2:
        return 0.0, 0.0, 0.0
    gain = 0.0
    loss = 0.0
    for previous, current in zip(points, points[1:]):
        delta = current - previous
        if delta > 0:
            gain += delta
        elif delta < 0:
            loss += abs(delta)
    return gain, loss, points[-1] - points[0]


def average_at_indices(values: list[Any], indices: list[int]) -> float | None:
    numbers = [number_at(values, index) for index in indices]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def max_at_indices(values: list[Any], indices: list[int]) -> float | None:
    numbers = [number_at(values, index) for index in indices]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None
    return max(numbers)


def number_at(values: list[Any], index: int) -> float | None:
    if index < 0 or index >= len(values):
        return None
    return number_or_none(values[index])


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


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def slugify_filename(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "activity"
