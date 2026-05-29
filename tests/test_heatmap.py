from __future__ import annotations

import unittest
from datetime import date, timedelta

from garmin_drive.heatmap import (
    activity_map_contribution,
    activity_map_payload,
    heatmap_state_from_archives,
    merge_heatmap_state,
    render_activity_map_html,
)


def archive(
    activity_id: int,
    sport_type: str = "Run",
    *,
    local_day: str = "2026-05-20",
    heartrate: list[int] | None = None,
) -> dict:
    streams = {
        "latlng": {"data": [[40.0, -75.0], [40.0005, -75.0], [40.001, -75.0]]},
        "velocity_smooth": {"data": [3.0, 3.2, 3.4]},
        "altitude": {"data": [100.0, 105.0, 102.0]},
        "grade_smooth": {"data": [0.0, 4.0, -3.0]},
    }
    if heartrate is not None:
        streams["heartrate"] = {"data": heartrate}
    return {
        "source_activity_id": str(activity_id),
        "activity": {
            "id": activity_id,
            "name": f"Activity {activity_id}",
            "sport_type": sport_type,
            "start_date_local": f"{local_day}T07:00:00Z",
            "distance": 1200,
        },
        "streams": streams,
    }


class HeatmapTests(unittest.TestCase):
    def test_heatmap_state_includes_activity_types_and_cells(self) -> None:
        state = heatmap_state_from_archives([archive(1, "Run", heartrate=[140, 145, 150]), archive(2, "Ride")])
        payload = activity_map_payload("All Time Activity Map", state)

        self.assertEqual(payload["activity_count"], 2)
        self.assertEqual(payload["sport_types"], ["Ride", "Run"])
        self.assertTrue(payload["bounds"])
        self.assertTrue(all(item["cells"] for item in payload["contributions"]))

    def test_repeated_routes_increase_frequency_cells(self) -> None:
        first = activity_map_contribution(archive(1, "Run"))
        state = heatmap_state_from_archives([archive(1, "Run"), archive(2, "Run")])

        first_count = sum(cell["count"] for cell in first["cells"])
        combined_count = sum(
            cell["count"]
            for contribution in state["contributions"]
            for cell in contribution["cells"]
        )

        self.assertGreater(combined_count, first_count)

    def test_missing_heart_rate_still_maps_route(self) -> None:
        contribution = activity_map_contribution(archive(1, "Run", heartrate=None))

        self.assertIsNotNone(contribution)
        self.assertTrue(contribution["cells"])
        self.assertTrue(all("hr_count" not in cell for cell in contribution["cells"]))

    def test_merge_replaces_existing_activity_by_id(self) -> None:
        old_state = heatmap_state_from_archives([archive(1, "Run", local_day="2026-05-01")])
        replacement = activity_map_contribution(archive(1, "Ride", local_day="2026-05-02"))
        merged = merge_heatmap_state(old_state, [replacement])

        self.assertEqual(len(merged["contributions"]), 1)
        self.assertEqual(merged["contributions"][0]["sport_type"], "Ride")

    def test_recent_payload_filters_by_local_date(self) -> None:
        today = date.today()
        state = heatmap_state_from_archives(
            [
                archive(1, "Run", local_day=today.isoformat()),
                archive(2, "Run", local_day=(today - timedelta(days=30)).isoformat()),
            ]
        )
        payload = activity_map_payload("Recent Activity Map", state, recent_days=14)

        self.assertEqual(payload["activity_count"], 1)
        self.assertEqual(payload["contributions"][0]["source_activity_id"], "1")

    def test_rendered_html_contains_viewer_controls(self) -> None:
        state = heatmap_state_from_archives([archive(1, "Run", heartrate=[140, 145, 150])])
        html = render_activity_map_html("All Time Activity Map", state)

        self.assertIn("activityFilter", html)
        self.assertIn("displayMode", html)
        self.assertIn("metricMode", html)
        self.assertIn("Frequency", html)
        self.assertIn("Routes", html)
        self.assertIn("drawFrequencyRoutes", html)


if __name__ == "__main__":
    unittest.main()
