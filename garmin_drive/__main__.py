from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import requests

from . import sql_sink
from .config import Settings, ensure_local_dirs, get_settings
from .corpus import (
    RETIRED_TOP_LEVEL_FILES,
    GeneratedFile,
    merge_run_history,
    normalize_activity,
    past_summary_cutoff_year,
    render_corpus,
    run_history_payload,
    write_generated,
)
from .deep_archive import (
    ALL_ROUTES_NAME,
    RAW_DATA_DIR,
    RAW_ROUTES_DIR,
    RAW_RUNS_DIR,
    archive_enrichment_fields,
    build_raw_archive,
    load_cached_archive,
    load_geojson_text,
    merge_route_features,
    save_cached_archive,
    write_raw_archive_files,
    year_for_activity,
)
from .heatmap import (
    ALL_TIME_ACTIVITY_MAP_NAME,
    HEATMAP_STATE_NAME,
    MAPS_DIR,
    RAW_HEATMAP_DIR,
    RECENT_ACTIVITY_MAP_NAME,
    contributions_from_archives,
    heatmap_state_from_archives,
    load_heatmap_state_text,
    merge_heatmap_state,
    render_activity_map_html,
)
from .garmin_health import (
    auth_garmin as authorize_garmin,
    fetch_daily_health_archive,
    load_garmin_client,
    read_token_text as read_garmin_token_text,
    save_garmin_token,
)
from .health_corpus import (
    RETIRED_HEALTH_TOP_LEVEL_FILES,
    health_days_from_history,
    health_history_payload,
    health_raw_manifest_key,
    merge_health_history,
    normalize_health_archive,
    render_health_corpus,
)
from .render import is_run
from .state import load_state, save_state
from .strava import (
    StravaClient,
    StravaDailyRateLimitExceeded,
    StravaRequestBudgetExceeded,
    StravaShortRateLimitExceeded,
)

if TYPE_CHECKING:
    from .drive import DriveClient


APPDATA_STRAVA_TOKEN = "strava_token.json"
APPDATA_SYNC_STATE = "sync_state.json"
APPDATA_RUN_HISTORY = "run_history.json"
APPDATA_RAW_MANIFEST = "raw_manifest.json"
APPDATA_GARMIN_TOKEN = "garmin_token.json"
APPDATA_GARMIN_HEALTH_SYNC_STATE = "garmin_health_sync_state.json"
APPDATA_GARMIN_HEALTH_HISTORY = "garmin_health_history.json"
APPDATA_GARMIN_HEALTH_RAW_MANIFEST = "garmin_health_raw_manifest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync Strava activity history and Garmin Connect health data into Google Drive for ChatGPT."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-strava", help="Authorize Strava and store the OAuth token locally.")
    subparsers.add_parser("auth-google", help="Authorize Google Drive and store the OAuth token locally.")
    subparsers.add_parser("auth-garmin", help="Authorize Garmin Connect and store the tokenstore locally.")
    subparsers.add_parser("bootstrap-appdata", help="Upload the local Strava token into hidden Google Drive app data.")
    subparsers.add_parser(
        "bootstrap-garmin-appdata",
        help="Upload the local Garmin tokenstore into hidden Google Drive app data.",
    )

    publish_cache_parser = subparsers.add_parser(
        "publish-cache",
        help="Rebuild and upload Drive outputs from the local raw archive cache without calling Strava.",
    )
    publish_cache_parser.add_argument("--force-upload", action="store_true", help="Rewrite visible Drive files.")
    publish_cache_parser.add_argument("--recent-mile-days", type=int, default=14, help="Days of mile splits to expose in top-level files.")
    publish_cache_parser.add_argument("--no-upload", action="store_true", help="Write local files but skip Drive uploads.")
    publish_cache_parser.add_argument(
        "--trash-id-only-raw",
        action="store_true",
        help="Move legacy ID-only raw files in Drive Raw Data folders to trash after uploading named files.",
    )
    publish_cache_parser.add_argument(
        "--cleanup-local-id-only-raw",
        action="store_true",
        help="Delete legacy ID-only raw files from local output Raw Data folders.",
    )
    publish_cache_parser.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where stored history and raw manifest live. Defaults to env/auto.",
    )

    delete_run_parser = subparsers.add_parser(
        "delete-run",
        help="Remove one activity from run history and rebuild visible summary outputs.",
    )
    delete_run_parser.add_argument("activity_id", help="Strava/source activity ID to remove from run history.")
    delete_run_parser.add_argument("--dry-run", action="store_true", help="Print what would be removed without writing.")
    delete_run_parser.add_argument("--no-upload", action="store_true", help="Update history/local output but skip visible Drive trash/uploads.")
    delete_run_parser.add_argument("--recent-mile-days", type=int, default=14, help="Days of mile splits to expose in top-level files.")
    delete_run_parser.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where stored history and raw manifest live. Defaults to env/auto.",
    )

    sync = subparsers.add_parser("sync-strava", help="Fetch included Strava activities and publish the Drive corpus.")
    sync.add_argument("--days", type=int, default=14, help="How many days of Strava history to inspect.")
    sync.add_argument("--max-pages", type=int, default=5, help="Maximum Strava pages to fetch, 200 activities each.")
    sync.add_argument("--no-upload", action="store_true", help="Write local files and state but skip visible Drive uploads.")
    sync.add_argument("--dry-run", action="store_true", help="Print what would happen without writing or uploading.")
    sync.add_argument("--force-upload", action="store_true", help="Rewrite visible Drive files even if run history is unchanged.")
    sync.add_argument(
        "--enrich",
        choices=["none", "missing", "full"],
        default="missing",
        help="Fetch detailed activity and stream data: none, missing cached runs, or every fetched run.",
    )
    sync.add_argument(
        "--publish-raw",
        dest="publish_raw",
        action="store_true",
        default=None,
        help="Publish detailed raw archive files into the visible Raw Data Drive subfolder.",
    )
    sync.add_argument(
        "--no-publish-raw",
        dest="publish_raw",
        action="store_false",
        help="Skip visible Raw Data archive publishing.",
    )
    sync.add_argument(
        "--skip-maps",
        action="store_true",
        help="Skip heavyweight aggregate map and route outputs during scheduled syncs.",
    )
    sync.add_argument("--recent-mile-days", type=int, default=14, help="Days of mile splits to expose in top-level files.")
    sync.add_argument("--request-budget", type=int, default=900, help="Maximum Strava API requests to spend on this run.")
    sync.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where rotating Strava tokens and run history live. Defaults to env/auto.",
    )

    health_sync = subparsers.add_parser(
        "sync-garmin-health",
        help="Fetch Garmin Connect health data and publish the health corpus.",
    )
    add_health_sync_args(health_sync)

    health_backfill = subparsers.add_parser(
        "backfill-garmin-health",
        help="Fetch a date range of Garmin Connect health data.",
    )
    add_health_sync_args(health_backfill)

    sync_all = subparsers.add_parser("sync-all", help="Run Strava activity sync and Garmin health sync.")
    sync_all.add_argument("--days", type=int, default=14, help="How many days of Strava history to inspect.")
    sync_all.add_argument("--health-days", type=int, default=14, help="How many recent Garmin health days to fetch.")
    sync_all.add_argument("--max-pages", type=int, default=5, help="Maximum Strava pages to fetch, 200 activities each.")
    sync_all.add_argument("--no-upload", action="store_true", help="Write local files and state but skip visible Drive uploads.")
    sync_all.add_argument("--force-upload", action="store_true", help="Rewrite visible Drive files.")
    sync_all.add_argument(
        "--enrich",
        choices=["none", "missing", "full"],
        default="missing",
        help="Fetch detailed Strava activity and stream data.",
    )
    sync_all.add_argument(
        "--publish-raw",
        dest="publish_raw",
        action="store_true",
        default=None,
        help="Publish detailed raw Strava archive files.",
    )
    sync_all.add_argument(
        "--no-publish-raw",
        dest="publish_raw",
        action="store_false",
        help="Skip visible Strava raw archive publishing.",
    )
    sync_all.add_argument(
        "--skip-maps",
        action="store_true",
        help="Skip heavyweight aggregate map and route outputs during the Strava sync.",
    )
    sync_all.add_argument("--recent-mile-days", type=int, default=14, help="Days of mile splits to expose.")
    sync_all.add_argument("--request-budget", type=int, default=900, help="Maximum Strava API requests to spend.")
    sync_all.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where tokens and sync state live. Defaults to env/auto.",
    )

    args = parser.parse_args(argv)
    settings = get_settings()
    ensure_local_dirs(settings)

    if args.command == "auth-strava":
        return auth_strava(settings)
    if args.command == "auth-google":
        return auth_google(settings)
    if args.command == "auth-garmin":
        return auth_garmin(settings)
    if args.command == "bootstrap-appdata":
        return bootstrap_appdata(settings)
    if args.command == "bootstrap-garmin-appdata":
        return bootstrap_garmin_appdata(settings)
    if args.command == "publish-cache":
        return publish_cache(settings, args)
    if args.command == "delete-run":
        return delete_run(settings, args)
    if args.command == "sync-strava":
        return sync_strava(settings, args)
    if args.command == "sync-garmin-health":
        return sync_garmin_health(settings, args)
    if args.command == "backfill-garmin-health":
        return backfill_garmin_health(settings, args)
    if args.command == "sync-all":
        return sync_all_sources(settings, args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def add_health_sync_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=14, help="How many recent Garmin health days to fetch.")
    parser.add_argument("--start-date", help="First Garmin health date to fetch, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Last Garmin health date to fetch, YYYY-MM-DD. Defaults to today for ranges.")
    parser.add_argument("--force-refetch", action="store_true", help="Refetch range dates even if raw archives exist.")
    parser.add_argument("--no-upload", action="store_true", help="Write local files and state but skip visible Drive uploads.")
    parser.add_argument("--force-upload", action="store_true", help="Rewrite visible Drive files.")
    parser.add_argument(
        "--state-backend",
        choices=["auto", "local", "drive"],
        default=None,
        help="Where Garmin health tokens and state live. Defaults to env/auto.",
    )


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
    existing_manifest = drive.get_appdata_json(APPDATA_RAW_MANIFEST)
    if existing_manifest is None:
        drive.put_appdata_json(APPDATA_RAW_MANIFEST, {"schema_version": 1, "files": {}})
    print(f"Uploaded {APPDATA_STRAVA_TOKEN} to hidden Google Drive app data.")
    print("Render can now refresh and persist Strava tokens without using your PC.")
    return 0


