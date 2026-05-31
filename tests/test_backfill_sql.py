"""Unit tests for the one-time SQL backfill helpers (no database or Drive required)."""
from __future__ import annotations

from types import SimpleNamespace

from garmin_drive import sql_sink
from garmin_drive.__main__ import chunked, download_raw_archives_from_drive


def test_chunked_splits_evenly_and_handles_remainder():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 50) == []
    assert chunked([1, 2], 0) == [[1], [2]]  # size floored to 1


def test_table_counts_returns_empty_when_sink_disabled():
    settings = SimpleNamespace(sql_sink_enabled=False, database_url=None, bodycompass_user_id="default")
    assert sql_sink.table_counts(settings) == {}


class _FakeDrive:
    """Minimal Drive stand-in: one year folder holding two JSON archives + a non-JSON file."""

    FOLDER = "application/vnd.google-apps.folder"

    def find_folder_path(self, root_folder_id, folder_parts):
        return {"id": "raw-root"}

    def list_files_in_folder(self, folder_id):
        if folder_id == "raw-root":
            return [{"id": "y2026", "name": "2026", "mimeType": self.FOLDER}]
        return [
            {"id": "a1", "name": "2026-05-30_run_1.json", "mimeType": "application/json"},
            {"id": "a2", "name": "2026-05-31_run_2.json", "mimeType": "application/json"},
            {"id": "skip", "name": "notes.txt", "mimeType": "text/plain"},
        ]

    def get_text_by_id(self, file_id):
        return '{"source_activity_id": "%s"}' % file_id


def test_download_raw_archives_reads_only_json_under_year_folders():
    archives = download_raw_archives_from_drive(_FakeDrive(), "run-root", ("Raw Data", "Runs"))
    assert [a["source_activity_id"] for a in archives] == ["a1", "a2"]


def test_download_raw_archives_returns_empty_when_folder_missing():
    drive = _FakeDrive()
    drive.find_folder_path = lambda root, parts: None
    assert download_raw_archives_from_drive(drive, "run-root", ("Raw Data", "Runs")) == []
