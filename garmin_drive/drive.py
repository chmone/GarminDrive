from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
DRIVE_SCOPES = [DRIVE_FILE_SCOPE, DRIVE_APPDATA_SCOPE]
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveClient:
    def __init__(self, client_secret_file: Path, token_file: Path, token_json: str | None = None) -> None:
        self.client_secret_file = client_secret_file
        self.token_file = token_file
        self.token_json = token_json
        self.service = build("drive", "v3", credentials=self._credentials())

    @classmethod
    def authorize(cls, client_secret_file: Path, token_file: Path) -> None:
        if not client_secret_file.exists():
            raise RuntimeError(f"Missing Google client secret file: {client_secret_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), DRIVE_SCOPES)
        credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")

    def _credentials(self) -> Credentials:
        loaded_from_env = False
        if self.token_json:
            credentials = Credentials.from_authorized_user_info(json.loads(self.token_json), DRIVE_SCOPES)
            loaded_from_env = True
        elif self.token_file.exists():
            credentials = Credentials.from_authorized_user_file(str(self.token_file), DRIVE_SCOPES)
        else:
            raise RuntimeError(
                f"Missing Google token at {self.token_file}. Run: python -m garmin_drive auth-google"
            )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            if not loaded_from_env:
                self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise RuntimeError("Google credentials are invalid. Run: python -m garmin_drive auth-google")
        return credentials

    def get_or_create_folder(self, folder_name: str, folder_id: str | None = None) -> dict[str, Any]:
        if folder_id:
            return self.service.files().get(fileId=folder_id, fields="id,name,webViewLink").execute()

        existing = self._find_first(
            f"mimeType = '{FOLDER_MIME}' and name = '{escape_query(folder_name)}' and trashed = false"
        )
        if existing:
            return existing

        metadata = {"name": folder_name, "mimeType": FOLDER_MIME}
        return self.service.files().create(body=metadata, fields="id,name,webViewLink").execute()

    def get_or_create_child_folder(self, parent_id: str, folder_name: str) -> dict[str, Any]:
        existing = self._find_first(
            f"mimeType = '{FOLDER_MIME}' and name = '{escape_query(folder_name)}' "
            f"and '{escape_query(parent_id)}' in parents and trashed = false"
        )
        if existing:
            return existing

        metadata = {"name": folder_name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self.service.files().create(body=metadata, fields="id,name,webViewLink").execute()

    def get_or_create_folder_path(self, root_folder_id: str, folder_parts: tuple[str, ...]) -> dict[str, Any]:
        current = {"id": root_folder_id}
        for folder_name in folder_parts:
            current = self.get_or_create_child_folder(current["id"], folder_name)
        return current

    def get_text_file_by_path(self, root_folder_id: str, folder_parts: tuple[str, ...], name: str) -> str | None:
        current_id = root_folder_id
        for folder_name in folder_parts:
            folder = self._find_first(
                f"mimeType = '{FOLDER_MIME}' and name = '{escape_query(folder_name)}' "
                f"and '{escape_query(current_id)}' in parents and trashed = false"
            )
            if not folder:
                return None
            current_id = folder["id"]

        existing = self._find_first(
            f"name = '{escape_query(name)}' and '{escape_query(current_id)}' in parents and trashed = false"
        )
        if not existing:
            return None
        request = self.service.files().get_media(fileId=existing["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue().decode("utf-8")

    def find_folder_path(self, root_folder_id: str, folder_parts: tuple[str, ...]) -> dict[str, Any] | None:
        current = {"id": root_folder_id}
        for folder_name in folder_parts:
            current = self._find_first(
                f"mimeType = '{FOLDER_MIME}' and name = '{escape_query(folder_name)}' "
                f"and '{escape_query(current['id'])}' in parents and trashed = false"
            )
            if not current:
                return None
        return current

    def list_files_in_folder(self, folder_id: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = None
        while True:
            result = (
                self.service.files()
                .list(
                    q=f"'{escape_query(folder_id)}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken,files(id,name,mimeType)",
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )
            files.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return files

    def trash_file(self, file_id: str) -> dict[str, Any]:
        return self.service.files().update(fileId=file_id, body={"trashed": True}, fields="id,name,trashed").execute()

    def upload_text_file(
        self,
        path: Path,
        *,
        folder_id: str,
        remote_name: str | None = None,
        as_google_doc: bool = False,
        mime_type: str = "text/plain",
    ) -> dict[str, Any]:
        name = remote_name or path.name
        existing = self._find_first(
            f"name = '{escape_query(name)}' and '{escape_query(folder_id)}' in parents and trashed = false"
        )

        metadata: dict[str, Any] = {"name": name}
        if as_google_doc:
            metadata["mimeType"] = GOOGLE_DOC_MIME
            upload_mime = "text/plain"
        else:
            upload_mime = mime_type

        media = MediaFileUpload(str(path), mimetype=upload_mime, resumable=False)

        if existing:
            return (
                self.service.files()
                .update(fileId=existing["id"], body=metadata, media_body=media, fields="id,name,webViewLink")
                .execute()
            )

        metadata["parents"] = [folder_id]
        return (
            self.service.files()
            .create(body=metadata, media_body=media, fields="id,name,webViewLink")
            .execute()
        )

    def get_appdata_text(self, name: str) -> str | None:
        existing = self._find_first(f"name = '{escape_query(name)}' and trashed = false", spaces="appDataFolder")
        if not existing:
            return None

        request = self.service.files().get_media(fileId=existing["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue().decode("utf-8")

    def put_appdata_text(self, name: str, content: str, mime_type: str = "application/json") -> dict[str, Any]:
        existing = self._find_first(f"name = '{escape_query(name)}' and trashed = false", spaces="appDataFolder")
        media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype=mime_type, resumable=False)
        metadata = {"name": name}

        if existing:
            return (
                self.service.files()
                .update(fileId=existing["id"], body=metadata, media_body=media, fields="id,name")
                .execute()
            )

        metadata["parents"] = ["appDataFolder"]
        return self.service.files().create(body=metadata, media_body=media, fields="id,name").execute()

    def get_appdata_json(self, name: str) -> Any | None:
        text = self.get_appdata_text(name)
        if text is None:
            return None
        return json.loads(text)

    def put_appdata_json(self, name: str, value: Any) -> dict[str, Any]:
        content = json.dumps(value, indent=2, sort_keys=True) + "\n"
        return self.put_appdata_text(name, content, mime_type="application/json")

    def _find_first(self, query: str, *, spaces: str = "drive") -> dict[str, Any] | None:
        result = (
            self.service.files()
            .list(q=query, spaces=spaces, fields="files(id,name,webViewLink,mimeType)", pageSize=1)
            .execute()
        )
        files = result.get("files", [])
        return files[0] if files else None


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