def auth_garmin(settings: Settings) -> int:
    authorize_garmin(settings)
    print(f"Saved Garmin tokenstore to {settings.garmin_token_file}")
    return 0


def bootstrap_garmin_appdata(settings: Settings) -> int:
    token_text = read_garmin_token_text(settings.garmin_token_file)
    drive = drive_client(settings)
    drive.put_appdata_text(APPDATA_GARMIN_TOKEN, token_text, mime_type="application/json")
    if drive.get_appdata_json(APPDATA_GARMIN_HEALTH_HISTORY) is None:
        drive.put_appdata_json(APPDATA_GARMIN_HEALTH_HISTORY, health_history_payload([]))
    if drive.get_appdata_json(APPDATA_GARMIN_HEALTH_SYNC_STATE) is None:
        drive.put_appdata_json(APPDATA_GARMIN_HEALTH_SYNC_STATE, {})
    if drive.get_appdata_json(APPDATA_GARMIN_HEALTH_RAW_MANIFEST) is None:
        drive.put_appdata_json(APPDATA_GARMIN_HEALTH_RAW_MANIFEST, {"schema_version": 1, "files": {}})
    print(f"Uploaded {APPDATA_GARMIN_TOKEN} to hidden Google Drive app data.")
    print("Render can now reuse Garmin Connect tokens without local disk persistence.")
    return 0


