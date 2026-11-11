# Gmail & Calendar Multi-Account MCP Server

A local [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that connects multiple Gmail accounts and Google Calendars to Claude Desktop. Runs entirely on your machine — no cloud hosting required.

## Features

- **Multiple accounts** — connect as many Gmail or Google Workspace accounts as you need
- **Unified email search** — search across all accounts simultaneously with Gmail's full query syntax
- **Full read access** — read individual messages and entire threads
- **Send & draft** — compose and send emails, or save drafts, from any account
- **Label management** — list labels, mark as read/unread, star messages
- **Google Calendar** — list calendars, browse upcoming events, search by keyword
- **Google Docs** — create, read, update, and delete documents
- **Google Sheets** — create, read, update, and delete spreadsheets, with cell formatting
- **Google Forms** — create, read, update, and delete forms, questions, and settings; read submitted responses

## Requirements

- macOS (tested on macOS 14+)
- Python 3.11+
- A Google Cloud project with the Gmail API, Calendar API, Docs API, Sheets API, and Forms API enabled (free)
- Claude Desktop

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/DiegoMaldonadoRosas/gmail-mcp.git
cd gmail-mcp
```

### 2. Run the setup script

```bash
bash setup.sh
```

This creates a virtual environment and installs all Python dependencies.

### 3. Configure your accounts

Copy the example config and fill in your accounts:

```bash
cp config.json.example config.json
```

Edit `config.json`:

```json
{
  "accounts": {
    "personal": {
      "email": "you@gmail.com",
      "description": "Personal Gmail",
      "signature_html": "<div><br>--<br><strong>Your Name</strong><br>example.com</div>",
      "signature_image_path": "./signatures/personal.png"
    },
    "work": {
      "email": "you@company.com",
      "description": "Work account",
      "signature_image_path": "./signatures/work.png"
    }
  },
  "credentials_dir": "./credentials"
}
```

The account keys (`personal`, `work`) are the names you'll use when asking Claude to interact with a specific account.

#### Per-account signatures (optional)

Each account can have a signature applied automatically to every message it
sends — you never pass signature info as a tool parameter. The signature is
resolved from the first available source in this **priority order** (only one
source is ever used per email):

1. **Auto-fetched Gmail signature** — the signature set in that account's own
   Gmail Settings (*Settings → See all settings → Signature*), read live via the
   Gmail Settings API. If a signature exists there, it's used and config.json is
   ignored. Nothing to configure.
2. **`signature_html`** (config.json) — used only if no Gmail signature is set.
   Appended to the HTML part of the email.
3. **`signature_image_path`** (config.json) — used only if neither of the above
   applies. The image is embedded inline at the bottom of the email (via
   `Content-ID` / `<img src="cid:...">`), so it appears as a signature image, not
   a file attachment. Path is relative to the project root or absolute.
4. **No signature** — if none of the above is set.

A plain-text body is always included as a fallback for clients that don't render
HTML.

| Field | Behavior |
|-------|----------|
| `signature_html` | HTML block appended to the HTML part. Used only when no Gmail Settings signature is found. |
| `signature_image_path` | Inline signature image. Used only when neither a Gmail Settings signature nor `signature_html` is set. |

> **Re-authentication required:** Auto-fetching needs the
> `gmail.settings.basic` scope, which was added to `SCOPES`. Accounts
> authenticated before this change must be re-authenticated once
> (`python setup_auth.py`) before the auto-fetched signature will work.
> Until then, the server silently falls back to the config.json sources.

### 4. Get Google OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable the **Gmail API**, **Google Calendar API**, **Google Drive API**, **Google Docs API**, **Google Sheets API**, and **Google Forms API**
3. Go to **APIs & Services → Credentials → + Create Credentials → OAuth 2.0 Client ID**
4. Choose **Desktop app** as the application type
5. Download the JSON file and save it as `credentials/client_secret.json`
6. Go to **APIs & Services → OAuth consent screen → Test users** and add every email address you configured in `config.json`

### 5. Authenticate your accounts

```bash
source .venv/bin/activate
python setup_auth.py
```

A browser window will open for each account. Sign in with the correct Google account. Tokens are saved locally and refreshed automatically — you only need to do this once per account.

> **Note:** If you previously authenticated for Gmail only, you must re-run `setup_auth.py` after adding Calendar support so the tokens include the new Calendar permissions.
> The same applies after adding Google Docs, Sheets, or Forms support — re-run `setup_auth.py` once per account so tokens pick up the new `documents`/`spreadsheets`/`forms.body`/`forms.responses.readonly` scopes, or `docs_*`/`sheets_*`/`forms_*` calls will fail with a permission error.

### 6. Add the server to Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` and add:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "/absolute/path/to/gmail-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/gmail-mcp/server.py"]
    }
  }
}
```

Replace `/absolute/path/to/gmail-mcp` with the actual path where you cloned the repo.

### 7. Restart Claude Desktop

All tools will appear automatically.

## Running over HTTP (optional, e.g. Railway)

By default the server uses stdio (Claude Desktop launches it for you). To serve it over the web instead:

```bash
python server.py --transport http
```

| Path | What it is |
|------|------------|
| `/mcp` | Streamable HTTP (current MCP standard, stateless, so restarts and dropped connections do not lose a session) |
| `/sse` | `GET`: legacy SSE stream for older clients. `POST`: handled as Streamable HTTP, so a client pointed at `/sse` that POSTs first does not get a 405 |
| `/messages/` | Companion endpoint used by the legacy SSE transport |

Paths work with or without a trailing slash.

**Host and port:** if the `PORT` environment variable is set (Railway sets it), the server listens on `0.0.0.0:$PORT`. Otherwise it listens on `127.0.0.1:8000`. `--host` and `--port` override both.

**Optional secret (`MCP_SECRET`):** if the `MCP_SECRET` environment variable is not set (or is empty), **no authentication is required** and the server prints a warning at startup. If it is set (at least 20 characters; letters, digits, `-` and `_` only), the secret becomes part of every path (`/mcp/<secret>`, `/sse/<secret>`, `/messages/<secret>/`) and any other request gets a `404`. A set but too-short secret makes the server refuse to start, so a typo never silently leaves it open. Request lines are not logged.

> **Security:** without `MCP_SECRET`, anyone who can reach the URL can read and send mail from your Google accounts through this server. Set a secret, or put authentication in front of it, whenever the server is reachable from the internet.

## Available Tools

### Gmail

| Tool | Description |
|------|-------------|
| `list_accounts` | List all configured accounts and their auth status |
| `gmail_get_profile` | Get account profile and mailbox stats |
| `gmail_search` | Search emails using Gmail query syntax (one or all accounts) |
| `gmail_read_message` | Read the full content of a message |
| `gmail_read_thread` | Read all messages in a thread |
| `gmail_send` | Send an email from a specific account (supports HTML body + attachments) |
| `gmail_create_draft` | Save an email as a draft (supports HTML body + attachments) |
| `gmail_list_drafts` | List drafts in an account |
| `gmail_list_labels` | List all labels and folders |
| `gmail_modify_labels` | Add or remove labels (mark read/unread, star, etc.) |
| `gmail_trash` | Move a message to trash |

#### `gmail_send` / `gmail_create_draft` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account` | yes | Account name to send from / draft in |
| `to` | yes | Recipient(s), comma-separated |
| `subject` | yes | Email subject |
| `body` | yes | Plain-text body. Always sent as a fallback for non-HTML clients. |
| `html_body` | no | HTML body. The account's configured signature is appended automatically. |
| `cc` | no | CC recipients, comma-separated |
| `bcc` | no | BCC recipients, comma-separated |
| `attachments` | no | Array of local file paths to attach. A missing path returns a clear error. |
| `thread_id` | no | Gmail thread ID to reply into, nesting the message/draft into an existing conversation instead of starting a new thread. |
| `in_reply_to` | no | Message-ID header of the message being replied to. Sets the MIME `In-Reply-To` header. |
| `references` | no | Space-separated chain of Message-IDs for the thread. Sets the MIME `References` header. |

