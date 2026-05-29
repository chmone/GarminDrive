from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from .corpus import GeneratedFile, write_generated


RAW_HEALTH_DIR = "Raw Health"
HEALTH_HISTORY_NAME = "Health History Data.json"
RECENT_RECOVERY_NAME = "Recent Recovery Metrics.csv"
RECOVERY_SUMMARY_NAME = "Recovery Summary for ChatGPT"

HEALTH_CSV_FIELDS = [
    "date",
    "resting_hr",
    "avg_hr",
    "min_hr",
    "max_hr",
    "avg_stress",
    "max_stress",
    "body_battery_start",
    "body_battery_end",
    "body_battery_min",
    "body_battery_max",
    "sleep_duration_hours",
    "sleep_score",
    "hrv_avg",
    "hrv_status",
    "respiration_avg",
    "spo2_avg",
    "training_readiness_score",
    "available_metrics",
    "metric_errors",
    "fetched_at",
]


def health_history_payload(days: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day_count": len(days),
        "days": days,
    }


def health_days_from_history(history: Any) -> list[dict[str, Any]]:
    if isinstance(history, dict):
        days = history.get("days", [])
    elif isinstance(history, list):
        days = history
    else:
        days = []
    return [day for day in days if isinstance(day, dict) and day.get("date")]


def merge_health_history(existing: Any, fetched_days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(day["date"]): day for day in health_days_from_history(existing)}
    for day in fetched_days:
        if day.get("date"):
            merged[str(day["date"])] = day
    return sorted(merged.values(), key=lambda day: str(day.get("date") or ""), reverse=True)


def normalize_health_archive(archive: dict[str, Any]) -> dict[str, Any]:
    payloads = archive.get("payloads") if isinstance(archive.get("payloads"), dict) else {}
    metric_errors = archive.get("metric_errors") if isinstance(archive.get("metric_errors"), dict) else {}
    stats = payloads.get("stats")
    heart_rates = payloads.get("heart_rates")
    sleep = payloads.get("sleep")
    hrv = payloads.get("hrv")
    stress = payloads.get("stress")
    all_day_stress = payloads.get("all_day_stress")
    body_battery = payloads.get("body_battery")
    respiration = payloads.get("respiration")
    spo2 = payloads.get("spo2")
    training_readiness = payloads.get("training_readiness")

    hr_samples = sample_numbers_from_keys(heart_rates, ("heartRateValues", "heartRateValueDescriptors"))
    stress_samples = sample_numbers_from_keys(
        all_day_stress,
        ("stressValuesArray", "stressValues", "allDayStressValuesArray"),
    )
    body_battery_samples = sample_numbers_from_keys(
        body_battery,
        ("bodyBatteryValuesArray", "bodyBatteryValues", "bodyBatteryValueDescriptors"),
    )

    sleep_seconds = first_number_from_keys(
        sleep,
        ("sleepTimeSeconds", "totalSleepSeconds", "sleepDurationSeconds", "durationInSeconds"),
    )
    sleep_score = first_number_from_keys(sleep, ("overall", "sleepScore", "overallSleepScore", "score"))
    hrv_avg = first_number_from_keys(
        sleep,
        ("avgSleepHRV", "averageSleepHrv", "averageHRV", "lastNightAvg", "weeklyAvg"),
    )
    if hrv_avg is None:
        hrv_avg = first_number_from_keys(hrv, ("lastNightAvg", "weeklyAvg", "average", "avgHrv", "hrvAvg"))

    normalized = {
        "date": str(archive.get("date") or ""),
        "source": "garmin_connect",
        "resting_hr": round_or_none(
            first_number_from_keys(heart_rates, stats, sleep, ("restingHeartRate", "restingHR", "restingHr")),
            0,
        ),
        "avg_hr": round_or_none(
            mean(hr_samples) or first_number_from_keys(heart_rates, stats, ("averageHeartRate", "avgHeartRate")),
            0,
        ),
        "min_hr": round_or_none(
            min(hr_samples) if hr_samples else first_number_from_keys(heart_rates, stats, ("minHeartRate",)),
            0,
        ),
        "max_hr": round_or_none(
            max(hr_samples) if hr_samples else first_number_from_keys(heart_rates, stats, ("maxHeartRate",)),
            0,
        ),
        "avg_stress": round_or_none(
            mean(stress_samples)
            or first_number_from_keys(all_day_stress, stress, stats, ("avgStressLevel", "averageStressLevel")),
            0,
        ),
        "max_stress": round_or_none(
            max(stress_samples)
            if stress_samples
            else first_number_from_keys(all_day_stress, stress, stats, ("maxStressLevel", "maxStress")),
            0,
        ),
        "body_battery_start": round_or_none(first_or_none(body_battery_samples), 0),
        "body_battery_end": round_or_none(last_or_none(body_battery_samples), 0),
        "body_battery_min": round_or_none(
            min(body_battery_samples)
            if body_battery_samples
            else first_number_from_keys(body_battery, stats, ("bodyBatteryLowestValue", "bodyBatteryMin")),
            0,
        ),
        "body_battery_max": round_or_none(
            max(body_battery_samples)
            if body_battery_samples
            else first_number_from_keys(body_battery, stats, ("bodyBatteryHighestValue", "bodyBatteryMax")),
            0,
        ),
        "sleep_duration_hours": round_or_none(sleep_seconds / 3600 if sleep_seconds is not None else None, 2),
        "sleep_score": round_or_none(sleep_score, 0),
        "hrv_avg": round_or_none(hrv_avg, 0),
        "hrv_status": first_string_from_keys(hrv, sleep, ("status", "hrvStatus", "hrvStatusDescription")),
        "respiration_avg": round_or_none(
            first_number_from_keys(respiration, sleep, ("avgRespiration", "averageRespiration", "avgWakingRespirationValue")),
            1,
        ),
        "spo2_avg": round_or_none(
            first_number_from_keys(spo2, sleep, ("avgSpO2", "averageSpO2", "averageSpo2", "avgSPO2")),
            1,
        ),
        "training_readiness_score": round_or_none(
            first_number_from_keys(training_readiness, ("score", "trainingReadinessScore", "readinessScore")),
            0,
        ),
        "available_metrics": available_metrics(payloads),
        "metric_errors": dict(sorted((str(k), str(v)) for k, v in metric_errors.items())),
        "fetched_at": str(archive.get("fetched_at") or datetime.now(timezone.utc).isoformat()),
    }
    return normalized