def sync_strava(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise RuntimeError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env or GitHub Secrets first.")

    backend = resolve_state_backend(args.state_backend or settings.state_backend)
    drive = drive_client(settings) if backend == "drive" or not args.no_upload else None
    publish_raw = args.publish_raw if args.publish_raw is not None else backend == "drive"
    strava_token = load_strava_token(settings, backend=backend, drive=drive)

    after = datetime.now(timezone.utc) - timedelta(days=args.days)
    after_epoch = int(after.timestamp())

    strava = StravaClient(
        settings.strava_client_id,
        settings.strava_client_secret,
        settings.strava_token_file if backend == "local" else None,
        token=strava_token,
        on_token_update=(lambda token: save_strava_token(settings, token, backend=backend, drive=drive)),
        request_budget=args.request_budget,
    )
    activities = strava.iter_activities(after_epoch=after_epoch, max_pages=args.max_pages)
    existing_history = load_run_history(settings, backend=backend, drive=drive)
    sync_state = load_sync_state(settings, backend=backend, drive=drive)
    excluded_ids = sync_state_activity_ids(sync_state, "excluded_activity_ids")
    if excluded_ids:
        before_count = len(activities)
        activities = filter_excluded_activities(activities, excluded_ids)
        skipped_count = before_count - len(activities)
        if skipped_count:
            print(f"Skipped {skipped_count} excluded Strava activit{'y' if skipped_count == 1 else 'ies'}.")
    existing_runs = runs_from_history(existing_history)
    existing_runs_by_id = {
        str(run.get("source_activity_id")): run
        for run in existing_runs
        if isinstance(run, dict) and run.get("source_activity_id")
    }
    enrich_args = argparse.Namespace(**vars(args))
    if args.dry_run:
        enrich_args.enrich = "none"
    fetched_runs, archives, enrich_status = prepare_fetched_runs(settings, enrich_args, strava, activities, existing_runs_by_id)

    print(f"Fetched {len(activities)} Strava activities; {len(fetched_runs)} are included activities.")
    if enrich_status:
        print(enrich_status)
    if args.dry_run:
        for run in fetched_runs[:20]:
            print(f"- {run.get('local_date')} {run.get('name')} ({run.get('source_activity_id')})")
        if len(fetched_runs) > 20:
            print(f"...and {len(fetched_runs) - 20} more")
        return 0

    merged_runs = merge_run_history(existing_history, fetched_runs)
    current_digest = run_history_digest(merged_runs)
    raw_manifest = load_raw_manifest(settings, backend=backend, drive=drive)
    should_publish = args.force_upload or sync_state.get("last_published_digest") != current_digest
    should_render_outputs = args.no_upload or should_publish or bool(archives)
    render_maps = not getattr(args, "skip_maps", False)

    generated_files = []
    if should_render_outputs:
        route_features = None
        heatmap_state = None
        if render_maps:
            # Only fetch the (potentially large) existing routes + heatmap state from
            # Drive when we actually have something to render. On the common "nothing
            # new" tick this is skipped entirely, avoiding megabytes of download/parse.
            route_features = build_route_collection(
                settings, drive, publish_raw=publish_raw and not args.no_upload, archives=archives
            )
            heatmap_state = build_heatmap_state_collection(
                settings,
                drive,
                publish_raw=publish_raw and not args.no_upload,
                archives=archives,
                rebuild=False,
            )
        generated_files = render_corpus(
            merged_runs,
            settings.output_dir,
            markdown_as_google_docs=settings.google_upload_as_google_docs,
            recent_mile_days=args.recent_mile_days,
        )
        if render_maps and heatmap_state is not None:
            generated_files.extend(
                render_heatmap_outputs(
                    settings,
                    heatmap_state,
                    recent_days=args.recent_mile_days,
                    include_raw_state=publish_raw or args.no_upload,
                )
            )
        if publish_raw:
            generated_files.extend(
                render_raw_outputs(
                    settings,
                    archives,
                    route_features,
                    include_all_routes=render_maps,
                )
            )

    if merged_runs != existing_runs:
        save_run_history(settings, merged_runs, backend=backend, drive=drive)

    uploaded_count = 0
    trashed_map_count = 0
    trashed_year_doc_count = 0
    if not args.no_upload:
        if generated_files and should_render_outputs:
            if drive is None:
                drive = drive_client(settings)
            uploaded_count = upload_generated_files(
                settings,
                drive,
                generated_files,
                root_folder=get_run_drive_folder(settings, drive),
                raw_manifest=raw_manifest,
                force_upload=args.force_upload,
            )
            print(f"Uploaded or updated {uploaded_count} visible Drive files.")
            folder = get_run_drive_folder(settings, drive)
            trashed_year_doc_count = trash_legacy_top_level_year_docs(drive, folder["id"], past_summary_cutoff_year())
            trash_named_files_in_folder(drive, folder["id"], set(RETIRED_TOP_LEVEL_FILES))
            if generated_activity_maps(generated_files):
                trashed_map_count = trash_legacy_map_html_files(drive, folder["id"])
            if publish_raw:
                save_raw_manifest(settings, raw_manifest, backend=backend, drive=drive)
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
            "last_strava_included_activity_count": len(fetched_runs),
            "last_enriched_included_activity_count": sum(1 for run in fetched_runs if run.get("enriched")),
            "stored_run_count": len(merged_runs),
            "current_history_digest": current_digest,
        }
    )
    save_sync_state(settings, sync_state, backend=backend, drive=drive)

    # Additive Body Compass sink: mirror the merged history into Postgres (best-effort; never blocks
    # the Drive publish). Skipped on a no-op tick. `route_features` is present only when maps were
    # rendered (not on --skip-maps crons), so routes refresh on full/publish-cache runs.
    if settings.sql_sink_enabled and (merged_runs != existing_runs or args.force_upload):
        sql_sink.sync_runs(settings, merged_runs, route_features if should_render_outputs else None)

    if trashed_map_count:
        print(f"Moved {trashed_map_count} legacy route-only map HTML files to trash.")
    if trashed_year_doc_count:
        print(f"Moved {trashed_year_doc_count} legacy top-level yearly docs to trash (now in Past Summary).")
    print(f"Stored history contains {len(merged_runs)} included activities.")
    print(f"Local output: {settings.output_dir}")
    return 0


def sync_garmin_health(settings: Settings, args: argparse.Namespace) -> int:
    backend = resolve_state_backend(args.state_backend or settings.state_backend)
    drive = drive_client(settings) if backend == "drive" or not args.no_upload else None
    date_strings, range_mode = health_sync_dates(settings, args)
    raw_manifest = load_health_raw_manifest(settings, backend=backend, drive=drive)
    existing_history = load_health_history(settings, backend=backend, drive=drive)
    existing_days = health_days_from_history(existing_history)

    dates_to_fetch = []
    skipped_dates = []
    for cdate in date_strings:
        manifest_key = health_raw_manifest_key(cdate)
        if range_mode and not args.force_refetch and manifest_key in raw_manifest.get("files", {}):
            skipped_dates.append(cdate)
            continue
        dates_to_fetch.append(cdate)

    raw_archives: list[dict[str, Any]] = []
    if dates_to_fetch:
        api = load_garmin_client(settings, backend=backend, drive=drive)
        for cdate in dates_to_fetch:
            raw_archives.append(fetch_daily_health_archive(api, cdate))
        save_garmin_token(settings, backend=backend, drive=drive)

    fetched_days = [normalize_health_archive(archive) for archive in raw_archives]
    merged_days = merge_health_history(existing_history, fetched_days)
    current_digest = health_history_digest(merged_days)
    sync_state = load_health_sync_state(settings, backend=backend, drive=drive)
    should_publish = (
        args.force_upload
        or args.force_refetch
        or sync_state.get("last_published_digest") != current_digest
        or bool(raw_archives)
    )

    generated_files = []
    if args.no_upload or should_publish:
        generated_files = render_health_corpus(
            merged_days,
            raw_archives,
            settings.health_output_dir,
            markdown_as_google_docs=settings.google_upload_as_google_docs,
            recent_days=args.days,
        )

    if fetched_days or merged_days != existing_days:
        save_health_history(settings, merged_days, backend=backend, drive=drive)

    uploaded_count = 0
    if not args.no_upload:
        if generated_files and should_publish:
            if drive is None:
                drive = drive_client(settings)
            uploaded_count = upload_generated_files(
                settings,
                drive,
                generated_files,
                root_folder=get_health_drive_folder(settings, drive),
                raw_manifest=raw_manifest,
                force_upload=args.force_upload or args.force_refetch,
                raw_skip_prefixes=("Raw Health/19", "Raw Health/20", "Raw Health/21"),
            )
            trash_named_files_in_folder(
                drive,
                get_health_drive_folder(settings, drive)["id"],
                set(RETIRED_HEALTH_TOP_LEVEL_FILES),
            )
            save_health_raw_manifest(settings, raw_manifest, backend=backend, drive=drive)
            sync_state["last_published_digest"] = current_digest
            sync_state["last_published_at"] = datetime.now(timezone.utc).isoformat()
        else:
            print("Health history is unchanged; skipped visible Drive uploads.")

    sync_state.update(
        {
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
            "state_backend": backend,
            "last_health_requested_dates": len(date_strings),
            "last_health_fetched_dates": len(raw_archives),
            "last_health_skipped_dates": len(skipped_dates),
            "stored_health_day_count": len(merged_days),
            "current_history_digest": current_digest,
        }
    )
    save_health_sync_state(settings, sync_state, backend=backend, drive=drive)

    # Additive Body Compass sink (best-effort; never blocks the Drive publish).
    if settings.sql_sink_enabled and (fetched_days or merged_days != existing_days or args.force_upload):
        sql_sink.sync_health(settings, merged_days)

    print(f"Fetched Garmin health data for {len(raw_archives)} days; skipped {len(skipped_dates)} archived days.")
    if not args.no_upload:
        print(f"Uploaded or updated {uploaded_count} health Drive files.")
    print(f"Stored health history contains {len(merged_days)} days.")
    print(f"Local health output: {settings.health_output_dir}")
    return 0


def backfill_garmin_health(settings: Settings, args: argparse.Namespace) -> int:
    if not args.start_date:
        raise RuntimeError("backfill-garmin-health requires --start-date YYYY-MM-DD.")
    if not args.end_date:
        args.end_date = health_today(settings).isoformat()
    return sync_garmin_health(settings, args)


