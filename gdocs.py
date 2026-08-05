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

    # ------------------------------------------------------------------ format

    def format_text(
        self,
        document_id: str,
        find_text: str = "",
        match_case: bool = False,
        match_all: bool = True,
        start_index: int = None,
        end_index: int = None,
        bold: bool = None,
        italic: bool = None,
        underline: bool = None,
        strikethrough: bool = None,
        font_size: float = None,
        font_family: str = None,
        foreground_color: str = None,
        background_color: str = None,
    ) -> Dict[str, Any]:
        """Apply character formatting (bold, italic, underline, strikethrough,
        font size/family, text color, highlight color) to a range of text in a
        Google Doc. Target the range either by `find_text` (the first match,
        or every match when `match_all` is true) or by an explicit
        `start_index`/`end_index` pair."""
        text_style: Dict[str, Any] = {}
        fields = []

        if bold is not None:
            text_style["bold"] = bold
            fields.append("bold")
        if italic is not None:
            text_style["italic"] = italic
            fields.append("italic")
        if underline is not None:
            text_style["underline"] = underline
            fields.append("underline")
        if strikethrough is not None:
            text_style["strikethrough"] = strikethrough
            fields.append("strikethrough")
        if font_size is not None:
            text_style["fontSize"] = {"magnitude": font_size, "unit": "PT"}
            fields.append("fontSize")
        if font_family:
            text_style["weightedFontFamily"] = {"fontFamily": font_family}
            fields.append("weightedFontFamily")
        if foreground_color:
            text_style["foregroundColor"] = {
                "color": {"rgbColor": self._hex_to_rgb(foreground_color)}
            }
            fields.append("foregroundColor")
        if background_color:
            text_style["backgroundColor"] = {
                "color": {"rgbColor": self._hex_to_rgb(background_color)}
            }
            fields.append("backgroundColor")

        if not fields:
            raise ValueError(
                "format_text requires at least one style change: bold, italic, "
                "underline, strikethrough, font_size, font_family, "
                "foreground_color, and/or background_color."
            )

        if start_index is not None or end_index is not None:
            if start_index is None or end_index is None:
                raise ValueError(
                    "format_text requires both start_index and end_index when "
                    "using an explicit range."
                )
            ranges = [(start_index, end_index)]
        elif find_text:
            doc = self.service.documents().get(documentId=document_id).execute()
            ranges = self._find_text_ranges(doc, find_text, match_case, match_all)
            if not ranges:
                raise ValueError(f"Text not found in document: {find_text!r}")
        else:
            raise ValueError(
                "format_text requires either find_text or a start_index/end_index range."
            )

        requests = [
            {
                "updateTextStyle": {
                    "range": {"startIndex": s, "endIndex": e},
                    "textStyle": text_style,
                    "fields": ",".join(fields),
                }
            }
            for s, e in ranges
        ]

        self.service.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()

        return self.get_document(document_id)

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> Dict[str, float]:
        """Convert a '#RRGGBB' string to a Docs API RgbColor dict (0-1 floats)."""
        h = hex_color.lstrip("#")
        if len(h) != 6:
            raise ValueError(
                f"Invalid hex color: {hex_color!r}. Expected format '#RRGGBB'."
            )
        try:
            r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
        except ValueError:
            raise ValueError(
                f"Invalid hex color: {hex_color!r}. Expected format '#RRGGBB'."
            )
        return {"red": r, "green": g, "blue": b}

    def _find_text_ranges(
        self, doc: Dict[str, Any], find_text: str, match_case: bool, match_all: bool
    ):
        """Locate document-index ranges for occurrences of `find_text` within
        the doc's text runs (paragraphs and table cells), in reading order."""
        runs: list = []
        cursor = [1]
        self._collect_runs(doc.get("body", {}).get("content", []), runs, cursor)

        concatenated = "".join(text for text, _ in runs)
        char_index = []  # document index for each character in `concatenated`
        for text, start in runs:
            char_index.extend(range(start, start + len(text)))

        haystack = concatenated if match_case else concatenated.lower()
        needle = find_text if match_case else find_text.lower()

        ranges = []
        pos = 0
        while True:
            idx = haystack.find(needle, pos)
            if idx == -1:
                break
            doc_start = char_index[idx]
            doc_end = char_index[idx + len(needle) - 1] + 1
            ranges.append((doc_start, doc_end))
            if not match_all:
                break
            pos = idx + len(needle)
        return ranges

    def _collect_runs(self, content, runs: list, cursor: list) -> None:
        """Recursively collect (text, start_index) pairs for every textRun in
        `content`, threading a running index cursor for runs missing an
        explicit startIndex."""
        for elem in content:
            paragraph = elem.get("paragraph")
            if paragraph:
                for run in paragraph.get("elements", []):
                    text_run = run.get("textRun")
                    if text_run:
                        start = run.get("startIndex", cursor[0])
                        text = text_run.get("content", "")
                        runs.append((text, start))
                        cursor[0] = start + len(text)

            table = elem.get("table")
            if table:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        self._collect_runs(cell.get("content", []), runs, cursor)

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
