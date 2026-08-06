"""
Gmail Multi-Account MCP Server
-------------------------------
Exposes Gmail operations for multiple Google accounts via the
Model Context Protocol (MCP) stdio transport.

Start with:  python server.py
Configure accounts in config.json and authenticate with: python setup_auth.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from auth import AuthManager
from config import get_accounts, get_client_secret_path, get_credentials_dir, load_config
from gcalendar import CalendarService
from gmail import GmailService
from gdrive import DriveService
from gdocs import DocsService
from gsheets import SheetsService
from gforms import FormsService

# ---------------------------------------------------------------------------
# Bootstrap: load config and auth manager at startup
# ---------------------------------------------------------------------------

try:
    _config = load_config()
    _accounts = get_accounts(_config)
    _credentials_dir = get_credentials_dir(_config)
    _client_secret_path = get_client_secret_path(_config)
    _auth = AuthManager(_credentials_dir, _client_secret_path)
except FileNotFoundError as exc:
    print(f"STARTUP ERROR: {exc}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_creds(account_name: str):
    """Return valid credentials for an account or raise ValueError."""
    if account_name not in _accounts:
        raise ValueError(
            f"Unknown account '{account_name}'. Available: {list(_accounts.keys())}"
        )
    creds = _auth.get_credentials(account_name)
    if creds is None:
        email = _accounts[account_name].get("email", account_name)
        raise ValueError(
            f"Account '{account_name}' ({email}) is not authenticated. "
            "Run 'python setup_auth.py' to authenticate."
        )
    return creds


def _get_service(account_name: str) -> GmailService:
    info = _accounts.get(account_name, {})
    return GmailService(
        _get_creds(account_name),
        account_name,
        signature_html=info.get("signature_html", ""),
        signature_image_path=info.get("signature_image_path", ""),
    )


def _get_calendar(account_name: str) -> CalendarService:
    return CalendarService(_get_creds(account_name), account_name)


def _get_drive(account_name: str) -> DriveService:
    return DriveService(_get_creds(account_name), account_name)


def _get_docs(account_name: str) -> DocsService:
    return DocsService(_get_creds(account_name), account_name)


def _get_sheets(account_name: str) -> SheetsService:
    return SheetsService(_get_creds(account_name), account_name)


def _get_forms(account_name: str) -> FormsService:
    return FormsService(_get_creds(account_name), account_name)


def _download_drive_attachments(account_name: str, drive_ids: list, tmp_dir: str) -> list:
    """Download each Drive file ID to tmp_dir for use as a real email
    attachment. Caller owns tmp_dir and is responsible for cleaning it up."""
    drive_svc = _get_drive(account_name)
    return [
        drive_svc.download_to_path(destination_dir=tmp_dir, file_id=fid)["path"]
        for fid in drive_ids
    ]


def _check_cross_account_conflicts(
    time_min: str, time_max: str, exclude_account: str
) -> Dict[str, list]:
    """Check every other configured account's primary calendar for busy periods
    overlapping the given window. Skips accounts that aren't authenticated."""
    conflicts: Dict[str, list] = {}
    for acct in _accounts:
        if acct == exclude_account:
            continue
        try:
            busy = _get_calendar(acct).get_busy_periods(time_min, time_max)
        except ValueError:
            continue
        if busy:
            conflicts[acct] = busy
    return conflicts


def _sync_linked_events(
    source_account: str, source_event_id: str, start_time: str = None, end_time: str = None
) -> list:
    """Update the blocking events on other accounts' primary calendars that were
    created for source_event_id, to match its new start/end time."""
    synced_on = []
    for acct in _accounts:
        if acct == source_account:
            continue
        try:
            cal = _get_calendar(acct)
        except ValueError:
            continue
        for linked in cal.find_linked_events(source_event_id):
            cal.update_event(
                event_id=linked["id"],
                start_time=start_time,
                end_time=end_time,
            )
            synced_on.append(acct)
    return synced_on


def _delete_linked_events(source_account: str, source_event_id: str) -> list:
    """Delete the blocking events on other accounts' primary calendars that were
    created for source_event_id."""
    deleted_on = []
    for acct in _accounts:
        if acct == source_account:
            continue
        try:
            cal = _get_calendar(acct)
        except ValueError:
            continue
        for linked in cal.find_linked_events(source_event_id):
            cal.delete_event(event_id=linked["id"])
            deleted_on.append(acct)
    return deleted_on