def sync_all_sources(settings: Settings, args: argparse.Namespace) -> int:
    strava_args = argparse.Namespace(
        days=args.days,
        max_pages=args.max_pages,
        no_upload=args.no_upload,
        dry_run=False,
        force_upload=args.force_upload,
        enrich=args.enrich,
        publish_raw=args.publish_raw,
        skip_maps=getattr(args, "skip_maps", False),
        recent_mile_days=args.recent_mile_days,
        request_budget=args.request_budget,
        state_backend=args.state_backend,
    )
    strava_result = sync_strava(settings, strava_args)
    health_args = argparse.Namespace(
        days=args.health_days,
        start_date=None,
        end_date=None,
        force_refetch=False,
        no_upload=args.no_upload,
        force_upload=args.force_upload,
        state_backend=args.state_backend,
    )
    try:
        sync_garmin_health(settings, health_args)
    except Exception as exc:
        print(f"Garmin health sync failed without interrupting Strava sync: {exc}", file=sys.stderr)
    return strava_result


def delete_run(settings: Settings, args: argparse.Namespace) -> int:
    activity_id = str(args.activity_id).strip()
    if not activity_id:
        raise RuntimeError("delete-run requires a non-empty activity ID.")

    backend = resolve_state_backend(args.state_backend or settings.state_backend)
    drive = drive_client(settings) if backend == "drive" else None
    existing_history = load_run_history(settings, backend=backend, drive=drive)
    existing_runs = runs_from_history(existing_history)
    deleted_runs = [
        run for run in existing_runs if str(run.get("source_activity_id") or "") == activity_id
    ]

    remaining_runs = [
        run for run in existing_runs if str(run.get("source_activity_id") or "") != activity_id
    ]
    raw_manifest = load_raw_manifest(settings, backend=backend, drive=drive)
    raw_paths = raw_relative_paths_for_runs(deleted_runs) | raw_manifest_paths_for_activity(raw_manifest, activity_id)
    years = years_for_runs(deleted_runs, raw_paths)
    local_files = local_activity_files(settings, activity_id, raw_paths)

    if deleted_runs:
        print(f"Found {len(deleted_runs)} run(s) for activity {activity_id}:")
        for run in deleted_runs:
            print(f"- {run.get('local_date') or 'unknown-date'} {run.get('name') or 'Untitled Run'}")
    else:
        print(f"No run-history entry found for activity {activity_id}; continuing cleanup and summary rebuild.")
    if raw_paths:
        print("Known raw paths:")
        for path in sorted(raw_paths):
            print(f"- {path}")
    if local_files:
        print("Local raw/cache files:")
        for path in local_files:
            print(f"- {path}")

    if args.dry_run:
        print("Dry run only; no history, local, or Drive files were changed.")
        return 0

    local_removed = remove_local_activity_files(local_files)
    remove_manifest = backend == "local" or not args.no_upload
    manifest_removed = (
        remove_raw_manifest_entries_for_activity(raw_manifest, activity_id, raw_paths)
        if remove_manifest
        else 0
    )
    drive_trashed = 0
    uploaded_count = 0
    trashed_year_doc_count = 0
    if drive is not None and not args.no_upload:
        folder = get_run_drive_folder(settings, drive)
        drive_trashed = trash_drive_activity_raw_files(
            drive,
            folder["id"],
            activity_id,
            raw_paths,
            years,
        )

    save_run_history(settings, remaining_runs, backend=backend, drive=drive)
    generated_files = render_corpus(
        remaining_runs,
        settings.output_dir,
        markdown_as_google_docs=settings.google_upload_as_google_docs,
        recent_mile_days=args.recent_mile_days,
    )

    current_digest = run_history_digest(remaining_runs)
    if not args.no_upload:
        if drive is None:
            drive = drive_client(settings)
        folder = get_run_drive_folder(settings, drive)
        uploaded_count = upload_generated_files(
            settings,
            drive,
            generated_files,
            root_folder=folder,
            raw_manifest=raw_manifest,
            force_upload=True,
        )
        trashed_year_doc_count = trash_legacy_top_level_year_docs(drive, folder["id"], past_summary_cutoff_year())
        trash_named_files_in_folder(drive, folder["id"], set(RETIRED_TOP_LEVEL_FILES))

    if remove_manifest:
        save_raw_manifest(settings, raw_manifest, backend=backend, drive=drive)

    sync_state = load_sync_state(settings, backend=backend, drive=drive)
    sync_state.update(
        {
            "last_delete_run_at": datetime.now(timezone.utc).isoformat(),
            "last_deleted_activity_id": activity_id,
            "excluded_activity_ids": sorted({*sync_state_activity_ids(sync_state, "excluded_activity_ids"), activity_id}),
            "stored_run_count": len(remaining_runs),
            "current_history_digest": current_digest,
        }
    )
    if not args.no_upload:
        sync_state["last_published_digest"] = current_digest
        sync_state["last_published_at"] = datetime.now(timezone.utc).isoformat()
    save_sync_state(settings, sync_state, backend=backend, drive=drive)

    print(f"Removed {len(deleted_runs)} run(s) from hidden history.")
    print(f"Removed {local_removed} local raw/cache file(s).")
    if not args.no_upload:
        print(f"Moved {drive_trashed} visible Drive raw file(s) to trash.")
        print(f"Removed {manifest_removed} raw manifest entr{'y' if manifest_removed == 1 else 'ies'}.")
        print(f"Uploaded or updated {uploaded_count} visible summary file(s).")
    else:
        print("Skipped visible Drive file trash/upload because --no-upload was set.")
    if trashed_year_doc_count:
        print(f"Moved {trashed_year_doc_count} legacy top-level yearly docs to trash (now in Past Summary).")
    print(f"Local output: {settings.output_dir}")
    return 0


def raw_relative_paths_for_runs(runs: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for run in runs:
        for key in ("raw_data_path", "route_geojson_path"):
            value = run.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(normalize_relative_path(value))
    return paths


def sync_state_activity_ids(sync_state: dict[str, Any], key: str) -> set[str]:
    values = sync_state.get(key)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if str(value).strip()}


def filter_excluded_activities(activities: list[dict[str, Any]], excluded_ids: set[str]) -> list[dict[str, Any]]:
    return [
        activity
        for activity in activities
        if str(activity.get("id") or activity.get("source_activity_id") or "") not in excluded_ids
    ]


def raw_manifest_paths_for_activity(raw_manifest: dict[str, Any], activity_id: str) -> set[str]:
    files = raw_manifest.get("files", {})
    if not isinstance(files, dict):
        return set()
    return {
        normalize_relative_path(str(key))
        for key in files
        if raw_manifest_key_matches_activity(normalize_relative_path(str(key)), activity_id)
    }


def years_for_runs(runs: list[dict[str, Any]], raw_paths: set[str]) -> set[str]:
    years: set[str] = set()
    for run in runs:
        for key in ("local_date", "start_date_local", "start_date"):
            value = str(run.get(key) or "")
            if len(value) >= 4 and value[:4].isdigit():
                years.add(value[:4])
    for path in raw_paths:
        parts = relative_path_parts(path)
        for folder_name in (RAW_RUNS_DIR, RAW_ROUTES_DIR):
            if folder_name in parts:
                index = parts.index(folder_name)
                if len(parts) > index + 1:
                    years.add(parts[index + 1])
    return years