### Google Calendar

| Tool | Description |
|------|-------------|
| `calendar_list_calendars` | List all calendars for an account (primary, work, shared, etc.) |
| `calendar_list_events` | List upcoming events, optionally filtered by date range |
| `calendar_search` | Search events by keyword (title, description, location, attendees) |
| `calendar_get_event` | Get full details of a specific event |

### Google Docs

| Tool | Description |
|------|-------------|
| `docs_create` | Create a new Google Doc, optionally seeded with initial text content |
| `docs_read` | Read a document's title and plain-text content |
| `docs_update` | Append text and/or find-and-replace text across a document |
| `docs_format` | Apply bold, italic, underline, strikethrough, font size/family, or color to text |
| `docs_rename` | Rename a document (changes its title/file name) |
| `docs_delete` | Move a document to Drive trash |

#### `docs_update` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account` | yes | Account name |
| `document_id` | yes | Google Docs document ID |
| `append_text` | no | Text to append to the end of the document |
| `find_text` | no | Text to find and replace across the whole document |
| `replace_text` | no | Replacement text used when `find_text` is set (defaults to empty string, i.e. deletes `find_text`) |
| `match_case` | no | Whether `find_text` matching is case-sensitive (default: false) |

At least one of `append_text` or `find_text` must be provided.

