from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, parse_qs

import requests


AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
REDIRECT_URI = "http://localhost/exchange_token"


class StravaClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: Path | None = None,
        *,
        token: dict[str, Any] | None = None,
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.on_token_update = on_token_update
        self.token: dict[str, Any] = token or self._load_token()

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
        response = requests.get(
            f"{API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.access_token()}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

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


def extract_code(code_or_url: str) -> str:
    value = code_or_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        if "code" not in params:
            raise ValueError("Callback URL does not contain a code query parameter")
        return params["code"][0]
    return value