def local_activity_files(settings: Settings, activity_id: str, raw_paths: set[str]) -> list[Path]:
    candidates: set[Path] = set()
    for raw_path in raw_paths:
        candidates.add(settings.output_dir.joinpath(*relative_path_parts(raw_path)))

    cache_root = settings.data_dir / "raw_archive" / RAW_RUNS_DIR
    output_roots = [
        settings.output_dir / RAW_DATA_DIR / RAW_RUNS_DIR,
        settings.output_dir / RAW_DATA_DIR / RAW_ROUTES_DIR,
        cache_root,
    ]
    for root in output_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and raw_file_name_matches_activity(path.name, activity_id):
                candidates.add(path)
    return sorted(path for path in candidates if path.exists())


def remove_local_activity_files(paths: list[Path]) -> int:
    removed = 0
    for path in paths:
        if not path.exists():
            continue
        path.unlink()
        removed += 1
    return removed


def remove_raw_manifest_entries_for_activity(
    raw_manifest: dict[str, Any],
    activity_id: str,
    raw_paths: set[str],
) -> int:
    files = raw_manifest.setdefault("files", {})
    if not isinstance(files, dict):
        raw_manifest["files"] = {}
        return 0
    removed = 0
    normalized_paths = {normalize_relative_path(path) for path in raw_paths}
    for key in list(files.keys()):
        normalized_key = normalize_relative_path(str(key))
        if normalized_key in normalized_paths or raw_manifest_key_matches_activity(normalized_key, activity_id):
            files.pop(key, None)
            removed += 1
    return removed


def trash_drive_activity_raw_files(
    drive: DriveClient,
    root_folder_id: str,
    activity_id: str,
    raw_paths: set[str],
    years: set[str],
) -> int:
    trashed_ids: set[str] = set()
    trashed = 0
    for raw_path in raw_paths:
        for item in drive_files_for_relative_path(drive, root_folder_id, raw_path):
            file_id = str(item.get("id") or "")
            if file_id and file_id not in trashed_ids:
                drive.trash_file(file_id)
                trashed_ids.add(file_id)
                trashed += 1

    for folder_name in (RAW_RUNS_DIR, RAW_ROUTES_DIR):
        parent = drive.find_folder_path(root_folder_id, (RAW_DATA_DIR, folder_name))
        if not parent:
            continue
        year_folders = [
            item
            for item in drive.list_files_in_folder(parent["id"])
            if item.get("mimeType") == "application/vnd.google-apps.folder"
            and (not years or str(item.get("name") or "") in years)
        ]
        for year_folder in year_folders:
            for item in drive.list_files_in_folder(year_folder["id"]):
                if not raw_file_name_matches_activity(str(item.get("name") or ""), activity_id):
                    continue
                file_id = str(item.get("id") or "")
                if file_id and file_id not in trashed_ids:
                    drive.trash_file(file_id)
                    trashed_ids.add(file_id)
                    trashed += 1
    return trashed


def drive_files_for_relative_path(
    drive: DriveClient,
    root_folder_id: str,
    relative_path: str,
) -> list[dict[str, Any]]:
    parts = relative_path_parts(relative_path)
    if not parts:
        return []
    folder = drive.find_folder_path(root_folder_id, tuple(parts[:-1]))
    if not folder:
        return []
    name = parts[-1]
    return [item for item in drive.list_files_in_folder(folder["id"]) if str(item.get("name") or "") == name]


def raw_manifest_key_matches_activity(key: str, activity_id: str) -> bool:
    parts = relative_path_parts(key)
    if len(parts) < 4 or parts[0] != RAW_DATA_DIR:
        return False
    if parts[1] not in {RAW_RUNS_DIR, RAW_ROUTES_DIR}:
        return False
    return raw_file_name_matches_activity(parts[-1], activity_id)


def raw_file_name_matches_activity(name: str, activity_id: str) -> bool:
    return name in {f"{activity_id}.json", f"{activity_id}.geojson"} or name.endswith(
        f"_{activity_id}.json"
    ) or name.endswith(f"_{activity_id}.geojson")


def normalize_relative_path(value: str) -> str:
    return "/".join(relative_path_parts(value))


def relative_path_parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[\\/]+", str(value).strip()) if part and part != ".")


def publish_cache(settings: Settings, args: argparse.Namespace) -> int:
    backend = resolve_state_backend(args.state_backend or settings.state_backend)
    drive = drive_client(settings) if backend == "drive" or not args.no_upload else None
    archives = load_cached_archives(settings)
    if not archives:
        raise RuntimeError(f"No cached raw archives found under {settings.data_dir / 'raw_archive'}.")

    existing_history = load_run_history(settings, backend=backend, drive=drive)
    cached_runs = []
    for archive in archives:
        activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else None
        if not activity or not is_run(activity):
            continue
        run = normalize_activity(activity)
        run.update(archive_enrichment_fields(archive))
        cached_runs.append(run)

    merged_runs = merge_run_history(existing_history, cached_runs)
    route_features = build_route_collection(settings, drive, publish_raw=not args.no_upload, archives=archives)
    heatmap_state = build_heatmap_state_collection(
        settings,
        drive,
        publish_raw=not args.no_upload,
        archives=archives,
        rebuild=True,
    )
    generated_files = render_corpus(
        merged_runs,
        settings.output_dir,
        markdown_as_google_docs=settings.google_upload_as_google_docs,
        recent_mile_days=args.recent_mile_days,
    )
    generated_files.extend(
        render_heatmap_outputs(
            settings,
            heatmap_state,
            recent_days=args.recent_mile_days,
            include_raw_state=True,
        )
    )
    generated_files.extend(render_raw_outputs(settings, archives, route_features))

    removed_local = remove_legacy_id_only_local_raw_files(settings.output_dir) if args.cleanup_local_id_only_raw else 0
    uploaded_count = 0
    trashed_count = 0
    trashed_map_count = 0
    trashed_year_doc_count = 0
    raw_manifest = load_raw_manifest(settings, backend=backend, drive=drive)
    if not args.no_upload:
        if drive is None:
            drive = drive_client(settings)
        uploaded_count = upload_generated_files(
            settings,
            drive,
            generated_files,
            root_folder=get_run_drive_folder(settings, drive),
            raw_manifest=raw_manifest,
            force_upload=args.force_upload,
        )
        folder = get_run_drive_folder(settings, drive)
        trashed_year_doc_count = trash_legacy_top_level_year_docs(drive, folder["id"], past_summary_cutoff_year())
        trash_named_files_in_folder(drive, folder["id"], set(RETIRED_TOP_LEVEL_FILES))
        if args.trash_id_only_raw:
            trashed_count = trash_legacy_id_only_raw_files(drive, folder["id"], raw_manifest)
        if generated_activity_maps(generated_files):
            trashed_map_count = trash_legacy_map_html_files(drive, folder["id"])
        save_raw_manifest(settings, raw_manifest, backend=backend, drive=drive)

    save_run_history(settings, merged_runs, backend=backend, drive=drive)
    sync_state = load_sync_state(settings, backend=backend, drive=drive)
    sync_state.update(
        {
            "last_publish_cache_at": datetime.now(timezone.utc).isoformat(),
            "last_publish_cache_archive_count": len(archives),
            "last_publish_cache_included_activity_count": len(cached_runs),
            "last_publish_cache_uploaded_count": uploaded_count,
        }
    )
    save_sync_state(settings, sync_state, backend=backend, drive=drive)

    print(f"Loaded {len(archives)} cached raw archives; {len(cached_runs)} match the configured activity types.")
    print(f"Uploaded or updated {uploaded_count} Drive files.")
    if trashed_count:
        print(f"Moved {trashed_count} legacy ID-only raw Drive files to trash.")
    if trashed_map_count:
        print(f"Moved {trashed_map_count} legacy route-only map HTML files to trash.")
    if trashed_year_doc_count:
        print(f"Moved {trashed_year_doc_count} legacy top-level yearly docs to trash (now in Past Summary).")
    if removed_local:
        print(f"Removed {removed_local} legacy ID-only local raw output files.")
    print(f"Local output: {settings.output_dir}")
    return 0


