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
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Fields returned for a single file/folder resource
_FILE_FIELDS = "id,name,mimeType,size,modifiedTime,parents,webViewLink,owners,driveId,description"


class DriveService:
    def __init__(self, credentials: Credentials, account_name: str = ""):
        self.service = build("drive", "v3", credentials=credentials)
        self.account_name = account_name

    # ------------------------------------------------------------------ list / search

    def list_files(
        self,
        max_results: int = 20,
        folder_id: Optional[str] = None,
        folder_name: Optional[str] = None,
        mime_type: Optional[str] = None,
        drive_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        folder_id = self._resolve_ref(folder_id, folder_name, mime_type=_FOLDER_MIME, label="folder")

        query_parts = ["trashed = false"]
        if folder_id:
            query_parts.append(f"'{folder_id}' in parents")
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")

        list_kwargs = self._all_drives_list_kwargs(drive_id)
        result = self.service.files().list(
            q=" and ".join(query_parts),
            pageSize=min(max_results, 50),
            fields=f"files({_FILE_FIELDS})",
            orderBy="modifiedTime desc",
            **list_kwargs,
        ).execute()

        return [self._parse_file(f) for f in result.get("files", [])]

    def search_files(
        self,
        query: str,
        max_results: int = 20,
        drive_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search by name/content, most recently modified first — except an
        exact name match (e.g. the query is a folder's literal name) is
        always pinned to the top, since it would otherwise get buried under
        unrelated, more-recently-touched partial matches."""
        safe = query.replace("'", "\\'")
        list_kwargs = self._all_drives_list_kwargs(drive_id)

        exact = self.service.files().list(
            q=f"name = '{safe}' and trashed = false",
            pageSize=min(max_results, 50),
            fields=f"files({_FILE_FIELDS})",
            **list_kwargs,
        ).execute().get("files", [])

        fuzzy_needed = max_results - len(exact)
        fuzzy = []
        if fuzzy_needed > 0:
            fuzzy = self.service.files().list(
                q=f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false",
                pageSize=min(fuzzy_needed, 50),
                fields=f"files({_FILE_FIELDS})",
                orderBy="modifiedTime desc",
                **list_kwargs,
            ).execute().get("files", [])

        seen = {f["id"] for f in exact}
        combined = exact + [f for f in fuzzy if f["id"] not in seen]
        return [self._parse_file(f) for f in combined[:max_results]]

    def list_folders(self, max_results: int = 20, drive_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.list_files(max_results=max_results, mime_type=_FOLDER_MIME, drive_id=drive_id)

    def list_shared_drives(self, max_results: int = 20) -> List[Dict[str, Any]]:
        """List Shared Drives (Team Drives) this account can access. Pass the
        returned id as drive_id to list_files/search_files, or as
        folder_id/parent_folder_id (a Shared Drive's id doubles as the id of
        its root folder) to create/upload/move directly into one."""
        result = self.service.drives().list(
            pageSize=min(max_results, 100), fields="drives(id,name)"
        ).execute()
        return [{"id": d["id"], "name": d["name"]} for d in result.get("drives", [])]

    def find_drive_id(self, name: str) -> str:
        """Resolve a Shared Drive's exact name to its id. Raises ValueError
        if nothing matches, or if the name is ambiguous."""
        safe = name.replace("'", "\\'")
        result = self.service.drives().list(
            q=f"name = '{safe}'", fields="drives(id,name)"
        ).execute()
        matches = result.get("drives", [])

        if not matches:
            raise ValueError(f"No shared drive found named {name!r}.")
        if len(matches) > 1:
            candidates = ", ".join(f"{m['id']} ({m['name']})" for m in matches)
            raise ValueError(
                f"Multiple shared drives named {name!r} found — use drive_id to "
                f"disambiguate. Candidates: {candidates}"
            )
        return matches[0]["id"]

    def delete_shared_drive(
        self,
        drive_id: Optional[str] = None,
        drive_name: Optional[str] = None,
    ) -> Dict[str, str]:
        """Permanently delete a Shared Drive. There is no trash for Shared
        Drives themselves — this cannot be undone. The drive must be empty
        first (permanently delete every item inside it via delete_file with
        permanent=True; a trashed-but-not-deleted item still counts as
        occupying the drive and will block this call)."""
        if drive_id and drive_name:
            raise ValueError("Provide either drive_id or drive_name, not both.")
        resolved = drive_id or (self.find_drive_id(drive_name) if drive_name else None)
        if not resolved:
            raise ValueError("delete_shared_drive requires drive_id or drive_name.")

        self.service.drives().delete(driveId=resolved).execute()
        return {"status": "deleted", "drive_id": resolved}

    # ------------------------------------------------------------------ single file

    def get_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("get_file requires file_id or file_name.")

        f = self.service.files().get(
            fileId=resolved,
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        return self._parse_file(f)

    def read_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read file content. Google Docs/Sheets/Slides are exported as text/CSV.
        Binary files return a base64 note instead of raw bytes."""
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("read_file requires file_id or file_name.")

        meta = self.service.files().get(
            fileId=resolved, fields="id,name,mimeType,size", supportsAllDrives=True
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", resolved)

        buf = io.BytesIO()

        if mime in _GOOGLE_TYPES:
            export_mime = _EXPORT_MAP[mime]
            req = self.service.files().export_media(fileId=resolved, mimeType=export_mime)
        elif mime.startswith("text/") or mime in (
            "application/json",
            "application/xml",
            "application/javascript",
        ):
            req = self.service.files().get_media(fileId=resolved, supportsAllDrives=True)
        else:
            return {
                "file_id": resolved,
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
            "file_id": resolved,
            "name": name,
            "mimeType": mime,
            "content": content,
            "length": len(content),
        }

    def download_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download raw bytes and return as utf-8 text (best-effort) or hex for binary."""
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("download_file requires file_id or file_name.")

        meta = self.service.files().get(
            fileId=resolved, fields="id,name,mimeType,size", supportsAllDrives=True
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", resolved)

        if mime in _GOOGLE_TYPES:
            export_mime = _EXPORT_MAP[mime]
            req = self.service.files().export_media(fileId=resolved, mimeType=export_mime)
        else:
            req = self.service.files().get_media(fileId=resolved, supportsAllDrives=True)

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
            "file_id": resolved,
            "name": name,
            "mimeType": mime,
            "encoding": encoding,
            "size_bytes": len(raw),
            "content": content,
        }

    def download_to_path(
        self,
        destination_dir: str,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        export_mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download a Drive file to a real local file (unlike download_file,
        which returns content inline as text/hex) — the missing link for
        attaching a Drive file to an email, since gmail_send/create_draft
        only accept local file paths. Google-native files (Docs/Sheets/
        Slides) are exported as PDF by default; pass export_mime_type to get
        a different format (e.g. the Office XML mimetypes for .docx/.xlsx/.pptx)."""
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("download_to_path requires file_id or file_name.")

        meta = self.service.files().get(
            fileId=resolved, fields="id,name,mimeType", supportsAllDrives=True
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", resolved)

        if mime in _GOOGLE_TYPES:
            export_mime = export_mime_type or "application/pdf"
            req = self.service.files().export_media(fileId=resolved, mimeType=export_mime)
            ext = mimetypes.guess_extension(export_mime) or ""
            out_name = name if not ext or name.lower().endswith(ext.lower()) else f"{name}{ext}"
        else:
            req = self.service.files().get_media(fileId=resolved, supportsAllDrives=True)
            out_name = name

        os.makedirs(destination_dir, exist_ok=True)
        out_path = os.path.join(destination_dir, out_name)

        with open(out_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, req)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        return {
            "file_id": resolved,
            "name": out_name,
            "mimeType": mime,
            "path": out_path,
            "size_bytes": os.path.getsize(out_path),
        }

    def upload_file(
        self,
        file_path: str,
        folder_id: Optional[str] = None,
        folder_name: Optional[str] = None,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a local file to Drive, optionally into a specific folder
        (including a folder inside a Shared Drive)."""
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")

        resolved_folder = self._resolve_ref(folder_id, folder_name, mime_type=_FOLDER_MIME, label="folder")
        resolved_mime = mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        body: Dict[str, Any] = {"name": name or os.path.basename(file_path)}
        if resolved_folder:
            body["parents"] = [resolved_folder]

        media = MediaFileUpload(file_path, mimetype=resolved_mime, resumable=True)
        f = self.service.files().create(
            body=body,
            media_body=media,
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        return self._parse_file(f)

    def create_folder(
        self,
        name: str,
        parent_folder_id: Optional[str] = None,
        parent_folder_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new folder in Drive, optionally nested inside a parent
        folder (including a folder inside a Shared Drive — pass the Shared
        Drive's own id as parent_folder_id to create at its root)."""
        resolved_parent = self._resolve_ref(
            parent_folder_id, parent_folder_name, mime_type=_FOLDER_MIME, label="parent folder"
        )
        body: Dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME}
        if resolved_parent:
            body["parents"] = [resolved_parent]

        f = self.service.files().create(
            body=body,
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        return self._parse_file(f)

    def delete_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        permanent: bool = False,
    ) -> Dict[str, str]:
        """Delete a file. By default moves it to trash (recoverable from
        Drive's trash). Pass permanent=True to bypass trash entirely and
        delete it outright — trashed items still count as occupying a
        Shared Drive, so permanent deletion is required to empty one before
        delete_shared_drive will accept it."""
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("delete_file requires file_id or file_name.")

        if permanent:
            self.service.files().delete(fileId=resolved, supportsAllDrives=True).execute()
            return {"status": "deleted", "file_id": resolved}

        self.service.files().update(
            fileId=resolved, body={"trashed": True}, supportsAllDrives=True
        ).execute()
        return {"status": "trashed", "file_id": resolved}

    # ------------------------------------------------------------------ sharing

    def add_permission(
        self,
        email: str,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        role: str = "writer",
        permission_type: str = "user",
        send_notification: bool = True,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Grant an email address access to a file, folder, or Shared Drive
        (pass the Shared Drive's own id/name as file_id/file_name to add it
        as a drive member rather than sharing one item). role is one of
        'reader', 'commenter', 'writer', 'fileOrganizer' (Content Manager),
        or 'organizer' (Manager, Shared Drives only)."""
        resolved = self._resolve_ref(file_id, file_name, label="file")
        if not resolved:
            raise ValueError("add_permission requires file_id or file_name.")
        if not email:
            raise ValueError("add_permission requires an email address.")

        body: Dict[str, Any] = {"type": permission_type, "role": role}
        if permission_type in ("user", "group"):
            body["emailAddress"] = email

        kwargs: Dict[str, Any] = {
            "fileId": resolved,
            "body": body,
            "supportsAllDrives": True,
            "sendNotificationEmail": send_notification,
            "fields": "id,type,role,emailAddress",
        }
        if message:
            kwargs["emailMessage"] = message

        result = self.service.permissions().create(**kwargs).execute()
        return {
            "file_id": resolved,
            "permission_id": result.get("id"),
            "type": result.get("type"),
            "role": result.get("role"),
            "email": result.get("emailAddress", email),
        }

    # ------------------------------------------------------------------ move / copy

    def move_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        folder_id: Optional[str] = None,
        folder_name: Optional[str] = None,
        remove_from_current: bool = True,
    ) -> Dict[str, Any]:
        """Move a file into a folder — including into or out of a Shared
        Drive. Drive files don't live "in" a single path — they're addressed
        by parent folder ID(s) — so a move is one files().update() call that
        adds the destination as a parent and (by default) removes the
        current parent(s). No copy or delete involved, so this works even
        when delete/trash actions are restricted."""
        resolved_file = self._resolve_ref(file_id, file_name, label="file")
        if not resolved_file:
            raise ValueError("move_file requires file_id or file_name.")

        resolved_folder = self._resolve_ref(
            folder_id, folder_name, mime_type=_FOLDER_MIME, label="destination folder"
        )
        if not resolved_folder:
            raise ValueError("move_file requires a destination folder_id or folder_name.")

        current = self.service.files().get(
            fileId=resolved_file, fields="parents", supportsAllDrives=True
        ).execute()
        previous_parents = ",".join(current.get("parents", []))

        kwargs: Dict[str, Any] = {
            "fileId": resolved_file,
            "addParents": resolved_folder,
            "fields": _FILE_FIELDS,
            "supportsAllDrives": True,
        }
        if remove_from_current and previous_parents:
            kwargs["removeParents"] = previous_parents

        f = self.service.files().update(**kwargs).execute()
        return self._parse_file(f)

    def copy_file(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        folder_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Copy a Drive file, optionally renaming the copy and/or placing it
        in a specific folder (including one inside a Shared Drive)."""
        resolved_file = self._resolve_ref(file_id, file_name, label="file")
        if not resolved_file:
            raise ValueError("copy_file requires file_id or file_name.")

        resolved_folder = self._resolve_ref(
            folder_id, folder_name, mime_type=_FOLDER_MIME, label="destination folder"
        )

        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if resolved_folder:
            body["parents"] = [resolved_folder]

        f = self.service.files().copy(
            fileId=resolved_file,
            body=body,
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        return self._parse_file(f)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _all_drives_list_kwargs(drive_id: Optional[str] = None) -> Dict[str, Any]:
        """Common kwargs for files().list() calls so results include Shared
        Drive items alongside My Drive, or are scoped to one Shared Drive."""
        kwargs: Dict[str, Any] = {
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            kwargs["driveId"] = drive_id
            kwargs["corpora"] = "drive"
        else:
            kwargs["corpora"] = "allDrives"
        return kwargs

    def _resolve_ref(
        self,
        id_value: Optional[str],
        name_value: Optional[str],
        mime_type: Optional[str] = None,
        label: str = "file",
    ) -> Optional[str]:
        """Resolve an (id, name) pair down to a single file ID. Returns None
        if neither is given — callers decide whether that's an error."""
        if id_value and name_value:
            raise ValueError(f"Provide either {label}_id or {label}_name, not both.")
        if id_value:
            return id_value
        if name_value:
            return self.find_file_id(name_value, mime_type=mime_type)
        return None

    def find_file_id(self, name: str, mime_type: Optional[str] = None) -> str:
        """Resolve an exact file/folder name to its Drive file ID, searching
        My Drive and every Shared Drive this account can access. Raises
        ValueError if nothing matches, or if the name is ambiguous (multiple
        files share it) — disambiguate with the corresponding *_id parameter
        instead of guessing."""
        safe = name.replace("'", "\\'")
        query_parts = [f"name = '{safe}'", "trashed = false"]
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")

        result = self.service.files().list(
            q=" and ".join(query_parts),
            pageSize=10,
            fields="files(id,name,mimeType,modifiedTime,driveId)",
            **self._all_drives_list_kwargs(),
        ).execute()
        matches = result.get("files", [])

        if not matches:
            raise ValueError(f"No file found named {name!r}.")
        if len(matches) > 1:
            candidates = "; ".join(
                f"{m['id']} (mimeType={m.get('mimeType')}, modified {m.get('modifiedTime', '?')}"
                f"{', shared drive' if m.get('driveId') else ''})"
                for m in matches
            )
            raise ValueError(
                f"Multiple files named {name!r} found — use the corresponding *_id "
                f"parameter to disambiguate. Candidates: {candidates}"
            )
        return matches[0]["id"]

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
            "drive_id": f.get("driveId"),
        }
