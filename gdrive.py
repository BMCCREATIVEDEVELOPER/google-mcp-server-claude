import io
import mimetypes
import os
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# MIME types that can be exported as plain text from Google Workspace formats
_EXPORT_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_GOOGLE_TYPES = set(_EXPORT_MAP.keys())


class DriveService:
    def __init__(self, credentials: Credentials, account_name: str = ""):
        self.service = build("drive", "v3", credentials=credentials)
        self.account_name = account_name

    # ------------------------------------------------------------------ list / search

    def list_files(
        self,
        max_results: int = 20,
        folder_id: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query_parts = ["trashed = false"]
        if folder_id:
            query_parts.append(f"'{folder_id}' in parents")
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")

        result = self.service.files().list(
            q=" and ".join(query_parts),
            pageSize=min(max_results, 50),
            fields="files(id,name,mimeType,size,modifiedTime,parents,webViewLink,owners)",
            orderBy="modifiedTime desc",
        ).execute()

        return [self._parse_file(f) for f in result.get("files", [])]

    def search_files(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        safe = query.replace("'", "\\'")
        q = f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false"

        result = self.service.files().list(
            q=q,
            pageSize=min(max_results, 50),
            fields="files(id,name,mimeType,size,modifiedTime,parents,webViewLink,owners)",
            orderBy="modifiedTime desc",
        ).execute()

        return [self._parse_file(f) for f in result.get("files", [])]

    def list_folders(self, max_results: int = 20) -> List[Dict[str, Any]]:
        return self.list_files(
            max_results=max_results,
            mime_type="application/vnd.google-apps.folder",
        )

    # ------------------------------------------------------------------ single file

    def get_file(self, file_id: str) -> Dict[str, Any]:
        f = self.service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,parents,webViewLink,owners,description",
        ).execute()
        return self._parse_file(f)

    def read_file(self, file_id: str) -> Dict[str, Any]:
        """Read file content. Google Docs/Sheets/Slides are exported as text/CSV.
        Binary files return a base64 note instead of raw bytes."""
        meta = self.service.files().get(
            fileId=file_id, fields="id,name,mimeType,size"
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)

        buf = io.BytesIO()

        if mime in _GOOGLE_TYPES:
            export_mime = _EXPORT_MAP[mime]
            req = self.service.files().export_media(fileId=file_id, mimeType=export_mime)
        elif mime.startswith("text/") or mime in (
            "application/json",
            "application/xml",
            "application/javascript",
        ):
            req = self.service.files().get_media(fileId=file_id)
        else:
            return {
                "file_id": file_id,
                "name": name,
                "mimeType": mime,
                "content": None,
                "note": f"Binary file ({mime}) — use drive_download_file to get raw bytes.",
            }

        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        content = buf.getvalue().decode("utf-8", errors="replace")
        return {
            "file_id": file_id,
            "name": name,
            "mimeType": mime,
            "content": content,
            "length": len(content),
        }

    def download_file(self, file_id: str) -> Dict[str, Any]:
        """Download raw bytes and return as utf-8 text (best-effort) or hex for binary."""
        meta = self.service.files().get(
            fileId=file_id, fields="id,name,mimeType,size"
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)

        if mime in _GOOGLE_TYPES:
            export_mime = _EXPORT_MAP[mime]
            req = self.service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            req = self.service.files().get_media(fileId=file_id)

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        raw = buf.getvalue()
        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = raw.hex()
            encoding = "hex"

        return {
            "file_id": file_id,
            "name": name,
            "mimeType": mime,
            "encoding": encoding,
            "size_bytes": len(raw),
            "content": content,
        }

    def upload_file(
        self,
        file_path: str,
        folder_id: Optional[str] = None,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a local file to Drive, optionally into a specific folder."""
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")

        resolved_mime = mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        body: Dict[str, Any] = {"name": name or os.path.basename(file_path)}
        if folder_id:
            body["parents"] = [folder_id]

        media = MediaFileUpload(file_path, mimetype=resolved_mime, resumable=True)
        f = self.service.files().create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,size,modifiedTime,parents,webViewLink,owners",
        ).execute()
        return self._parse_file(f)

    def create_folder(
        self,
        name: str,
        parent_folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new folder in Drive, optionally nested inside a parent folder."""
        body: Dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_folder_id:
            body["parents"] = [parent_folder_id]

        f = self.service.files().create(
            body=body,
            fields="id,name,mimeType,size,modifiedTime,parents,webViewLink,owners",
        ).execute()
        return self._parse_file(f)

    def delete_file(self, file_id: str) -> Dict[str, str]:
        """Move a file to trash."""
        self.service.files().update(
            fileId=file_id, body={"trashed": True}
        ).execute()
        return {"status": "trashed", "file_id": file_id}

    # ------------------------------------------------------------------ internals

    def _parse_file(self, f: Dict[str, Any]) -> Dict[str, Any]:
        owners = [o.get("emailAddress", "") for o in f.get("owners", [])]
        return {
            "id": f.get("id", ""),
            "name": f.get("name", ""),
            "mimeType": f.get("mimeType", ""),
            "size": f.get("size"),
            "modifiedTime": f.get("modifiedTime", ""),
            "webViewLink": f.get("webViewLink", ""),
            "owners": owners,
            "parents": f.get("parents", []),
            "description": f.get("description", ""),
        }
