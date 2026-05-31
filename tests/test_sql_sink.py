"""Unit tests for the Body Compass SQL sink row builders (no database required).

These verify the column mapping / value normalization that turns GarminDrive's normalized dicts into
upsert tuples. Live DB behaviour (connect + upsert idempotency) is exercised manually against Supabase
and is not part of CI.
"""
from __future__ import annotations

from garmin_drive import sql_sink

UID = "default"


# A tiny Jsonb stand-in so we don't depend on psycopg being importable in CI; the builders only need
# something to wrap the raw dict. The real Jsonb is supplied by sql_sink at runtime.
class _J:
    def __init__(self, obj):
        self.obj = obj


def test_run_params_maps_columns_and_keeps_raw():
    runs = [{
        "source_activity_id": "123", "local_date": "2026-05-30", "sport_type": "Run",
        "distance_miles": 5.0, "pace_seconds_per_mile": 475.2, "route_available": True,
        "name": "Morning Run", "extra_unknown_field": "kept-in-raw",
    }]
    params = sql_sink._run_params(runs, UID, _J)
    assert len(params) == 1
    row = params[0]
    assert row[0] == UID                          # user_id first
    assert "123" in row                           # source_activity_id mapped
    assert isinstance(row[-1], _J)                # raw jsonb last
    assert row[-1].obj["extra_unknown_field"] == "kept-in-raw"   # unmapped field preserved in raw


def test_run_params_skips_rows_without_activity_id():
    assert sql_sink._run_params([{"name": "no id"}], UID, _J) == []


def test_split_params_fills_missing_split_index_per_activity():
    rows = [
        {"source_activity_id": "a", "distance_miles": 1.0},   # no split_index -> 0
        {"source_activity_id": "a", "distance_miles": 1.0},   # -> 1
        {"source_activity_id": "b", "split_index": 5},        # explicit
    ]
    params = sql_sink._split_params(rows, UID, _J)
    # column order: user_id, then SPLIT_COLUMNS (source_activity_id, split_index, ...)
    idx_pos = 1 + [c for c, _ in sql_sink.SPLIT_COLUMNS].index("split_index")
    got = [(r[1], r[idx_pos]) for r in params]    # (source_activity_id, split_index)
    assert got == [("a", 0), ("a", 1), ("b", 5)]


def test_health_params_stringifies_list_and_dict_and_nulls_empty_date():
    days = [
        {"date": "2026-05-30", "resting_hr": 44,
         "available_metrics": ["stress", "sleep"], "metric_errors": {"hrv": "missing"}},
        {"date": "", "resting_hr": 50},           # empty date -> skipped
    ]
    params = sql_sink._health_params(days, UID, _J)
    assert len(params) == 1
    cols = ["user_id"] + [c for c, _ in sql_sink.HEALTH_COLUMNS]
    row = dict(zip(cols, params[0]))
    assert row["available_metrics"] == "stress,sleep"        # list -> comma text (CSV-style)
    assert row["metric_errors"] == '{"hrv": "missing"}'      # dict -> json text


def test_route_params_extracts_start_point_and_skips_short_lines():
    features = [
        {"id": "r1", "properties": {"source_activity_id": "r1", "local_date": "2026-05-30"},
         "geometry": {"type": "LineString", "coordinates": [[-71.1, 42.3], [-71.2, 42.4]]}},
        {"id": "r2", "properties": {"source_activity_id": "r2"},
         "geometry": {"type": "LineString", "coordinates": [[-71.1, 42.3]]}},   # <2 pts -> skipped
    ]
    params = sql_sink._route_params(features, UID, _J)
    assert len(params) == 1
    row = params[0]
    assert row[1] == "r1"
    # start_lat/start_lon are columns 9 and 10 (user_id + ROUTE_COLUMNS up to start_lon)
    cols = ["user_id"] + sql_sink.ROUTE_COLUMNS
    d = dict(zip(cols, row))
    assert d["start_lon"] == -71.1 and d["start_lat"] == 42.3   # coords are [lon, lat]
    assert isinstance(row[-1], _J)                              # geometry jsonb last