def render_health_corpus(
    days: list[dict[str, Any]],
    raw_archives: list[dict[str, Any]],
    output_dir: Path,
    *,
    markdown_as_google_docs: bool,
    recent_days: int = 14,
) -> list[GeneratedFile]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[GeneratedFile] = []
    payload = health_history_payload(days)
    generated.append(
        write_generated(
            output_dir / HEALTH_HISTORY_NAME,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            remote_name=HEALTH_HISTORY_NAME,
            mime_type="application/json",
            as_google_doc=False,
        )
    )
    generated.append(
        write_generated(
            output_dir / RECENT_RECOVERY_NAME,
            render_recent_recovery_csv(days, recent_days=recent_days),
            remote_name=RECENT_RECOVERY_NAME,
            mime_type="text/csv",
            as_google_doc=False,
        )
    )
    generated.append(
        write_generated(
            output_dir / f"{RECOVERY_SUMMARY_NAME}.md",
            render_recovery_summary(days, recent_days=recent_days),
            remote_name=RECOVERY_SUMMARY_NAME,
            mime_type="text/markdown",
            as_google_doc=markdown_as_google_docs,
        )
    )
    for archive in raw_archives:
        path = raw_health_path(output_dir, str(archive.get("date") or "unknown-date"))
        generated.append(
            write_generated(
                path,
                json.dumps(archive, indent=2, sort_keys=True) + "\n",
                remote_name=path.name,
                mime_type="application/json",
                as_google_doc=False,
                remote_folder_parts=tuple(path.relative_to(output_dir).parts[:-1]),
            )
        )
    return generated


def render_recent_recovery_csv(days: list[dict[str, Any]], *, recent_days: int = 14) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEALTH_CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for day in sorted(days, key=lambda item: str(item.get("date") or ""), reverse=True)[:recent_days]:
        row = dict(day)
        row["available_metrics"] = ",".join(str(item) for item in row.get("available_metrics") or [])
        row["metric_errors"] = ",".join(sorted((row.get("metric_errors") or {}).keys()))
        writer.writerow(row)
    return buffer.getvalue()