def drive_client(settings: Settings) -> DriveClient:
    from .drive import DriveClient

    return DriveClient(settings.google_client_secret_file, settings.google_token_file, settings.google_token_json)


def get_run_drive_folder(settings: Settings, drive: DriveClient) -> dict[str, Any]:
    if settings.use_legacy_drive_folder:
        return drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
    projects = drive.get_or_create_folder(
        settings.google_drive_projects_folder_name,
        settings.google_drive_projects_folder_id,
    )
    if settings.google_drive_run_folder_id:
        return drive.get_or_create_folder(settings.google_drive_run_folder_name, settings.google_drive_run_folder_id)
    return drive.get_or_create_child_folder(projects["id"], settings.google_drive_run_folder_name)


def get_health_drive_folder(settings: Settings, drive: DriveClient) -> dict[str, Any]:
    projects = drive.get_or_create_folder(
        settings.google_drive_projects_folder_name,
        settings.google_drive_projects_folder_id,
    )
    if settings.google_drive_health_folder_id:
        return drive.get_or_create_folder(settings.google_drive_health_folder_name, settings.google_drive_health_folder_id)
    return drive.get_or_create_child_folder(projects["id"], settings.google_drive_health_folder_name)


def load_cached_archives(settings: Settings) -> list[dict[str, Any]]:
    cache_root = settings.data_dir / "raw_archive" / RAW_RUNS_DIR
    if not cache_root.exists():
        return []

    archives: list[dict[str, Any]] = []
    for path in sorted(cache_root.rglob("*.json")):
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Skipped invalid cached archive: {path}")
            continue
        if isinstance(archive, dict) and isinstance(archive.get("activity"), dict):
            archives.append(archive)
    return archives


