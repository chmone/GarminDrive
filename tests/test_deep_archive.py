from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from garmin_drive.__main__ import activity_sort_key, delete_run, has_named_raw_replacement, render_raw_outputs
from garmin_drive.config import Settings
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
from garmin_drive.state import load_state
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
            out = Path(temp_dir)
            render_corpus([run], out, markdown_as_google_docs=False)
            main_payload = json.loads((out / "Raw Data" / "Run History Data.json").read_text(encoding="utf-8"))
            run_csv = (out / "Run History Data.csv").read_text(encoding="utf-8")
            recent_csv = (out / "Recent Mile Splits.csv").read_text(encoding="utf-8")
            all_splits_csv = (out / "Mile Splits Data.csv").read_text(encoding="utf-8")
            top_level_json_exists = (out / "Run History Data.json").exists()
            recent_json_exists = (out / "Recent Mile Splits.json").exists()
            old_map_exists = (out / "Recent Run Map.html").exists()

        self.assertNotIn("mile_splits", main_payload["runs"][0])
        self.assertEqual(main_payload["runs"][0]["mile_split_count"], 1)
        self.assertIn("Morning Run", run_csv)
        self.assertIn("10:00/mi", recent_csv)
        self.assertIn("10:00/mi", all_splits_csv)
        self.assertFalse(top_level_json_exists)
        self.assertFalse(recent_json_exists)
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

    def test_render_raw_outputs_can_skip_all_routes_aggregate(self) -> None:
        activity = {
            "id": 123,
            "name": "Evening Run!",
            "sport_type": "Run",
            "start_date_local": "2026-05-27T18:00:00Z",
        }
        route = {
            "type": "Feature",
            "id": "123",
            "properties": {"source_activity_id": "123", "local_date": "2026-05-27"},
            "geometry": {"type": "LineString", "coordinates": [[-75.0, 40.0], [-75.1, 40.1]]},
        }
        archive = {"source_activity_id": "123", "activity": activity, "streams": {}, "route": route}

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            settings = SimpleNamespace(output_dir=output_dir)
            generated = render_raw_outputs(
                settings,
                [archive],
                {"type": "FeatureCollection", "features": [route]},
                include_all_routes=False,
            )
            generated_paths = {str(item.path.relative_to(output_dir)).replace("\\", "/") for item in generated}
            aggregate_exists = (output_dir / "Raw Data" / "All Run Routes.geojson").exists()

        self.assertIn("Raw Data/Runs/2026/2026-05-27_run-evening-run_123.json", generated_paths)
        self.assertIn("Raw Data/Routes/2026/2026-05-27_run-evening-run_123.geojson", generated_paths)
        self.assertNotIn("Raw Data/All Run Routes.geojson", generated_paths)
        self.assertFalse(aggregate_exists)

    def test_delete_run_tolerates_already_missing_history_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / ".data"
            output_dir = root / "Run History"
            current = Settings(
                data_dir=data_dir,
                output_dir=output_dir,
                health_output_dir=root / "Health Data",
                strava_client_id=None,
                strava_client_secret=None,
                strava_scope="activity:read_all",
                strava_token_json_bootstrap=None,
                google_client_secret_file=root / "client_secret_google.json",
                google_token_json=None,
                google_drive_projects_folder_name="Projects",
                google_drive_projects_folder_id=None,
                google_drive_run_folder_name="Run History",
                google_drive_run_folder_id=None,
                google_drive_health_folder_name="Health Data",
                google_drive_health_folder_id=None,
                google_drive_folder_name="Run History",
                google_drive_folder_id=None,
                use_legacy_drive_folder=False,
                google_upload_as_google_docs=False,
                state_backend="local",
                garmin_email=None,
                garmin_password=None,
                garmin_health_timezone="America/New_York",
            )
            state_file = data_dir / "state.json"
            raw_run = output_dir / "Raw Data" / "Runs" / "2026" / "2026-05-27_run-fast_123.json"
            raw_route = output_dir / "Raw Data" / "Routes" / "2026" / "2026-05-27_run-fast_123.geojson"
            cache_file = data_dir / "raw_archive" / "Runs" / "2026" / "123.json"
            for path in (raw_run, raw_route, cache_file):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                json.dumps(
                    {
                        "run_history": [],
                        "raw_manifest": {
                            "schema_version": 1,
                            "files": {
                                "Raw Data/Runs/2026/2026-05-27_run-fast_123.json": {},
                                "Raw Data/Routes/2026/2026-05-27_run-fast_123.geojson": {},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = delete_run(
                current,
                argparse.Namespace(
                    activity_id="123",
                    dry_run=False,
                    no_upload=True,
                    recent_mile_days=14,
                    state_backend="local",
                ),
            )
            state = load_state(state_file)

            self.assertEqual(result, 0)
            self.assertFalse(raw_run.exists())
            self.assertFalse(raw_route.exists())
            self.assertFalse(cache_file.exists())
            self.assertEqual(state["raw_manifest"]["files"], {})
            self.assertEqual(state["excluded_activity_ids"], ["123"])
            self.assertTrue((output_dir / "Raw Data" / "Run History Data.json").exists())


if __name__ == "__main__":
    unittest.main()
