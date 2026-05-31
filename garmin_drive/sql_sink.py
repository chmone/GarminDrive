"""Optional Supabase/Postgres sink — mirror each sync's normalized rows into Body Compass's database.

This is **additive** to the Google Drive publish path: GarminDrive keeps writing the visible Drive
files (used elsewhere), and — when ``DATABASE_URL`` is set — also upserts the same normalized rows
into Postgres so Body Compass can read durable, query-ready data instead of parsing Drive files.

Design:
- **Best-effort.** Any failure (missing dep, unreachable DB, bad row) is logged and swallowed so a
  sync's Drive output is never blocked by the sink.
- **Idempotent.** Every table is keyed by ``(user_id, …)`` and written with ``INSERT … ON CONFLICT
  DO UPDATE``. Because GarminDrive's ``merge_run_history`` / ``merge_health_history`` already hold the
  *full* history in memory, a single sink-enabled sync populates the whole dataset; re-runs are no-ops
  at the data level.
- **Migration-proof.** Each table carries a ``raw`` jsonb column holding the entire normalized row, so
  a new/rare source field never requires a schema change — the read side can fall back to ``raw``.
- **Single writer.** This sync is the only writer; reads happen in the Body Compass app.

Connection: ``psycopg`` (v3) against the Supabase **session pooler** URI in ``DATABASE_URL``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Sequence

from .config import Settings

log = logging.getLogger("garmin_drive.sql_sink")

# (column, source-key) pairs per table. The row's full dict also lands in `raw` jsonb, so unmapped
# fields are never lost. Keep these names aligned with the garmin-drive-data contract.
RUN_COLUMNS: list[tuple[str, str]] = [
    ("source_activity_id", "source_activity_id"), ("local_date", "local_date"),
    ("sport_type", "sport_type"), ("distance_miles", "distance_miles"),
    ("distance_kilometers", "distance_kilometers"), ("moving_time_seconds", "moving_time_seconds"),
    ("elapsed_time_seconds", "elapsed_time_seconds"), ("pace_seconds_per_mile", "pace_seconds_per_mile"),
    ("average_heartrate", "average_heartrate"), ("max_heartrate", "max_heartrate"),
    ("average_cadence", "average_cadence"), ("elevation_gain_feet", "elevation_gain_feet"),
    ("elevation_gain_meters", "elevation_gain_meters"), ("average_speed_mps", "average_speed_mps"),
    ("max_speed_mps", "max_speed_mps"), ("calories", "calories"),
    ("route_available", "route_available"), ("mile_split_count", "mile_split_count"),
    ("name", "name"), ("timezone", "timezone"), ("start_date_local", "start_date_local"),
    ("source", "source"), ("strava_activity_url", "strava_activity_url"),
    ("route_geojson_path", "route_geojson_path"), ("raw_data_path", "raw_data_path"),
]

SPLIT_COLUMNS: list[tuple[str, str]] = [
    ("source_activity_id", "source_activity_id"), ("split_index", "split_index"),
    ("local_date", "local_date"), ("split_type", "split_type"), ("source", "source"),
    ("distance_miles", "distance_miles"), ("moving_time", "moving_time"),
    ("pace_per_mile", "pace_per_mile"), ("average_heartrate", "average_heartrate"),
    ("max_heartrate", "max_heartrate"), ("elevation_gain_feet", "elevation_gain_feet"),
    ("elevation_loss_feet", "elevation_loss_feet"),
    ("net_elevation_change_feet", "net_elevation_change_feet"),
    ("average_cadence", "average_cadence"), ("average_grade", "average_grade"),
    ("route_available", "route_available"), ("name", "name"),
    ("strava_activity_url", "strava_activity_url"),
]

# Health mirrors the daily CSV faithfully (incl. the device-empty columns) so the read side is a
# drop-in for the CSV parse. `available_metrics`/`metric_errors` are stored as text like the CSV.
HEALTH_COLUMNS: list[tuple[str, str]] = [
    ("date", "date"), ("resting_hr", "resting_hr"), ("avg_hr", "avg_hr"),
    ("min_hr", "min_hr"), ("max_hr", "max_hr"), ("avg_stress", "avg_stress"),
    ("max_stress", "max_stress"), ("body_battery_start", "body_battery_start"),
    ("body_battery_end", "body_battery_end"), ("body_battery_min", "body_battery_min"),
    ("body_battery_max", "body_battery_max"), ("sleep_duration_hours", "sleep_duration_hours"),
    ("sleep_score", "sleep_score"), ("hrv_avg", "hrv_avg"), ("hrv_status", "hrv_status"),
    ("respiration_avg", "respiration_avg"), ("spo2_avg", "spo2_avg"),
    ("training_readiness_score", "training_readiness_score"),
    ("available_metrics", "available_metrics"), ("metric_errors", "metric_errors"),
    ("fetched_at", "fetched_at"),
]

ROUTE_COLUMNS = [
    "source_activity_id", "local_date", "sport_type", "name", "source",
    "distance_miles", "start_date", "start_date_local", "start_lat", "start_lon",
]

SCHEMA_SQL = """
create table if not exists health (
  user_id text not null,
  date date not null,
  resting_hr double precision, avg_hr double precision,
  min_hr double precision, max_hr double precision,
  avg_stress double precision, max_stress double precision,
  body_battery_start double precision, body_battery_end double precision,
  body_battery_min double precision, body_battery_max double precision,
  sleep_duration_hours double precision, sleep_score double precision,
  hrv_avg double precision, hrv_status text,
  respiration_avg double precision, spo2_avg double precision,
  training_readiness_score double precision,
  available_metrics text, metric_errors text, fetched_at text,
  raw jsonb,
  primary key (user_id, date)
);
create table if not exists runs (
  user_id text not null,
  source_activity_id text not null,
  local_date date, sport_type text,
  distance_miles double precision, distance_kilometers double precision,
  moving_time_seconds double precision, elapsed_time_seconds double precision,
  pace_seconds_per_mile double precision,
  average_heartrate double precision, max_heartrate double precision, average_cadence double precision,
  elevation_gain_feet double precision, elevation_gain_meters double precision,
  average_speed_mps double precision, max_speed_mps double precision,
  calories double precision, route_available boolean, mile_split_count integer,
  name text, timezone text, start_date_local text, source text,
  strava_activity_url text, route_geojson_path text, raw_data_path text,
  raw jsonb,
  primary key (user_id, source_activity_id)
);
create table if not exists splits (
  user_id text not null,
  source_activity_id text not null,
  split_index integer not null,
  local_date date, split_type text, source text,
  distance_miles double precision, moving_time text, pace_per_mile text,
  average_heartrate double precision, max_heartrate double precision,
  elevation_gain_feet double precision, elevation_loss_feet double precision,
  net_elevation_change_feet double precision, average_cadence double precision,
  average_grade double precision, route_available boolean,
  name text, strava_activity_url text,
  raw jsonb,
  primary key (user_id, source_activity_id, split_index)
);
create table if not exists routes (
  user_id text not null,
  source_activity_id text not null,
  local_date date, sport_type text, name text, source text,
  distance_miles double precision, start_date text, start_date_local text,
  start_lat double precision, start_lon double precision,
  geometry jsonb not null,
  primary key (user_id, source_activity_id)
);
create table if not exists ingest_meta (
  user_id text not null, source text not null,
  last_ingested_at timestamptz default now(), row_count integer,
  primary key (user_id, source)
);
"""


def _psycopg():
    try:
        import psycopg  # noqa: PLC0415 — lazy so the dep is optional
        from psycopg.types.json import Jsonb  # noqa: PLC0415
        return psycopg, Jsonb
    except ImportError:  # pragma: no cover
        log.warning("psycopg not installed; SQL sink disabled. Add psycopg[binary] to requirements.")
        return None, None


def _norm(value: Any) -> Any:
    """Empty strings → NULL (so a missing date/number doesn't fail a typed column)."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _as_text(value: Any) -> Any:
    """Render list/dict values (available_metrics, metric_errors) as the CSV-style text the app reads."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True) if value else ""
    return value


def _upsert(cur, Jsonb, table: str, columns: Sequence[str], conflict: Sequence[str],
            params: list[tuple]) -> int:
    if not params:
        return 0
    cols = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in columns if c not in conflict)
    sql = (
        f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) '
        f'ON CONFLICT ({", ".join(conflict)}) DO UPDATE SET {updates}'
    )
    cur.executemany(sql, params)
    return len(params)


# --- row builders ------------------------------------------------------------

def _run_params(runs: Iterable[dict], user_id: str, Jsonb) -> list[tuple]:
    out = []
    for run in runs:
        if not isinstance(run, dict) or not run.get("source_activity_id"):
            continue
        vals = [user_id] + [_norm(run.get(key)) for _, key in RUN_COLUMNS] + [Jsonb(run)]
        out.append(tuple(vals))
    return out


def _split_params(rows: Iterable[dict], user_id: str, Jsonb) -> list[tuple]:
    out, counters = [], {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("source_activity_id"):
            continue
        aid = str(row["source_activity_id"])
        # split_index should be present; fall back to per-activity order so the PK is always satisfied.
        idx = row.get("split_index")
        if idx is None:
            idx = counters.get(aid, 0)
        counters[aid] = int(idx) + 1
        row = {**row, "split_index": int(idx)}
        vals = [user_id] + [_norm(row.get(key)) for _, key in SPLIT_COLUMNS] + [Jsonb(row)]
        out.append(tuple(vals))
    return out


def _health_params(days: Iterable[dict], user_id: str, Jsonb) -> list[tuple]:
    out = []
    for day in days:
        if not isinstance(day, dict) or not _norm(day.get("date")):
            continue
        vals = [user_id]
        for _, key in HEALTH_COLUMNS:
            v = day.get(key)
            vals.append(_as_text(v) if key in ("available_metrics", "metric_errors") else _norm(v))
        vals.append(Jsonb(day))
        out.append(tuple(vals))
    return out


def _route_params(features: Iterable[dict], user_id: str, Jsonb) -> list[tuple]:
    out = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        aid = props.get("source_activity_id") or feat.get("id")
        coords = geom.get("coordinates") or []
        if not aid or len(coords) < 2:
            continue
        start_lon, start_lat = (coords[0][0], coords[0][1]) if coords and len(coords[0]) >= 2 else (None, None)
        vals = [
            user_id, str(aid), _norm(props.get("local_date")), props.get("sport_type"),
            props.get("name"), props.get("source"), props.get("distance_miles"),
            props.get("start_date"), props.get("start_date_local"), start_lat, start_lon,
            Jsonb(geom),
        ]
        out.append(tuple(vals))
    return out


# --- public API --------------------------------------------------------------

def sync_runs(settings: Settings, runs: list[dict], route_features: dict | None = None) -> None:
    """Upsert runs + their mile splits (+ routes when a fresh route collection is available)."""
    if not settings.sql_sink_enabled:
        return
    from .corpus import all_mile_split_rows  # noqa: PLC0415 — avoid a cycle at import time
    psycopg, Jsonb = _psycopg()
    if psycopg is None:
        return
    uid = settings.bodycompass_user_id
    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                n_runs = _upsert(cur, Jsonb, "runs",
                                 ["user_id"] + [c for c, _ in RUN_COLUMNS] + ["raw"],
                                 ["user_id", "source_activity_id"],
                                 _run_params(runs, uid, Jsonb))
                n_splits = _upsert(cur, Jsonb, "splits",
                                   ["user_id"] + [c for c, _ in SPLIT_COLUMNS] + ["raw"],
                                   ["user_id", "source_activity_id", "split_index"],
                                   _split_params(all_mile_split_rows(runs), uid, Jsonb))
                n_routes = 0
                if route_features and isinstance(route_features.get("features"), list):
                    n_routes = _upsert(cur, Jsonb, "routes",
                                       ["user_id"] + ROUTE_COLUMNS + ["geometry"],
                                       ["user_id", "source_activity_id"],
                                       _route_params(route_features["features"], uid, Jsonb))
                _write_meta(cur, uid, "strava", n_runs)
            conn.commit()
        log.info("SQL sink: upserted %s runs, %s splits, %s routes (user=%s).",
                 n_runs, n_splits, n_routes, uid)
    except Exception as exc:  # noqa: BLE001 — never let the sink break the Drive publish
        log.warning("SQL sink (runs) failed; Drive output is unaffected. (%s)", exc)


def sync_health(settings: Settings, days: list[dict]) -> None:
    if not settings.sql_sink_enabled:
        return
    psycopg, Jsonb = _psycopg()
    if psycopg is None:
        return
    uid = settings.bodycompass_user_id
    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                n = _upsert(cur, Jsonb, "health",
                            ["user_id"] + [c for c, _ in HEALTH_COLUMNS] + ["raw"],
                            ["user_id", "date"],
                            _health_params(days, uid, Jsonb))
                _write_meta(cur, uid, "garmin_health", n)
            conn.commit()
        log.info("SQL sink: upserted %s health days (user=%s).", n, uid)
    except Exception as exc:  # noqa: BLE001
        log.warning("SQL sink (health) failed; Drive output is unaffected. (%s)", exc)


def _write_meta(cur, user_id: str, source: str, row_count: int) -> None:
    cur.execute(
        'INSERT INTO ingest_meta (user_id, source, last_ingested_at, row_count) '
        'VALUES (%s, %s, now(), %s) ON CONFLICT (user_id, source) '
        'DO UPDATE SET last_ingested_at = excluded.last_ingested_at, row_count = excluded.row_count',
        (user_id, source, row_count),
    )