def remove_legacy_id_only_local_raw_files(output_dir: Path) -> int:
    removed = 0
    roots = [
        output_dir / RAW_DATA_DIR / RAW_RUNS_DIR,
        output_dir / RAW_DATA_DIR / RAW_ROUTES_DIR,
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and is_legacy_id_only_raw_name(path.name):
                path.unlink()
                removed += 1
    return removed


def trash_legacy_id_only_raw_files(drive: DriveClient, root_folder_id: str, raw_manifest: dict[str, Any]) -> int:
    trashed = 0
    for folder_name in (RAW_RUNS_DIR, RAW_ROUTES_DIR):
        parent = drive.find_folder_path(root_folder_id, (RAW_DATA_DIR, folder_name))
        if not parent:
            continue
        extension = "json" if folder_name == RAW_RUNS_DIR else "geojson"
        year_folders = [
            item
            for item in drive.list_files_in_folder(parent["id"])
            if item.get("mimeType") == "application/vnd.google-apps.folder"
        ]
        for year_folder in year_folders:
            items = drive.list_files_in_folder(year_folder["id"])
            present_named_ids = named_raw_activity_ids(items, extension)
            for item in items:
                name = str(item.get("name") or "")
                if not is_legacy_id_only_raw_name(name):
                    continue
                activity_id = name.split(".", maxsplit=1)[0]
                # Only trash an ID-only file once a date/name/id replacement exists.
                # Trust the Drive folder listing first; fall back to the manifest so we
                # still clean up even when the manifest missed earlier uploads.
                has_replacement = activity_id in present_named_ids or has_named_raw_replacement(
                    raw_manifest, folder_name, str(year_folder.get("name") or ""), activity_id
                )
                if not has_replacement:
                    continue
                drive.trash_file(item["id"])
                manifest_key = f"{RAW_DATA_DIR}/{folder_name}/{year_folder.get('name')}/{name}"
                raw_manifest.get("files", {}).pop(manifest_key, None)
                trashed += 1
    return trashed


def named_raw_activity_ids(items: list[dict[str, Any]], extension: str) -> set[str]:
    pattern = re.compile(rf"_(\d+)\.{extension}$")
    ids: set[str] = set()
    for item in items:
        name = str(item.get("name") or "")
        if is_legacy_id_only_raw_name(name):
            continue
        match = pattern.search(name)
        if match:
            ids.add(match.group(1))
    return ids


def trash_legacy_map_html_files(drive: DriveClient, root_folder_id: str) -> int:
    trashed = 0
    # Old map names, plus the current map names that used to live at the top level
    # before maps moved into the Maps/ subfolder.
    trashed += trash_named_files_in_folder(
        drive,
        root_folder_id,
        {"Recent Run Map.html", RECENT_ACTIVITY_MAP_NAME, ALL_TIME_ACTIVITY_MAP_NAME},
    )
    raw_folder = drive.find_folder_path(root_folder_id, (RAW_DATA_DIR,))
    if raw_folder:
        trashed += trash_named_files_in_folder(drive, raw_folder["id"], {"All Runs Map.html"})
    return trashed


def trash_legacy_top_level_year_docs(drive: DriveClient, root_folder_id: str, cutoff_year: int) -> int:
    """Trash top-level `Runs YYYY` docs for years that now live in Past Summary."""
    trashed = 0
    pattern = re.compile(r"^Runs (\d{4})(\.md)?$")
    for item in drive.list_files_in_folder(root_folder_id):
        if item.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        match = pattern.match(str(item.get("name") or ""))
        if not match or int(match.group(1)) >= cutoff_year:
            continue
        drive.trash_file(item["id"])
        trashed += 1
    return trashed


def trash_named_files_in_folder(drive: DriveClient, folder_id: str, names: set[str]) -> int:
    trashed = 0
    for item in drive.list_files_in_folder(folder_id):
        if str(item.get("name") or "") not in names:
            continue
        drive.trash_file(item["id"])
        trashed += 1
    return trashed


def generated_activity_maps(generated_files: list[GeneratedFile]) -> bool:
    generated_names = {generated.remote_name for generated in generated_files}
    return RECENT_ACTIVITY_MAP_NAME in generated_names and ALL_TIME_ACTIVITY_MAP_NAME in generated_names


def has_named_raw_replacement(raw_manifest: dict[str, Any], folder_name: str, year: str, activity_id: str) -> bool:
    extension = "json" if folder_name == RAW_RUNS_DIR else "geojson"
    prefix = f"{RAW_DATA_DIR}/{folder_name}/{year}/"
    suffix = f"_{activity_id}.{extension}"
    return any(
        key.startswith(prefix) and key.endswith(suffix)
        for key in raw_manifest.get("files", {})
    )


def is_legacy_id_only_raw_name(name: str) -> bool:
    return re.fullmatch(r"\d+\.(json|geojson)", name) is not None


def prepare_fetched_runs(
    settings: Settings,
    args: argparse.Namespace,
    strava: StravaClient,
    activities: list[dict[str, Any]],
    existing_runs_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    runs: list[dict[str, Any]] = []
    archives: list[dict[str, Any]] = []

    for activity in sorted(activities, key=activity_sort_key, reverse=True):
        if not is_run(activity):
            continue

        summary_run = normalize_activity(activity)
        activity_id = str(summary_run["source_activity_id"])
        existing_run = existing_runs_by_id.get(activity_id)
        run = merge_existing_enrichment(summary_run, existing_run)

        if should_enrich_run(args.enrich, run):
            try:
                archive = load_or_fetch_archive(settings, strava, activity, force_fetch=args.enrich == "full")
            except (StravaRequestBudgetExceeded, StravaDailyRateLimitExceeded, StravaShortRateLimitExceeded) as exc:
                runs.append(run)
                return runs, archives, str(exc)
            except requests.HTTPError as exc:
                print(f"Skipped enrichment for Strava activity {activity_id}: {http_error_label(exc)}")
                archive = None

            if archive:
                archive_activity = archive.get("activity") if isinstance(archive.get("activity"), dict) else activity
                run = merge_existing_enrichment(normalize_activity(archive_activity), existing_run)
                run.update(archive_enrichment_fields(archive))
                archives.append(archive)

        runs.append(run)

    return runs, archives, None


def activity_sort_key(activity: dict[str, Any]) -> tuple[str, str]:
    return (
        str(activity.get("start_date_local") or activity.get("start_date") or ""),
        str(activity.get("id") or ""),
    )


def should_enrich_run(enrich_mode: str, run: dict[str, Any]) -> bool:
    if enrich_mode == "none":
        return False
    if enrich_mode == "full":
        return True
    return not run.get("enriched") or not run.get("raw_data_path")


def merge_existing_enrichment(summary_run: dict[str, Any], existing_run: dict[str, Any] | None) -> dict[str, Any]:
    if not existing_run:
        return summary_run
    merged = dict(existing_run)
    merged.update(summary_run)
    for key in [
        "enriched",
        "enriched_at",
        "stream_types",
        "stream_sample_count",
        "mile_splits",
        "mile_split_count",
        "route_available",
        "raw_data_path",
        "route_geojson_path",
    ]:
        if key in existing_run:
            merged[key] = existing_run[key]
    return merged


def load_or_fetch_archive(
    settings: Settings,
    strava: StravaClient,
    activity: dict[str, Any],
    *,
    force_fetch: bool,
) -> dict[str, Any]:
    activity_id = str(activity["id"])
    year = year_for_activity(activity)
    if not force_fetch:
        cached = load_cached_archive(settings.data_dir, activity_id, year)
        if cached:
            return cached

    detailed = strava.get_activity(activity_id)
    try:
        streams = strava.get_activity_streams(activity_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {403, 404}:
            print(f"Streams unavailable for Strava activity {activity_id}; archiving detailed activity only.")
            streams = {}
        else:
            raise

    archive = build_raw_archive(detailed, streams)
    save_cached_archive(settings.data_dir, archive)
    return archive


def http_error_label(exc: requests.HTTPError) -> str:
    if exc.response is None:
        return str(exc)
    return f"HTTP {exc.response.status_code} {exc.response.reason}"


def build_route_collection(
    settings: Settings,
    drive: DriveClient | None,
    *,
    publish_raw: bool,
    archives: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_routes = None
    if publish_raw and drive is not None:
        folder = get_run_drive_folder(settings, drive)
        existing_routes = load_geojson_text(
            drive.get_text_file_by_path(folder["id"], (RAW_DATA_DIR,), ALL_ROUTES_NAME)
        )
    else:
        local_routes = settings.output_dir / RAW_DATA_DIR / ALL_ROUTES_NAME
        if local_routes.exists():
            existing_routes = load_geojson_text(local_routes.read_text(encoding="utf-8"))

    new_features = [
        archive["route"]
        for archive in archives
        if isinstance(archive.get("route"), dict)
    ]
    return merge_route_features(existing_routes, new_features)


def build_heatmap_state_collection(
    settings: Settings,
    drive: DriveClient | None,
    *,
    publish_raw: bool,
    archives: list[dict[str, Any]],
    rebuild: bool,
) -> dict[str, Any]:
    if rebuild:
        return heatmap_state_from_archives(archives)

    existing_state = None
    if publish_raw and drive is not None:
        folder = get_run_drive_folder(settings, drive)
        existing_state = load_heatmap_state_text(
            drive.get_text_file_by_path(folder["id"], (RAW_DATA_DIR, RAW_HEATMAP_DIR), HEATMAP_STATE_NAME)
        )
    else:
        local_state = settings.output_dir / RAW_DATA_DIR / RAW_HEATMAP_DIR / HEATMAP_STATE_NAME
        if local_state.exists():
            existing_state = load_heatmap_state_text(local_state.read_text(encoding="utf-8"))

    return merge_heatmap_state(existing_state, contributions_from_archives(archives))


def render_heatmap_outputs(
    settings: Settings,
    heatmap_state: dict[str, Any],
    *,
    recent_days: int,
    include_raw_state: bool,
) -> list[GeneratedFile]:
    maps_dir = settings.output_dir / MAPS_DIR
    maps_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (RECENT_ACTIVITY_MAP_NAME, ALL_TIME_ACTIVITY_MAP_NAME):
        stale_top_level = settings.output_dir / stale_name
        if stale_top_level.exists():
            stale_top_level.unlink()
    generated = [
        write_generated(
            maps_dir / RECENT_ACTIVITY_MAP_NAME,
            render_activity_map_html("Recent Activity Map", heatmap_state, recent_days=recent_days),
            remote_name=RECENT_ACTIVITY_MAP_NAME,
            mime_type="text/html",
            as_google_doc=False,
            remote_folder_parts=(MAPS_DIR,),
        ),
        write_generated(
            maps_dir / ALL_TIME_ACTIVITY_MAP_NAME,
            render_activity_map_html("All Time Activity Map", heatmap_state),
            remote_name=ALL_TIME_ACTIVITY_MAP_NAME,
            mime_type="text/html",
            as_google_doc=False,
            remote_folder_parts=(MAPS_DIR,),
        ),
    ]
    state_dir = settings.output_dir / RAW_DATA_DIR / RAW_HEATMAP_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    state_content = json.dumps(heatmap_state, indent=2, sort_keys=True) + "\n"
    state_path = state_dir / HEATMAP_STATE_NAME
    if include_raw_state:
        generated.append(
            write_generated(
                state_path,
                state_content,
                remote_name=HEATMAP_STATE_NAME,
                mime_type="application/json",
                as_google_doc=False,
                remote_folder_parts=(RAW_DATA_DIR, RAW_HEATMAP_DIR),
            )
        )
    else:
        state_path.write_text(state_content, encoding="utf-8")
    return generated


def render_raw_outputs(
    settings: Settings,
    archives: list[dict[str, Any]],
    route_features: dict[str, Any] | None,
    *,
    include_all_routes: bool = True,
) -> list[GeneratedFile]:
    generated: list[GeneratedFile] = []
    seen_paths: set[Path] = set()

    for archive in archives:
        for path in write_raw_archive_files(settings.output_dir, archive):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            generated.append(generated_file_from_path(settings.output_dir, path))

    if include_all_routes and route_features is not None:
        raw_dir = settings.output_dir / RAW_DATA_DIR
        raw_dir.mkdir(parents=True, exist_ok=True)
        generated.append(
            write_generated(
                raw_dir / ALL_ROUTES_NAME,
                json.dumps(route_features, indent=2, sort_keys=True) + "\n",
                remote_name=ALL_ROUTES_NAME,
                mime_type="application/geo+json",
                as_google_doc=False,
                remote_folder_parts=(RAW_DATA_DIR,),
            )
        )
    return generated


def generated_file_from_path(output_dir: Path, path: Path) -> GeneratedFile:
    relative_parts = path.relative_to(output_dir).parts
    suffix = path.suffix.lower()
    if suffix == ".geojson":
        mime_type = "application/geo+json"
    elif suffix == ".json":
        mime_type = "application/json"
    elif suffix == ".html":
        mime_type = "text/html"
    else:
        mime_type = "text/plain"
    return GeneratedFile(
        path=path,
        remote_name=relative_parts[-1],
        mime_type=mime_type,
        as_google_doc=False,
        changed=True,
        remote_folder_parts=tuple(relative_parts[:-1]),
    )


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


def load_raw_manifest(settings: Settings, *, backend: str, drive: DriveClient | None) -> dict[str, Any]:
    if backend == "local":
        state = load_state(settings.state_file)
        manifest = state.get("raw_manifest")
    else:
        if drive is None:
            raise RuntimeError("Drive state backend requires Google Drive credentials.")
        manifest = drive.get_appdata_json(APPDATA_RAW_MANIFEST)
    if not isinstance(manifest, dict):
        return {"schema_version": 1, "files": {}}
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("files", {})
    return manifest


def save_raw_manifest(
    settings: Settings,
    manifest: dict[str, Any],
    *,
    backend: str,
    drive: DriveClient | None,
) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    if backend == "local":
        state = load_state(settings.state_file)
        state["raw_manifest"] = manifest
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    drive.put_appdata_json(APPDATA_RAW_MANIFEST, manifest)


def load_health_history(settings: Settings, *, backend: str, drive: DriveClient | None) -> Any:
    if backend == "local":
        state = load_state(settings.state_file)
        return state.get("garmin_health_history", {"days": []})
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    return drive.get_appdata_json(APPDATA_GARMIN_HEALTH_HISTORY) or {"days": []}


def save_health_history(
    settings: Settings,
    days: list[dict[str, Any]],
    *,
    backend: str,
    drive: DriveClient | None,
) -> None:
    payload = health_history_payload(days)
    if backend == "local":
        state = load_state(settings.state_file)
        state["garmin_health_history"] = payload["days"]
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    drive.put_appdata_json(APPDATA_GARMIN_HEALTH_HISTORY, payload)


def load_health_sync_state(settings: Settings, *, backend: str, drive: DriveClient | None) -> dict[str, Any]:
    if backend == "local":
        state = load_state(settings.state_file)
        health_state = state.get("garmin_health_sync_state")
        return health_state if isinstance(health_state, dict) else {}
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    state = drive.get_appdata_json(APPDATA_GARMIN_HEALTH_SYNC_STATE) or {}
    return state if isinstance(state, dict) else {}


def save_health_sync_state(
    settings: Settings,
    update: dict[str, Any],
    *,
    backend: str,
    drive: DriveClient | None,
) -> None:
    if backend == "local":
        state = load_state(settings.state_file)
        health_state = state.get("garmin_health_sync_state")
        if not isinstance(health_state, dict):
            health_state = {}
        health_state.update(update)
        state["garmin_health_sync_state"] = health_state
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    state = drive.get_appdata_json(APPDATA_GARMIN_HEALTH_SYNC_STATE) or {}
    state.update(update)
    drive.put_appdata_json(APPDATA_GARMIN_HEALTH_SYNC_STATE, state)


def load_health_raw_manifest(settings: Settings, *, backend: str, drive: DriveClient | None) -> dict[str, Any]:
    if backend == "local":
        state = load_state(settings.state_file)
        manifest = state.get("garmin_health_raw_manifest")
    else:
        if drive is None:
            raise RuntimeError("Drive state backend requires Google Drive credentials.")
        manifest = drive.get_appdata_json(APPDATA_GARMIN_HEALTH_RAW_MANIFEST)
    if not isinstance(manifest, dict):
        return {"schema_version": 1, "files": {}}
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("files", {})
    return manifest


def save_health_raw_manifest(
    settings: Settings,
    manifest: dict[str, Any],
    *,
    backend: str,
    drive: DriveClient | None,
) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    if backend == "local":
        state = load_state(settings.state_file)
        state["garmin_health_raw_manifest"] = manifest
        save_state(settings.state_file, state)
        return
    if drive is None:
        raise RuntimeError("Drive state backend requires Google Drive credentials.")
    drive.put_appdata_json(APPDATA_GARMIN_HEALTH_RAW_MANIFEST, manifest)


def run_history_digest(runs: list[dict[str, Any]]) -> str:
    content = json.dumps(runs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def health_history_digest(days: list[dict[str, Any]]) -> str:
    content = json.dumps(days, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def health_sync_dates(settings: Settings, args: argparse.Namespace) -> tuple[list[str], bool]:
    if args.start_date:
        start = parse_date_arg(args.start_date, "start-date")
        end = parse_date_arg(args.end_date, "end-date") if args.end_date else health_today(settings)
        if end < start:
            raise RuntimeError("--end-date must be on or after --start-date.")
        return date_range(start, end), True
    if args.end_date:
        raise RuntimeError("--end-date requires --start-date.")

    days = max(1, int(args.days))
    end = health_today(settings)
    start = end - timedelta(days=days - 1)
    return date_range(start, end), False


def parse_date_arg(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid --{label}: {value}. Expected YYYY-MM-DD.") from exc


def date_range(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def health_today(settings: Settings) -> date:
    try:
        return datetime.now(ZoneInfo(settings.garmin_health_timezone)).date()
    except Exception:
        return date.today()


def upload_generated_files(
    settings: Settings,
    drive: DriveClient,
    generated_files: list[GeneratedFile],
    *,
    root_folder: dict[str, Any] | None = None,
    raw_manifest: dict[str, Any] | None = None,
    force_upload: bool = False,
    raw_skip_prefixes: tuple[str, ...] | None = None,
) -> int:
    folder = root_folder or get_run_drive_folder(settings, drive)
    folder_id = folder["id"]
    folder_cache: dict[tuple[str, ...], str] = {(): folder_id}

    count = 0
    for generated in generated_files:
        remote_parts = generated.remote_folder_parts
        manifest_key = "/".join((*remote_parts, generated.remote_name))
        if should_skip_raw_upload(manifest_key, raw_manifest, force_upload, raw_skip_prefixes):
            continue
        target_folder_id = folder_cache.get(remote_parts)
        if target_folder_id is None:
            target_folder = drive.get_or_create_folder_path(folder_id, remote_parts)
            target_folder_id = target_folder["id"]
            folder_cache[remote_parts] = target_folder_id

        drive.upload_text_file(
            generated.path,
            folder_id=target_folder_id,
            remote_name=generated.remote_name,
            as_google_doc=generated.as_google_doc,
            mime_type=generated.mime_type,
        )
        if raw_manifest is not None and remote_parts:
            raw_manifest.setdefault("files", {})[manifest_key] = {
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "local_path": str(generated.path),
                "mime_type": generated.mime_type,
            }
        count += 1
    return count


def should_skip_raw_upload(
    manifest_key: str,
    raw_manifest: dict[str, Any] | None,
    force_upload: bool,
    raw_skip_prefixes: tuple[str, ...] | None = None,
) -> bool:
    if force_upload or raw_manifest is None:
        return False
    if raw_skip_prefixes is not None:
        return any(manifest_key.startswith(prefix) for prefix in raw_skip_prefixes) and manifest_key in raw_manifest.get(
            "files", {}
        )
    if manifest_key == f"{RAW_DATA_DIR}/{ALL_ROUTES_NAME}":
        return False
    if not (
        manifest_key.startswith(f"{RAW_DATA_DIR}/{RAW_RUNS_DIR}/")
        or manifest_key.startswith(f"{RAW_DATA_DIR}/{RAW_ROUTES_DIR}/")
    ):
        return False
    return manifest_key in raw_manifest.get("files", {})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