#### `docs_format` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account` | yes | Account name |
| `document_id` | yes | Google Docs document ID |
| `find_text` | no | Text to locate and format (alternative to `start_index`/`end_index`) |
| `match_case` | no | Whether `find_text` matching is case-sensitive (default: false) |
| `match_all` | no | Format every occurrence of `find_text`, not just the first (default: true) |
| `start_index` / `end_index` | no | Explicit range to format, in place of `find_text` |
| `bold` / `italic` / `underline` / `strikethrough` | no | Set (true) or clear (false) the style |
| `font_size` | no | Font size in points, e.g. `14` |
| `font_family` | no | Font family name, e.g. `"Arial"` |
| `foreground_color` | no | Text color as a hex string, e.g. `"#FF0000"` |
| `background_color` | no | Highlight color as a hex string, e.g. `"#FFFF00"` |

At least one style change (`bold`, `italic`, `underline`, `strikethrough`, `font_size`, `font_family`, `foreground_color`, and/or `background_color`) must be provided, and either `find_text` or `start_index`/`end_index` to target the range.

### Google Sheets

| Tool | Description |
|------|-------------|
| `sheets_create` | Create a new spreadsheet, optionally seeded with initial row data |
| `sheets_info` | Get a spreadsheet's title, URL, and list of tabs |
| `sheets_read` | Read cell values for a range (defaults to the whole first tab) |
| `sheets_update` | Overwrite cell values for a range |
| `sheets_append` | Append rows after the last row of existing data |
| `sheets_clear` | Clear cell values (not formatting) in a range |
| `sheets_format` | Apply bold, italic, underline, strikethrough, font size/family, colors, alignment, or number format to a range |
| `sheets_add_tab` | Add a new tab to a spreadsheet |
| `sheets_rename_tab` | Rename a tab |
| `sheets_delete_tab` | Delete a tab |
| `sheets_rename` | Rename a spreadsheet (changes its title/file name) |
| `sheets_delete` | Move a spreadsheet to Drive trash |

Ranges use A1 notation, e.g. `"Sheet1!A1:D10"` or just `"Sheet1"` for the whole tab.

#### `sheets_format` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account` | yes | Account name |
| `spreadsheet_id` | yes | Google Sheets spreadsheet ID |
| `range` | yes | A1-notation range to format, e.g. `"Sheet1!A1:C5"` |
| `bold` / `italic` / `underline` / `strikethrough` | no | Set (true) or clear (false) the style |
| `font_size` | no | Font size in points, e.g. `12` |
| `font_family` | no | Font family name, e.g. `"Arial"` |
| `foreground_color` | no | Text color as a hex string, e.g. `"#FF0000"` |
| `background_color` | no | Cell fill/highlight color as a hex string, e.g. `"#FFFF00"` |
| `horizontal_alignment` | no | `"LEFT"`, `"CENTER"`, or `"RIGHT"` |
| `number_format` | no | Number format pattern, e.g. `"#,##0.00"` or `"yyyy-mm-dd"` |
| `number_format_type` | no | Category for `number_format`: `NUMBER`, `CURRENCY`, `PERCENT`, `DATE`, `TIME`, `DATE_TIME`, `SCIENTIFIC`, or `TEXT` (default: `NUMBER`) |

At least one style change must be provided.

### Google Forms