def _fmt(data: Any) -> list[types.TextContent]:
    if isinstance(data, str):
        return [types.TextContent(type="text", text=data)]
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("gmail-multi-account")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_accounts",
            description=(
                "List all Gmail accounts configured in this MCP server, "
                "along with their authentication status."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="gmail_get_profile",
            description="Get the Gmail profile (email address, message count, thread count) for an account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account name as defined in config.json (e.g. 'personal', 'work')",
                    }
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="gmail_search",
            description=(
                "Search emails using Gmail search syntax. "
                "Searches a single account or all accounts if 'account' is omitted. "
                "Example queries: 'from:boss@company.com is:unread', 'subject:invoice has:attachment'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account to search. Omit to search all configured accounts.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Gmail search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results per account (default 10, max 50)",
                        "default": 10,
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "Include full message body in results (slower). Default: false.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="gmail_read_message",
            description="Read the full content of a Gmail message by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account that owns the message",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID (from search results)",
                    },
                },
                "required": ["account", "message_id"],
            },
        ),
        types.Tool(
            name="gmail_read_thread",
            description="Read all messages in a Gmail thread/conversation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account that owns the thread",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID",
                    },
                },
                "required": ["account", "thread_id"],
            },
        ),
        types.Tool(
            name="gmail_send",
            description="Send an email from a specific Gmail account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account to send from",
                    },
                    "to": {
                        "type": "string",
                        "description": "Recipient(s), comma-separated",
                    },
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body (plain text). Always sent as a fallback for non-HTML clients."},
                    "html_body": {"type": "string", "description": "Optional HTML body. The account's configured signature is appended automatically."},
                    "cc": {"type": "string", "description": "CC recipients, comma-separated"},
                    "bcc": {"type": "string", "description": "BCC recipients, comma-separated"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of local file paths to attach.",
                    },
                    "drive_attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of Google Drive file IDs to attach as real files (downloaded and attached automatically — Docs/Sheets/Slides are exported as PDF). Combine with attachments for a mix of local and Drive files.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID to reply into. When set, the message is nested into the existing conversation instead of starting a new thread.",
                    },
                    "in_reply_to": {
                        "type": "string",
                        "description": "Message-ID header of the message being replied to. Sets the MIME In-Reply-To header (get this from a prior gmail_read_message/gmail_read_thread result's 'message-id' field).",
                    },
                    "references": {
                        "type": "string",
                        "description": "Space-separated chain of Message-IDs for the thread being replied to. Sets the MIME References header.",
                    },
                },
                "required": ["account", "to", "subject", "body"],
            },
        ),
        types.Tool(
            name="gmail_create_draft",
            description="Save an email as a draft in a specific Gmail account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Account to create the draft in",
                    },
                    "to": {"type": "string", "description": "Recipient(s), comma-separated"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body (plain text). Always sent as a fallback for non-HTML clients."},
                    "html_body": {"type": "string", "description": "Optional HTML body. The account's configured signature is appended automatically."},
                    "cc": {"type": "string", "description": "CC recipients"},
                    "bcc": {"type": "string", "description": "BCC recipients"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of local file paths to attach.",
                    },
                    "drive_attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of Google Drive file IDs to attach as real files (downloaded and attached automatically — Docs/Sheets/Slides are exported as PDF). Combine with attachments for a mix of local and Drive files.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID to reply into. When set, the draft is nested into the existing conversation instead of starting a new thread.",
                    },
                    "in_reply_to": {
                        "type": "string",
                        "description": "Message-ID header of the message being replied to. Sets the MIME In-Reply-To header (get this from a prior gmail_read_message/gmail_read_thread result's 'message-id' field).",
                    },
                    "references": {
                        "type": "string",
                        "description": "Space-separated chain of Message-IDs for the thread being replied to. Sets the MIME References header.",
                    },
                },
                "required": ["account", "to", "subject", "body"],
            },
        ),
        types.Tool(
            name="gmail_list_drafts",
            description="List draft emails in a Gmail account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max drafts to return (default 10)",
                        "default": 10,
                    },
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="gmail_get_draft",
            description="Get the full content (to/cc/bcc/subject/body) of a single draft by its draft ID. Use gmail_list_drafts first to find the ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "draft_id": {"type": "string", "description": "Draft ID (from gmail_list_drafts)"},
                },
                "required": ["account", "draft_id"],
            },
        ),
        types.Tool(
            name="gmail_update_draft",
            description=(
                "Edit an existing draft. Only the fields you pass are changed — anything omitted "
                "keeps its current value from the draft (pass an empty string to explicitly clear "
                "a field like cc/bcc). This rebuilds the draft's MIME message, so any existing file "
                "attachments are dropped unless you pass attachments again."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "draft_id": {"type": "string", "description": "Draft ID to update (from gmail_list_drafts)"},
                    "to": {"type": "string", "description": "New recipient(s), comma-separated. Omit to keep the current value."},
                    "subject": {"type": "string", "description": "New subject. Omit to keep the current value."},
                    "body": {"type": "string", "description": "New plain-text body. Omit to keep the current value."},
                    "html_body": {"type": "string", "description": "New HTML body. Omit to keep the current value (with its existing formatting, signature excluded to avoid duplication)."},
                    "cc": {"type": "string", "description": "New CC recipients. Omit to keep the current value."},
                    "bcc": {"type": "string", "description": "New BCC recipients. Omit to keep the current value."},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Local file paths to attach, replacing any existing attachments. Omit to drop attachments (Gmail's draft update always rebuilds the message).",
                    },
                    "drive_attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Google Drive file IDs to attach as real files, replacing any existing attachments (Docs/Sheets/Slides are exported as PDF). Combine with attachments for a mix of local and Drive files.",
                    },
                },
                "required": ["account", "draft_id"],
            },
        ),
        types.Tool(
            name="gmail_delete_draft",
            description="Permanently delete a draft (not the same as trashing a sent message — there is no undo).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "draft_id": {"type": "string", "description": "Draft ID to delete (from gmail_list_drafts)"},
                },
                "required": ["account", "draft_id"],
            },
        ),
        types.Tool(
            name="gmail_list_labels",
            description="List all labels and folders in a Gmail account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"}
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="gmail_modify_labels",
            description=(
                "Add or remove labels on a Gmail message. "
                "Common label IDs: STARRED, UNREAD, INBOX, SPAM, TRASH, IMPORTANT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "add_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to add (e.g. ['STARRED', 'UNREAD'])",
                    },
                    "remove_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to remove (e.g. ['UNREAD'])",
                    },
                },
                "required": ["account", "message_id"],
            },
        ),
        types.Tool(
            name="gmail_trash",
            description="Move a Gmail message to the Trash.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                },
                "required": ["account", "message_id"],
            },
        ),
        # ── Calendar tools ──────────────────────────────────────────────────
        types.Tool(
            name="calendar_list_calendars",
            description="List all Google Calendars available for an account (primary, work, shared, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="calendar_list_events",
            description=(
                "List upcoming calendar events for an account. "
                "Optionally filter by time range and calendar. "
                "Times must be in RFC3339 format, e.g. '2026-03-10T00:00:00Z'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary'). Use calendar_list_calendars to get IDs.",
                        "default": "primary",
                    },
                    "time_min": {
                        "type": "string",
                        "description": "Start of range (RFC3339). Defaults to now.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "End of range (RFC3339). Optional.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max events to return (default 20, max 50)",
                        "default": 20,
                    },
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="calendar_search",
            description="Search for events by keyword across a calendar (title, description, location, attendees).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "query": {"type": "string", "description": "Search keyword(s)"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary')",
                        "default": "primary",
                    },
                    "time_min": {
                        "type": "string",
                        "description": "Start of range (RFC3339). Defaults to now.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "End of range (RFC3339). Optional.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["account", "query"],
            },
        ),
        types.Tool(
            name="calendar_get_event",
            description="Get full details of a specific calendar event by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "event_id": {"type": "string", "description": "Event ID (from list or search results)"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["account", "event_id"],
            },
        ),
        types.Tool(
            name="calendar_create_event",
            description=(
                "Create a new event on a Google Calendar. "
                "Times must be in RFC3339 format, e.g. '2026-03-10T14:00:00Z'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "title": {"type": "string", "description": "Event title/summary"},
                    "start_time": {"type": "string", "description": "Start time (RFC3339)"},
                    "end_time": {"type": "string", "description": "End time (RFC3339)"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary')",
                        "default": "primary",
                    },
                    "description": {"type": "string", "description": "Event description"},
                    "location": {"type": "string", "description": "Event location"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attendee email addresses to invite",
                    },
                    "block_other_calendars": {
                        "type": "boolean",
                        "description": (
                            "If true (default), checks all other configured accounts for conflicts "
                            "and creates a matching busy-block event on each of their primary calendars, "
                            "titled '[<account> booking]'."
                        ),
                        "default": True,
                    },
                },
                "required": ["account", "title", "start_time", "end_time"],
            },
        ),
        types.Tool(
            name="calendar_update_event",
            description=(
                "Update an existing calendar event. Only the provided fields are changed. "
                "Times must be in RFC3339 format, e.g. '2026-03-10T14:00:00Z'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "event_id": {"type": "string", "description": "Event ID to update"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary')",
                        "default": "primary",
                    },
                    "title": {"type": "string", "description": "New event title/summary"},
                    "start_time": {"type": "string", "description": "New start time (RFC3339)"},
                    "end_time": {"type": "string", "description": "New end time (RFC3339)"},
                    "description": {"type": "string", "description": "New event description"},
                    "location": {"type": "string", "description": "New event location"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replaces the attendee list with these email addresses",
                    },
                },
                "required": ["account", "event_id"],
            },
        ),
        types.Tool(
            name="calendar_delete_event",
            description="Delete a calendar event.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "event_id": {"type": "string", "description": "Event ID to delete"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["account", "event_id"],
            },
        ),
        # ── Drive tools ─────────────────────────────────────────────────────
        types.Tool(
            name="drive_list_files",
            description=(
                "List files in Google Drive for an account, ordered by most recently modified. "
                "Includes files in My Drive and every Shared Drive the account can access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "max_results": {"type": "integer", "description": "Max files to return (default 20)", "default": 20},
                    "folder_id": {"type": "string", "description": "Limit to files inside this folder ID. Alternative to folder_name. Omit both for all files."},
                    "folder_name": {"type": "string", "description": "Limit to files inside the folder with this exact name. Alternative to folder_id."},
                    "mime_type": {"type": "string", "description": "Filter by MIME type, e.g. 'application/vnd.google-apps.document'"},
                    "drive_id": {"type": "string", "description": "Scope the listing to one Shared Drive (get its id from drive_list_shared_drives). Omit to search My Drive plus all Shared Drives."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_search",
            description=(
                "Search Google Drive files by name or content across an account. "
                "Includes files in My Drive and every Shared Drive the account can access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "query": {"type": "string", "description": "Search keyword(s) — matches file names and full text"},
                    "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    "drive_id": {"type": "string", "description": "Scope the search to one Shared Drive (get its id from drive_list_shared_drives). Omit to search My Drive plus all Shared Drives."},
                },
                "required": ["account", "query"],
            },
        ),
        types.Tool(
            name="drive_list_folders",
            description="List all folders in Google Drive for an account, including folders inside Shared Drives.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "max_results": {"type": "integer", "description": "Max folders to return (default 20)", "default": 20},
                    "drive_id": {"type": "string", "description": "Scope the listing to one Shared Drive (get its id from drive_list_shared_drives). Omit to search My Drive plus all Shared Drives."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_list_shared_drives",
            description=(
                "List the Shared Drives (Team Drives) this account can access. Use a returned "
                "drive id as drive_id in drive_list_files/drive_search to scope to that drive, "
                "or as a folder_id/parent_folder_id to create or upload directly into its root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "max_results": {"type": "integer", "description": "Max shared drives to return (default 20)", "default": 20},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_get_file",
            description="Get metadata for a specific Google Drive file, identified by ID or exact name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact file name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple files share it)."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_read_file",
            description=(
                "Read the text content of a Google Drive file, identified by ID or exact name. "
                "Google Docs export as plain text, Sheets as CSV, Slides as plain text. "
                "Binary files (images, PDFs) are not readable — use drive_download_file instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact file name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple files share it)."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_download_file",
            description="Download the raw content of a Google Drive file, identified by ID or exact name. Returns UTF-8 text or hex for binary files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact file name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple files share it)."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_download_to_file",
            description=(
                "Download a Google Drive file to a real local file on disk, identified by ID or "
                "exact name — unlike drive_download_file (which returns content inline as text/hex), "
                "this gives you a local path, e.g. to then attach the file to an email with "
                "gmail_send/gmail_create_draft's attachments parameter. Google Docs/Sheets/Slides "
                "are exported as PDF by default."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact file name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple files share it)."},
                    "destination_dir": {"type": "string", "description": "Local directory to save into. Defaults to the system temp directory."},
                    "export_mime_type": {"type": "string", "description": "For Google Docs/Sheets/Slides only: override the export format, e.g. 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' for .docx. Defaults to PDF."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_delete_file",
            description=(
                "Delete a Google Drive file, identified by ID or exact name. By default moves it "
                "to trash (recoverable); pass permanent=true to delete outright, bypassing trash — "
                "required to empty a Shared Drive before drive_delete_shared_drive will accept it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact file name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple files share it)."},
                    "permanent": {"type": "boolean", "description": "Delete outright instead of moving to trash. Cannot be undone. Default: false.", "default": False},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_delete_shared_drive",
            description=(
                "Permanently delete a Shared Drive, identified by ID or exact name. There is no "
                "trash for Shared Drives — this cannot be undone. The drive must be empty first "
                "(use drive_delete_file with permanent=true on every item inside it)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "drive_id": {"type": "string", "description": "Shared Drive ID. Alternative to drive_name."},
                    "drive_name": {"type": "string", "description": "Exact name of the Shared Drive. Alternative to drive_id. Errors if the name is ambiguous."},
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_share",
            description=(
                "Grant an email address access to a Drive file, folder, or Shared Drive "
                "(identified by ID or exact name). To add someone as a member of a Shared "
                "Drive itself, pass that Shared Drive's own id/name (from drive_list_shared_drives) "
                "as file_id/file_name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file/folder/Shared Drive ID. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact name to look up. Alternative to file_id. Errors if the name is ambiguous (multiple items share it)."},
                    "email": {"type": "string", "description": "Email address to grant access to"},
                    "role": {
                        "type": "string",
                        "description": (
                            "Access level. 'reader' (view only), 'commenter' (view + comment), "
                            "'writer' (Contributor: view/add/edit), 'fileOrganizer' (Content Manager: "
                            "also move/delete files — Shared Drives only), or 'organizer' (Manager: "
                            "full control including membership — Shared Drives only). Default: 'writer'."
                        ),
                        "enum": ["reader", "commenter", "writer", "fileOrganizer", "organizer"],
                        "default": "writer",
                    },
                    "send_notification": {"type": "boolean", "description": "Send an email notifying the recipient. Default: true.", "default": True},
                    "message": {"type": "string", "description": "Optional custom message to include in the notification email."},
                },
                "required": ["account", "email"],
            },
        ),
        types.Tool(
            name="drive_create_folder",
            description=(
                "Create a new folder in Google Drive, optionally nested inside a parent folder "
                "(identified by ID or exact name). To create at the root of a Shared Drive, pass "
                "that Shared Drive's own id (from drive_list_shared_drives) as parent_folder_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "name": {"type": "string", "description": "Name of the folder to create"},
                    "parent_folder_id": {"type": "string", "description": "Parent folder ID to create the new folder inside. Alternative to parent_folder_name. Omit both for Drive root."},
                    "parent_folder_name": {"type": "string", "description": "Exact name of the parent folder. Alternative to parent_folder_id."},
                },
                "required": ["account", "name"],
            },
        ),
        types.Tool(
            name="drive_upload_file",
            description=(
                "Upload a local file to Google Drive, optionally into a specific folder "
                "(identified by ID or exact name). To upload to the root of a Shared Drive, "
                "pass that Shared Drive's own id (from drive_list_shared_drives) as folder_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_path": {"type": "string", "description": "Absolute path to the local file to upload"},
                    "folder_id": {"type": "string", "description": "Destination folder ID. Alternative to folder_name. Omit both to upload to Drive root."},
                    "folder_name": {"type": "string", "description": "Exact name of the destination folder. Alternative to folder_id."},
                    "name": {"type": "string", "description": "Name to give the uploaded file. Defaults to the local file name."},
                    "mime_type": {"type": "string", "description": "MIME type override. Auto-detected from the file extension if omitted."},
                },
                "required": ["account", "file_path"],
            },
        ),
        types.Tool(
            name="drive_move_file",
            description=(
                "Move a Google Drive file into a folder, identified by ID or exact name. Uses Drive's "
                "parent reassignment (a single add/remove-parents call), not copy-then-delete — "
                "so it works even in contexts where delete/trash actions are blocked."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID to move. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact name of the file to move. Alternative to file_id. Errors if the name is ambiguous."},
                    "folder_id": {"type": "string", "description": "Destination folder ID. Alternative to folder_name."},
                    "folder_name": {"type": "string", "description": "Exact name of the destination folder. Alternative to folder_id."},
                    "remove_from_current": {
                        "type": "boolean",
                        "description": "Remove the file from its current folder(s) so it ends up only in the destination (true), or leave it there too so it appears in both places (false). Default: true.",
                        "default": True,
                    },
                },
                "required": ["account"],
            },
        ),
        types.Tool(
            name="drive_copy_file",
            description="Copy a Google Drive file (identified by ID or exact name), optionally renaming the copy and/or placing it in a specific folder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "file_id": {"type": "string", "description": "Google Drive file ID to copy. Alternative to file_name."},
                    "file_name": {"type": "string", "description": "Exact name of the file to copy. Alternative to file_id. Errors if the name is ambiguous."},
                    "name": {"type": "string", "description": "Name for the copy. Defaults to 'Copy of <original name>'."},
                    "folder_id": {"type": "string", "description": "Destination folder ID for the copy. Alternative to folder_name. Omit both to place it alongside the original."},
                    "folder_name": {"type": "string", "description": "Exact name of the destination folder for the copy. Alternative to folder_id."},
                },
                "required": ["account"],
            },
        ),
        # ── Docs tools ──────────────────────────────────────────────────────
        types.Tool(
            name="docs_create",
            description="Create a new Google Doc, optionally seeded with initial text content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "title": {"type": "string", "description": "Title of the new document"},
                    "content": {"type": "string", "description": "Initial plain-text content to insert into the document. Optional."},
                },
                "required": ["account", "title"],
            },
        ),
        types.Tool(
            name="docs_read",
            description="Read a Google Doc's title and plain-text content by its document ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "document_id": {"type": "string", "description": "Google Docs document ID (also works with a Drive file ID for a Doc)"},
                },
                "required": ["account", "document_id"],
            },
        ),
        types.Tool(
            name="docs_update",
            description=(
                "Update a Google Doc: append text to the end and/or find-and-replace text "
                "across the whole document. Provide append_text and/or find_text/replace_text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "document_id": {"type": "string", "description": "Google Docs document ID"},
                    "append_text": {"type": "string", "description": "Text to append to the end of the document. Optional."},
                    "find_text": {"type": "string", "description": "Text to find and replace, across the whole document. Optional — pair with replace_text."},
                    "replace_text": {"type": "string", "description": "Replacement text used when find_text is set. Defaults to empty string (i.e. deletes find_text)."},
                    "match_case": {"type": "boolean", "description": "Whether find_text matching is case-sensitive. Default: false.", "default": False},
                },
                "required": ["account", "document_id"],
            },
        ),
        types.Tool(
            name="docs_format",
            description=(
                "Apply character formatting (bold, italic, underline, strikethrough, "
                "font size, font family, text color, highlight color) to text in a "
                "Google Doc. Target the text either by find_text (formats the first "
                "match, or every match when match_all is true) or by an explicit "
                "start_index/end_index range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "document_id": {"type": "string", "description": "Google Docs document ID"},
                    "find_text": {"type": "string", "description": "Text to locate and format. Optional — alternative to start_index/end_index."},
                    "match_case": {"type": "boolean", "description": "Whether find_text matching is case-sensitive. Default: false.", "default": False},
                    "match_all": {"type": "boolean", "description": "Format every occurrence of find_text, not just the first. Default: true.", "default": True},
                    "start_index": {"type": "integer", "description": "Start index of the range to format (alternative to find_text). Must be paired with end_index."},
                    "end_index": {"type": "integer", "description": "End index (exclusive) of the range to format (alternative to find_text). Must be paired with start_index."},
                    "bold": {"type": "boolean", "description": "Set (true) or clear (false) bold."},
                    "italic": {"type": "boolean", "description": "Set (true) or clear (false) italic."},
                    "underline": {"type": "boolean", "description": "Set (true) or clear (false) underline."},
                    "strikethrough": {"type": "boolean", "description": "Set (true) or clear (false) strikethrough."},
                    "font_size": {"type": "number", "description": "Font size in points, e.g. 14."},
                    "font_family": {"type": "string", "description": "Font family name, e.g. 'Arial'."},
                    "foreground_color": {"type": "string", "description": "Text color as a hex string, e.g. '#FF0000'."},
                    "background_color": {"type": "string", "description": "Highlight color as a hex string, e.g. '#FFFF00'."},
                },
                "required": ["account", "document_id"],
            },
        ),
        types.Tool(
            name="docs_rename",
            description="Rename a Google Doc (changes its title/file name).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "document_id": {"type": "string", "description": "Google Docs document ID"},
                    "title": {"type": "string", "description": "New title for the document"},
                },
                "required": ["account", "document_id", "title"],
            },
        ),
        types.Tool(
            name="docs_delete",
            description="Move a Google Doc to Drive trash.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "document_id": {"type": "string", "description": "Google Docs document ID"},
                },
                "required": ["account", "document_id"],
            },
        ),
        # ── Sheets tools ─────────────────────────────────────────────────────
        types.Tool(
            name="sheets_create",
            description="Create a new Google Sheet, optionally seeded with initial row data on its first tab.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "title": {"type": "string", "description": "Title of the new spreadsheet"},
                    "data": {
                        "type": "array",
                        "description": "Initial rows to write, as a 2D array (each inner array is one row of cell values). Optional.",
                        "items": {"type": "array", "items": {}},
                    },
                    "sheet_name": {"type": "string", "description": "Name of the first tab. Default: 'Sheet1'.", "default": "Sheet1"},
                },
                "required": ["account", "title"],
            },
        ),
        types.Tool(
            name="sheets_info",
            description="Get a spreadsheet's title, URL, and the list of tabs (sheets) it contains, with each tab's id/title/row and column count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                },
                "required": ["account", "spreadsheet_id"],
            },
        ),
        types.Tool(
            name="sheets_read",
            description="Read cell values for a range, e.g. 'Sheet1!A1:D10'. Defaults to the whole first tab if range is omitted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "range": {"type": "string", "description": "A1-notation range, e.g. 'Sheet1!A1:D10' or 'Sheet1' for the whole tab. Optional — defaults to the whole first tab."},
                },
                "required": ["account", "spreadsheet_id"],
            },
        ),
        types.Tool(
            name="sheets_update",
            description="Overwrite cell values for a range with a 2D array of rows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "range": {"type": "string", "description": "A1-notation range to write into, e.g. 'Sheet1!A1'"},
                    "values": {
                        "type": "array",
                        "description": "Rows to write, as a 2D array (each inner array is one row of cell values).",
                        "items": {"type": "array", "items": {}},
                    },
                    "value_input_option": {
                        "type": "string",
                        "description": "'USER_ENTERED' parses formulas/numbers/dates like typing in the UI; 'RAW' stores values as literal strings. Default: USER_ENTERED.",
                        "enum": ["USER_ENTERED", "RAW"],
                        "default": "USER_ENTERED",
                    },
                },
                "required": ["account", "spreadsheet_id", "range", "values"],
            },
        ),
        types.Tool(
            name="sheets_append",
            description="Append rows of values after the last row of existing data in a range/sheet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "range": {"type": "string", "description": "A1-notation range/sheet to append after, e.g. 'Sheet1' or 'Sheet1!A1'"},
                    "values": {
                        "type": "array",
                        "description": "Rows to append, as a 2D array (each inner array is one row of cell values).",
                        "items": {"type": "array", "items": {}},
                    },
                    "value_input_option": {
                        "type": "string",
                        "description": "'USER_ENTERED' parses formulas/numbers/dates like typing in the UI; 'RAW' stores values as literal strings. Default: USER_ENTERED.",
                        "enum": ["USER_ENTERED", "RAW"],
                        "default": "USER_ENTERED",
                    },
                },
                "required": ["account", "spreadsheet_id", "range", "values"],
            },
        ),
        types.Tool(
            name="sheets_clear",
            description="Clear cell values (not formatting) in a range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "range": {"type": "string", "description": "A1-notation range to clear, e.g. 'Sheet1!A1:D10'"},
                },
                "required": ["account", "spreadsheet_id", "range"],
            },
        ),
        types.Tool(
            name="sheets_format",
            description=(
                "Apply cell formatting (bold, italic, underline, strikethrough, font size/family, "
                "text color, fill color, horizontal alignment, number format) to a range, "
                "e.g. 'Sheet1!A1:C5'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "range": {"type": "string", "description": "A1-notation range to format, e.g. 'Sheet1!A1:C5' or 'Sheet1' for the whole tab"},
                    "bold": {"type": "boolean", "description": "Set (true) or clear (false) bold."},
                    "italic": {"type": "boolean", "description": "Set (true) or clear (false) italic."},
                    "underline": {"type": "boolean", "description": "Set (true) or clear (false) underline."},
                    "strikethrough": {"type": "boolean", "description": "Set (true) or clear (false) strikethrough."},
                    "font_size": {"type": "number", "description": "Font size in points, e.g. 12."},
                    "font_family": {"type": "string", "description": "Font family name, e.g. 'Arial'."},
                    "foreground_color": {"type": "string", "description": "Text color as a hex string, e.g. '#FF0000'."},
                    "background_color": {"type": "string", "description": "Cell fill/highlight color as a hex string, e.g. '#FFFF00'."},
                    "horizontal_alignment": {"type": "string", "description": "Horizontal text alignment.", "enum": ["LEFT", "CENTER", "RIGHT"]},
                    "number_format": {"type": "string", "description": "Number format pattern, e.g. '#,##0.00' or 'yyyy-mm-dd'. Optional."},
                    "number_format_type": {"type": "string", "description": "Category for number_format. Default: NUMBER.", "enum": ["NUMBER", "CURRENCY", "PERCENT", "DATE", "TIME", "DATE_TIME", "SCIENTIFIC", "TEXT"], "default": "NUMBER"},
                },
                "required": ["account", "spreadsheet_id", "range"],
            },
        ),
        types.Tool(
            name="sheets_add_tab",
            description="Add a new tab (sheet) to an existing spreadsheet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "title": {"type": "string", "description": "Name for the new tab"},
                },
                "required": ["account", "spreadsheet_id", "title"],
            },
        ),
        types.Tool(
            name="sheets_rename_tab",
            description="Rename a tab within a spreadsheet. Identify it by its current name (sheet_name) or numeric sheet_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "new_title": {"type": "string", "description": "New name for the tab"},
                    "sheet_name": {"type": "string", "description": "Current name of the tab to rename. Alternative to sheet_id."},
                    "sheet_id": {"type": "integer", "description": "Numeric id of the tab to rename. Alternative to sheet_name."},
                },
                "required": ["account", "spreadsheet_id", "new_title"],
            },
        ),
        types.Tool(
            name="sheets_delete_tab",
            description="Delete a tab from a spreadsheet. Identify it by its name (sheet_name) or numeric sheet_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "sheet_name": {"type": "string", "description": "Name of the tab to delete. Alternative to sheet_id."},
                    "sheet_id": {"type": "integer", "description": "Numeric id of the tab to delete. Alternative to sheet_name."},
                },
                "required": ["account", "spreadsheet_id"],
            },
        ),
        types.Tool(
            name="sheets_rename",
            description="Rename a spreadsheet (changes its title/file name).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                    "title": {"type": "string", "description": "New title for the spreadsheet"},
                },
                "required": ["account", "spreadsheet_id", "title"],
            },
        ),
        types.Tool(
            name="sheets_delete",
            description="Move a spreadsheet to Drive trash.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "spreadsheet_id": {"type": "string", "description": "Google Sheets spreadsheet ID"},
                },
                "required": ["account", "spreadsheet_id"],
            },
        ),
        # ── Forms tools ──────────────────────────────────────────────────────
        types.Tool(
            name="forms_create",
            description=(
                "Create a new blank Google Form. Only the title (and optionally a separate Drive "
                "file name) can be set at creation — add questions afterward with forms_add_question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "title": {"type": "string", "description": "The form's visible title, shown to respondents"},
                    "document_title": {"type": "string", "description": "Optional separate Drive file name. Defaults to matching title."},
                },
                "required": ["account", "title"],
            },
        ),
        types.Tool(
            name="forms_read",
            description="Get a form's full structure: title, description, every question (with its item_id/type/options/required flag), the public share link, and publish state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                },
                "required": ["account", "form_id"],
            },
        ),
        types.Tool(
            name="forms_add_question",
            description=(
                "Add a question to a form. RADIO/CHECKBOX/DROPDOWN require options. "
                "FILE_UPLOAD questions cannot be created via the Forms API — not supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "title": {"type": "string", "description": "The question text"},
                    "question_type": {
                        "type": "string",
                        "description": "SHORT_TEXT, PARAGRAPH, RADIO, CHECKBOX, DROPDOWN, SCALE, DATE, TIME, or RATING",
                        "enum": ["SHORT_TEXT", "PARAGRAPH", "RADIO", "CHECKBOX", "DROPDOWN", "SCALE", "DATE", "TIME", "RATING"],
                    },
                    "options": {"type": "array", "items": {"type": "string"}, "description": "Answer choices. Required for RADIO/CHECKBOX/DROPDOWN."},
                    "required": {"type": "boolean", "description": "Whether the question must be answered. Default: false.", "default": False},
                    "description": {"type": "string", "description": "Optional helper text shown under the question."},
                    "low": {"type": "integer", "description": "SCALE only: lowest value. Default: 1."},
                    "high": {"type": "integer", "description": "SCALE only: highest value. Default: 5."},
                    "low_label": {"type": "string", "description": "SCALE only: label for the lowest value."},
                    "high_label": {"type": "string", "description": "SCALE only: label for the highest value."},
                    "include_year": {"type": "boolean", "description": "DATE only: include the year. Default: true.", "default": True},
                    "include_time": {"type": "boolean", "description": "DATE only: also collect a time. Default: false.", "default": False},
                    "duration": {"type": "boolean", "description": "TIME only: true for an elapsed duration, false for a time of day. Default: false.", "default": False},
                    "rating_icon": {"type": "string", "description": "RATING only: icon to use.", "enum": ["STAR", "HEART", "THUMB_UP"], "default": "STAR"},
                    "rating_levels": {"type": "integer", "description": "RATING only: number of icons in the scale. Default: 5.", "default": 5},
                    "index": {"type": "integer", "description": "0-based position to insert at. Omit to append at the end."},
                },
                "required": ["account", "form_id", "title", "question_type"],
            },
        ),
        types.Tool(
            name="forms_update_question",
            description=(
                "Update an existing question, found by item_id (from forms_add_question or forms_read). "
                "Any field left unset keeps its current value. Pass question_type (plus options for "
                "choice types) to change the question's kind; otherwise only required/options are patched."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "item_id": {"type": "string", "description": "Item ID of the question to update (from forms_read)"},
                    "title": {"type": "string", "description": "New question text. Omit to keep current."},
                    "question_type": {
                        "type": "string",
                        "description": "Change the question's kind. Omit to keep the current kind.",
                        "enum": ["SHORT_TEXT", "PARAGRAPH", "RADIO", "CHECKBOX", "DROPDOWN", "SCALE", "DATE", "TIME", "RATING"],
                    },
                    "options": {"type": "array", "items": {"type": "string"}, "description": "New answer choices (for choice types)."},
                    "required": {"type": "boolean", "description": "Whether the question must be answered."},
                    "description": {"type": "string", "description": "New helper text."},
                    "low": {"type": "integer", "description": "SCALE only: lowest value."},
                    "high": {"type": "integer", "description": "SCALE only: highest value."},
                    "low_label": {"type": "string", "description": "SCALE only: label for the lowest value."},
                    "high_label": {"type": "string", "description": "SCALE only: label for the highest value."},
                    "include_year": {"type": "boolean", "description": "DATE only: include the year."},
                    "include_time": {"type": "boolean", "description": "DATE only: also collect a time."},
                    "duration": {"type": "boolean", "description": "TIME only: elapsed duration vs. time of day."},
                    "rating_icon": {"type": "string", "description": "RATING only: icon to use.", "enum": ["STAR", "HEART", "THUMB_UP"]},
                    "rating_levels": {"type": "integer", "description": "RATING only: number of icons in the scale."},
                },
                "required": ["account", "form_id", "item_id"],
            },
        ),
        types.Tool(
            name="forms_move_question",
            description="Move a question/item to a new 0-based position within a form.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "item_id": {"type": "string", "description": "Item ID of the question to move (from forms_read)"},
                    "new_index": {"type": "integer", "description": "New 0-based position for the item"},
                },
                "required": ["account", "form_id", "item_id", "new_index"],
            },
        ),
        types.Tool(
            name="forms_delete_question",
            description="Delete a question/item from a form.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "item_id": {"type": "string", "description": "Item ID of the question to delete (from forms_read)"},
                },
                "required": ["account", "form_id", "item_id"],
            },
        ),
        types.Tool(
            name="forms_update_info",
            description="Update a form's visible title and/or description (shown to respondents).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "title": {"type": "string", "description": "New title. Omit to keep current."},
                    "description": {"type": "string", "description": "New description. Omit to keep current."},
                },
                "required": ["account", "form_id"],
            },
        ),
        types.Tool(
            name="forms_update_settings",
            description="Update form-level settings: email collection and/or quiz mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "email_collection_type": {
                        "type": "string",
                        "description": "Whether/how to collect respondent emails.",
                        "enum": ["DO_NOT_COLLECT", "VERIFIED", "RESPONDER_INPUT"],
                    },
                    "is_quiz": {"type": "boolean", "description": "Turn quiz grading on/off. Turning off deletes existing grading."},
                },
                "required": ["account", "form_id"],
            },
        ),
        types.Tool(
            name="forms_set_publish_settings",
            description="Publish/unpublish a form and control whether it's currently accepting responses. Accepting responses requires the form to be published.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "is_published": {"type": "boolean", "description": "Whether the form is published/visible"},
                    "is_accepting_responses": {"type": "boolean", "description": "Whether the form currently accepts new responses"},
                },
                "required": ["account", "form_id", "is_published", "is_accepting_responses"],
            },
        ),
        types.Tool(
            name="forms_list_responses",
            description="List submitted responses to a form.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "max_results": {"type": "integer", "description": "Max responses to return (default 20)", "default": 20},
                },
                "required": ["account", "form_id"],
            },
        ),
        types.Tool(
            name="forms_get_response",
            description="Get a single submitted response by its response ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "response_id": {"type": "string", "description": "Response ID (from forms_list_responses)"},
                },
                "required": ["account", "form_id", "response_id"],
            },
        ),
        types.Tool(
            name="forms_rename",
            description="Rename a form's Drive file name (the document title) — not the same as the visible title respondents see; use forms_update_info for that.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                    "title": {"type": "string", "description": "New Drive file name"},
                },
                "required": ["account", "form_id", "title"],
            },
        ),
        types.Tool(
            name="forms_delete",
            description="Move a form to Drive trash (the Forms API has no delete-form call — deletion goes through Drive, same as Docs/Sheets).",
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name"},
                    "form_id": {"type": "string", "description": "Google Form ID"},
                },
                "required": ["account", "form_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}

    try:
        # ---- list_accounts ------------------------------------------------
        if name == "list_accounts":
            result = []
            for acct, info in _accounts.items():
                authenticated = _auth.is_authenticated(acct)
                result.append({
                    "name": acct,
                    "email": info.get("email", ""),
                    "description": info.get("description", ""),
                    "authenticated": authenticated,
                    "status": "ready" if authenticated else "not authenticated — run setup_auth.py",
                })
            return _fmt(result)

        # ---- gmail_get_profile --------------------------------------------
        elif name == "gmail_get_profile":
            svc = _get_service(args["account"])
            return _fmt(svc.get_profile())

        # ---- gmail_search -------------------------------------------------
        elif name == "gmail_search":
            query: str = args["query"]
            max_results: int = int(args.get("max_results", 10))
            include_body: bool = bool(args.get("include_body", False))
            account: str | None = args.get("account")

            if account:
                svc = _get_service(account)
                data = svc.search_messages(query, max_results, include_body=include_body)
                data["account"] = account
                data["email"] = _accounts[account].get("email", "")
                return _fmt(data)
            else:
                all_results = []
                for acct in _accounts:
                    try:
                        svc = _get_service(acct)
                        data = svc.search_messages(query, max_results, include_body=include_body)
                        all_results.append({
                            "account": acct,
                            "email": _accounts[acct].get("email", ""),
                            **data,
                        })
                    except ValueError as exc:
                        all_results.append({
                            "account": acct,
                            "error": str(exc),
                            "messages": [],
                        })
                return _fmt(all_results)

        # ---- gmail_read_message -------------------------------------------
        elif name == "gmail_read_message":
            svc = _get_service(args["account"])
            return _fmt(svc.get_message(args["message_id"]))

        # ---- gmail_read_thread --------------------------------------------
        elif name == "gmail_read_thread":
            svc = _get_service(args["account"])
            return _fmt(svc.get_thread(args["thread_id"]))

        # ---- gmail_send ---------------------------------------------------
        elif name == "gmail_send":
            svc = _get_service(args["account"])
            tmp_dir = None
            try:
                attachments = list(args.get("attachments") or [])
                if args.get("drive_attachment_ids"):
                    tmp_dir = tempfile.mkdtemp(prefix="gdrive_attach_")
                    attachments += _download_drive_attachments(
                        args["account"], args["drive_attachment_ids"], tmp_dir
                    )
                result = svc.send_message(
                    to=args["to"],
                    subject=args["subject"],
                    body=args["body"],
                    cc=args.get("cc", ""),
                    bcc=args.get("bcc", ""),
                    html_body=args.get("html_body", ""),
                    attachments=attachments or None,
                    thread_id=args.get("thread_id", ""),
                    in_reply_to=args.get("in_reply_to", ""),
                    references=args.get("references", ""),
                )
                return _fmt({
                    "status": "sent",
                    "message_id": result.get("id"),
                    "thread_id": result.get("threadId"),
                })
            finally:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        # ---- gmail_create_draft -------------------------------------------
        elif name == "gmail_create_draft":
            svc = _get_service(args["account"])
            tmp_dir = None
            try:
                attachments = list(args.get("attachments") or [])
                if args.get("drive_attachment_ids"):
                    tmp_dir = tempfile.mkdtemp(prefix="gdrive_attach_")
                    attachments += _download_drive_attachments(
                        args["account"], args["drive_attachment_ids"], tmp_dir
                    )
                result = svc.create_draft(
                    to=args["to"],
                    subject=args["subject"],
                    body=args["body"],
                    cc=args.get("cc", ""),
                    bcc=args.get("bcc", ""),
                    html_body=args.get("html_body", ""),
                    attachments=attachments or None,
                    thread_id=args.get("thread_id", ""),
                    in_reply_to=args.get("in_reply_to", ""),
                    references=args.get("references", ""),
                )
                return _fmt({"status": "draft created", "draft_id": result.get("id")})
            finally:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        # ---- gmail_list_drafts --------------------------------------------
        elif name == "gmail_list_drafts":
            svc = _get_service(args["account"])
            drafts = svc.list_drafts(int(args.get("max_results", 10)))
            return _fmt({"count": len(drafts), "drafts": drafts})

        # ---- gmail_get_draft -----------------------------------------------
        elif name == "gmail_get_draft":
            svc = _get_service(args["account"])
            return _fmt(svc.get_draft(args["draft_id"]))

        # ---- gmail_update_draft ----------------------------------------------
        elif name == "gmail_update_draft":
            svc = _get_service(args["account"])
            tmp_dir = None
            try:
                attachments = args.get("attachments")
                if args.get("drive_attachment_ids"):
                    tmp_dir = tempfile.mkdtemp(prefix="gdrive_attach_")
                    attachments = list(attachments or []) + _download_drive_attachments(
                        args["account"], args["drive_attachment_ids"], tmp_dir
                    )
                result = svc.update_draft(
                    draft_id=args["draft_id"],
                    to=args.get("to"),
                    subject=args.get("subject"),
                    body=args.get("body"),
                    cc=args.get("cc"),
                    bcc=args.get("bcc"),
                    html_body=args.get("html_body"),
                    attachments=attachments,
                )
                return _fmt({"status": "draft updated", "draft_id": result.get("id")})
            finally:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        # ---- gmail_delete_draft ----------------------------------------------
        elif name == "gmail_delete_draft":
            svc = _get_service(args["account"])
            return _fmt(svc.delete_draft(args["draft_id"]))

        # ---- gmail_list_labels --------------------------------------------
        elif name == "gmail_list_labels":
            svc = _get_service(args["account"])
            return _fmt(svc.list_labels())

        # ---- gmail_modify_labels -----------------------------------------
        elif name == "gmail_modify_labels":
            svc = _get_service(args["account"])
            svc.modify_labels(
                message_id=args["message_id"],
                add_labels=args.get("add_labels"),
                remove_labels=args.get("remove_labels"),
            )
            return _fmt({"status": "labels updated", "message_id": args["message_id"]})

        # ---- gmail_trash -------------------------------------------------
        elif name == "gmail_trash":
            svc = _get_service(args["account"])
            svc.trash_message(args["message_id"])
            return _fmt({"status": "moved to trash", "message_id": args["message_id"]})

        # ---- calendar_list_calendars --------------------------------------
        elif name == "calendar_list_calendars":
            svc = _get_calendar(args["account"])
            return _fmt(svc.list_calendars())

        # ---- calendar_list_events -----------------------------------------
        elif name == "calendar_list_events":
            svc = _get_calendar(args["account"])
            return _fmt(svc.list_events(
                time_min=args.get("time_min"),
                time_max=args.get("time_max"),
                max_results=int(args.get("max_results", 20)),
                calendar_id=args.get("calendar_id", "primary"),
            ))

        # ---- calendar_search ----------------------------------------------
        elif name == "calendar_search":
            svc = _get_calendar(args["account"])
            return _fmt(svc.search_events(
                query=args["query"],
                time_min=args.get("time_min"),
                time_max=args.get("time_max"),
                max_results=int(args.get("max_results", 20)),
                calendar_id=args.get("calendar_id", "primary"),
            ))

        # ---- calendar_get_event -------------------------------------------
        elif name == "calendar_get_event":
            svc = _get_calendar(args["account"])
            return _fmt(svc.get_event(
                event_id=args["event_id"],
                calendar_id=args.get("calendar_id", "primary"),
            ))

        # ---- calendar_create_event -----------------------------------------
        elif name == "calendar_create_event":
            account = args["account"]
            start_time = args["start_time"]
            end_time = args["end_time"]

            conflicts = _check_cross_account_conflicts(start_time, end_time, account)

            svc = _get_calendar(account)
            created = svc.create_event(
                title=args["title"],
                start_time=start_time,
                end_time=end_time,
                calendar_id=args.get("calendar_id", "primary"),
                description=args.get("description", ""),
                location=args.get("location", ""),
                attendees=args.get("attendees"),
            )

            blocked_on = []
            if args.get("block_other_calendars", True):
                for acct in _accounts:
                    if acct == account:
                        continue
                    try:
                        _get_calendar(acct).create_event(
                            title=f"[{account} booking]",
                            start_time=start_time,
                            end_time=end_time,
                            description=f"Auto-blocked because '{account}' has a booking at this time.",
                            private_properties={
                                "sync_source_account": account,
                                "sync_source_event_id": created["id"],
                            },
                        )
                        blocked_on.append(acct)
                    except ValueError:
                        continue

            result = dict(created)
            if conflicts:
                result["conflicts_detected"] = conflicts
            if blocked_on:
                result["blocked_on_accounts"] = blocked_on
            return _fmt(result)

        # ---- calendar_update_event -----------------------------------------
        elif name == "calendar_update_event":
            account = args["account"]
            svc = _get_calendar(account)
            updated = svc.update_event(
                event_id=args["event_id"],
                calendar_id=args.get("calendar_id", "primary"),
                title=args.get("title"),
                start_time=args.get("start_time"),
                end_time=args.get("end_time"),
                description=args.get("description"),
                location=args.get("location"),
                attendees=args.get("attendees"),
            )

            result = dict(updated)
            if args.get("start_time") or args.get("end_time"):
                synced_on = _sync_linked_events(
                    account,
                    args["event_id"],
                    start_time=updated["start"],
                    end_time=updated["end"],
                )
                if synced_on:
                    result["synced_on_accounts"] = synced_on
            return _fmt(result)

        # ---- calendar_delete_event -----------------------------------------
        elif name == "calendar_delete_event":
            account = args["account"]
            deleted_on = _delete_linked_events(account, args["event_id"])

            svc = _get_calendar(account)
            result = svc.delete_event(
                event_id=args["event_id"],
                calendar_id=args.get("calendar_id", "primary"),
            )
            if deleted_on:
                result["deleted_on_accounts"] = deleted_on
            return _fmt(result)

        # ---- drive_list_files --------------------------------------------
        elif name == "drive_list_files":
            svc = _get_drive(args["account"])
            return _fmt(svc.list_files(
                max_results=int(args.get("max_results", 20)),
                folder_id=args.get("folder_id"),
                folder_name=args.get("folder_name"),
                mime_type=args.get("mime_type"),
                drive_id=args.get("drive_id"),
            ))

        # ---- drive_search ------------------------------------------------
        elif name == "drive_search":
            svc = _get_drive(args["account"])
            return _fmt(svc.search_files(
                query=args["query"],
                max_results=int(args.get("max_results", 20)),
                drive_id=args.get("drive_id"),
            ))

        # ---- drive_list_folders ------------------------------------------
        elif name == "drive_list_folders":
            svc = _get_drive(args["account"])
            return _fmt(svc.list_folders(
                max_results=int(args.get("max_results", 20)),
                drive_id=args.get("drive_id"),
            ))

        # ---- drive_list_shared_drives -------------------------------------
        elif name == "drive_list_shared_drives":
            svc = _get_drive(args["account"])
            return _fmt(svc.list_shared_drives(max_results=int(args.get("max_results", 20))))

        # ---- drive_get_file ----------------------------------------------
        elif name == "drive_get_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.get_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
            ))

        # ---- drive_read_file ---------------------------------------------
        elif name == "drive_read_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.read_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
            ))

        # ---- drive_download_file -----------------------------------------
        elif name == "drive_download_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.download_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
            ))

        # ---- drive_download_to_file -----------------------------------------
        elif name == "drive_download_to_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.download_to_path(
                destination_dir=args.get("destination_dir") or tempfile.gettempdir(),
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
                export_mime_type=args.get("export_mime_type"),
            ))

        # ---- drive_delete_file -------------------------------------------
        elif name == "drive_delete_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.delete_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
                permanent=bool(args.get("permanent", False)),
            ))

        # ---- drive_delete_shared_drive --------------------------------------
        elif name == "drive_delete_shared_drive":
            svc = _get_drive(args["account"])
            return _fmt(svc.delete_shared_drive(
                drive_id=args.get("drive_id"),
                drive_name=args.get("drive_name"),
            ))

        # ---- drive_share ---------------------------------------------------
        elif name == "drive_share":
            svc = _get_drive(args["account"])
            return _fmt(svc.add_permission(
                email=args["email"],
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
                role=args.get("role", "writer"),
                send_notification=bool(args.get("send_notification", True)),
                message=args.get("message"),
            ))

        # ---- drive_create_folder -------------------------------------------
        elif name == "drive_create_folder":
            svc = _get_drive(args["account"])
            return _fmt(svc.create_folder(
                name=args["name"],
                parent_folder_id=args.get("parent_folder_id"),
                parent_folder_name=args.get("parent_folder_name"),
            ))

        # ---- drive_upload_file --------------------------------------------
        elif name == "drive_upload_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.upload_file(
                file_path=args["file_path"],
                folder_id=args.get("folder_id"),
                folder_name=args.get("folder_name"),
                name=args.get("name"),
                mime_type=args.get("mime_type"),
            ))

        # ---- drive_move_file -------------------------------------------------
        elif name == "drive_move_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.move_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
                folder_id=args.get("folder_id"),
                folder_name=args.get("folder_name"),
                remove_from_current=bool(args.get("remove_from_current", True)),
            ))

        # ---- drive_copy_file -------------------------------------------------
        elif name == "drive_copy_file":
            svc = _get_drive(args["account"])
            return _fmt(svc.copy_file(
                file_id=args.get("file_id"),
                file_name=args.get("file_name"),
                name=args.get("name"),
                folder_id=args.get("folder_id"),
                folder_name=args.get("folder_name"),
            ))

        # ---- docs_create ---------------------------------------------------
        elif name == "docs_create":
            svc = _get_docs(args["account"])
            return _fmt(svc.create_document(
                title=args["title"],
                content=args.get("content", ""),
            ))

        # ---- docs_read -------------------------------------------------------
        elif name == "docs_read":
            svc = _get_docs(args["account"])
            return _fmt(svc.get_document(args["document_id"]))

        # ---- docs_update -----------------------------------------------------
        elif name == "docs_update":
            svc = _get_docs(args["account"])
            return _fmt(svc.update_document(
                document_id=args["document_id"],
                append_text=args.get("append_text", ""),
                find_text=args.get("find_text", ""),
                replace_text=args.get("replace_text", ""),
                match_case=bool(args.get("match_case", False)),
            ))

        # ---- docs_format -----------------------------------------------------
        elif name == "docs_format":
            svc = _get_docs(args["account"])
            return _fmt(svc.format_text(
                document_id=args["document_id"],
                find_text=args.get("find_text", ""),
                match_case=bool(args.get("match_case", False)),
                match_all=bool(args.get("match_all", True)),
                start_index=args.get("start_index"),
                end_index=args.get("end_index"),
                bold=args.get("bold"),
                italic=args.get("italic"),
                underline=args.get("underline"),
                strikethrough=args.get("strikethrough"),
                font_size=args.get("font_size"),
                font_family=args.get("font_family"),
                foreground_color=args.get("foreground_color"),
                background_color=args.get("background_color"),
            ))

        # ---- docs_rename -----------------------------------------------------
        elif name == "docs_rename":
            svc = _get_docs(args["account"])
            return _fmt(svc.rename_document(
                document_id=args["document_id"],
                title=args["title"],
            ))

        # ---- docs_delete -----------------------------------------------------
        elif name == "docs_delete":
            svc = _get_docs(args["account"])
            return _fmt(svc.delete_document(args["document_id"]))

        # ---- sheets_create -----------------------------------------------------
        elif name == "sheets_create":
            svc = _get_sheets(args["account"])
            return _fmt(svc.create_spreadsheet(
                title=args["title"],
                data=args.get("data"),
                sheet_name=args.get("sheet_name", "Sheet1"),
            ))

        # ---- sheets_info -----------------------------------------------------
        elif name == "sheets_info":
            svc = _get_sheets(args["account"])
            return _fmt(svc.get_spreadsheet(args["spreadsheet_id"]))

        # ---- sheets_read -----------------------------------------------------
        elif name == "sheets_read":
            svc = _get_sheets(args["account"])
            return _fmt(svc.read_values(
                spreadsheet_id=args["spreadsheet_id"],
                range_name=args.get("range", ""),
            ))

        # ---- sheets_update -----------------------------------------------------
        elif name == "sheets_update":
            svc = _get_sheets(args["account"])
            return _fmt(svc.update_values(
                spreadsheet_id=args["spreadsheet_id"],
                range_name=args["range"],
                values=args["values"],
                value_input_option=args.get("value_input_option", "USER_ENTERED"),
            ))

        # ---- sheets_append -----------------------------------------------------
        elif name == "sheets_append":
            svc = _get_sheets(args["account"])
            return _fmt(svc.append_values(
                spreadsheet_id=args["spreadsheet_id"],
                range_name=args["range"],
                values=args["values"],
                value_input_option=args.get("value_input_option", "USER_ENTERED"),
            ))

        # ---- sheets_clear -----------------------------------------------------
        elif name == "sheets_clear":
            svc = _get_sheets(args["account"])
            return _fmt(svc.clear_values(
                spreadsheet_id=args["spreadsheet_id"],
                range_name=args["range"],
            ))

        # ---- sheets_format -----------------------------------------------------
        elif name == "sheets_format":
            svc = _get_sheets(args["account"])
            return _fmt(svc.format_cells(
                spreadsheet_id=args["spreadsheet_id"],
                range_name=args["range"],
                bold=args.get("bold"),
                italic=args.get("italic"),
                underline=args.get("underline"),
                strikethrough=args.get("strikethrough"),
                font_size=args.get("font_size"),
                font_family=args.get("font_family"),
                foreground_color=args.get("foreground_color"),
                background_color=args.get("background_color"),
                horizontal_alignment=args.get("horizontal_alignment"),
                number_format=args.get("number_format"),
                number_format_type=args.get("number_format_type", "NUMBER"),
            ))

        # ---- sheets_add_tab -----------------------------------------------------
        elif name == "sheets_add_tab":
            svc = _get_sheets(args["account"])
            return _fmt(svc.add_sheet(
                spreadsheet_id=args["spreadsheet_id"],
                title=args["title"],
            ))

        # ---- sheets_rename_tab -----------------------------------------------------
        elif name == "sheets_rename_tab":
            svc = _get_sheets(args["account"])
            return _fmt(svc.rename_sheet(
                spreadsheet_id=args["spreadsheet_id"],
                new_title=args["new_title"],
                sheet_name=args.get("sheet_name", ""),
                sheet_id=args.get("sheet_id"),
            ))

        # ---- sheets_delete_tab -----------------------------------------------------
        elif name == "sheets_delete_tab":
            svc = _get_sheets(args["account"])
            return _fmt(svc.delete_sheet(
                spreadsheet_id=args["spreadsheet_id"],
                sheet_name=args.get("sheet_name", ""),
                sheet_id=args.get("sheet_id"),
            ))

        # ---- sheets_rename -----------------------------------------------------
        elif name == "sheets_rename":
            svc = _get_sheets(args["account"])
            return _fmt(svc.rename_spreadsheet(
                spreadsheet_id=args["spreadsheet_id"],
                title=args["title"],
            ))

        # ---- sheets_delete -----------------------------------------------------
        elif name == "sheets_delete":
            svc = _get_sheets(args["account"])
            return _fmt(svc.delete_spreadsheet(args["spreadsheet_id"]))

        # ---- forms_create ---------------------------------------------------
        elif name == "forms_create":
            svc = _get_forms(args["account"])
            return _fmt(svc.create_form(
                title=args["title"],
                document_title=args.get("document_title"),
            ))

        # ---- forms_read -------------------------------------------------------
        elif name == "forms_read":
            svc = _get_forms(args["account"])
            return _fmt(svc.get_form(args["form_id"]))

        # ---- forms_add_question -----------------------------------------------
        elif name == "forms_add_question":
            svc = _get_forms(args["account"])
            return _fmt(svc.add_question(
                form_id=args["form_id"],
                title=args["title"],
                question_type=args["question_type"],
                options=args.get("options"),
                required=bool(args.get("required", False)),
                description=args.get("description"),
                low=args.get("low"),
                high=args.get("high"),
                low_label=args.get("low_label"),
                high_label=args.get("high_label"),
                include_year=bool(args.get("include_year", True)),
                include_time=bool(args.get("include_time", False)),
                duration=bool(args.get("duration", False)),
                rating_icon=args.get("rating_icon", "STAR"),
                rating_levels=int(args.get("rating_levels", 5)),
                index=args.get("index"),
            ))

        # ---- forms_update_question ---------------------------------------------
        elif name == "forms_update_question":
            svc = _get_forms(args["account"])
            return _fmt(svc.update_question(
                form_id=args["form_id"],
                item_id=args["item_id"],
                title=args.get("title"),
                question_type=args.get("question_type"),
                options=args.get("options"),
                required=args.get("required"),
                description=args.get("description"),
                low=args.get("low"),
                high=args.get("high"),
                low_label=args.get("low_label"),
                high_label=args.get("high_label"),
                include_year=args.get("include_year"),
                include_time=args.get("include_time"),
                duration=args.get("duration"),
                rating_icon=args.get("rating_icon"),
                rating_levels=args.get("rating_levels"),
            ))

        # ---- forms_move_question -----------------------------------------------
        elif name == "forms_move_question":
            svc = _get_forms(args["account"])
            return _fmt(svc.move_question(
                form_id=args["form_id"],
                item_id=args["item_id"],
                new_index=int(args["new_index"]),
            ))

        # ---- forms_delete_question ---------------------------------------------
        elif name == "forms_delete_question":
            svc = _get_forms(args["account"])
            return _fmt(svc.delete_question(
                form_id=args["form_id"],
                item_id=args["item_id"],
            ))

        # ---- forms_update_info -------------------------------------------------
        elif name == "forms_update_info":
            svc = _get_forms(args["account"])
            return _fmt(svc.update_info(
                form_id=args["form_id"],
                title=args.get("title"),
                description=args.get("description"),
            ))

        # ---- forms_update_settings ---------------------------------------------
        elif name == "forms_update_settings":
            svc = _get_forms(args["account"])
            return _fmt(svc.update_settings(
                form_id=args["form_id"],
                email_collection_type=args.get("email_collection_type"),
                is_quiz=args.get("is_quiz"),
            ))

        # ---- forms_set_publish_settings -----------------------------------------
        elif name == "forms_set_publish_settings":
            svc = _get_forms(args["account"])
            return _fmt(svc.set_publish_settings(
                form_id=args["form_id"],
                is_published=bool(args["is_published"]),
                is_accepting_responses=bool(args["is_accepting_responses"]),
            ))

        # ---- forms_list_responses ----------------------------------------------
        elif name == "forms_list_responses":
            svc = _get_forms(args["account"])
            return _fmt(svc.list_responses(
                form_id=args["form_id"],
                max_results=int(args.get("max_results", 20)),
            ))

        # ---- forms_get_response -------------------------------------------------
        elif name == "forms_get_response":
            svc = _get_forms(args["account"])
            return _fmt(svc.get_response(
                form_id=args["form_id"],
                response_id=args["response_id"],
            ))

        # ---- forms_rename -----------------------------------------------------
        elif name == "forms_rename":
            svc = _get_forms(args["account"])
            return _fmt(svc.rename_form(
                form_id=args["form_id"],
                title=args["title"],
            ))

        # ---- forms_delete -----------------------------------------------------
        elif name == "forms_delete":
            svc = _get_forms(args["account"])
            return _fmt(svc.delete_form(args["form_id"]))

        else:
            return _fmt(f"Unknown tool: {name}")

    except ValueError as exc:
        return _fmt(f"Error: {exc}")
    except Exception as exc:
        return _fmt(f"Error in '{name}': {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())