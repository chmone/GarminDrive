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
    .leaflet-heatmap-tile { pointer-events: none; }
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

    leafletMap.createPane("heatPane");
    leafletMap.getPane("heatPane").style.zIndex = 420;
    leafletMap.getPane("heatPane").style.pointerEvents = "none";
    const routeLayer = L.layerGroup().addTo(leafletMap);
    const activityFilter = document.getElementById("activityFilter");
    const displayMode = document.getElementById("displayMode");
    const metricMode = document.getElementById("metricMode");
    const status = document.getElementById("status");
    const countLabel = document.getElementById("activityCount");
    const TILE_SIZE = 256;
    const EARTH_RADIUS = 6378137.0;
    const MERCATOR_SPAN = 2 * Math.PI * EARTH_RADIUS;
    const CELL_SIZE_METERS = payload.cell_size_meters || 25;
    const BUCKET_CELL_SPAN = 64;
    const TILE_OVERDRAW_PIXELS = 44;
    const MAX_BUCKET_QUERY_SCAN = 8000;
    const ROUTE_BUCKET_METERS = 512;
    const ROUTE_STROKE_MIN_ZOOM = 14;
    const ROUTE_STROKE_ONLY_ZOOM = 16;
    const ROUTE_STROKE_OVERDRAW_PIXELS = 96;
    const MAX_ROUTE_SEGMENT_METERS = 650;
    const MAX_ROUTE_BUCKET_QUERY_SCAN = 5000;
    const MAX_HEAT_TILE_CACHE = 360;
    const aggregationCache = new Map();
    const heatTileCache = new Map();
    let heatRefreshTimer = null;

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

    function aggregationForSelection() {
      const key = activityFilter.value || "All";
      if (aggregationCache.has(key)) return aggregationCache.get(key);
      const contributions = filteredContributions();
      const cells = aggregateCells(contributions);
      const view = {
        contributions,
        cells,
        index: buildCellIndex(cells),
        cellLookup: buildCellLookup(cells),
        scaleMax: frequencyScale(cells),
        metricRanges: buildMetricRanges(cells),
        metricSampleScales: buildMetricSampleScales(cells)
      };
      aggregationCache.set(key, view);
      return view;
    }

    function clearHeatTileCache() {
      heatTileCache.clear();
    }

    function heatTileCacheKey(coords, metric) {
      const ratio = window.devicePixelRatio || 1;
      return `${activityFilter.value || "All"}|${metric}|${ratio}|${coords.z}:${coords.x}:${coords.y}`;
    }

    function drawCachedTile(tile, cacheKey) {
      const cached = heatTileCache.get(cacheKey);
      if (!cached) return false;
      heatTileCache.delete(cacheKey);
      heatTileCache.set(cacheKey, cached);
      tile.width = cached.width;
      tile.height = cached.height;
      tile.style.width = `${TILE_SIZE}px`;
      tile.style.height = `${TILE_SIZE}px`;
      tile.getContext("2d").drawImage(cached, 0, 0);
      return true;
    }

    function rememberTile(cacheKey, tile) {
      const cached = document.createElement("canvas");
      cached.width = tile.width;
      cached.height = tile.height;
      cached.getContext("2d").drawImage(tile, 0, 0);
      heatTileCache.set(cacheKey, cached);
      while (heatTileCache.size > MAX_HEAT_TILE_CACHE) {
        heatTileCache.delete(heatTileCache.keys().next().value);
      }
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

    function metricSampleCount(cell, metric) {
      if (metric === "Speed") return cell.speed_count || 0;
      if (metric === "Heart Rate") return cell.hr_count || 0;
      if (metric === "Steepness") return cell.grade_count || 0;
      if (metric === "Uphill/Downhill") return cell.grade_count || (cell.elevation_abs_sum > 0 ? 1 : 0);
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
      return Number.isFinite(sorted[index]) ? sorted[index] : 1;
    }

    function frequencyScale(cells) {
      const counts = cells.map((cell) => cell.count || 0);
      return Math.max(2, percentile(counts, 98));
    }

    function metricRange(cells, metric) {
      if (metric === "Uphill/Downhill") return { available: true, low: -1, high: 1 };
      const values = cells.map((cell) => metricValue(cell, metric)).filter((value) => Number.isFinite(value));
      if (!values.length) return { available: false, low: null, high: null };
      let low = percentile(values, 5);
      let high = percentile(values, 95);
      if (!Number.isFinite(low) || !Number.isFinite(high)) return { available: false, low: null, high: null };
      if (high <= low) {
        const spread = metric === "Heart Rate" ? 1 : metric === "Speed" ? 0.1 : 0.5;
        low -= spread;
        high += spread;
      }
      return { available: true, low, high };
    }

    function buildMetricRanges(cells) {
      return {
        Speed: metricRange(cells, "Speed"),
        "Heart Rate": metricRange(cells, "Heart Rate"),
        Steepness: metricRange(cells, "Steepness"),
        "Uphill/Downhill": metricRange(cells, "Uphill/Downhill")
      };
    }

    function buildMetricSampleScales(cells) {
      const scales = {};
      for (const metric of ["Speed", "Heart Rate", "Steepness", "Uphill/Downhill"]) {
        const samples = cells.map((cell) => metricSampleCount(cell, metric)).filter((value) => value > 0);
        scales[metric] = Math.max(2, percentile(samples, 95));
      }
      return scales;
    }

    function logIntensity(count, scaleMax) {
      return clamp(Math.log1p(count || 0) / Math.log1p(scaleMax || 1), 0, 1);
    }

    function metricConfidence(cell, metric, view) {
      const frequencyConfidence = logIntensity(cell.count || 0, view.scaleMax);
      const sampleScale = view.metricSampleScales[metric] || view.scaleMax;
      const sampleConfidence = clamp(Math.log1p(metricSampleCount(cell, metric)) / Math.log1p(sampleScale || 1), 0, 1);
      return clamp(0.35 * frequencyConfidence + 0.65 * sampleConfidence, 0.1, 1);
    }

    function normalizedMetric(value, range) {
      if (!range || !range.available || range.high <= range.low) return 0.5;
      return clamp((value - range.low) / (range.high - range.low), 0, 1);
    }

    function rgba(rgb, alpha) {
      return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${clamp(alpha, 0, 1)})`;
    }

    function metricColor(cell, metric, view) {
      if (metric === "Frequency") {
        const intensity = logIntensity(cell.count || 0, view.scaleMax);
        const warm = intensity < 0.45
          ? mix([102, 25, 0], [252, 76, 2], intensity / 0.45)
          : mix([252, 76, 2], [255, 246, 180], (intensity - 0.45) / 0.55);
        return { rgb: warm, alpha: 0.045 + 0.64 * Math.pow(intensity, 1.35) };
      }
      const value = metricValue(cell, metric);
      if (value === null || Number.isNaN(value)) return null;
      const confidence = Math.pow(metricConfidence(cell, metric, view), 0.72);
      if (metric === "Speed") {
        const t = normalizedMetric(value, view.metricRanges.Speed);
        const rgb = t < 0.55
          ? mix([20, 42, 112], [22, 178, 184], t / 0.55)
          : mix([22, 178, 184], [230, 255, 255], (t - 0.55) / 0.45);
        return { rgb, alpha: 0.16 + 0.48 * confidence };
      }
      if (metric === "Heart Rate") {
        const t = normalizedMetric(value, view.metricRanges["Heart Rate"]);
        const rgb = t < 0.55
          ? mix([86, 20, 54], [232, 64, 48], t / 0.55)
          : mix([232, 64, 48], [255, 222, 190], (t - 0.55) / 0.45);
        return { rgb, alpha: 0.16 + 0.5 * confidence };
      }
      if (metric === "Steepness") {
        const t = normalizedMetric(value, view.metricRanges.Steepness);
        return { rgb: mix([58, 62, 70], [255, 255, 255], t), alpha: 0.12 + 0.5 * confidence };
      }
      const t = Math.max(-1, Math.min(1, value));
      const alpha = 0.14 + 0.44 * confidence;
      if (t < -0.04) return { rgb: mix([20, 82, 40], [112, 245, 126], Math.abs(t)), alpha };
      if (t > 0.04) return { rgb: mix([66, 28, 94], [220, 96, 255], t), alpha };
      return { rgb: [176, 170, 150], alpha: 0.12 + 0.26 * confidence };
    }

    function latLngToMercator(point) {
      if (!Array.isArray(point) || point.length < 2) return null;
      const lat = Number(point[0]);
      const lng = Number(point[1]);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
      const clampedLat = clamp(lat, -85.05112878, 85.05112878);
      return {
        x: EARTH_RADIUS * lng * Math.PI / 180,
        y: EARTH_RADIUS * Math.log(Math.tan(Math.PI / 4 + clampedLat * Math.PI / 360))
      };
    }

    function buildCellIndex(cells) {
      const buckets = new Map();
      for (const cell of cells) {
        const bucketX = Math.floor(cell.x / BUCKET_CELL_SPAN);
        const bucketY = Math.floor(cell.y / BUCKET_CELL_SPAN);
        const key = `${bucketX}:${bucketY}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(cell);
      }
      return buckets;
    }

    function buildCellLookup(cells) {
      const lookup = new Map();
      for (const cell of cells) lookup.set(`${cell.x}:${cell.y}`, cell);
      return lookup;
    }

    function routeIndexForView(view) {
      if (view.routeIndex) return view.routeIndex;
      view.routeIndex = buildRouteIndex(view.contributions);
      return view.routeIndex;
    }

    function buildRouteIndex(contributions) {
      const buckets = new Map();
      const segments = [];
      for (const contribution of contributions) {
        const route = contribution.route || [];
        let previous = null;
        for (const point of route) {
          const current = latLngToMercator(point);
          if (previous && current) {
            const length = Math.hypot(current.x - previous.x, current.y - previous.y);
            if (length > 0 && length <= MAX_ROUTE_SEGMENT_METERS) {
              const segment = {
                id: segments.length,
                x0: previous.x,
                y0: previous.y,
                x1: current.x,
                y1: current.y
              };
              segments.push(segment);
              indexRouteSegment(buckets, segment);
            }
          }
          previous = current;
        }
      }
      return { buckets, segments };
    }

    function indexRouteSegment(buckets, segment) {
      const minBucketX = Math.floor(Math.min(segment.x0, segment.x1) / ROUTE_BUCKET_METERS);
      const maxBucketX = Math.floor(Math.max(segment.x0, segment.x1) / ROUTE_BUCKET_METERS);
      const minBucketY = Math.floor(Math.min(segment.y0, segment.y1) / ROUTE_BUCKET_METERS);
      const maxBucketY = Math.floor(Math.max(segment.y0, segment.y1) / ROUTE_BUCKET_METERS);
      for (let bucketX = minBucketX; bucketX <= maxBucketX; bucketX += 1) {
        for (let bucketY = minBucketY; bucketY <= maxBucketY; bucketY += 1) {
          const key = `${bucketX}:${bucketY}`;
          if (!buckets.has(key)) buckets.set(key, []);
          buckets.get(key).push(segment);
        }
      }
    }

    function queryRouteSegments(routeIndex, bounds) {
      const minBucketX = Math.floor(bounds.west / ROUTE_BUCKET_METERS);
      const maxBucketX = Math.floor(bounds.east / ROUTE_BUCKET_METERS);
      const minBucketY = Math.floor(bounds.south / ROUTE_BUCKET_METERS);
      const maxBucketY = Math.floor(bounds.north / ROUTE_BUCKET_METERS);
      const bucketVisits = (maxBucketX - minBucketX + 1) * (maxBucketY - minBucketY + 1);
      const segments = [];
      const seen = new Set();
      const maybeAdd = (segment) => {
        if (seen.has(segment.id)) return;
        if (
          Math.max(segment.x0, segment.x1) < bounds.west ||
          Math.min(segment.x0, segment.x1) > bounds.east ||
          Math.max(segment.y0, segment.y1) < bounds.south ||
          Math.min(segment.y0, segment.y1) > bounds.north
        ) return;
        seen.add(segment.id);
        segments.push(segment);
      };
      if (bucketVisits > MAX_ROUTE_BUCKET_QUERY_SCAN) {
        for (const segment of routeIndex.segments) maybeAdd(segment);
        return segments;
      }
      for (let bucketX = minBucketX; bucketX <= maxBucketX; bucketX += 1) {
        for (let bucketY = minBucketY; bucketY <= maxBucketY; bucketY += 1) {
          const bucket = routeIndex.buckets.get(`${bucketX}:${bucketY}`);
          if (!bucket) continue;
          for (const segment of bucket) maybeAdd(segment);
        }
      }
      return segments;
    }

    function queryIndexedCells(view, bounds) {
      const cells = [];
      const minBucketX = Math.floor(bounds.minCellX / BUCKET_CELL_SPAN);
      const maxBucketX = Math.floor(bounds.maxCellX / BUCKET_CELL_SPAN);
      const minBucketY = Math.floor(bounds.minCellY / BUCKET_CELL_SPAN);
      const maxBucketY = Math.floor(bounds.maxCellY / BUCKET_CELL_SPAN);
      const bucketVisits = (maxBucketX - minBucketX + 1) * (maxBucketY - minBucketY + 1);
      if (bucketVisits > MAX_BUCKET_QUERY_SCAN) {
        for (const cell of view.cells) {
          if (cell.x >= bounds.minCellX && cell.x <= bounds.maxCellX && cell.y >= bounds.minCellY && cell.y <= bounds.maxCellY) {
            cells.push(cell);
          }
        }
        return cells;
      }
      for (let bucketX = minBucketX; bucketX <= maxBucketX; bucketX += 1) {
        for (let bucketY = minBucketY; bucketY <= maxBucketY; bucketY += 1) {
          const bucket = view.index.get(`${bucketX}:${bucketY}`);
          if (!bucket) continue;
          for (const cell of bucket) {
            if (cell.x >= bounds.minCellX && cell.x <= bounds.maxCellX && cell.y >= bounds.minCellY && cell.y <= bounds.maxCellY) {
              cells.push(cell);
            }
          }
        }
      }
      return cells;
    }

    function mercatorToPoint(x, y, zoom) {
      const scale = TILE_SIZE * Math.pow(2, zoom);
      return {
        x: (x / MERCATOR_SPAN + 0.5) * scale,
        y: (0.5 - y / MERCATOR_SPAN) * scale
      };
    }

    function pointToMercator(x, y, zoom) {
      const scale = TILE_SIZE * Math.pow(2, zoom);
      return {
        x: (x / scale - 0.5) * MERCATOR_SPAN,
        y: (0.5 - y / scale) * MERCATOR_SPAN
      };
    }

    function tileCellBounds(coords) {
      const bounds = tileMercatorBounds(coords, TILE_OVERDRAW_PIXELS);
      return {
        minCellX: Math.floor(bounds.west / CELL_SIZE_METERS) - 1,
        maxCellX: Math.floor(bounds.east / CELL_SIZE_METERS) + 1,
        minCellY: Math.floor(bounds.south / CELL_SIZE_METERS) - 1,
        maxCellY: Math.floor(bounds.north / CELL_SIZE_METERS) + 1
      };
    }

    function tileMercatorBounds(coords, overdrawPixels) {
      const minPixelX = coords.x * TILE_SIZE - overdrawPixels;
      const minPixelY = coords.y * TILE_SIZE - overdrawPixels;
      const maxPixelX = (coords.x + 1) * TILE_SIZE + overdrawPixels;
      const maxPixelY = (coords.y + 1) * TILE_SIZE + overdrawPixels;
      const first = pointToMercator(minPixelX, minPixelY, coords.z);
      const second = pointToMercator(maxPixelX, maxPixelY, coords.z);
      const west = Math.min(first.x, second.x);
      const east = Math.max(first.x, second.x);
      const south = Math.min(first.y, second.y);
      const north = Math.max(first.y, second.y);
      return { west, east, south, north };
    }

    function cellTileRect(cell, zoom, origin) {
      const west = cell.x * CELL_SIZE_METERS;
      const east = (cell.x + 1) * CELL_SIZE_METERS;
      const south = cell.y * CELL_SIZE_METERS;
      const north = (cell.y + 1) * CELL_SIZE_METERS;
      const topLeft = mercatorToPoint(west, north, zoom);
      const bottomRight = mercatorToPoint(east, south, zoom);
      return {
        left: topLeft.x - origin.x,
        top: topLeft.y - origin.y,
        right: bottomRight.x - origin.x,
        bottom: bottomRight.y - origin.y,
        width: bottomRight.x - topLeft.x,
        height: bottomRight.y - topLeft.y
      };
    }

    function tileBlurPixels(zoom) {
      if (zoom >= 18) return 3.2;
      if (zoom >= 16) return 2.4;
      if (zoom >= 14) return 1.7;
      return 1.1;
    }

    function expandedRect(rect, zoom, core = false) {
      const centerX = (rect.left + rect.right) / 2;
      const centerY = (rect.top + rect.bottom) / 2;
      const width = Math.max(0.7, Math.abs(rect.width));
      const height = Math.max(0.7, Math.abs(rect.height));
      const size = Math.max(width, height);
      const minSize = zoom < 12 ? 1.8 : zoom < 15 ? 2.8 : 4.5;
      const inflate = core ? 0 : clamp(size * 0.18, 0.85, 7);
      const drawWidth = Math.max(width + inflate * 2, minSize) * (core ? 0.58 : 1);
      const drawHeight = Math.max(height + inflate * 2, minSize) * (core ? 0.58 : 1);
      return {
        left: centerX - drawWidth / 2,
        top: centerY - drawHeight / 2,
        width: drawWidth,
        height: drawHeight
      };
    }

    function fillCellRect(ctx, rect, color, zoom, alphaScale = 1, core = false) {
      const drawRect = expandedRect(rect, zoom, core);
      if (
        drawRect.left > TILE_SIZE + TILE_OVERDRAW_PIXELS ||
        drawRect.top > TILE_SIZE + TILE_OVERDRAW_PIXELS ||
        drawRect.left + drawRect.width < -TILE_OVERDRAW_PIXELS ||
        drawRect.top + drawRect.height < -TILE_OVERDRAW_PIXELS
      ) return;
      ctx.fillStyle = rgba(color.rgb, color.alpha * alphaScale);
      ctx.fillRect(drawRect.left, drawRect.top, drawRect.width, drawRect.height);
    }

    function bridgeLineWidth(zoom, rect) {
      const cellPixels = Math.max(Math.abs(rect.width), Math.abs(rect.height), 1);
      const zoomBoost = zoom >= 17 ? 1.1 : zoom >= 15 ? 0.95 : 0.8;
      return clamp(cellPixels * zoomBoost, 2.4, 26);
    }

    function bridgeCellDistance(zoom) {
      if (zoom >= 17) return 2;
      if (zoom >= 14) return 1;
      return 0;
    }

    function drawCellBridges(ctx, cells, metric, view, zoom, origin) {
      const maxDistance = bridgeCellDistance(zoom);
      if (!maxDistance || cells.length > 18000) return;
      const byKey = new Map();
      for (const cell of cells) byKey.set(`${cell.x}:${cell.y}`, cell);
      const offsets = maxDistance >= 2
        ? [[1, 0], [0, 1], [1, 1], [1, -1], [2, 0], [0, 2], [2, 1], [1, 2], [2, 2], [2, -1], [1, -2], [2, -2]]
        : [[1, 0], [0, 1], [1, 1], [1, -1]];
      ctx.save();
      ctx.globalCompositeOperation = metric === "Frequency" ? "lighter" : "source-over";
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const cell of cells) {
        const color = metricColor(cell, metric, view);
        if (!color) continue;
        const rect = cellTileRect(cell, zoom, origin);
        const startX = (rect.left + rect.right) / 2;
        const startY = (rect.top + rect.bottom) / 2;
        const lineWidth = bridgeLineWidth(zoom, rect);
        for (const [dx, dy] of offsets) {
          const neighbor = byKey.get(`${cell.x + dx}:${cell.y + dy}`);
          if (!neighbor) continue;
          const neighborColor = metricColor(neighbor, metric, view);
          if (!neighborColor) continue;
          const neighborRect = cellTileRect(neighbor, zoom, origin);
          const endX = (neighborRect.left + neighborRect.right) / 2;
          const endY = (neighborRect.top + neighborRect.bottom) / 2;
          if (
            Math.max(startX, endX) < -TILE_OVERDRAW_PIXELS ||
            Math.min(startX, endX) > TILE_SIZE + TILE_OVERDRAW_PIXELS ||
            Math.max(startY, endY) < -TILE_OVERDRAW_PIXELS ||
            Math.min(startY, endY) > TILE_SIZE + TILE_OVERDRAW_PIXELS
          ) continue;
          ctx.strokeStyle = rgba(color.rgb, color.alpha * 0.72);
          ctx.lineWidth = lineWidth;
          ctx.beginPath();
          ctx.moveTo(startX, startY);
          ctx.lineTo(endX, endY);
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    function frequencyRouteStrokeWidth(zoom) {
      return clamp(2.2 * Math.pow(1.36, Math.max(0, zoom - ROUTE_STROKE_MIN_ZOOM)), 2.2, 16);
    }

    function frequencyRouteIntensity(cellCount, scaleMax) {
      const base = logIntensity(cellCount || 0, scaleMax);
      return clamp(Math.pow(base, 0.72), 0.08, 1);
    }

    function frequencyHeatGlowAlpha(zoom) {
      if (zoom >= ROUTE_STROKE_ONLY_ZOOM) return 0;
      if (zoom >= ROUTE_STROKE_ONLY_ZOOM - 1) return 0.16;
      return 0.32;
    }

    function frequencyCellForMercator(view, x, y) {
      const cellX = Math.floor(x / CELL_SIZE_METERS);
      const cellY = Math.floor(y / CELL_SIZE_METERS);
      return view.cellLookup.get(`${cellX}:${cellY}`) || null;
    }

    function routeSegmentIntensity(segment, view) {
      const midpoint = frequencyCellForMercator(view, (segment.x0 + segment.x1) / 2, (segment.y0 + segment.y1) / 2);
      const start = frequencyCellForMercator(view, segment.x0, segment.y0);
      const end = frequencyCellForMercator(view, segment.x1, segment.y1);
      const count = Math.max(midpoint?.count || 0, start?.count || 0, end?.count || 0);
      return frequencyRouteIntensity(count || 1, view.scaleMax);
    }

    function routeSegmentBands(segments, view) {
      const bands = [
        { min: 0, max: 0.24, intensity: 0.18, segments: [] },
        { min: 0.24, max: 0.45, intensity: 0.34, segments: [] },
        { min: 0.45, max: 0.65, intensity: 0.54, segments: [] },
        { min: 0.65, max: 0.82, intensity: 0.74, segments: [] },
        { min: 0.82, max: 1.01, intensity: 0.94, segments: [] }
      ];
      for (const segment of segments) {
        const intensity = routeSegmentIntensity(segment, view);
        const band = bands.find((item) => intensity >= item.min && intensity < item.max) || bands[bands.length - 1];
        band.segments.push(segment);
      }
      return bands.filter((band) => band.segments.length);
    }

    function drawRouteSegments(ctx, segments, coords, origin, width, strokeStyle, blurPixels = 0) {
      if (!segments.length) return;
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "butt";
      ctx.lineJoin = "round";
      ctx.lineWidth = width;
      ctx.strokeStyle = strokeStyle;
      if (blurPixels > 0) ctx.filter = `blur(${blurPixels}px)`;
      ctx.beginPath();
      for (const segment of segments) {
        const start = mercatorToPoint(segment.x0, segment.y0, coords.z);
        const end = mercatorToPoint(segment.x1, segment.y1, coords.z);
        ctx.moveTo(start.x - origin.x, start.y - origin.y);
        ctx.lineTo(end.x - origin.x, end.y - origin.y);
      }
      ctx.stroke();
      ctx.restore();
    }

    function drawFrequencyRouteStrokes(ctx, view, coords, origin) {
      if (coords.z < ROUTE_STROKE_MIN_ZOOM) return false;
      const routeIndex = routeIndexForView(view);
      const bounds = tileMercatorBounds(coords, ROUTE_STROKE_OVERDRAW_PIXELS);
      const segments = queryRouteSegments(routeIndex, bounds);
      if (!segments.length) return false;
      const width = frequencyRouteStrokeWidth(coords.z);
      for (const band of routeSegmentBands(segments, view)) {
        const t = band.intensity;
        const bandWidth = width * (0.72 + t * 0.42);
        drawRouteSegments(ctx, band.segments, coords, origin, bandWidth * 5.4, `rgba(74, 20, 0, ${0.026 + t * 0.032})`, 4.2);
        drawRouteSegments(ctx, band.segments, coords, origin, bandWidth * 3.15, `rgba(252, 76, 2, ${0.045 + t * 0.09})`, 2);
        drawRouteSegments(ctx, band.segments, coords, origin, bandWidth * 1.72, `rgba(255, 124, 48, ${0.12 + t * 0.22})`, 0.45);
        drawRouteSegments(ctx, band.segments, coords, origin, bandWidth * 0.92, `rgba(255, 214, 132, ${0.14 + t * 0.26})`);
        if (t > 0.4) {
          drawRouteSegments(ctx, band.segments, coords, origin, bandWidth * 0.42, `rgba(255, 250, 220, ${(t - 0.4) * 0.42})`);
        }
      }
      return true;
    }

    function drawHeatTile(tile, coords) {
      const metric = metricMode.value;
      const cacheKey = heatTileCacheKey(coords, metric);
      if (drawCachedTile(tile, cacheKey)) return;

      const ratio = window.devicePixelRatio || 1;
      tile.width = TILE_SIZE * ratio;
      tile.height = TILE_SIZE * ratio;
      tile.style.width = `${TILE_SIZE}px`;
      tile.style.height = `${TILE_SIZE}px`;
      const ctx = tile.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

      const view = aggregationForSelection();
      if (!view.cells.length) return;
      if (metric !== "Frequency" && view.metricRanges[metric] && !view.metricRanges[metric].available) return;
      const origin = { x: coords.x * TILE_SIZE, y: coords.y * TILE_SIZE };
      const routeStrokeMode = metric === "Frequency" && coords.z >= ROUTE_STROKE_MIN_ZOOM;
      const routeStrokeOnlyMode = metric === "Frequency" && coords.z >= ROUTE_STROKE_ONLY_ZOOM;

      if (routeStrokeOnlyMode && drawFrequencyRouteStrokes(ctx, view, coords, origin)) {
        rememberTile(cacheKey, tile);
        return;
      }

      const cells = queryIndexedCells(view, tileCellBounds(coords));
      if (!cells.length && metric !== "Frequency") return;
      const orderedCells = metric === "Frequency"
        ? cells.slice().sort((a, b) => (a.count || 0) - (b.count || 0))
        : cells;

      if (orderedCells.length) {
        ctx.save();
        ctx.globalCompositeOperation = metric === "Frequency" ? "lighter" : "source-over";
        ctx.filter = `blur(${tileBlurPixels(coords.z)}px)`;
        for (const cell of orderedCells) {
          const color = metricColor(cell, metric, view);
          if (!color) continue;
          const alphaScale = routeStrokeMode ? frequencyHeatGlowAlpha(coords.z) : 0.9;
          fillCellRect(ctx, cellTileRect(cell, coords.z, origin), color, coords.z, alphaScale);
        }
        ctx.restore();
      }

      const routeStrokesDrawn = routeStrokeMode && drawFrequencyRouteStrokes(ctx, view, coords, origin);
      if (!routeStrokesDrawn) drawCellBridges(ctx, orderedCells, metric, view, coords.z, origin);

      if (!routeStrokesDrawn || metric !== "Frequency") {
        ctx.save();
        ctx.globalCompositeOperation = metric === "Frequency" ? "lighter" : "source-over";
        for (const cell of orderedCells) {
          const color = metricColor(cell, metric, view);
          if (!color) continue;
          fillCellRect(ctx, cellTileRect(cell, coords.z, origin), color, coords.z, metric === "Frequency" ? 0.18 : 0.12, true);
        }
        ctx.restore();
      }
      rememberTile(cacheKey, tile);
    }

    const heatLayer = L.gridLayer({
      attribution: "",
      className: "leaflet-heatmap-tile",
      keepBuffer: 1,
      noWrap: true,
      pane: "heatPane",
      tileSize: TILE_SIZE,
      updateInterval: 220,
      updateWhenIdle: true,
      updateWhenZooming: false
    });
    heatLayer.createTile = function(coords) {
      const tile = L.DomUtil.create("canvas", "leaflet-heatmap-tile");
      drawHeatTile(tile, coords);
      return tile;
    };

    function routeColor(type) {
      let hash = 0;
      for (const char of String(type || "Activity")) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
      const colors = ["#ff7c4d", "#66f0d4", "#82a8ff", "#ff61b6", "#ffd35f", "#8cff8a"];
      return colors[Math.abs(hash) % colors.length];
    }

    function drawRoutes() {
      routeLayer.clearLayers();
      if (displayMode.value !== "Routes") return;
      for (const contribution of aggregationForSelection().contributions) {
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

    function formatRangeNumber(value, digits) {
      return Number.isFinite(value) ? value.toFixed(digits) : "n/a";
    }

    function metricRangeLabel(view, metric) {
      if (metric === "Frequency") return `frequency log scale, 98th pct ${Math.round(view.scaleMax)} samples`;
      if (metric === "Uphill/Downhill") return "green downhill, purple uphill";
      const range = view.metricRanges[metric];
      if (!range || !range.available) return `${metric}: no samples`;
      if (metric === "Speed") return `speed ${formatRangeNumber(range.low, 1)}-${formatRangeNumber(range.high, 1)} mph`;
      if (metric === "Heart Rate") return `heart rate ${Math.round(range.low)}-${Math.round(range.high)} bpm`;
      if (metric === "Steepness") return `steepness ${formatRangeNumber(range.low, 1)}-${formatRangeNumber(range.high, 1)}% grade`;
      return metric;
    }

    function updateStatus() {
      const view = aggregationForSelection();
      const label = payload.scope === "recent" ? `${payload.recent_days} days` : "all time";
      countLabel.textContent = `${view.contributions.length} activities`;
      status.textContent = view.contributions.length
        ? `${label}: ${view.contributions.length} mapped activities, ${view.cells.length} heat cells; ${metricRangeLabel(view, metricMode.value)}`
        : "No GPS stream data is available for this view.";
    }

    function syncLayers() {
      if (displayMode.value === "Heatmap") {
        routeLayer.clearLayers();
        if (!leafletMap.hasLayer(heatLayer)) heatLayer.addTo(leafletMap);
        heatLayer.redraw();
      } else {
        if (leafletMap.hasLayer(heatLayer)) leafletMap.removeLayer(heatLayer);
        drawRoutes();
      }
      updateStatus();
    }

    function redraw() {
      syncLayers();
    }

    function refreshHeatLayerSoon() {
      if (!leafletMap.hasLayer(heatLayer)) return;
      if (heatRefreshTimer !== null) window.clearTimeout(heatRefreshTimer);
      heatRefreshTimer = window.setTimeout(() => {
        heatRefreshTimer = null;
        if (leafletMap.hasLayer(heatLayer)) heatLayer.redraw();
      }, 80);
    }

    function setHeatPaneOpacity(value) {
      leafletMap.getPane("heatPane").style.opacity = String(value);
    }

    buildFilters();
    setInitialView();
    redraw();
    leafletMap.on("zoomstart", () => setHeatPaneOpacity(0.58));
    leafletMap.on("zoomend", () => setHeatPaneOpacity(1));
    leafletMap.on("zoomend", refreshHeatLayerSoon);
    activityFilter.addEventListener("change", () => {
      clearHeatTileCache();
      redraw();
    });
    displayMode.addEventListener("change", redraw);
    metricMode.addEventListener("change", () => {
      clearHeatTileCache();
      if (leafletMap.hasLayer(heatLayer)) heatLayer.redraw();
      updateStatus();
    });
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
