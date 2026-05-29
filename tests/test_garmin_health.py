from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from garmin_drive.__main__ import (
    bootstrap_garmin_appdata,
    get_health_drive_folder,
    get_run_drive_folder,
    health_sync_dates,
    should_skip_raw_upload,
    sync_all_sources,
)
from garmin_drive.config import Settings
from garmin_drive.garmin_health import fetch_daily_health_archive
from garmin_drive.health_corpus import (
    health_raw_manifest_key,
    merge_health_history,
    normalize_health_archive,
    raw_health_path,
    render_health_corpus,
)


class FakeDrive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.appdata: dict[str, object] = {}
        self.next_id = 1

    def get_or_create_folder(self, folder_name: str, folder_id: str | None = None) -> dict:
        self.calls.append(("root", folder_name, folder_id))
        return {"id": folder_id or self._id(folder_name), "name": folder_name}

    def get_or_create_child_folder(self, parent_id: str, folder_name: str) -> dict:
        self.calls.append(("child", parent_id, folder_name))
        return {"id": self._id(folder_name), "name": folder_name}

    def _id(self, name: str) -> str:
        self.next_id += 1
        return f"{name}-{self.next_id}"

    def put_appdata_text(self, name: str, content: str, mime_type: str = "application/json") -> dict:
        self.appdata[name] = content
        return {"id": name, "name": name}

    def get_appdata_json(self, name: str) -> object | None:
        return self.appdata.get(name)

    def put_appdata_json(self, name: str, value: object) -> dict:
        self.appdata[name] = value
        return {"id": name, "name": name}


def settings(**overrides: object) -> Settings:
    base = {
        "data_dir": Path(".data"),
        "output_dir": Path("run_summaries"),
        "health_output_dir": Path("health_summaries"),
        "strava_client_id": None,
        "strava_client_secret": None,
        "strava_scope": "activity:read_all",
        "strava_token_json_bootstrap": None,
        "google_client_secret_file": Path("client_secret_google.json"),
        "google_token_json": None,
        "google_drive_projects_folder_name": "Projects",
        "google_drive_projects_folder_id": None,
        "google_drive_run_folder_name": "Run History",
        "google_drive_run_folder_id": None,
        "google_drive_health_folder_name": "Health Data",
        "google_drive_health_folder_id": None,
        "google_drive_folder_name": "Legacy Runs",
        "google_drive_folder_id": None,
        "use_legacy_drive_folder": False,
        "google_upload_as_google_docs": True,
        "state_backend": "local",
        "garmin_email": None,
        "garmin_password": None,
        "garmin_health_timezone": "America/New_York",
    }
    base.update(overrides)
    return Settings(**base)


