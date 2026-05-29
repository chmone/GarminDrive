from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Strava run history into Google Drive for ChatGPT.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-strava", help="Authorize Strava and store the OAuth token locally.")
    subparsers.add_parser("auth-google", help="Authorize Google Drive and store the OAuth token locally.")
    subparsers.add_parser("bootstrap-appdata", help="Upload the local Strava token into hidden Google Drive app data.")

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
        help="Delete legacy ID-only raw files from local run_summaries Raw Data folders.",
    )
    publish_cache_parser.add_argument(
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
    sync.add_argument("--recent-mile-days", type=int, default=14, help="Days of mile splits to expose in top-level files.")
    sync.add_argument("--request-budget", type=int, default=900, help="Maximum Strava API requests to spend on this run.")
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
    if args.command == "publish-cache":
        return publish_cache(settings, args)
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
    existing_manifest = drive.get_appdata_json(APPDATA_RAW_MANIFEST)
    if existing_manifest is None:
        drive.put_appdata_json(APPDATA_RAW_MANIFEST, {"schema_version": 1, "files": {}})
    print(f"Uploaded {APPDATA_STRAVA_TOKEN} to hidden Google Drive app data.")
    print("GitHub Actions can now refresh and persist Strava tokens without using your PC.")
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
    sync_state = load_sync_state(settings, backend=backend, drive=drive)
    raw_manifest = load_raw_manifest(settings, backend=backend, drive=drive)
    should_publish = args.force_upload or sync_state.get("last_published_digest") != current_digest
    should_render_outputs = args.no_upload or should_publish or bool(archives)

    generated_files = []
    route_features = build_route_collection(settings, drive, publish_raw=publish_raw and not args.no_upload, archives=archives)
    heatmap_state = build_heatmap_state_collection(
        settings,
        drive,
        publish_raw=publish_raw and not args.no_upload,
        archives=archives,
        rebuild=False,
    )
    if should_render_outputs:
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
                include_raw_state=publish_raw or args.no_upload,
            )
        )
    raw_files: list[GeneratedFile] = []
    if publish_raw and should_render_outputs:
        raw_files = render_raw_outputs(settings, archives, route_features)
        generated_files.extend(raw_files)

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
                raw_manifest=raw_manifest,
                force_upload=args.force_upload,
            )
            print(f"Uploaded or updated {uploaded_count} visible Drive files.")
            folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
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

    if trashed_map_count:
        print(f"Moved {trashed_map_count} legacy route-only map HTML files to trash.")
    if trashed_year_doc_count:
        print(f"Moved {trashed_year_doc_count} legacy top-level yearly docs to trash (now in Past Summary).")
    print(f"Stored history contains {len(merged_runs)} included activities.")
    print(f"Local output: {settings.output_dir}")
    return 0


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
            raw_manifest=raw_manifest,
            force_upload=args.force_upload,
        )
        folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
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
        folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
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
        folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
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


def render_raw_outputs(settings: Settings, archives: list[dict[str, Any]], route_features: dict[str, Any]) -> list[GeneratedFile]:
    generated: list[GeneratedFile] = []
    seen_paths: set[Path] = set()

    for archive in archives:
        for path in write_raw_archive_files(settings.output_dir, archive):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            generated.append(generated_file_from_path(settings.output_dir, path))

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


def run_history_digest(runs: list[dict[str, Any]]) -> str:
    content = json.dumps(runs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def upload_generated_files(
    settings: Settings,
    drive: DriveClient,
    generated_files: list[GeneratedFile],
    *,
    raw_manifest: dict[str, Any] | None = None,
    force_upload: bool = False,
) -> int:
    folder = drive.get_or_create_folder(settings.google_drive_folder_name, settings.google_drive_folder_id)
    folder_id = folder["id"]
    folder_cache: dict[tuple[str, ...], str] = {(): folder_id}

    count = 0
    for generated in generated_files:
        remote_parts = generated.remote_folder_parts
        manifest_key = "/".join((*remote_parts, generated.remote_name))
        if should_skip_raw_upload(manifest_key, raw_manifest, force_upload):
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


def should_skip_raw_upload(manifest_key: str, raw_manifest: dict[str, Any] | None, force_upload: bool) -> bool:
    if force_upload or raw_manifest is None:
        return False
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