| Tool | Description |
|------|-------------|
| `forms_create` | Create a new blank form (title only — add questions afterward) |
| `forms_read` | Get a form's title, description, questions (with item IDs/types/options), share link, and publish state |
| `forms_add_question` | Add a question: SHORT_TEXT, PARAGRAPH, RADIO, CHECKBOX, DROPDOWN, SCALE, DATE, TIME, or RATING |
| `forms_update_question` | Update an existing question by item ID |
| `forms_move_question` | Move a question to a new position |
| `forms_delete_question` | Delete a question |
| `forms_update_info` | Update the form's visible title/description |
| `forms_update_settings` | Update email collection and/or quiz mode |
| `forms_set_publish_settings` | Publish/unpublish a form and control whether it accepts responses |
| `forms_list_responses` | List submitted responses |
| `forms_get_response` | Get a single submitted response |
| `forms_rename` | Rename a form's Drive file name (not the visible title — use `forms_update_info` for that) |
| `forms_delete` | Move a form to Drive trash |

**Known limitation:** the Forms API cannot create FILE_UPLOAD questions at all (a hard Google API restriction, not something this server can work around). Submitted file-upload answers can still be read via `forms_get_response`/`forms_list_responses` (returned as Drive file IDs), just not created as a new question.

#### `forms_add_question` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account` | yes | Account name |
| `form_id` | yes | Google Form ID |
| `title` | yes | The question text |
| `question_type` | yes | `SHORT_TEXT`, `PARAGRAPH`, `RADIO`, `CHECKBOX`, `DROPDOWN`, `SCALE`, `DATE`, `TIME`, or `RATING` |
| `options` | conditional | Answer choices — required for `RADIO`/`CHECKBOX`/`DROPDOWN` |
| `required` | no | Whether the question must be answered (default: false) |
| `description` | no | Helper text shown under the question |
| `low` / `high` / `low_label` / `high_label` | no | `SCALE` only |
| `include_year` / `include_time` | no | `DATE` only |
| `duration` | no | `TIME` only — elapsed duration vs. time of day |
| `rating_icon` / `rating_levels` | no | `RATING` only |
| `index` | no | 0-based position to insert at (default: append at the end) |

## Usage Examples

Once connected, you can ask Claude things like:

**Email:**
- *"Do I have any unread emails in my work account?"*
- *"Search for invoices received in the last month across all my accounts"*
- *"Read the last email from John in my personal account"*
- *"Draft a reply to the budget email in my work account"*
- *"Mark all emails from newsletter@example.com as read"*

**Calendar:**
- *"What meetings do I have this week in my work account?"*
- *"Search for events related to 'product launch' in my personal calendar"*
- *"List all my calendars in my work account"*
- *"What are the details of tomorrow's standup?"*

**Docs:**
- *"Create a Google Doc called 'Meeting Notes' in my work account"*
- *"Read the content of this doc: <document_id>"*
- *"Append today's action items to the end of my project plan doc"*
- *"Replace all instances of 'Q1' with 'Q2' in that doc"*

## Adding a New Account

1. Add the account to `config.json`
2. Add the email as a Test User in Google Cloud Console (OAuth consent screen)
3. Run `python setup_auth.py` — it will only prompt for the new account
4. Restart Claude Desktop

## Security

- OAuth tokens are stored locally in `credentials/tokens/` and are excluded from version control via `.gitignore`
- `config.json` (which contains your email addresses) is also excluded from version control
- Nothing is sent to any third-party server — all communication is directly between your Mac and Google's APIs
- To revoke access at any time, visit [myaccount.google.com/permissions](https://myaccount.google.com/permissions)

## Project Structure

```
gmail-mcp/
├── server.py           # MCP server — exposes tools to Claude
├── auth.py             # OAuth2 token manager (per account)
├── gmail.py            # Gmail API wrapper
├── gcalendar.py        # Google Calendar API wrapper
├── gdrive.py           # Google Drive API wrapper
├── gdocs.py            # Google Docs API wrapper
├── config.py           # Configuration loader
├── setup_auth.py       # One-time authentication script
├── setup.sh            # First-time installer
├── requirements.txt    # Python dependencies
├── config.json.example # Account configuration template
└── .gitignore          # Excludes credentials and config.json
```

## License

MIT
