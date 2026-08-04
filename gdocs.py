from typing import Any, Dict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class DocsService:
    def __init__(self, credentials: Credentials, account_name: str = ""):
        self.service = build("docs", "v1", credentials=credentials)
        self.drive_service = build("drive", "v3", credentials=credentials)
        self.account_name = account_name

    # ------------------------------------------------------------------ create

    def create_document(self, title: str, content: str = "") -> Dict[str, Any]:
        """Create a new Google Doc, optionally seeded with initial text content."""
        doc = self.service.documents().create(body={"title": title}).execute()
        document_id = doc["documentId"]

        if content:
            self.service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {"insertText": {"location": {"index": 1}, "text": content}}
                    ]
                },
            ).execute()

        return self.get_document(document_id)

    # ------------------------------------------------------------------ read

    def get_document(self, document_id: str) -> Dict[str, Any]:
        """Read a Google Doc's title and plain-text content."""
        doc = self.service.documents().get(documentId=document_id).execute()
        content = self._extract_text(doc)
        return {
            "id": doc.get("documentId", document_id),
            "title": doc.get("title", ""),
            "content": content,
            "length": len(content),
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }

    # ------------------------------------------------------------------ update

    def update_document(
        self,
        document_id: str,
        append_text: str = "",
        find_text: str = "",
        replace_text: str = "",
        match_case: bool = False,
    ) -> Dict[str, Any]:
        """Update a Google Doc. Supports find/replace across the whole document
        and/or appending text to the end. At least one of `append_text` or
        `find_text` must be provided."""
        if not append_text and not find_text:
            raise ValueError(
                "update_document requires append_text and/or find_text/replace_text."
            )

        if find_text:
            self.service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "replaceAllText": {
                                "containsText": {
                                    "text": find_text,
                                    "matchCase": match_case,
                                },
                                "replaceText": replace_text,
                            }
                        }
                    ]
                },
            ).execute()

        if append_text:
            # Re-fetch to get the current end index (replaceAllText above, if any,
            # can change the document length).
            doc = self.service.documents().get(documentId=document_id).execute()
            end_index = doc["body"]["content"][-1]["endIndex"] - 1
            self.service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "location": {"index": end_index},
                                "text": append_text,
                            }
                        }
                    ]
                },
            ).execute()

        return self.get_document(document_id)

    # ------------------------------------------------------------------ rename

    def rename_document(self, document_id: str, title: str) -> Dict[str, Any]:
        """Rename a Google Doc. The title lives on the Drive file, not the Docs
        resource itself, so this goes through the Drive API."""
        if not title:
            raise ValueError("rename_document requires a non-empty title.")

        self.drive_service.files().update(
            fileId=document_id, body={"name": title}
        ).execute()
        return self.get_document(document_id)

    # ------------------------------------------------------------------ delete

    def delete_document(self, document_id: str) -> Dict[str, str]:
        """Move a Google Doc to Drive trash (the Docs API has no hard delete)."""
        self.drive_service.files().update(
            fileId=document_id, body={"trashed": True}
        ).execute()
        return {"status": "trashed", "document_id": document_id}

    # ------------------------------------------------------------------ internals

    def _extract_text(self, doc: Dict[str, Any]) -> str:
        """Flatten a Docs API document body (paragraphs + table cells) to plain text."""
        text = ""
        for elem in doc.get("body", {}).get("content", []):
            text += self._extract_structural_element(elem)
        return text

    def _extract_structural_element(self, elem: Dict[str, Any]) -> str:
        text = ""
        paragraph = elem.get("paragraph")
        if paragraph:
            for run in paragraph.get("elements", []):
                text_run = run.get("textRun")
                if text_run:
                    text += text_run.get("content", "")

        table = elem.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    for cell_elem in cell.get("content", []):
                        text += self._extract_structural_element(cell_elem)

        return text
