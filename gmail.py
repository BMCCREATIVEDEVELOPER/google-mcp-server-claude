import base64
import mimetypes
import os
import re
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class GmailService:
    def __init__(
        self,
        credentials: Credentials,
        account_name: str = "",
        signature_html: str = "",
        signature_image_path: str = "",
    ):
        self.service = build("gmail", "v1", credentials=credentials)
        self.account_name = account_name
        self.signature_html = signature_html or ""
        self.signature_image_path = signature_image_path or ""
        # Cache for the auto-fetched Gmail signature (None = not yet fetched).
        self._gmail_signature: Optional[str] = None

    # ------------------------------------------------------------------ profile

    def get_profile(self) -> Dict[str, Any]:
        return self.service.users().getProfile(userId="me").execute()

    # ------------------------------------------------------------------ search / read

    def search_messages(
        self,
        query: str,
        max_results: int = 20,
        page_token: Optional[str] = None,
        include_body: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": min(max_results, 100),
        }
        if page_token:
            params["pageToken"] = page_token

        result = self.service.users().messages().list(**params).execute()
        raw_messages = result.get("messages", [])

        messages = []
        for raw in raw_messages:
            fmt = "full" if include_body else "metadata"
            msg = self._get_raw_message(raw["id"], format=fmt)
            messages.append(self._parse_message(msg))

        return {
            "messages": messages,
            "nextPageToken": result.get("nextPageToken"),
            "resultSizeEstimate": result.get("resultSizeEstimate", 0),
        }

    def get_message(self, message_id: str) -> Dict[str, Any]:
        msg = self._get_raw_message(message_id, format="full")
        return self._parse_message(msg)

    def get_thread(self, thread_id: str) -> Dict[str, Any]:
        thread = self.service.users().threads().get(userId="me", id=thread_id).execute()
        messages = [self._parse_message(m) for m in thread.get("messages", [])]
        return {
            "id": thread["id"],
            "messageCount": len(messages),
            "messages": messages,
        }

    # ------------------------------------------------------------------ send / draft

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html_body: str = "",
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachments=attachments,
        )
        return self.service.users().messages().send(
            userId="me", body={"raw": self._encode(msg)}
        ).execute()

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html_body: str = "",
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachments=attachments,
        )
        return self.service.users().drafts().create(
            userId="me", body={"message": {"raw": self._encode(msg)}}
        ).execute()

    # ------------------------------------------------------------------ signature fetch

    def get_gmail_signature(self) -> str:
        """Return the HTML signature configured in the account's own Gmail
        Settings (Settings > See all settings > Signature), for the primary
        send-as address.

        Requires the gmail.settings.basic scope. The result is cached on the
        instance. Any failure (missing scope, API error, no signature set)
        returns an empty string rather than raising.
        """
        if self._gmail_signature is not None:
            return self._gmail_signature

        signature = ""
        try:
            result = self.service.users().settings().sendAs().list(
                userId="me"
            ).execute()
            send_as = result.get("sendAs", [])
            # The primary login address is flagged isPrimary (exactly one per
            # account). Fall back to isDefault, then the first entry.
            entry = (
                next((s for s in send_as if s.get("isPrimary")), None)
                or next((s for s in send_as if s.get("isDefault")), None)
                or (send_as[0] if send_as else None)
            )
            if entry:
                signature = entry.get("signature", "") or ""
        except Exception:
            signature = ""

        self._gmail_signature = signature
        return signature

    # ------------------------------------------------------------------ message building

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html_body: str = "",
        attachments: Optional[List[str]] = None,
    ) -> MIMEMultipart:
        """Assemble a MIME message with optional HTML body, per-account
        signature (HTML or inline image fallback) and file attachments.

        Structure (outer to inner):
            mixed     -> holds file attachments
              related -> holds the inline signature image (cid)
                alternative -> text/plain + text/html

        A plain-text body is always included as a fallback for clients that
        do not render HTML.

        Signature priority (only ONE source is ever used per email):
            1. Auto-fetched Gmail Settings signature (gmail.settings.basic)
            2. config.json signature_html
            3. config.json signature_image_path (inline image)
            4. No signature
        """
        # Resolve the single effective HTML signature, in priority order.
        gmail_signature = self.get_gmail_signature()
        effective_html_signature = gmail_signature or self.signature_html

        # The inline image fallback is used only when neither HTML signature
        # source (auto-fetched or config) provided anything.
        use_inline_image = (
            bool(self.signature_image_path)
            and not effective_html_signature
            and os.path.isfile(self.signature_image_path)
        )

        html_content = html_body or ""
        if effective_html_signature:
            # Append the resolved HTML signature to the HTML part.
            base_html = html_content or self._text_to_html(body)
            html_content = base_html + effective_html_signature
        elif use_inline_image:
            base_html = html_content or self._text_to_html(body)
            html_content = base_html + '<br><img src="cid:signature_image">'

        # --- alternative: plain text + (optional) html ---
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(body, "plain", "utf-8"))
        if html_content:
            alternative.attach(MIMEText(html_content, "html", "utf-8"))

        # --- related: alternative + inline signature image (if any) ---
        if use_inline_image:
            related = MIMEMultipart("related")
            related.attach(alternative)
            related.attach(self._inline_image(self.signature_image_path))
            content_root = related
        else:
            content_root = alternative

        # --- mixed: content + file attachments ---
        attachments = attachments or []
        if attachments:
            outer = MIMEMultipart("mixed")
            outer.attach(content_root)
            for path in attachments:
                outer.attach(self._file_attachment(path))
            msg = outer
        else:
            msg = content_root

        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc
        if bcc:
            msg["bcc"] = bcc
        return msg

    @staticmethod
    def _text_to_html(text: str) -> str:
        """Escape plain text and convert newlines to <br> for the HTML part."""
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return escaped.replace("\n", "<br>")

    @staticmethod
    def _inline_image(path: str) -> MIMEImage:
        if not os.path.isfile(path):
            raise ValueError(f"Signature image not found: {path}")
        with open(path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<signature_image>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
        return img

    @staticmethod
    def _file_attachment(path: str) -> MIMEBase:
        if not os.path.isfile(path):
            raise ValueError(f"Attachment file not found: {path}")
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            part = MIMEBase(maintype, subtype)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", "attachment", filename=os.path.basename(path)
        )
        return part

    def list_drafts(self, max_results: int = 20) -> List[Dict[str, Any]]:
        result = self.service.users().drafts().list(
            userId="me", maxResults=min(max_results, 50)
        ).execute()

        drafts = []
        for draft in result.get("drafts", []):
            details = self.service.users().drafts().get(
                userId="me", id=draft["id"], format="full"
            ).execute()
            msg = self._parse_message(details.get("message", {}))
            msg["draft_id"] = draft["id"]
            drafts.append(msg)

        return drafts

    # ------------------------------------------------------------------ labels

    def list_labels(self) -> List[Dict[str, Any]]:
        result = self.service.users().labels().list(userId="me").execute()
        return [
            {"id": lbl["id"], "name": lbl["name"], "type": lbl.get("type", "")}
            for lbl in result.get("labels", [])
        ]

    def modify_labels(
        self,
        message_id: str,
        add_labels: Optional[List[str]] = None,
        remove_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels

        return self.service.users().messages().modify(
            userId="me", id=message_id, body=body
        ).execute()

    def trash_message(self, message_id: str) -> Dict[str, Any]:
        return self.service.users().messages().trash(
            userId="me", id=message_id
        ).execute()

    # ------------------------------------------------------------------ internals

    def _get_raw_message(self, message_id: str, format: str = "full") -> Dict[str, Any]:
        return self.service.users().messages().get(
            userId="me", id=message_id, format=format
        ).execute()

    def _parse_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        payload = msg.get("payload", {})
        headers: Dict[str, str] = {}
        for h in payload.get("headers", []):
            headers[h["name"].lower()] = h["value"]

        body = self._extract_body(payload)

        return {
            "id": msg.get("id", ""),
            "threadId": msg.get("threadId", ""),
            "labels": msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
            "date": headers.get("date", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", "(no subject)"),
            "body": body,
        }

    def _extract_body(self, payload: Dict[str, Any]) -> str:
        if not payload:
            return ""

        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")

        if body_data:
            decoded = base64.urlsafe_b64decode(body_data.encode()).decode(
                "utf-8", errors="replace"
            )
            if "html" in mime_type:
                decoded = re.sub(r"<[^>]+>", " ", decoded)
                decoded = (
                    decoded.replace("&nbsp;", " ")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&amp;", "&")
                    .replace("&quot;", '"')
                )
                decoded = re.sub(r"\s+", " ", decoded)
            return decoded.strip()

        parts = payload.get("parts", [])

        # Prefer text/plain parts
        for part in parts:
            if part.get("mimeType") == "text/plain":
                result = self._extract_body(part)
                if result:
                    return result

        # Fallback: any part that returns content
        for part in parts:
            result = self._extract_body(part)
            if result:
                return result

        return ""

    @staticmethod
    def _encode(msg: MIMEText | MIMEMultipart) -> str:
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
