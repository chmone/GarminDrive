from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _path_from_env(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    output_dir: Path
    strava_client_id: str | None
    strava_client_secret: str | None
    strava_scope: str
    strava_token_json_bootstrap: str | None
    google_client_secret_file: Path
    google_token_json: str | None
    google_drive_folder_name: str
    google_drive_folder_id: str | None
    google_upload_as_google_docs: bool
    state_backend: str

    @property
    def token_dir(self) -> Path:
        return self.data_dir / "tokens"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def strava_token_file(self) -> Path:
        return self.token_dir / "strava_token.json"

    @property
    def google_token_file(self) -> Path:
        return self.token_dir / "google_token.json"


def get_settings() -> Settings:
    google_client_secret = Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "client_secret_google.json")).expanduser()
    if not google_client_secret.is_absolute():
        google_client_secret = PROJECT_ROOT / google_client_secret

    return Settings(
        data_dir=_path_from_env("GARMIN_DRIVE_DATA_DIR", ".data"),
        output_dir=_path_from_env("GARMIN_DRIVE_OUTPUT_DIR", "run_summaries"),
        strava_client_id=os.getenv("STRAVA_CLIENT_ID"),
        strava_client_secret=os.getenv("STRAVA_CLIENT_SECRET"),
        strava_scope=os.getenv("STRAVA_SCOPE", "activity:read_all"),
        strava_token_json_bootstrap=os.getenv("STRAVA_TOKEN_JSON_BOOTSTRAP") or None,
        google_client_secret_file=google_client_secret,
        google_token_json=os.getenv("GOOGLE_TOKEN_JSON") or None,
        google_drive_folder_name=os.getenv("GOOGLE_DRIVE_FOLDER_NAME", "Run History for ChatGPT"),
        google_drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID") or None,
        google_upload_as_google_docs=env_bool("GOOGLE_UPLOAD_AS_GOOGLE_DOCS", True),
        state_backend=os.getenv("GARMIN_DRIVE_STATE_BACKEND", "auto"),
    )


def ensure_local_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.token_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
