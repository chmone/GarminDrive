from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, parse_qs

import requests


AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
REDIRECT_URI = "http://localhost/exchange_token"
STREAM_KEYS = [
    "time",
    "distance",
    "latlng",
    "altitude",
    "velocity_smooth",
    "heartrate",
    "cadence",
    "moving",
    "grade_smooth",
    "temp",
]


class StravaRequestBudgetExceeded(RuntimeError):
    pass


class StravaDailyRateLimitExceeded(RuntimeError):
    pass


class StravaShortRateLimitExceeded(RuntimeError):
    pass


class StravaClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: Path | None = None,
        *,
        token: dict[str, Any] | None = None,
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
        request_budget: int | None = None,
        sleep_on_rate_limit: bool = True,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.on_token_update = on_token_update
        self.token: dict[str, Any] = token or self._load_token()
        self.request_budget = request_budget
        self.sleep_on_rate_limit = sleep_on_rate_limit
        self.rate_limits: dict[str, tuple[int, int] | None] = {
            "overall_limit": None,
            "overall_usage": None,
            "read_limit": None,
            "read_usage": None,
        }

    @staticmethod
    def authorization_url(client_id: str, scope: str) -> str:
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "approval_prompt": "force",
                "scope": scope,
            }
        )
        return f"{AUTH_URL}?{query}"

    @classmethod
    def exchange_code(
        cls,
        client_id: str,
        client_secret: str,
        token_file: Path,
        code_or_url: str,
    ) -> dict[str, Any]:
        code = extract_code(code_or_url)
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return token

    def _load_token(self) -> dict[str, Any]:
        if self.token_file is None:
            raise RuntimeError("Missing Strava token. Run auth-strava and bootstrap Drive app data first.")
        if not self.token_file.exists():
            raise RuntimeError(
                f"Missing Strava token at {self.token_file}. Run: python -m garmin_drive auth-strava"
            )
        return json.loads(self.token_file.read_text(encoding="utf-8"))

    def _save_token(self) -> None:
        if self.token_file is not None:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(json.dumps(self.token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if self.on_token_update is not None:
            self.on_token_update(self.token)

    def access_token(self) -> str:
        expires_at = int(self.token.get("expires_at", 0))
        if expires_at <= int(time.time()) + 600:
            self.refresh()
        access_token = self.token.get("access_token")
        if not access_token:
            raise RuntimeError("Strava token is missing access_token")
        return access_token

    def refresh(self) -> None:
        refresh_token = self.token.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Strava token is missing refresh_token")

        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        self.token.update(response.json())
        self._save_token()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        while True:
            self._wait_or_raise_before_request()
            self._consume_request_budget()
            response = requests.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token()}"},
                timeout=30,
            )
            self._update_rate_limits(response)
            if response.status_code == 429:
                self._handle_rate_limited_response()
                continue
            response.raise_for_status()
            return response.json()

    def get_activity(self, activity_id: str | int) -> dict[str, Any]:
        return self.get(f"/activities/{activity_id}", params={"include_all_efforts": "false"})

    def get_activity_streams(self, activity_id: str | int, keys: list[str] | None = None) -> dict[str, Any]:
        return self.get(
            f"/activities/{activity_id}/streams",
            params={"keys": ",".join(keys or STREAM_KEYS), "key_by_type": "true"},
        )

    def iter_activities(
        self,
        *,
        after_epoch: int | None = None,
        before_epoch: int | None = None,
        per_page: int = 200,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"page": page, "per_page": per_page}
            if after_epoch is not None:
                params["after"] = after_epoch
            if before_epoch is not None:
                params["before"] = before_epoch

            batch = self.get("/athlete/activities", params=params)
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < per_page:
                break
        return activities

    def _consume_request_budget(self) -> None:
        if self.request_budget is None:
            return
        if self.request_budget <= 0:
            raise StravaRequestBudgetExceeded(
                "Strava request budget reached. Rerun later to continue the resumable backfill."
            )
        self.request_budget -= 1

    def _wait_or_raise_before_request(self) -> None:
        read_limit = self.rate_limits.get("read_limit")
        read_usage = self.rate_limits.get("read_usage")
        if not read_limit or not read_usage:
            return

        short_limit, daily_limit = read_limit
        short_usage, daily_usage = read_usage
        if daily_usage >= daily_limit:
            raise StravaDailyRateLimitExceeded(
                "Strava daily read limit reached. Rerun after midnight UTC to continue the backfill."
            )
        if short_usage >= short_limit:
            sleep_seconds = seconds_until_next_15_minute_window()
            if not self.sleep_on_rate_limit:
                raise StravaShortRateLimitExceeded(
                    f"Strava 15-minute read limit reached. Rerun in about {sleep_seconds} seconds."
                )
            print(f"Strava 15-minute read limit reached; sleeping {sleep_seconds} seconds before continuing.")
            time.sleep(sleep_seconds)

    def _handle_rate_limited_response(self) -> None:
        read_limit = self.rate_limits.get("read_limit")
        read_usage = self.rate_limits.get("read_usage")
        if read_limit and read_usage and read_usage[1] >= read_limit[1]:
            raise StravaDailyRateLimitExceeded(
                "Strava daily read limit reached. Rerun after midnight UTC to continue the backfill."
            )
        sleep_seconds = seconds_until_next_15_minute_window()
        if not self.sleep_on_rate_limit:
            raise StravaShortRateLimitExceeded(
                f"Strava 15-minute read limit reached. Rerun in about {sleep_seconds} seconds."
            )
        print(f"Strava 15-minute read limit reached; sleeping {sleep_seconds} seconds before retrying.")
        time.sleep(sleep_seconds)

    def _update_rate_limits(self, response: requests.Response) -> None:
        overall_limit = parse_limit_header(response.headers.get("X-RateLimit-Limit"))
        overall_usage = parse_limit_header(response.headers.get("X-RateLimit-Usage"))
        read_limit = parse_limit_header(response.headers.get("X-ReadRateLimit-Limit"))
        read_usage = parse_limit_header(response.headers.get("X-ReadRateLimit-Usage"))
        if overall_limit:
            self.rate_limits["overall_limit"] = overall_limit
        if overall_usage:
            self.rate_limits["overall_usage"] = overall_usage
        if read_limit:
            self.rate_limits["read_limit"] = read_limit
        if read_usage:
            self.rate_limits["read_usage"] = read_usage


def extract_code(code_or_url: str) -> str:
    value = code_or_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        if "code" not in params:
            raise ValueError("Callback URL does not contain a code query parameter")
        return params["code"][0]
    return value


def parse_limit_header(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def seconds_until_next_15_minute_window() -> int:
    now = datetime.now(timezone.utc)
    seconds_since_hour = now.minute * 60 + now.second
    next_boundary = ((seconds_since_hour // 900) + 1) * 900
    return max(1, next_boundary - seconds_since_hour + 5)
