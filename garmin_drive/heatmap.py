from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any

from .render import is_run, local_date, meters_to_miles, number_or_none


RECENT_ACTIVITY_MAP_NAME = "Recent Activity Map.html"
ALL_TIME_ACTIVITY_MAP_NAME = "All Time Activity Map.html"
RAW_HEATMAP_DIR = "Heatmaps"
HEATMAP_STATE_NAME = "All Time Activity Map Data.json"

CELL_SIZE_METERS = 25
INTERPOLATION_STEP_METERS = 10
MAX_SEGMENT_GAP_METERS = 500
EARTH_RADIUS_METERS = 6378137.0


def heatmap_state_from_archives(archives: list[dict[str, Any]]) -> dict[str, Any]:
    return normalize_heatmap_state(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cell_size_meters": CELL_SIZE_METERS,
            "contributions": contributions_from_archives(archives),
        }
    )


def contributions_from_archives(archives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contributions = []
    for archive in archives:
        contribution = activity_map_contribution(archive)
        if contribution:
            contributions.append(contribution)
    return sorted(contributions, key=contribution_sort_key, reverse=True)


def merge_heatmap_state(existing_state: dict[str, Any] | None, new_contributions: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    existing = normalize_heatmap_state(existing_state)
    for contribution in existing.get("contributions", []):
        activity_id = str(contribution.get("source_activity_id") or "")
        if activity_id:
            merged[activity_id] = contribution
    for contribution in new_contributions:
        activity_id = str(contribution.get("source_activity_id") or "")
        if activity_id:
            merged[activity_id] = contribution
    return normalize_heatmap_state(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cell_size_meters": CELL_SIZE_METERS,
            "contributions": sorted(merged.values(), key=contribution_sort_key, reverse=True),
        }
    )


def normalize_heatmap_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = {}
    contributions = [
        contribution
        for contribution in state.get("contributions", [])
        if isinstance(contribution, dict) and contribution.get("source_activity_id")
    ]
    return {
        "schema_version": 1,
        "generated_at": state.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "cell_size_meters": int(state.get("cell_size_meters") or CELL_SIZE_METERS),
        "contributions": sorted(contributions, key=contribution_sort_key, reverse=True),
    }


def load_heatmap_state_text(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return normalize_heatmap_state(parsed) if isinstance(parsed, dict) else None


def activity_map_contribution(archive: dict[str, Any]) -> dict[str, Any] | None:
    activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else {}
    if not activity or not is_run(activity):
        return None

    streams = archive.get("streams") if isinstance(archive.get("streams"), dict) else {}
    latlng = stream_values(streams, "latlng") or []
    route_points, route_indices = valid_route_points(latlng)
    if len(route_points) < 2:
        return None

    cells = rasterize_cells(route_points, route_indices, streams)
    if not cells:
        return None

    activity_id = str(archive.get("source_activity_id") or activity.get("id") or "")
    sport_type = str(activity.get("sport_type") or activity.get("type") or "Activity")
    return {
        "source": "strava",
        "source_activity_id": activity_id,
        "name": str(activity.get("name") or "Untitled Activity"),
        "sport_type": sport_type,
        "local_date": local_date(activity),
        "start_date": activity.get("start_date"),
        "start_date_local": activity.get("start_date_local"),
        "distance_miles": round_or_none(meters_to_miles(activity.get("distance")), 4),
        "stream_sample_count": len(latlng),
        "route": simplify_route(route_points),
        "bounds": route_bounds(route_points),
        "cells": cells,
    }


def valid_route_points(latlng: list[Any]) -> tuple[list[list[float]], list[int]]:
    points: list[list[float]] = []
    indices: list[int] = []
    for index, point in enumerate(latlng):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        lat = number_or_none(point[0])
        lng = number_or_none(point[1])
        if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            continue
        points.append([round(lat, 6), round(lng, 6)])
        indices.append(index)
    return points, indices


def rasterize_cells(route_points: list[list[float]], route_indices: list[int], streams: dict[str, Any]) -> list[dict[str, Any]]:
    speed = stream_values(streams, "velocity_smooth") or []
    heartrate = stream_values(streams, "heartrate") or []
    grade = stream_values(streams, "grade_smooth") or []
    altitude = stream_values(streams, "altitude") or []
    cells: dict[tuple[int, int], dict[str, Any]] = {}

    for offset in range(1, len(route_points)):
        previous = route_points[offset - 1]
        current = route_points[offset]
        previous_index = route_indices[offset - 1]
        current_index = route_indices[offset]
        x0, y0 = latlng_to_mercator(previous[0], previous[1])
        x1, y1 = latlng_to_mercator(current[0], current[1])
        segment_meters = math.hypot(x1 - x0, y1 - y0)
        if segment_meters <= 0 or segment_meters > MAX_SEGMENT_GAP_METERS:
            continue

        steps = max(1, min(100, math.ceil(segment_meters / INTERPOLATION_STEP_METERS)))
        speed_value = average_pair(speed, previous_index, current_index)
        heartrate_value = average_pair(heartrate, previous_index, current_index)
        grade_value = average_pair(grade, previous_index, current_index)
        elevation_delta = delta_pair(altitude, previous_index, current_index)

        for step in range(steps):
            portion = (step + 0.5) / steps
            x = x0 + (x1 - x0) * portion
            y = y0 + (y1 - y0) * portion
            key = (math.floor(x / CELL_SIZE_METERS), math.floor(y / CELL_SIZE_METERS))
            cell = cells.setdefault(
                key,
                {
                    "x": key[0],
                    "y": key[1],
                    "count": 0,
                    "speed_sum": 0.0,
                    "speed_count": 0,
                    "hr_sum": 0.0,
                    "hr_count": 0,
                    "grade_sum": 0.0,
                    "grade_count": 0,
                    "steepness_sum": 0.0,
                    "elevation_delta_sum": 0.0,
                    "elevation_abs_sum": 0.0,
                },
            )
            cell["count"] += 1
            if speed_value is not None:
                cell["speed_sum"] += speed_value
                cell["speed_count"] += 1
            if heartrate_value is not None:
                cell["hr_sum"] += heartrate_value
                cell["hr_count"] += 1
            if grade_value is not None:
                cell["grade_sum"] += grade_value
                cell["grade_count"] += 1
                cell["steepness_sum"] += abs(grade_value)
            if elevation_delta is not None:
                weighted_delta = elevation_delta / steps
                cell["elevation_delta_sum"] += weighted_delta
                cell["elevation_abs_sum"] += abs(weighted_delta)

    return [compact_cell(cell) for cell in sorted(cells.values(), key=lambda item: (item["y"], item["x"]))]


def compact_cell(cell: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "x": int(cell["x"]),
        "y": int(cell["y"]),
        "count": int(cell["count"]),
    }
    for key in [
        "speed_sum",
        "hr_sum",
        "grade_sum",
        "steepness_sum",
        "elevation_delta_sum",
        "elevation_abs_sum",
    ]:
        if abs(float(cell.get(key) or 0.0)) > 0:
            compact[key] = round(float(cell[key]), 4)
    for key in ["speed_count", "hr_count", "grade_count"]:
        if int(cell.get(key) or 0) > 0:
            compact[key] = int(cell[key])
    return compact


def simplify_route(route_points: list[list[float]], min_meters: float = 25) -> list[list[float]]:
    if len(route_points) <= 2:
        return route_points
    simplified = [route_points[0]]
    last_kept = route_points[0]
    for point in route_points[1:-1]:
        if distance_meters(last_kept, point) >= min_meters:
            simplified.append(point)
            last_kept = point
    if simplified[-1] != route_points[-1]:
        simplified.append(route_points[-1])
    return simplified


def route_bounds(route_points: list[list[float]]) -> dict[str, float] | None:
    if not route_points:
        return None
    lats = [point[0] for point in route_points]
    lngs = [point[1] for point in route_points]
    return {
        "south": round(min(lats), 6),
        "west": round(min(lngs), 6),
        "north": round(max(lats), 6),
        "east": round(max(lngs), 6),
    }


def activity_map_payload(title: str, state: dict[str, Any], *, recent_days: int | None = None) -> dict[str, Any]:
    normalized = normalize_heatmap_state(state)
    contributions = normalized["contributions"]
    if recent_days is not None:
        contributions = filter_recent_contributions(contributions, recent_days=recent_days)
    sport_types = sorted({str(item.get("sport_type") or "Activity") for item in contributions})
    return {
        "schema_version": 1,
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "recent" if recent_days is not None else "all_time",
        "recent_days": recent_days,
        "cell_size_meters": normalized["cell_size_meters"],
        "activity_count": len(contributions),
        "sport_types": sport_types,
        "bounds": combined_bounds(contributions),
        "contributions": contributions,
    }


def filter_recent_contributions(contributions: list[dict[str, Any]], *, recent_days: int) -> list[dict[str, Any]]:
    cutoff = date.today().toordinal() - max(0, recent_days - 1)
    recent = []
    for contribution in contributions:
        parsed = parse_date(str(contribution.get("local_date") or ""))
        if parsed and parsed.toordinal() >= cutoff:
            recent.append(contribution)
    return recent


def combined_bounds(contributions: list[dict[str, Any]]) -> dict[str, float] | None:
    bounds = [item.get("bounds") for item in contributions if isinstance(item.get("bounds"), dict)]
    if not bounds:
        return None
    return {
        "south": min(float(item["south"]) for item in bounds),
        "west": min(float(item["west"]) for item in bounds),
        "north": max(float(item["north"]) for item in bounds),
        "east": max(float(item["east"]) for item in bounds),
    }


def render_activity_map_html(title: str, state: dict[str, Any], *, recent_days: int | None = None) -> str:
    payload = activity_map_payload(title, state, recent_days=recent_days)
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { background: #050609; color: #f4f7fb; font-family: Arial, sans-serif; overflow: hidden; }
    #map { background: #050609; }
    .heat-canvas { position: absolute; inset: 0; z-index: 420; pointer-events: none; }
    .panel {
      position: absolute;
      top: 16px;
      left: 16px;
      z-index: 800;
      display: grid;
      grid-template-columns: repeat(3, minmax(132px, auto));
      gap: 8px;
      align-items: end;
      max-width: calc(100vw - 32px);
      padding: 10px;
      background: rgba(7, 10, 17, 0.84);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 8px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
      backdrop-filter: blur(8px);
    }
    .title {
      grid-column: 1 / -1;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 14px;
      font-weight: 700;
      line-height: 1.2;
    }
    .count { color: #aab7c8; font-weight: 400; white-space: nowrap; }
    label { color: #aab7c8; display: grid; gap: 4px; font-size: 11px; }
    select {
      min-width: 0;
      height: 34px;
      color: #f4f7fb;
      background: #101521;
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 6px;
      padding: 0 28px 0 9px;
      font-size: 13px;
    }
    #status {
      position: absolute;
      right: 16px;
      bottom: 16px;
      z-index: 800;
      max-width: min(420px, calc(100vw - 32px));
      color: #cdd7e5;
      background: rgba(7, 10, 17, 0.78);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
    }
    .leaflet-control-attribution { background: rgba(5, 6, 9, 0.78); color: #b7c1cf; }
    .leaflet-control-attribution a { color: #d7e6ff; }
    @media (max-width: 720px) {
      .panel { grid-template-columns: 1fr; right: 12px; left: 12px; top: 12px; }
      .title { font-size: 13px; }
      select { width: 100%; }
      #status { left: 12px; right: 12px; bottom: 12px; }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <div class="title"><span>__TITLE__</span><span class="count" id="activityCount"></span></div>
    <label>Activity
      <select id="activityFilter"></select>
    </label>
    <label>Display
      <select id="displayMode">
        <option value="Heatmap">Heatmap</option>
        <option value="Routes">Routes</option>
      </select>
    </label>
    <label>Metric
      <select id="metricMode">
        <option value="Frequency">Frequency</option>
        <option value="Speed">Speed</option>
        <option value="Heart Rate">Heart Rate</option>
        <option value="Steepness">Steepness</option>
        <option value="Uphill/Downhill">Uphill/Downhill</option>
      </select>
    </label>
  </div>
  <div id="status"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const payload = __PAYLOAD__;
    const leafletMap = L.map("map", { zoomControl: false, preferCanvas: true });
    L.control.zoom({ position: "bottomright" }).addTo(leafletMap);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 20,
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO"
    }).addTo(leafletMap);

    const canvas = document.createElement("canvas");
    canvas.className = "heat-canvas";
    leafletMap.getContainer().appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const routeLayer = L.layerGroup().addTo(leafletMap);
    const activityFilter = document.getElementById("activityFilter");
    const displayMode = document.getElementById("displayMode");
    const metricMode = document.getElementById("metricMode");
    const status = document.getElementById("status");
    const countLabel = document.getElementById("activityCount");

    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[char]));

    function setInitialView() {
      const bounds = payload.bounds;
      if (bounds) {
        leafletMap.fitBounds([[bounds.south, bounds.west], [bounds.north, bounds.east]], { padding: [28, 28] });
      } else {
        leafletMap.setView([39.5, -98.35], 4);
      }
    }

    function buildFilters() {
      const options = ["All", ...(payload.sport_types || [])];
      activityFilter.innerHTML = "";
      for (const option of options) {
        const element = document.createElement("option");
        element.value = option;
        element.textContent = option;
        activityFilter.appendChild(element);
      }
    }

    function filteredContributions() {
      const selected = activityFilter.value || "All";
      return (payload.contributions || []).filter((item) => selected === "All" || item.sport_type === selected);
    }

    function aggregateCells(contributions) {
      const merged = new Map();
      for (const contribution of contributions) {
        for (const cell of contribution.cells || []) {
          const key = `${cell.x}:${cell.y}`;
          if (!merged.has(key)) {
            merged.set(key, {
              x: cell.x,
              y: cell.y,
              count: 0,
              speed_sum: 0,
              speed_count: 0,
              hr_sum: 0,
              hr_count: 0,
              grade_sum: 0,
              grade_count: 0,
              steepness_sum: 0,
              elevation_delta_sum: 0,
              elevation_abs_sum: 0
            });
          }
          const target = merged.get(key);
          target.count += cell.count || 0;
          target.speed_sum += cell.speed_sum || 0;
          target.speed_count += cell.speed_count || 0;
          target.hr_sum += cell.hr_sum || 0;
          target.hr_count += cell.hr_count || 0;
          target.grade_sum += cell.grade_sum || 0;
          target.grade_count += cell.grade_count || 0;
          target.steepness_sum += cell.steepness_sum || 0;
          target.elevation_delta_sum += cell.elevation_delta_sum || 0;
          target.elevation_abs_sum += cell.elevation_abs_sum || 0;
        }
      }
      return Array.from(merged.values());
    }

    function cellCenterLatLng(cell) {
      const size = payload.cell_size_meters || 25;
      const x = (cell.x + 0.5) * size;
      const y = (cell.y + 0.5) * size;
      const lng = (x / 6378137.0) * 180 / Math.PI;
      const lat = (2 * Math.atan(Math.exp(y / 6378137.0)) - Math.PI / 2) * 180 / Math.PI;
      return [lat, lng];
    }

    function resizeCanvas() {
      const ratio = window.devicePixelRatio || 1;
      const size = leafletMap.getSize();
      canvas.style.width = `${size.x}px`;
      canvas.style.height = `${size.y}px`;
      canvas.width = Math.max(1, Math.round(size.x * ratio));
      canvas.height = Math.max(1, Math.round(size.y * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    function metricValue(cell, metric) {
      if (metric === "Speed") return cell.speed_count ? (cell.speed_sum / cell.speed_count) * 2.23694 : null;
      if (metric === "Heart Rate") return cell.hr_count ? cell.hr_sum / cell.hr_count : null;
      if (metric === "Steepness") return cell.grade_count ? cell.steepness_sum / cell.grade_count : null;
      if (metric === "Uphill/Downhill") {
        if (cell.elevation_abs_sum > 0) return cell.elevation_delta_sum / cell.elevation_abs_sum;
        if (cell.grade_count) return Math.max(-1, Math.min(1, (cell.grade_sum / cell.grade_count) / 10));
        return null;
      }
      return cell.count || 0;
    }

    function mix(a, b, t) {
      return [
        Math.round(a[0] + (b[0] - a[0]) * t),
        Math.round(a[1] + (b[1] - a[1]) * t),
        Math.round(a[2] + (b[2] - a[2]) * t)
      ];
    }

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function percentile(values, pct) {
      const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
      if (!sorted.length) return 1;
      const index = clamp(Math.ceil((pct / 100) * sorted.length) - 1, 0, sorted.length - 1);
      return sorted[index] || 1;
    }

    function frequencyScale(cells) {
      const counts = cells.map((cell) => cell.count || 0);
      return Math.max(2, percentile(counts, 98));
    }

    function logIntensity(count, scaleMax) {
      return clamp(Math.log1p(count || 0) / Math.log1p(scaleMax || 1), 0, 1);
    }

    function projectedCellRadius(cell, intensity) {
      const size = payload.cell_size_meters || 25;
      const center = leafletMap.latLngToContainerPoint(cellCenterLatLng(cell));
      const edge = leafletMap.latLngToContainerPoint(cellCenterLatLng({ x: cell.x + 1, y: cell.y, count: cell.count }));
      const cellPixels = Math.max(1, Math.abs(edge.x - center.x));
      return clamp(cellPixels * 1.4 + intensity * 1.2, 1.15, 5.5);
    }

    function metricColor(cell, metric, maxCount) {
      if (metric === "Frequency") {
        const intensity = logIntensity(cell.count || 0, maxCount);
        const warm = intensity < 0.45
          ? mix([102, 25, 0], [252, 76, 2], intensity / 0.45)
          : mix([252, 76, 2], [255, 246, 180], (intensity - 0.45) / 0.55);
        return { rgb: warm, alpha: 0.025 + 0.62 * Math.pow(intensity, 1.55) };
      }
      const value = metricValue(cell, metric);
      if (value === null || Number.isNaN(value)) return { rgb: [180, 190, 205], alpha: 0.12 };
      if (metric === "Speed") {
        const t = Math.max(0, Math.min(1, (value - 4) / 18));
        return { rgb: mix([18, 52, 128], [162, 232, 255], t), alpha: 0.34 + 0.22 * t };
      }
      if (metric === "Heart Rate") {
        const t = Math.max(0, Math.min(1, (value - 110) / 80));
        return { rgb: mix([94, 28, 38], [255, 160, 160], t), alpha: 0.34 + 0.24 * t };
      }
      if (metric === "Steepness") {
        const t = Math.max(0, Math.min(1, value / 12));
        return { rgb: mix([70, 70, 70], [255, 255, 255], t), alpha: 0.24 + 0.36 * t };
      }
      const t = Math.max(-1, Math.min(1, value));
      if (t < -0.04) return { rgb: mix([16, 80, 28], [92, 230, 105], Math.abs(t)), alpha: 0.28 + 0.24 * Math.abs(t) };
      if (t > 0.04) return { rgb: mix([62, 24, 85], [210, 82, 255], t), alpha: 0.3 + 0.25 * t };
      return { rgb: [160, 160, 150], alpha: 0.18 };
    }

    function drawHeatmap() {
      resizeCanvas();
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const contributions = filteredContributions();
      const cells = aggregateCells(contributions);
      canvas.style.display = displayMode.value === "Heatmap" ? "block" : "none";
      if (displayMode.value !== "Heatmap") return;
      const metric = metricMode.value;
      const scaleMax = frequencyScale(cells);
      if (metric === "Frequency") {
        drawFrequencyRoutes(contributions);
        drawFrequencyCells(cells, scaleMax);
      } else {
        drawMetricCells(cells, metric, scaleMax);
      }
      ctx.globalCompositeOperation = "source-over";
    }

    function drawFrequencyRoutes(contributions) {
      const lineWidth = clamp(leafletMap.getZoom() / 7, 1.25, 2.4);
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = "rgba(252, 76, 2, 0.055)";
      ctx.lineWidth = lineWidth;
      for (const contribution of contributions) {
        if (!contribution.route || contribution.route.length < 2) continue;
        ctx.beginPath();
        for (let index = 0; index < contribution.route.length; index += 1) {
          const point = leafletMap.latLngToContainerPoint(contribution.route[index]);
          if (index === 0) ctx.moveTo(point.x, point.y);
          else ctx.lineTo(point.x, point.y);
        }
        ctx.stroke();
      }
    }

    function drawFrequencyCells(cells, scaleMax) {
      const size = leafletMap.getSize();
      ctx.globalCompositeOperation = "lighter";
      for (const cell of [...cells].sort((a, b) => (a.count || 0) - (b.count || 0))) {
        const point = leafletMap.latLngToContainerPoint(cellCenterLatLng(cell));
        if (point.x < -24 || point.y < -24 || point.x > size.x + 24 || point.y > size.y + 24) continue;
        const intensity = logIntensity(cell.count || 0, scaleMax);
        if (intensity < 0.03) continue;
        const { rgb, alpha } = metricColor(cell, "Frequency", scaleMax);
        const radius = projectedCellRadius(cell, intensity);
        const gradient = ctx.createRadialGradient(point.x, point.y, 0.2, point.x, point.y, radius);
        gradient.addColorStop(0, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`);
        gradient.addColorStop(0.65, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha * 0.35})`);
        gradient.addColorStop(1, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, 0)`);
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function drawMetricCells(cells, metric, scaleMax) {
      const size = leafletMap.getSize();
      ctx.globalCompositeOperation = "source-over";
      for (const cell of cells) {
        const point = leafletMap.latLngToContainerPoint(cellCenterLatLng(cell));
        if (point.x < -24 || point.y < -24 || point.x > size.x + 24 || point.y > size.y + 24) continue;
        const intensity = logIntensity(cell.count || 0, scaleMax);
        const { rgb, alpha } = metricColor(cell, metric, scaleMax);
        const radius = projectedCellRadius(cell, intensity);
        const gradient = ctx.createRadialGradient(point.x, point.y, 0.2, point.x, point.y, radius);
        gradient.addColorStop(0, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`);
        gradient.addColorStop(0.72, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha * 0.42})`);
        gradient.addColorStop(1, `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, 0)`);
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function routeColor(type) {
      let hash = 0;
      for (const char of String(type || "Activity")) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
      const colors = ["#ff7c4d", "#66f0d4", "#82a8ff", "#ff61b6", "#ffd35f", "#8cff8a"];
      return colors[Math.abs(hash) % colors.length];
    }

    function drawRoutes() {
      routeLayer.clearLayers();
      if (displayMode.value !== "Routes") return;
      for (const contribution of filteredContributions()) {
        if (!contribution.route || contribution.route.length < 2) continue;
        const popup = `<strong>${escapeHtml(contribution.local_date)}</strong><br>${escapeHtml(contribution.name)}<br>${escapeHtml(contribution.sport_type)}${contribution.distance_miles ? `<br>${escapeHtml(contribution.distance_miles)} mi` : ""}`;
        L.polyline(contribution.route, {
          color: routeColor(contribution.sport_type),
          weight: 2.8,
          opacity: 0.7,
          lineJoin: "round"
        }).bindPopup(popup).addTo(routeLayer);
      }
    }

    function updateStatus() {
      const contributions = filteredContributions();
      const cells = aggregateCells(contributions);
      const label = payload.scope === "recent" ? `${payload.recent_days} days` : "all time";
      countLabel.textContent = `${contributions.length} activities`;
      status.textContent = contributions.length
        ? `${label}: ${contributions.length} mapped activities, ${cells.length} heat cells`
        : "No GPS stream data is available for this view.";
    }

    function redraw() {
      drawRoutes();
      drawHeatmap();
      updateStatus();
    }

    buildFilters();
    setInitialView();
    redraw();
    leafletMap.on("move zoom resize", drawHeatmap);
    activityFilter.addEventListener("change", redraw);
    displayMode.addEventListener("change", redraw);
    metricMode.addEventListener("change", drawHeatmap);
  </script>
</body>
</html>
"""
    return template.replace("__TITLE__", html_escape(title)).replace("__PAYLOAD__", payload_json)


def stream_values(streams: dict[str, Any], key: str) -> list[Any] | None:
    stream = streams.get(key)
    if not isinstance(stream, dict):
        return None
    data = stream.get("data")
    return data if isinstance(data, list) else None


def average_pair(values: list[Any], first_index: int, second_index: int) -> float | None:
    first = number_at(values, first_index)
    second = number_at(values, second_index)
    if first is None:
        return second
    if second is None:
        return first
    return (first + second) / 2


def delta_pair(values: list[Any], first_index: int, second_index: int) -> float | None:
    first = number_at(values, first_index)
    second = number_at(values, second_index)
    if first is None or second is None:
        return None
    return second - first


def number_at(values: list[Any], index: int) -> float | None:
    if index < 0 or index >= len(values):
        return None
    return number_or_none(values[index])


def latlng_to_mercator(lat: float, lng: float) -> tuple[float, float]:
    clamped_lat = max(-85.05112878, min(85.05112878, lat))
    x = EARTH_RADIUS_METERS * math.radians(lng)
    y = EARTH_RADIUS_METERS * math.log(math.tan(math.pi / 4 + math.radians(clamped_lat) / 2))
    return x, y


def distance_meters(first: list[float], second: list[float]) -> float:
    x0, y0 = latlng_to_mercator(first[0], first[1])
    x1, y1 = latlng_to_mercator(second[0], second[1])
    return math.hypot(x1 - x0, y1 - y0)


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def contribution_sort_key(contribution: dict[str, Any]) -> tuple[str, str]:
    return (
        str(contribution.get("start_date_local") or contribution.get("local_date") or ""),
        str(contribution.get("source_activity_id") or ""),
    )


def round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