class GarminHealthTests(unittest.TestCase):
    def test_nested_drive_folders_default_to_projects_layout(self) -> None:
        drive = FakeDrive()
        current = settings()

        run_folder = get_run_drive_folder(current, drive)  # type: ignore[arg-type]
        health_folder = get_health_drive_folder(current, drive)  # type: ignore[arg-type]

        self.assertEqual(run_folder["name"], "Run History")
        self.assertEqual(health_folder["name"], "Health Data")
        self.assertIn(("root", "Projects", None), drive.calls)
        self.assertIn(("child", "Projects-2", "Run History"), drive.calls)
        self.assertIn(("child", "Projects-4", "Health Data"), drive.calls)

    def test_legacy_drive_folder_fallback(self) -> None:
        drive = FakeDrive()
        current = settings(use_legacy_drive_folder=True, google_drive_folder_id="legacy-id")

        run_folder = get_run_drive_folder(current, drive)  # type: ignore[arg-type]

        self.assertEqual(run_folder["id"], "legacy-id")
        self.assertEqual(drive.calls, [("root", "Legacy Runs", "legacy-id")])

    def test_merge_health_history_replaces_by_date(self) -> None:
        existing = {"days": [{"date": "2026-05-28", "resting_hr": 45}, {"date": "2026-05-27", "resting_hr": 44}]}
        fetched = [{"date": "2026-05-28", "resting_hr": 42}]

        merged = merge_health_history(existing, fetched)

        self.assertEqual([day["date"] for day in merged], ["2026-05-28", "2026-05-27"])
        self.assertEqual(merged[0]["resting_hr"], 42)

    def test_health_sync_dates_refetch_recent_and_backfill_range(self) -> None:
        current = settings()
        with patch("garmin_drive.__main__.health_today", return_value=date(2026, 5, 29)):
            rolling, rolling_is_range = health_sync_dates(
                current,
                argparse.Namespace(days=3, start_date=None, end_date=None),
            )
            ranged, ranged_is_range = health_sync_dates(
                current,
                argparse.Namespace(days=14, start_date="2026-05-01", end_date="2026-05-03"),
            )

        self.assertEqual(rolling, ["2026-05-27", "2026-05-28", "2026-05-29"])
        self.assertFalse(rolling_is_range)
        self.assertEqual(ranged, ["2026-05-01", "2026-05-02", "2026-05-03"])
        self.assertTrue(ranged_is_range)

    def test_backfill_manifest_skip_vs_force_upload(self) -> None:
        manifest = {"files": {health_raw_manifest_key("2026-05-29"): {}}}

        self.assertTrue(should_skip_raw_upload("Raw Health/2026/2026-05-29.json", manifest, False, ("Raw Health/",)))
        self.assertFalse(should_skip_raw_upload("Raw Health/2026/2026-05-29.json", manifest, True, ("Raw Health/",)))

    def test_fetch_daily_archive_captures_metric_errors(self) -> None:
        class FakeGarmin:
            def get_stats(self, cdate: str) -> dict:
                return {"restingHeartRate": 42}

            def get_heart_rates(self, cdate: str) -> dict:
                return {"heartRateValues": [[1, 40], [2, 80]]}

            def get_stress_data(self, cdate: str) -> dict:
                raise RuntimeError("not enabled")

            def get_all_day_stress(self, cdate: str) -> dict:
                return {}

            def get_body_battery(self, start: str, end: str) -> dict:
                return {"bodyBatteryValuesArray": [[1, 80], [2, 30]]}

            def get_body_battery_events(self, cdate: str) -> dict:
                return {}

            def get_sleep_data(self, cdate: str) -> dict:
                return {"dailySleepDTO": {"sleepTimeSeconds": 28800}, "sleepScores": {"overall": {"value": 88}}}

            def get_hrv_data(self, cdate: str) -> dict:
                return {"hrvSummary": {"lastNightAvg": 57, "status": "BALANCED"}}

            def get_respiration_data(self, cdate: str) -> dict:
                return {"avgRespiration": 14.5}

            def get_spo2_data(self, cdate: str) -> dict:
                return {"averageSpO2": 96}

            def get_training_readiness(self, cdate: str) -> dict:
                return {"score": 73}

        archive = fetch_daily_health_archive(FakeGarmin(), "2026-05-29")
        normalized = normalize_health_archive(archive)

        self.assertIn("stress", archive["metric_errors"])
        self.assertEqual(normalized["resting_hr"], 42)
        self.assertEqual(normalized["avg_hr"], 60)
        self.assertEqual(normalized["body_battery_start"], 80)
        self.assertEqual(normalized["body_battery_end"], 30)
        self.assertEqual(normalized["sleep_duration_hours"], 8)
        self.assertEqual(normalized["sleep_score"], 88)
        self.assertEqual(normalized["hrv_avg"], 57)
        self.assertEqual(normalized["spo2_avg"], 96)

    def test_bootstrap_garmin_appdata_uploads_token_and_initializes_state(self) -> None:
        drive = FakeDrive()
        with tempfile.TemporaryDirectory() as temp_dir:
            current = settings(data_dir=Path(temp_dir))
            current.token_dir.mkdir(parents=True, exist_ok=True)
            current.garmin_token_file.write_text('{"di_token":"token"}', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), patch("garmin_drive.__main__.drive_client", return_value=drive):
                result = bootstrap_garmin_appdata(current)

        self.assertEqual(result, 0)
        self.assertEqual(drive.appdata["garmin_token.json"], '{"di_token":"token"}')
        self.assertIn("garmin_health_history.json", drive.appdata)
        self.assertIn("garmin_health_sync_state.json", drive.appdata)
        self.assertIn("garmin_health_raw_manifest.json", drive.appdata)

    def test_sync_all_preserves_strava_success_when_garmin_fails(self) -> None:
        args = argparse.Namespace(
            days=14,
            health_days=14,
            max_pages=5,
            no_upload=False,
            force_upload=False,
            enrich="missing",
            publish_raw=True,
            recent_mile_days=14,
            request_budget=900,
            state_backend="local",
        )
        with (
            patch("garmin_drive.__main__.sync_strava", return_value=0) as strava,
            patch("garmin_drive.__main__.sync_garmin_health", side_effect=RuntimeError("nope")) as health,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = sync_all_sources(settings(), args)

        self.assertEqual(result, 0)
        strava.assert_called_once()
        health.assert_called_once()

    def test_render_health_outputs_and_raw_paths(self) -> None:
        day = {
            "date": "2026-05-29",
            "source": "garmin_connect",
            "resting_hr": 42,
            "avg_hr": 60,
            "min_hr": 40,
            "max_hr": 80,
            "available_metrics": ["heart_rate"],
            "metric_errors": {},
            "fetched_at": "2026-05-29T12:00:00Z",
        }
        raw = {"date": "2026-05-29", "payloads": {"stats": {"restingHeartRate": 42}}, "metric_errors": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            generated = render_health_corpus([day], [raw], output_dir, markdown_as_google_docs=False)
            raw_path = raw_health_path(output_dir, "2026-05-29")

            self.assertTrue((output_dir / "Health History Data.json").exists())
            self.assertTrue((output_dir / "Recent Recovery Metrics.csv").exists())
            self.assertTrue((output_dir / "Recovery Summary for ChatGPT.md").exists())
            self.assertTrue(raw_path.exists())
            self.assertIn("Raw Health", {part for item in generated for part in item.remote_folder_parts})


if __name__ == "__main__":
    unittest.main()
