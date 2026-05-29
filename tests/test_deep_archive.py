from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from garmin_drive.__main__ import activity_sort_key, has_named_raw_replacement
from garmin_drive.corpus import render_corpus
from garmin_drive.deep_archive import (
    MILE_METERS,
    build_route_feature,
    decode_polyline,
    derive_mile_splits,
    filter_routes_for_activity_ids,
    merge_route_features,
    raw_run_relative_path,
    route_relative_path,
)
from garmin_drive.render import is_run
from garmin_drive.strava import StravaClient, StravaRequestBudgetExceeded, parse_limit_header


class DeepArchiveTests(unittest.TestCase):
    def test_stream_mile_splits_include_hr_and_elevation(self) -> None:
        activity = {
            "id": 123,
            "name": "Morning Run",
            "sport_type": "Run",
            "start_date_local": "2026-05-20T07:00:00Z",
            "distance": MILE_METERS * 1.5,
        }
        streams = {
            "distance": {"data": [0, 800, MILE_METERS, MILE_METERS * 1.5]},
            "time": {"data": [0, 300, 600, 900]},
            "moving": {"data": [True, True, True, True]},
            "heartrate": {"data": [140, 145, 150, 155]},
            "altitude": {"data": [100, 110, 105, 120]},
            "cadence": {"data": [82, 84, 86, 88]},
            "grade_smooth": {"data": [0.1, 0.2, -0.1, 0.3]},
            "latlng": {"data": [[40.0, -75.0], [40.1, -75.1], [40.2, -75.2], [40.3, -75.3]]},
        }

        splits = derive_mile_splits(activity, streams)

        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[0]["split_type"], "mile")
        self.assertEqual(splits[0]["moving_time_seconds"], 600)
        self.assertEqual(splits[0]["pace_per_mile"], "10:00/mi")
        self.assertEqual(splits[0]["max_heartrate"], 150)
        self.assertGreater(splits[0]["elevation_gain_feet"], 0)
        self.assertEqual(splits[1]["split_type"], "partial_mile")
        self.assertTrue(splits[1]["route_available"])

    def test_missing_streams_fall_back_to_strava_splits(self) -> None:
        activity = {
            "id": 123,
            "name": "Morning Run",
            "sport_type": "Run",
            "start_date_local": "2026-05-20T07:00:00Z",
            "splits_standard": [
                {"distance": MILE_METERS, "moving_time": 480, "elapsed_time": 500, "elevation_difference": 3.0}
            ],
        }

        splits = derive_mile_splits(activity, {})

        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0]["source"], "strava_split")
        self.assertEqual(splits[0]["pace_per_mile"], "8:00/mi")

    def test_route_feature_prefers_latlng_stream_with_altitude(self) -> None:
        activity = {
            "id": 123,
            "name": "Morning Run",
            "sport_type": "Run",
            "start_date_local": "2026-05-20T07:00:00Z",
            "distance": MILE_METERS,
        }
        streams = {
            "latlng": {"data": [[40.0, -75.0], [40.1, -75.1]]},
            "altitude": {"data": [100.0, 101.0]},
        }

        route = build_route_feature(activity, streams)

        self.assertIsNotNone(route)
        self.assertEqual(route["geometry"]["coordinates"][0], [-75.0, 40.0, 100.0])

    def test_polyline_decode_and_route_merge(self) -> None:
        points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(points[0], (38.5, -120.2))

        existing = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "1",
                    "properties": {"source_activity_id": "1", "local_date": "2026-01-01"},
                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                }
            ],
        }
        new = {
            "type": "Feature",
            "id": "2",
            "properties": {"source_activity_id": "2", "local_date": "2026-01-02"},
            "geometry": {"type": "LineString", "coordinates": [[2, 2], [3, 3]]},
        }

        merged = merge_route_features(existing, [new])
        filtered = filter_routes_for_activity_ids(merged, {"2"})

        self.assertEqual(len(merged["features"]), 2)
        self.assertEqual(len(filtered["features"]), 1)
        self.assertEqual(filtered["features"][0]["id"], "2")

    def test_parse_limit_header(self) -> None:
        self.assertEqual(parse_limit_header("100,1000"), (100, 1000))
        self.assertIsNone(parse_limit_header("nope"))

    def test_strava_client_request_budget(self) -> None:
        class Response:
            status_code = 200
            headers = {
                "X-ReadRateLimit-Limit": "100,1000",
                "X-ReadRateLimit-Usage": "1,1",
            }

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {"ok": "yes"}

        client = StravaClient(
            "123",
            "secret",
            token={"access_token": "token", "expires_at": 9999999999},
            request_budget=1,
            sleep_on_rate_limit=False,
        )

        with patch("garmin_drive.strava.requests.get", return_value=Response()):
            self.assertEqual(client.get("/athlete"), {"ok": "yes"})
            with self.assertRaises(StravaRequestBudgetExceeded):
                client.get("/athlete")

    def test_render_corpus_keeps_main_json_compact(self) -> None:
        today = date.today().isoformat()
        run = {
            "source_activity_id": "123",
            "local_date": today,
            "start_date_local": f"{today}T07:00:00Z",
            "name": "Morning Run",
            "distance_miles": 1.0,
            "moving_time_seconds": 600,
            "moving_time": "10:00",
            "pace_per_mile": "10:00/mi",
            "mile_splits": [{"split_index": 1, "distance_miles": 1.0, "pace_per_mile": "10:00/mi"}],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            render_corpus([run], Path(temp_dir), markdown_as_google_docs=False)
            main_payload = json.loads((Path(temp_dir) / "Run History Data.json").read_text(encoding="utf-8"))
            recent_payload = json.loads((Path(temp_dir) / "Recent Mile Splits.json").read_text(encoding="utf-8"))
            old_map_exists = (Path(temp_dir) / "Recent Run Map.html").exists()

        self.assertNotIn("mile_splits", main_payload["runs"][0])
        self.assertEqual(main_payload["runs"][0]["mile_split_count"], 1)
        self.assertEqual(recent_payload["split_count"], 1)
        self.assertFalse(old_map_exists)

    def test_default_activity_filter_includes_runs_and_bikes(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(is_run({"sport_type": "Run"}))
            self.assertTrue(is_run({"sport_type": "TrailRun"}))
            self.assertTrue(is_run({"sport_type": "Treadmill"}))
            self.assertTrue(is_run({"sport_type": "Ride"}))
            self.assertTrue(is_run({"sport_type": "VirtualRide"}))
            self.assertFalse(is_run({"sport_type": "Swim"}))

    def test_activity_sort_key_prioritizes_newest_for_enrichment(self) -> None:
        activities = [
            {"id": 1, "start_date_local": "2024-01-01T10:00:00Z"},
            {"id": 2, "start_date_local": "2026-01-01T10:00:00Z"},
            {"id": 3, "start_date_local": "2025-01-01T10:00:00Z"},
        ]

        sorted_ids = [activity["id"] for activity in sorted(activities, key=activity_sort_key, reverse=True)]

        self.assertEqual(sorted_ids, [2, 3, 1])

    def test_raw_paths_include_date_name_and_id(self) -> None:
        activity = {
            "id": 123,
            "name": "Evening Run!",
            "sport_type": "Run",
            "start_date_local": "2026-05-27T18:00:00Z",
        }

        self.assertEqual(
            raw_run_relative_path("2026", activity, "123"),
            "Raw Data/Runs/2026/2026-05-27_run-evening-run_123.json",
        )
        self.assertEqual(
            route_relative_path("2026", activity, "123"),
            "Raw Data/Routes/2026/2026-05-27_run-evening-run_123.geojson",
        )

    def test_trash_guard_requires_named_replacement(self) -> None:
        manifest = {
            "files": {
                "Raw Data/Runs/2026/2026-05-27_run-evening-run_123.json": {},
                "Raw Data/Routes/2026/2026-05-27_run-evening-run_123.geojson": {},
            }
        }

        self.assertTrue(has_named_raw_replacement(manifest, "Runs", "2026", "123"))
        self.assertTrue(has_named_raw_replacement(manifest, "Routes", "2026", "123"))
        self.assertFalse(has_named_raw_replacement(manifest, "Runs", "2026", "456"))


if __name__ == "__main__":
    unittest.main()
