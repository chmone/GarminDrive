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
    health_output_dir: Path
    strava_client_id: str | None
    strava_client_secret: str | None
    strava_scope: str
    strava_token_json_bootstrap: str | None
    google_client_secret_file: Path
    google_token_json: str | None
    google_drive_projects_folder_name: str
    google_drive_projects_folder_id: str | None
    google_drive_run_folder_name: str
    google_drive_run_folder_id: str | None
    google_drive_health_folder_name: str
    google_drive_health_folder_id: str | None
    google_drive_folder_name: str
    google_drive_folder_id: str | None
    use_legacy_drive_folder: bool
    google_upload_as_google_docs: bool
    state_backend: str
    garmin_email: str | None
    garmin_password: str | None
    garmin_health_timezone: str
    # --- Body Compass Supabase sink (additive; the Drive publish path is unaffected) ---
    # Defaulted so existing callers/tests that build Settings without these keep working; the sink is
    # off until database_url is set.
    database_url: str | None = None   # Postgres/Supabase connection string (session pooler URI)
    bodycompass_user_id: str = "default"  # tag every synced row with this user id (app's DEFAULT_USER_ID)
    bodycompass_sql_sink: bool = True     # master switch; the sink also needs database_url to be set
    store_run_streams: bool = True        # mirror full per-sample run streams (large); off = summaries + details only
    intraday_enabled: bool = True         # store intraday time-series + the "now" snapshot from fetched health days
    intraday_days: int = 1                # how many trailing days the lightweight sync-garmin-intraday refreshes
    weather_enabled: bool = True          # fetch per-run weather (Open-Meteo archive) into the weather table

    @property
    def sql_sink_enabled(self) -> bool:
        """Mirror this sync into Postgres only when explicitly on AND a connection string exists."""
        return bool(self.bodycompass_sql_sink and self.database_url)

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

    @property
    def garmin_token_file(self) -> Path:
        return self.token_dir / "garmin_token.json"


def get_settings() -> Settings:
    google_client_secret = Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "client_secret_google.json")).expanduser()
    if not google_client_secret.is_absolute():
        google_client_secret = PROJECT_ROOT / google_client_secret
    new_drive_folder_env_present = any(
        os.getenv(name)
        for name in (
            "GOOGLE_DRIVE_PROJECTS_FOLDER_NAME",
            "GOOGLE_DRIVE_PROJECTS_FOLDER_ID",
            "GOOGLE_DRIVE_RUN_FOLDER_NAME",
            "GOOGLE_DRIVE_RUN_FOLDER_ID",
            "GOOGLE_DRIVE_HEALTH_FOLDER_NAME",
            "GOOGLE_DRIVE_HEALTH_FOLDER_ID",
        )
    )
    legacy_drive_folder_name = os.getenv("GOOGLE_DRIVE_FOLDER_NAME")
    legacy_drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") or None
    use_legacy_drive_folder = bool((legacy_drive_folder_name or legacy_drive_folder_id) and not new_drive_folder_env_present)

    return Settings(
        data_dir=_path_from_env("GARMIN_DRIVE_DATA_DIR", ".data"),
        output_dir=_path_from_env("GARMIN_DRIVE_OUTPUT_DIR", "Projects/Run History"),
        health_output_dir=_path_from_env("GARMIN_DRIVE_HEALTH_OUTPUT_DIR", "Projects/Health Data"),
        strava_client_id=os.getenv("STRAVA_CLIENT_ID"),
        strava_client_secret=os.getenv("STRAVA_CLIENT_SECRET"),
        strava_scope=os.getenv("STRAVA_SCOPE", "activity:read_all"),
        strava_token_json_bootstrap=os.getenv("STRAVA_TOKEN_JSON_BOOTSTRAP") or None,
        google_client_secret_file=google_client_secret,
        google_token_json=os.getenv("GOOGLE_TOKEN_JSON") or None,
        google_drive_projects_folder_name=os.getenv("GOOGLE_DRIVE_PROJECTS_FOLDER_NAME", "Projects"),
        google_drive_projects_folder_id=os.getenv("GOOGLE_DRIVE_PROJECTS_FOLDER_ID") or None,
        google_drive_run_folder_name=os.getenv("GOOGLE_DRIVE_RUN_FOLDER_NAME", "Run History"),
        google_drive_run_folder_id=os.getenv("GOOGLE_DRIVE_RUN_FOLDER_ID") or None,
        google_drive_health_folder_name=os.getenv("GOOGLE_DRIVE_HEALTH_FOLDER_NAME", "Health Data"),
        google_drive_health_folder_id=os.getenv("GOOGLE_DRIVE_HEALTH_FOLDER_ID") or None,
        google_drive_folder_name=legacy_drive_folder_name or "Run History",
        google_drive_folder_id=legacy_drive_folder_id,
        use_legacy_drive_folder=use_legacy_drive_folder,
        google_upload_as_google_docs=env_bool("GOOGLE_UPLOAD_AS_GOOGLE_DOCS", True),
        state_backend=os.getenv("GARMIN_DRIVE_STATE_BACKEND", "auto"),
        garmin_email=os.getenv("GARMIN_EMAIL") or None,
        garmin_password=os.getenv("GARMIN_PASSWORD") or None,
        garmin_health_timezone=os.getenv("GARMIN_HEALTH_TIMEZONE", "America/New_York"),
        database_url=os.getenv("DATABASE_URL") or None,
        bodycompass_user_id=os.getenv("BODYCOMPASS_USER_ID", "default"),
        bodycompass_sql_sink=env_bool("BODYCOMPASS_SQL_SINK", True),
        store_run_streams=env_bool("BODYCOMPASS_STORE_RUN_STREAMS", True),
        intraday_enabled=env_bool("BODYCOMPASS_INTRADAY", True),
        intraday_days=max(1, int(os.getenv("BODYCOMPASS_INTRADAY_DAYS", "1") or 1)),
        weather_enabled=env_bool("BODYCOMPASS_WEATHER", True),
    )


def ensure_local_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.token_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.health_output_dir.mkdir(parents=True, exist_ok=True)
