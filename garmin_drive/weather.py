"""Per-run weather lookup via the Open-Meteo archive API (free, no key required).

Body Compass uses per-run conditions to de-confound heat effects on HR/pace, so for each outdoor run
we fetch the historical hourly weather at the run's start location + date, pick the hour nearest the
run's start time, and return metric values. This module only does the *fetch + pick*; persistence
lives in ``sql_sink`` (the ``weather`` table).

Design:
- **Free + key-less.** ``https://archive-api.open-meteo.com/v1/archive`` with ``timezone=auto`` and
  metric units (temperature °C, wind km/h).
- **Cached by (lat, lon, date).** A backfill over many runs that start at the same place/day hits the
  API once. The cache lives for the process; repeated cron invocations skip already-stored runs at the
  ``sql_sink`` layer instead, so the API isn't re-hit for history every tick.
- **Rate-limit aware.** 429 / 5xx are retried with exponential backoff; a small polite delay spaces
  out real network calls during a large backfill.
- **Best-effort.** Any failure returns ``None`` so the caller simply leaves weather absent for that
  run (never invents values).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("garmin_drive.weather")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_SOURCE = "open-meteo-archive"

# Hourly variables requested from Open-Meteo (metric).
HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    "wind_speed_10m",
]

# Open-Meteo hourly variable -> our weather-dict / column key.
_VAR_TO_FIELD = {
    "temperature_2m": "temperature_c",
    "apparent_temperature": "apparent_temperature_c",
    "relative_humidity_2m": "relative_humidity_pct",
    "dew_point_2m": "dew_point_c",
    "wind_speed_10m": "wind_speed_kmh",
}

_TIMEOUT = 30
_MAX_RETRIES = 4
_POLITE_DELAY = 0.1  # seconds between real (uncached) API calls, to be a good citizen on backfills

# Process-local cache: (lat, lon, date) -> raw Open-Meteo response (or None when it failed/empty).
_CACHE: dict[tuple, dict | None] = {}


def clear_cache() -> None:
    """Drop the in-process response cache (used by tests)."""
    _CACHE.clear()


def _cache_key(lat: float, lon: float, local_date: str) -> tuple:
    # ~100 m resolution collapses near-identical run starts onto one API call.
    return (round(float(lat), 3), round(float(lon), 3), str(local_date))


def _request_archive(lat: float, lon: float, local_date: str) -> dict | None:
    """Fetch one day of hourly archive weather for a point. Retries 429/5xx with backoff."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": local_date,
        "end_date": local_date,
        "timezone": "auto",
        "hourly": ",".join(HOURLY_VARS),
        "wind_speed_unit": "kmh",
    }
    delay = 2
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(ARCHIVE_URL, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:  # noqa: PERF203 — network is the slow path anyway
            log.warning("Open-Meteo request error (%s); attempt %s/%s", exc, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            log.warning("Open-Meteo HTTP %s (rate/again); backing off %ss", resp.status_code, delay)
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code != 200:
            log.warning("Open-Meteo HTTP %s for (%s, %s, %s); skipping.", resp.status_code, lat, lon, local_date)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("Open-Meteo returned non-JSON for (%s, %s, %s); skipping.", lat, lon, local_date)
            return None
        if _POLITE_DELAY:
            time.sleep(_POLITE_DELAY)
        return data
    log.warning("Open-Meteo exhausted retries for (%s, %s, %s); skipping.", lat, lon, local_date)
    return None


def _target_hour(start_date_local: str | None) -> int:
    """Hour-of-day (0-23) nearest the run's start; fall back to local midday (12) if unknown.

    ``start_date_local`` is a local-time ISO string like ``2026-05-30T07:15:00``; we round to the
    nearest hour. Anything unparseable falls back to midday.
    """
    if isinstance(start_date_local, str) and len(start_date_local) >= 16:
        try:
            hh = int(start_date_local[11:13])
            mm = int(start_date_local[14:16])
            return min(23, hh + (1 if mm >= 30 else 0))
        except ValueError:
            pass
    return 12


def _pick_index(times: list, start_date_local: str | None) -> int:
    """Index into the hourly arrays whose hour is closest to the run's start hour."""
    target = _target_hour(start_date_local)
    best_i, best_d = 0, 1 << 30
    for i, t in enumerate(times):
        try:
            hh = int(str(t)[11:13])
        except (ValueError, IndexError):
            continue
        d = abs(hh - target)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def fetch_run_weather(
    lat: float | None,
    lon: float | None,
    local_date: str | None,
    start_date_local: str | None = None,
    tz: str | None = None,
) -> dict | None:
    """Return the run's start-time weather as a metric dict, or ``None`` if unavailable.

    Keys: ``temperature_c``, ``apparent_temperature_c``, ``relative_humidity_pct``, ``dew_point_c``,
    ``wind_speed_kmh``, plus ``weather_source`` / ``fetched_at`` and selection metadata for ``raw``.
    """
    if lat is None or lon is None or not local_date:
        return None

    key = _cache_key(lat, lon, local_date)
    if key in _CACHE:
        base = _CACHE[key]
    else:
        base = _request_archive(lat, lon, local_date)
        _CACHE[key] = base
    if not base:
        return None

    hourly = base.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None

    i = _pick_index(times, start_date_local)
    out: dict = {
        "weather_source": WEATHER_SOURCE,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    for var, field in _VAR_TO_FIELD.items():
        series = hourly.get(var) or []
        out[field] = series[i] if i < len(series) else None
    # Selection metadata — preserved in the weather row's `raw` jsonb (migration-proof / debuggable).
    out["latitude"] = base.get("latitude")
    out["longitude"] = base.get("longitude")
    out["api_timezone"] = base.get("timezone")
    out["requested_timezone"] = tz
    out["selected_time"] = times[i] if i < len(times) else None
    out["selected_index"] = i
    out["hourly_units"] = base.get("hourly_units")
    return out
