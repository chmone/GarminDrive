from __future__ import annotations

from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any, Callable

from .config import Settings


MetricCall = tuple[str, Callable[[Any, str], Any]]


def auth_garmin(settings: Settings) -> None:
    Garmin, errors = import_garminconnect()
    email = settings.garmin_email or input("Garmin email: ").strip()
    password = settings.garmin_password or getpass("Garmin password: ")
    if not email or not password:
        raise RuntimeError("Garmin email and password are required for first-time local auth.")

    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    try:
        api.login(str(settings.garmin_token_file))
    except errors as exc:
        raise RuntimeError(f"Garmin authentication failed: {exc}") from exc
    if not settings.garmin_token_file.exists():
        api.client.dump(str(settings.garmin_token_file))


def load_garmin_client(settings: Settings, *, backend: str, drive: Any | None) -> Any:
    if backend == "drive":
        if drive is None:
            raise RuntimeError("Drive state backend requires Google Drive credentials.")
        token_text = drive.get_appdata_text("garmin_token.json")
        if not token_text:
            raise RuntimeError("Missing Garmin token in Drive app data. Run bootstrap-garmin-appdata locally.")
        write_token_text(settings.garmin_token_file, token_text)

    if not settings.garmin_token_file.exists():
        raise RuntimeError(f"Missing Garmin token at {settings.garmin_token_file}. Run auth-garmin first.")

    Garmin, errors = import_garminconnect()
    api = Garmin(
        email=settings.garmin_email,
        password=settings.garmin_password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    try:
        api.login(str(settings.garmin_token_file))
    except errors as exc:
        raise RuntimeError(f"Garmin authentication failed: {exc}") from exc
    return api


def save_garmin_token(settings: Settings, *, backend: str, drive: Any | None) -> None:
    token_text = read_token_text(settings.garmin_token_file)
    if backend == "drive":
        if drive is None:
            raise RuntimeError("Drive state backend requires Google Drive credentials.")
        drive.put_appdata_text("garmin_token.json", token_text, mime_type="application/json")


def read_token_text(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Missing Garmin token at {path}. Run auth-garmin first.")
    return path.read_text(encoding="utf-8")


def write_token_text(path: Path, token_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token_text, encoding="utf-8")


def fetch_daily_health_archive(api: Any, cdate: str) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    metric_errors: dict[str, str] = {}
    for metric_name, metric_call in garmin_metric_calls():
        try:
            result = metric_call(api, cdate)
        except Exception as exc:  # Garmin endpoint availability varies by account/device.
            metric_errors[metric_name] = compact_error(exc)
            continue
        if result not in (None, {}, []):
            payloads[metric_name] = result
    return {
        "schema_version": 1,
        "date": cdate,
        "source": "garmin_connect",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "payloads": payloads,
        "metric_errors": metric_errors,
    }


def garmin_metric_calls() -> list[MetricCall]:
    return [
        ("stats", lambda api, cdate: api.get_stats(cdate)),
        ("heart_rates", lambda api, cdate: api.get_heart_rates(cdate)),
        ("stress", lambda api, cdate: api.get_stress_data(cdate)),
        ("all_day_stress", lambda api, cdate: api.get_all_day_stress(cdate)),
        ("body_battery", lambda api, cdate: api.get_body_battery(cdate, cdate)),
        ("body_battery_events", lambda api, cdate: api.get_body_battery_events(cdate)),
        ("sleep", lambda api, cdate: api.get_sleep_data(cdate)),
        ("hrv", lambda api, cdate: api.get_hrv_data(cdate)),
        ("respiration", lambda api, cdate: api.get_respiration_data(cdate)),
        ("spo2", lambda api, cdate: api.get_spo2_data(cdate)),
        ("training_readiness", lambda api, cdate: api.get_training_readiness(cdate)),
    ]


def compact_error(exc: Exception) -> str:
    label = exc.__class__.__name__
    message = str(exc).replace("\n", " ").strip()
    if len(message) > 300:
        message = f"{message[:297]}..."
    return f"{label}: {message}" if message else label


def import_garminconnect() -> tuple[Any, tuple[type[BaseException], ...]]:
    try:
        from garminconnect import (
            Garmin,
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        )
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install garminconnect from requirements.txt.") from exc
    return Garmin, (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
