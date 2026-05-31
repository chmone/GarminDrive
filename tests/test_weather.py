"""Unit tests for per-run weather: the Open-Meteo selection logic + the sql_sink weather helpers.

No network or database is required — the Open-Meteo request is monkeypatched, and the sql_sink row
builders are pure functions over dicts.
"""
from __future__ import annotations

from garmin_drive import sql_sink, weather

UID = "default"


# A tiny Jsonb stand-in (the real one is psycopg's; the builders only need something to wrap a dict).
class _J:
    def __init__(self, obj):
        self.obj = obj


# Two hours of fake archive data: 06:00 and 07:00 local.
_FAKE_ARCHIVE = {
    "latitude": 42.35,
    "longitude": -71.1,
    "timezone": "America/New_York",
    "hourly_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
    "hourly": {
        "time": ["2026-05-30T06:00", "2026-05-30T07:00"],
        "temperature_2m": [12.0, 14.0],
        "relative_humidity_2m": [80.0, 70.0],
        "dew_point_2m": [9.0, 8.5],
        "apparent_temperature": [11.0, 13.5],
        "wind_speed_10m": [5.0, 9.0],
    },
}


def _patch_archive(monkeypatch, calls=None):
    def fake(lat, lon, local_date):
        if calls is not None:
            calls.append((lat, lon, local_date))
        return _FAKE_ARCHIVE
    weather.clear_cache()
    monkeypatch.setattr(weather, "_request_archive", fake)


# --- weather module ---------------------------------------------------------

def test_target_hour_rounds_to_nearest_and_falls_back_to_midday():
    assert weather._target_hour("2026-05-30T07:15:00") == 7
    assert weather._target_hour("2026-05-30T07:30:00") == 8   # >=30 min rounds up
    assert weather._target_hour("2026-05-30T23:45:00") == 23  # clamped to 23
    assert weather._target_hour(None) == 12                   # missing start -> local midday
    assert weather._target_hour("garbage") == 12


def test_fetch_run_weather_picks_hour_nearest_start_and_returns_metric(monkeypatch):
    _patch_archive(monkeypatch)
    w = weather.fetch_run_weather(42.35, -71.1, "2026-05-30", start_date_local="2026-05-30T07:05:00")
    assert w["temperature_c"] == 14.0          # 07:00 row chosen
    assert w["apparent_temperature_c"] == 13.5
    assert w["relative_humidity_pct"] == 70.0
    assert w["dew_point_c"] == 8.5
    assert w["wind_speed_kmh"] == 9.0
    assert w["weather_source"] == "open-meteo-archive"
    assert w["selected_time"] == "2026-05-30T07:00"


def test_fetch_run_weather_midday_fallback_when_start_missing(monkeypatch):
    _patch_archive(monkeypatch)
    # Target hour 12 -> closest available is 07:00 (index 1).
    w = weather.fetch_run_weather(42.35, -71.1, "2026-05-30", start_date_local=None)
    assert w["selected_time"] == "2026-05-30T07:00"


def test_fetch_run_weather_caches_by_lat_lon_date(monkeypatch):
    calls: list = []
    _patch_archive(monkeypatch, calls)
    weather.fetch_run_weather(42.35001, -71.10001, "2026-05-30", start_date_local="2026-05-30T07:00:00")
    weather.fetch_run_weather(42.35002, -71.10002, "2026-05-30", start_date_local="2026-05-30T06:00:00")
    assert len(calls) == 1                      # rounded to ~100 m -> one API call


def test_fetch_run_weather_returns_none_without_coords(monkeypatch):
    _patch_archive(monkeypatch)
    assert weather.fetch_run_weather(None, -71.1, "2026-05-30") is None
    assert weather.fetch_run_weather(42.3, None, "2026-05-30") is None
    assert weather.fetch_run_weather(42.3, -71.1, None) is None


# --- sql_sink weather helpers ----------------------------------------------

def test_weather_params_maps_columns_and_keeps_raw():
    rows = [{
        "source_activity_id": "123", "local_date": "2026-05-30",
        "temperature_c": 14.0, "apparent_temperature_c": 13.5, "relative_humidity_pct": 70.0,
        "dew_point_c": 8.5, "wind_speed_kmh": 9.0, "weather_source": "open-meteo-archive",
        "fetched_at": "2026-05-30T11:00:00Z", "selected_index": 1,
    }]
    params = sql_sink._weather_params(rows, UID, _J)
    assert len(params) == 1
    cols = ["user_id"] + [c for c, _ in sql_sink.WEATHER_COLUMNS]
    d = dict(zip(cols, params[0]))
    assert d["user_id"] == UID
    assert d["source_activity_id"] == "123"
    assert d["temperature_c"] == 14.0 and d["wind_speed_kmh"] == 9.0
    assert isinstance(params[0][-1], _J)
    assert params[0][-1].obj["selected_index"] == 1   # unmapped metadata preserved in raw


def test_weather_params_skips_rows_without_activity_id():
    assert sql_sink._weather_params([{"temperature_c": 10.0}], UID, _J) == []


def test_start_coords_from_route_features_and_archives():
    route_features = {"features": [
        {"properties": {"source_activity_id": "r1"},
         "geometry": {"coordinates": [[-71.1, 42.3], [-71.2, 42.4]]}},
    ]}
    archives = [
        {"route": {"properties": {"source_activity_id": "r2"},
                   "geometry": {"coordinates": [[-72.0, 41.0], [-72.1, 41.1]]}}},
    ]
    coords = sql_sink._start_coords_by_activity(route_features, archives)
    assert coords["r1"] == (42.3, -71.1)   # (lat, lon) from [lon, lat]
    assert coords["r2"] == (41.0, -72.0)


def test_weather_candidates_skips_indoor_and_missing_coords():
    coords = {"a": (42.3, -71.1), "b": (40.0, -70.0)}
    runs = [
        {"source_activity_id": "a", "sport_type": "Run", "local_date": "2026-05-30",
         "start_date_local": "2026-05-30T07:00:00", "timezone": "America/New_York"},
        {"source_activity_id": "b", "sport_type": "VirtualRun", "local_date": "2026-05-30"},   # virtual
        {"source_activity_id": "c", "sport_type": "Run", "local_date": "2026-05-30"},          # no coords
        {"source_activity_id": "t", "sport_type": "Treadmill", "local_date": "2026-05-30"},    # treadmill
    ]
    cands = sql_sink._weather_candidates(runs, coords)
    assert [c["source_activity_id"] for c in cands] == ["a"]
    assert cands[0]["lat"] == 42.3 and cands[0]["lon"] == -71.1


def test_weather_candidate_local_date_falls_back_to_start():
    coords = {"a": (42.3, -71.1)}
    runs = [{"source_activity_id": "a", "sport_type": "Run",
             "start_date_local": "2026-01-02T06:30:00"}]
    cands = sql_sink._weather_candidates(runs, coords)
    assert cands[0]["local_date"] == "2026-01-02"


def test_weather_in_sink_tables():
    assert "weather" in sql_sink.SINK_TABLES