def render_recovery_summary(days: list[dict[str, Any]], *, recent_days: int = 14) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    recent = sorted(days, key=lambda item: str(item.get("date") or ""), reverse=True)[:recent_days]
    lines = [
        "# Recovery Summary for ChatGPT",
        "",
        f"Generated: {generated}",
        "",
        "This folder is generated from Garmin Connect wellness data. Use `Health History Data.json` for stable daily metrics and `Raw Health/` for detailed per-day Garmin payloads.",
        "",
        "## Recent Averages",
        "",
        f"- Days indexed: {len(days)}",
        f"- Recent window: {len(recent)} days",
        f"- Resting HR: {format_average(recent, 'resting_hr', 'bpm')}",
        f"- HRV: {format_average(recent, 'hrv_avg', 'ms')}",
        f"- Sleep: {format_average(recent, 'sleep_duration_hours', 'h')}",
        f"- Sleep score: {format_average(recent, 'sleep_score', '')}",
        f"- Stress: {format_average(recent, 'avg_stress', '')}",
        f"- Body Battery end: {format_average(recent, 'body_battery_end', '')}",
        "",
        "## Recent Daily Metrics",
        "",
        "| Date | Resting HR | HRV | Sleep | Sleep Score | Stress | Body Battery End | Available |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for day in recent:
        available = ", ".join(str(item) for item in day.get("available_metrics") or [])
        lines.append(
            "| "
            + " | ".join(
                [
                    str(day.get("date") or ""),
                    format_cell(day.get("resting_hr")),
                    format_cell(day.get("hrv_avg")),
                    format_cell(day.get("sleep_duration_hours")),
                    format_cell(day.get("sleep_score")),
                    format_cell(day.get("avg_stress")),
                    format_cell(day.get("body_battery_end")),
                    available,
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def raw_health_path(output_dir: Path, cdate: str) -> Path:
    year = cdate[:4] if len(cdate) >= 4 else "unknown-year"
    path = output_dir / RAW_HEALTH_DIR / year / f"{cdate}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def health_raw_manifest_key(cdate: str) -> str:
    year = cdate[:4] if len(cdate) >= 4 else "unknown-year"
    return f"{RAW_HEALTH_DIR}/{year}/{cdate}.json"


def available_metrics(payloads: dict[str, Any]) -> list[str]:
    metric_map = {
        "stats": "daily_stats",
        "heart_rates": "heart_rate",
        "stress": "stress",
        "all_day_stress": "stress",
        "body_battery": "body_battery",
        "body_battery_events": "body_battery",
        "sleep": "sleep",
        "hrv": "hrv",
        "respiration": "respiration",
        "spo2": "spo2",
        "training_readiness": "training_readiness",
    }
    found = {
        metric
        for key, metric in metric_map.items()
        if key in payloads and payloads[key] not in (None, {}, [])
    }
    return sorted(found)


def first_number_from_keys(*items: Any) -> float | None:
    if len(items) >= 2 and isinstance(items[-1], tuple):
        keys = items[-1]
        values = items[:-1]
    else:
        keys = ()
        values = items
    for value in values:
        for candidate in values_for_keys(value, keys):
            number = number_or_none(candidate)
            if number is not None:
                return number
    return None


def first_string_from_keys(*items: Any) -> str | None:
    if len(items) >= 2 and isinstance(items[-1], tuple):
        keys = items[-1]
        values = items[:-1]
    else:
        keys = ()
        values = items
    for value in values:
        for candidate in values_for_keys(value, keys):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def values_for_keys(value: Any, keys: tuple[str, ...]) -> Iterable[Any]:
    normalized_keys = {normalize_key(key) for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if normalize_key(str(key)) in normalized_keys:
                if isinstance(item, dict) and "value" in item:
                    yield item.get("value")
                else:
                    yield item
            yield from values_for_keys(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from values_for_keys(item, keys)


def sample_numbers_from_keys(value: Any, keys: tuple[str, ...]) -> list[float]:
    samples: list[float] = []
    for sample_container in values_for_keys(value, keys):
        samples.extend(extract_sample_numbers(sample_container))
    return samples


def extract_sample_numbers(value: Any) -> list[float]:
    if isinstance(value, dict):
        number = first_number_from_keys(value, ("value", "level", "stressLevel", "bodyBattery", "heartRate"))
        return [] if number is None else [number]
    if not isinstance(value, list):
        number = number_or_none(value)
        return [] if number is None else [number]

    samples: list[float] = []
    for item in value:
        if isinstance(item, dict):
            number = first_number_from_keys(item, ("value", "level", "stressLevel", "bodyBattery", "heartRate"))
        elif isinstance(item, list | tuple):
            number = number_or_none(item[1]) if len(item) >= 2 else number_or_none(item[0] if item else None)
        else:
            number = number_or_none(item)
        if number is not None and number >= 0:
            samples.append(number)
    return samples


def normalize_key(value: str) -> str:
    return value.replace("_", "").replace("-", "").lower()


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_or_none(value: float | None, digits: int) -> float | int | None:
    if value is None:
        return None
    rounded = round(value, digits)
    if digits == 0:
        return int(rounded)
    return rounded


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def first_or_none(values: list[float]) -> float | None:
    return values[0] if values else None


def last_or_none(values: list[float]) -> float | None:
    return values[-1] if values else None


def format_average(days: list[dict[str, Any]], key: str, unit: str) -> str:
    values = [number for day in days if (number := number_or_none(day.get(key))) is not None]
    if not values:
        return "unknown"
    suffix = f" {unit}" if unit else ""
    return f"{sum(values) / len(values):.1f}{suffix}"


def format_cell(value: Any) -> str:
    number = number_or_none(value)
    if number is None:
        return "unknown"
    if float(number).is_integer():
        return str(int(number))
    return f"{number:.1f}"
