#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Full Gmail access (read, send, modify, labels)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]

# Maps account names to Railway environment variable names
ACCOUNT_ENV_MAP = {
    "leecrockett": "Lee_Crockett_main",
    "brooketaylor": "Brooke_Taylor_Coaching",
    "afaraglobal": "Afara_Global",
    "voylegal": "VoyLegal",
}


def _log_auth_error(account_name: str, what: str, exc: Exception) -> None:
    """Write the real reason an account is not authenticated to stderr.

    stderr is safe for both transports (stdout carries the MCP protocol in
    stdio mode). Only the account name, the step and Google's error text are
    logged, never token contents.
    """
    print(f"[auth] {account_name}: {what}: {type(exc).__name__}: {exc}", file=sys.stderr)


class AuthManager:
    def __init__(self, credentials_dir: Path, client_secret_path: Path):
        self.credentials_dir = Path(credentials_dir)
        self.client_secret_path = Path(client_secret_path)
        self.tokens_dir = self.credentials_dir / "tokens"
        self.tokens_dir.mkdir(parents=True, exist_ok=True)

    def get_token_path(self, account_name: str) -> Path:
        return self.tokens_dir / f"{account_name}.json"

    def get_credentials(self, account_name: str) -> Optional[Credentials]:
        token_path = self.get_token_path(account_name)

        # Tokens are loaded with scopes=None so each one keeps the scopes it
        # was actually granted. Forcing the full SCOPES list here made Google
        # reject the refresh with "invalid_scope" for any token that was
        # created with fewer permissions (e.g. no Docs/Sheets/Forms).
        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), None)
            except Exception as exc:
                _log_auth_error(account_name, f"could not read token file {token_path.name}", exc)
                return None
        else:
            # Fall back to environment variable (used on Railway)
            env_var = ACCOUNT_ENV_MAP.get(account_name, account_name)
            token_json = os.environ.get(env_var)
            if not token_json:
                print(
                    f"[auth] {account_name}: no token file and environment variable "
                    f"'{env_var}' is not set",
                    file=sys.stderr,
                )
                return None
            try:
                creds = Credentials.from_authorized_user_info(
                    json.loads(token_json), None
                )
            except Exception as exc:
                _log_auth_error(account_name, f"could not read token from environment variable '{env_var}'", exc)
                return None

        if creds.valid:
            return creds

        if not creds.refresh_token:
            print(
                f"[auth] {account_name}: token is expired and has no refresh token",
                file=sys.stderr,
            )
            return None

        if creds.expired:
            try:
                creds.refresh(Request())
                self._save_token(account_name, creds)
                return creds
            except Exception as exc:
                _log_auth_error(account_name, "token refresh failed", exc)
                return None

        return None

    def authenticate(self, account_name: str, email: Optional[str] = None) -> Credentials:
        if not self.client_secret_path.exists():
            raise FileNotFoundError(
                f"client_secret.json not found at {self.client_secret_path}.\n"
                "Download it from Google Cloud Console > APIs & Services > Credentials."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secret_path), SCOPES
        )

        kwargs: dict = {"port": 0, "prompt": "select_account"}
        if email:
            kwargs["login_hint"] = email

        creds = flow.run_local_server(**kwargs)
        self._save_token(account_name, creds)
        return creds

    def _save_token(self, account_name: str, creds: Credentials) -> None:
        token_path = self.get_token_path(account_name)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    def is_authenticated(self, account_name: str) -> bool:
        return self.get_credentials(account_name) is not None

    def list_authenticated(self) -> List[str]:
        if not self.tokens_dir.exists():
            return []
        return [p.stem for p in self.tokens_dir.glob("*.json")]