def test_health_params_flags_today_as_partial():
    days = [{"date": "2026-05-31"}, {"date": "2026-05-30"}]
    params = sql_sink._health_params(days, UID, _J, today="2026-05-31", synced_at="2026-05-31T12:00:00Z")
    # tail order is ... is_partial, last_synced_at, raw
    today_row, past_row = params[0], params[1]
    assert today_row[-3] is True and past_row[-3] is False
    assert today_row[-2] == "2026-05-31T12:00:00Z"
    assert isinstance(today_row[-1], _J)


def test_run_detail_params_pulls_full_activity_splits_and_route():
    archives = [{
        "source_activity_id": "999", "schema_version": 1, "fetched_at": "2026-05-30T10:00:00Z",
        "activity": {"id": 999, "name": "Long Run", "sport_type": "Run", "suffer_score": 88,
                     "start_date_local": "2026-05-30T07:00:00Z"},
        "mile_splits": [{"split_index": 1}],
        "route": {"type": "Feature", "properties": {"local_date": "2026-05-30"}},
    }]
    params = sql_sink._run_detail_params(archives, UID, _J)
    cols = ["user_id", "source_activity_id", "local_date", "name", "sport_type",
            "schema_version", "fetched_at", "activity", "mile_splits", "route"]
    row = dict(zip(cols, params[0]))
    assert row["source_activity_id"] == "999"
    assert row["local_date"] == "2026-05-30"          # from route properties
    assert row["name"] == "Long Run"
    assert row["activity"].obj["suffer_score"] == 88  # full activity preserved in jsonb
    assert row["route"].obj["type"] == "Feature"


def test_run_detail_local_date_falls_back_to_activity_start():
    archives = [{"source_activity_id": "1", "activity": {"id": 1, "start_date_local": "2026-01-02T06:00:00Z"}}]
    params = sql_sink._run_detail_params(archives, UID, _J)
    assert params[0][2] == "2026-01-02"               # local_date derived from start_date_local


def test_run_stream_params_keeps_streams_and_skips_empty():
    archives = [
        {"source_activity_id": "5", "stream_types": ["time", "heartrate"], "stream_sample_count": 2,
         "activity": {"id": 5, "start_date_local": "2026-05-30T07:00:00Z"},
         "streams": {"time": {"data": [0, 1]}, "heartrate": {"data": [120, 130]}}},
        {"source_activity_id": "6", "activity": {"id": 6}, "streams": {}},  # no streams -> skipped
    ]
    params = sql_sink._run_stream_params(archives, UID, _J)
    assert len(params) == 1
    cols = ["user_id", "source_activity_id", "local_date", "stream_types", "sample_count",
            "streams", "fetched_at"]
    row = dict(zip(cols, params[0]))
    assert row["stream_types"] == ["time", "heartrate"]   # list adapts to text[]
    assert row["sample_count"] == 2
    assert row["streams"].obj["heartrate"]["data"] == [120, 130]


def test_health_intraday_params_flags_partial_and_wraps_series():
    rows = [{"date": "2026-05-31", "hr_series": [[1, 60]], "sample_counts": {"hr": 1}}]
    params = sql_sink._health_intraday_params(rows, UID, _J, today="2026-05-31", synced_at="now")
    row = params[0]
    assert row[0] == UID and row[1] == "2026-05-31"
    assert row[2] is True and row[3] == "now"             # is_partial, last_synced_at
    assert isinstance(row[4], _J) and row[4].obj == [[1, 60]]   # hr_series jsonb


def test_current_status_params_single_row_with_snapshot():
    status = {"as_of_date": "2026-05-31", "latest_hr": 72, "current_body_battery": 55,
              "is_partial": True, "steps": 8000}
    params = sql_sink._current_status_params(status, UID, _J)
    assert len(params) == 1
    row = params[0]
    assert row[0] == UID and row[1] == "2026-05-31" and row[2] == 72
    assert isinstance(row[-1], _J) and row[-1].obj["steps"] == 8000
    assert sql_sink._current_status_params(None, UID, _J) == []
