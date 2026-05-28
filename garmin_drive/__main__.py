from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .config import Settings, ensure_local_dirs, get_settings
from .corpus import merge_run_history, normalize_runs, render_corpus, run_history_payload
from .state import load_state, save_state
from .strava import StravaClient

if TYPE_CHECKING:
    from .drive import DriveClient


APPDATA_STRAVA_TOKEN = "strava_token.json"
APPDATA_SYNC_STATE = "sync_state.json"
APPDATA_RUN_HISTORY = "run_history.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Strava run history into Google Drive for ChatGPT.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-strava", help="Authorize Strava and store the OAuth token locally.")
    subparsers.add_parser("auth-google", help="Authorize Google Drive and store the OAuth token locally.")
    subparsers.add_parser("bootstrap-appdata", help="Upload the local Strava token into hidden Google Drive app data.")

    sync = subparsers.add_parser("sync-strava", help="Fetch Strava runs and publish the Drive corpus.")
    sync.add_argument("--days", type=int, default=14, help="How many days of Strava history to inspect.")
    sync.add_argument("--max-pages", type=int, default=5, help="Maximum Strava pages to fetch, 200 activities each.")
    sync.add_argument("--no-upload", action="store_true", help="Write local files and state but skip visible Drive uploads.")
    sync.add_argument("--dry-run", action="store_true", help="Print what would happen without writing or uploading.")
    sync.add_argument("--force-upload", action="store_true", help="Rewrite visible Drive files even if run history is unchanged.")
    sync.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where rotating Strava tokens and run history live. Defaults to env/auto.",
    )

    args = parser.parse_args(argv)
    settings = get_settings()
    ensure_local_dirs(settings)

    if args.command == "auth-strava":
        return auth_strava(settings)
    if args.command == "auth-google":
        return auth_google(settings)
    if args.command == "bootstrap-appdata":
        return bootstrap_appdata(settings)
    if args.command == "sync-strava":
        return sync_strava(settings, args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def auth_strava(settings: Settings) -> int:
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise RuntimeError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")

    url = StravaClient.authorization_url(settings.strava_client_id, settings.strava_scope)
    print("Open this URL, authorize the app, then paste the full callback URL or code here:")
    print()
    print(url)
    print()
    code_or_url = input("Callback URL or code: ").strip()
    token = StravaClient.exchange_code(
        settings.strava_client_id,
        settings.strava_client_secret,
        settings.strava_token_file,
        code_or_url,
    )
    athlete = token.get("athlete", {})
    print(f"Saved Strava token for athlete {athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip())
    return 0


def auth_google(settings: Settings) -> int:
    from .drive import DriveClient

    DriveClient.authorize(settings.google_client_secret_file, settings.google_token_file)
    print(f"Saved Google token to {settings.google_token_file}")
    print("This token includes Drive file access and hidden appDataFolder access.")
    return 0


def bootstrap_appdata(settings: Settings) -> int:
    token = load_local_strava_token(settings)
    drive = drive_client(settings)
    drive.put_appdata_json(APPDATA_STRAVA_TOKEN, token)
    existing_history = drive.get_appdata_json(APPDATA_RUN_HISTORY)
    if existing_history is None:
        drive.put_appdata_json(APPDATA_RUN_HISTORY, run_history_payload([]))
    print(f"Uploaded {APPDATA_STRAVA_TOKEN} to hidden Google Drive app data.")
    print("GitHub Actions can now refresh and persist Strava tokens without using your PC.")
    return 0


def sync_strava(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise RuntimeError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env or GitHub Secrets first.")

    backend = resolve_state_backend(args.state_backend or settings.state_backend)
    drive = drive_client(settings) if backend == "drive" or not args.no_upload else None
    strava_token = load_strava_token(settings, backend=backend, drive=drive)

    after = datetime.now(timezone.utc) - timedelta(days=args.days)
    after_epoch = int(after.timestamp())

    strava = StravaClient(
        settings.strava_client_id,
        settings.strava_client_secret,
        settings.strava_token_file if backend == "local" else None,
        token=strava_token,
        on_token_update=(lambda token: save_strava_token(settings, token, backend=backend, drive=drive)),
    )
    activities = strava.iter_activities(after_epoch=after_epoch, max_pages=args.max_pages)
    fetched_runs = normalize_runs(activities)

    print(f"Fetched {len(activities)} Strava activities; {len(fetched_runs)} are runs.")
    if args.dry_run:
        for run in fetched_runs[:20]:
            print(f"- {run.get('local_date')} {run.get('name')} ({run.get('source_activity_id')})")
        if len(fetched_runs) > 20:
            print(f"...and {len(fetched_runs) - 20} more")
        return 0

    existing_history = load_run_history(settings, backend=backend, drive=drive)
    existing_runs = runs_from_history(existing_history)
    merged_runs = merge_run_history(existing_history, fetched_runs)
    current_digest = run_history_digest(merged_runs)
    sync_state = load_sync_state(settings, backend=backend, drive=drive)
    should_publish = args.force_upload or sync_state.get("last_published_digest") != current_digest

    generated_files = []
    if args.no_upload or should_publish:
        generated_files = render_corpus(
            merged_runs,
            settings.output_dir,
            markdown_as_google_docs=settings.google_upload_as_google_docs,
        )

    if merged_runs != existing_runs:
        save_run_history(settings, merged_runs, backend=backend, drive=drive)

    uploaded_count = 0
    if not args.no_upload:
        if should_publish:
            if drive is None:
                drive = drive_client(settings)
            uploaded_count = upload_generated_files(settings, drive, generated_files)
            print(f"Uploaded or updated {uploaded_count} visible Drive files.")
            sync_state["last_published_digest"] = current_digest
            sync_state["last_published_at"] = datetime.now(timezone.utc).isoformat()
        else:
            print("Run history is unchanged; skipped visible Drive uploads.")

    sync_state.update(
        {
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
            "state_backend": backend,
            "last_strava_days": args.days,
            "last_strava_activity_count": len(activities),
            "last_strava_run_count": len(fetched_runs),
            "stored_run_count": len(merged_runs),
            "current_history_digest": current_digest,
        }
    )
    save_sync_state(settings, sync_state, backend=backend, drive=drive)

    print(f"Stored run history contains {len(merged_runs)} runs.")
    print(f"Local output: {settings.output_dir}")
    return 0


def drive_client(settings: Settings) -> DriveClient:
    from .drive import DriveClient

    return DriveClient(settings.google_client_secret_file, settings.google_token_file, settings.google_token_json)


def resolve_state_backend(value: str) -> str:
    if value == "auto":
        return "drive" if os.getenv("GITHUB_ACTIONS", "").lower() == "true" else "local"
    return value


def load_local_strava_token(settings: Settings) -> dict[str, Any]:
    if not settings.strava_token_file.exists():
        raise RuntimeError(f"Missing local Strava token at {settings.strava_token_file}. Run auth-strava first.")
    return json.loads(settings.strava_token_file.read_text(encoding="utf-8"))


def load_strava_token(settings: Settings, *, backend: str, drive: DriveClient | None) -> dict[str, Any]:
    if backend == "local":
        return load_local_strava_token(settings)

    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")

    token = drive.get_appdata_json(APPDATA_STRAVA_TOKEN)
    if token:
        return token

    if settings.strava_token_json_bootstrap:
        token = json.loads(settings.strava_token_json_bootstrap)
        drive.put_appdata_json(APPDATA_STRAVA_TOKEN, token)
        return token

    if settings.strava_token_file.exists():
        token = load_local_strava_token(settings)
        drive.put_appdata_json(APPDATA_STRAVA_TOKEN, token)
        return token

    raise RuntimeError(
        "Missing Strava token in Drive app data. Run bootstrap-appdata locally or set STRAVA_TOKEN_JSON_BOOTSTRAP once."
    )


def save_strava_token(settings: Settings, token: dict[str, Any], *, backend: str, drive: DriveClient | None) -> None:
    if backend == "local":
        settings.strava_token_file.parent.mkdir(parents=True, exist_ok=True)
        settings.strava_token_file.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    drive.put_appdata_json(APPDATA_STRAVA_TOKEN, token)


def load_run_history(settings: Settings, *, backend: str, drive: DriveClient | None) -> Any:
    if backend == "local":
        state = load_state(settings.state_file)
        return state.get("run_history", [])
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    return drive.get_appdata_json(APPDATA_RUN_HISTORY) or {"runs": []}


def runs_from_history(history: Any) -> list[dict[str, Any]]:
    if isinstance(history, dict):
        runs = history.get("runs", [])
    elif isinstance(history, list):
        runs = history
    else:
        runs = []
    return [run for run in runs if isinstance(run, dict)]


def save_run_history(settings: Settings, runs: list[dict[str, Any]], *, backend: str, drive: DriveClient | None) -> None:
    payload = run_history_payload(runs)
    if backend == "local":
        state = load_state(settings.state_file)
        state["run_history"] = payload["runs"]
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    drive.put_appdata_json(APPDATA_RUN_HISTORY, payload)


def load_sync_state(settings: Settings, *, backend: str, drive: DriveClient | None) -> dict[str, Any]:
    if backend == "local":
        return load_state(settings.state_file)
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    state = drive.get_appdata_json(APPDATA_SYNC_STATE) or {}
    return state if isinstance(state, dict) else {}


def save_sync_state(
    settings: Settings,
    update: dict[str, Any],
    *,
    backend: str,
    drive: DriveClient | None,
) -> None:
    if backend == "local":
        state = load_state(settings.state_file)
        state.update(update)
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    state = drive.get_appdata_json(APPDATA_SYNC_STATE) or {}
    state.update(update)
    drive.put_appdata_json(APPDATA_SYNC_STATE, state)


def run_history_digest(runs: list[dict[str, Any]]) -> str:
    content = json.dumps(runs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def upload_generated_files(settings: Settings, drive: DriveClient, generated_files: list) -> int:
    folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
    folder_id = folder["id"]

    count = 0
    for generated in generated_files:
        drive.upload_text_file(
            generated.path,
            folder_id=folder_id,
            remote_name=generated.remote_name,
            as_google_doc=generated.as_google_doc,
            mime_type=generated.mime_type,
        )
        count += 1
    return count


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
