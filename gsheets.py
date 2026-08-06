import re
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class SheetsService:
    def __init__(self, credentials: Credentials, account_name: str = ""):
        self.service = build("sheets", "v4", credentials=credentials)
        self.drive_service = build("drive", "v3", credentials=credentials)
        self.account_name = account_name

    # ------------------------------------------------------------------ create

    def create_spreadsheet(
        self,
        title: str,
        data: Optional[List[List[Any]]] = None,
        sheet_name: str = "Sheet1",
    ) -> Dict[str, Any]:
        """Create a new Google Sheet, optionally seeded with initial row data
        on its first tab."""
        body = {
            "properties": {"title": title},
            "sheets": [{"properties": {"title": sheet_name}}],
        }
        spreadsheet = self.service.spreadsheets().create(
            body=body, fields="spreadsheetId"
        ).execute()
        spreadsheet_id = spreadsheet["spreadsheetId"]

        if data:
            self.service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=sheet_name,
                valueInputOption="USER_ENTERED",
                body={"values": data},
            ).execute()

        return self.get_spreadsheet(spreadsheet_id)

    def add_sheet(self, spreadsheet_id: str, title: str) -> Dict[str, Any]:
        """Add a new tab to an existing spreadsheet."""
        if not title:
            raise ValueError("add_sheet requires a non-empty title.")

        result = self.service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        props = result["replies"][0]["addSheet"]["properties"]
        return {
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": props["sheetId"],
            "title": props["title"],
        }

    # ------------------------------------------------------------------ read

    def get_spreadsheet(self, spreadsheet_id: str) -> Dict[str, Any]:
        """Read a spreadsheet's title and the list of tabs (sheets) it contains."""
        meta = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties"
        ).execute()

        sheets = [
            {
                "sheet_id": s["properties"]["sheetId"],
                "title": s["properties"]["title"],
                "rows": s["properties"].get("gridProperties", {}).get("rowCount"),
                "columns": s["properties"].get("gridProperties", {}).get("columnCount"),
            }
            for s in meta.get("sheets", [])
        ]
        return {
            "id": spreadsheet_id,
            "title": meta.get("properties", {}).get("title", ""),
            "sheets": sheets,
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

    def read_values(self, spreadsheet_id: str, range_name: str = "") -> Dict[str, Any]:
        """Read cell values for a range, e.g. 'Sheet1!A1:D10'. Defaults to the
        whole first tab if range_name is omitted."""
        if not range_name:
            meta = self.get_spreadsheet(spreadsheet_id)
            if not meta["sheets"]:
                raise ValueError("Spreadsheet has no sheets.")
            range_name = meta["sheets"][0]["title"]

        result = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_name
        ).execute()
        values = result.get("values", [])
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": result.get("range", range_name),
            "values": values,
            "row_count": len(values),
        }

    # ------------------------------------------------------------------ update

    def update_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Overwrite cell values for a range with a 2D array of rows.
        value_input_option: 'USER_ENTERED' (parses formulas/numbers/dates like
        typing in the UI) or 'RAW' (stores values as literal strings)."""
        if not range_name:
            raise ValueError("update_values requires range_name, e.g. 'Sheet1!A1'.")
        if not values:
            raise ValueError("update_values requires non-empty values (a 2D list of rows).")

        result = self.service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            body={"values": values},
        ).execute()
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": result.get("updatedRange", range_name),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
            "updated_cells": result.get("updatedCells", 0),
        }

    def append_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Append rows of values after the last row of data in a range/sheet,
        e.g. range_name='Sheet1'."""
        if not range_name:
            raise ValueError("append_values requires range_name, e.g. 'Sheet1' or 'Sheet1!A1'.")
        if not values:
            raise ValueError("append_values requires non-empty values (a 2D list of rows).")

        result = self.service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        updates = result.get("updates", {})
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": updates.get("updatedRange", range_name),
            "updated_rows": updates.get("updatedRows", 0),
            "updated_cells": updates.get("updatedCells", 0),
        }

    def rename_sheet(
        self,
        spreadsheet_id: str,
        new_title: str,
        sheet_name: str = "",
        sheet_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Rename a tab within a spreadsheet. Identify the tab by its current
        sheet_name or numeric sheet_id."""
        if not new_title:
            raise ValueError("rename_sheet requires a non-empty new_title.")
        if sheet_id is None and not sheet_name:
            raise ValueError("rename_sheet requires sheet_name or sheet_id.")
        if sheet_id is None:
            sheet_id, _ = self._get_sheet_id(spreadsheet_id, sheet_name)

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sheet_id, "title": new_title},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
        return {
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "title": new_title,
        }

    def rename_spreadsheet(self, spreadsheet_id: str, title: str) -> Dict[str, Any]:
        """Rename a spreadsheet (changes its title/file name). The title lives
        on the Drive file, so this goes through the Drive API."""
        if not title:
            raise ValueError("rename_spreadsheet requires a non-empty title.")

        self.drive_service.files().update(
            fileId=spreadsheet_id, body={"name": title}
        ).execute()
        return self.get_spreadsheet(spreadsheet_id)

    # ------------------------------------------------------------------ format

    def format_cells(
        self,
        spreadsheet_id: str,
        range_name: str,
        bold: Optional[bool] = None,
        italic: Optional[bool] = None,
        underline: Optional[bool] = None,
        strikethrough: Optional[bool] = None,
        font_size: Optional[float] = None,
        font_family: Optional[str] = None,
        foreground_color: Optional[str] = None,
        background_color: Optional[str] = None,
        horizontal_alignment: Optional[str] = None,
        number_format: Optional[str] = None,
        number_format_type: str = "NUMBER",
    ) -> Dict[str, Any]:
        """Apply cell formatting (bold, italic, underline, strikethrough, font
        size/family, text color, fill color, horizontal alignment, number
        format) to a range, e.g. 'Sheet1!A1:C5'."""
        text_format: Dict[str, Any] = {}
        fields = []

        if bold is not None:
            text_format["bold"] = bold
            fields.append("userEnteredFormat.textFormat.bold")
        if italic is not None:
            text_format["italic"] = italic
            fields.append("userEnteredFormat.textFormat.italic")
        if underline is not None:
            text_format["underline"] = underline
            fields.append("userEnteredFormat.textFormat.underline")
        if strikethrough is not None:
            text_format["strikethrough"] = strikethrough
            fields.append("userEnteredFormat.textFormat.strikethrough")
        if font_size is not None:
            text_format["fontSize"] = font_size
            fields.append("userEnteredFormat.textFormat.fontSize")
        if font_family:
            text_format["fontFamily"] = font_family
            fields.append("userEnteredFormat.textFormat.fontFamily")
        if foreground_color:
            text_format["foregroundColor"] = self._hex_to_rgb(foreground_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")

        cell_format: Dict[str, Any] = {}
        if text_format:
            cell_format["textFormat"] = text_format
        if background_color:
            cell_format["backgroundColor"] = self._hex_to_rgb(background_color)
            fields.append("userEnteredFormat.backgroundColor")
        if horizontal_alignment:
            cell_format["horizontalAlignment"] = horizontal_alignment.upper()
            fields.append("userEnteredFormat.horizontalAlignment")
        if number_format:
            cell_format["numberFormat"] = {
                "type": number_format_type.upper(),
                "pattern": number_format,
            }
            fields.append("userEnteredFormat.numberFormat")

        if not fields:
            raise ValueError(
                "format_cells requires at least one style change: bold, italic, "
                "underline, strikethrough, font_size, font_family, "
                "foreground_color, background_color, horizontal_alignment, "
                "and/or number_format."
            )

        grid_range = self._parse_a1_range(spreadsheet_id, range_name)

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": grid_range,
                            "cell": {"userEnteredFormat": cell_format},
                            "fields": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute()

        return {"status": "formatted", "spreadsheet_id": spreadsheet_id, "range": range_name}

    # ------------------------------------------------------------------ delete

    def clear_values(self, spreadsheet_id: str, range_name: str) -> Dict[str, Any]:
        """Clear cell values (not formatting) in a range."""
        if not range_name:
            raise ValueError("clear_values requires range_name, e.g. 'Sheet1!A1:D10'.")

        self.service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=range_name, body={}
        ).execute()
        return {"status": "cleared", "spreadsheet_id": spreadsheet_id, "range": range_name}

    def delete_sheet(
        self,
        spreadsheet_id: str,
        sheet_name: str = "",
        sheet_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Delete a tab from a spreadsheet. Identify it by sheet_name or
        numeric sheet_id."""
        if sheet_id is None and not sheet_name:
            raise ValueError("delete_sheet requires sheet_name or sheet_id.")
        if sheet_id is None:
            sheet_id, _ = self._get_sheet_id(spreadsheet_id, sheet_name)

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
        ).execute()
        return {"status": "deleted", "spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id}

    def delete_spreadsheet(self, spreadsheet_id: str) -> Dict[str, str]:
        """Move a spreadsheet to Drive trash (the Sheets API has no hard delete)."""
        self.drive_service.files().update(
            fileId=spreadsheet_id, body={"trashed": True}
        ).execute()
        return {"status": "trashed", "spreadsheet_id": spreadsheet_id}

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> Dict[str, float]:
        """Convert a '#RRGGBB' string to a Sheets API Color dict (0-1 floats)."""
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

    def _get_sheet_id(self, spreadsheet_id: str, sheet_name: Optional[str] = None):
        """Resolve a tab name to its numeric sheetId. Returns the first tab's
        id if sheet_name is omitted."""
        meta = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties"
        ).execute()
        sheets = meta.get("sheets", [])
        if not sheets:
            raise ValueError("Spreadsheet has no sheets.")

        if sheet_name:
            for s in sheets:
                props = s["properties"]
                if props["title"] == sheet_name:
                    return props["sheetId"], props["title"]
            raise ValueError(f"Sheet not found: {sheet_name!r}")

        props = sheets[0]["properties"]
        return props["sheetId"], props["title"]

    def _parse_a1_range(self, spreadsheet_id: str, range_str: str) -> Dict[str, Any]:
        """Convert an A1-notation range (e.g. 'Sheet1!A1:C5') into a Sheets API
        GridRange, resolving the sheet name to its numeric sheetId. A bare
        name with no '!' (e.g. 'Sheet1') targets the whole tab."""
        if not range_str:
            raise ValueError("range_name is required, e.g. 'Sheet1!A1:C5' or 'Sheet1'.")

        if "!" in range_str:
            sheet_name, cell_range = range_str.split("!", 1)
        else:
            sheet_name, cell_range = range_str, ""

        sheet_id, _ = self._get_sheet_id(spreadsheet_id, sheet_name or None)
        grid_range: Dict[str, Any] = {"sheetId": sheet_id}

        if cell_range:
            parts = cell_range.split(":")
            start_ref = parts[0]
            end_ref = parts[1] if len(parts) > 1 else parts[0]

            start_col, start_row = self._split_cell_ref(start_ref)
            end_col, end_row = self._split_cell_ref(end_ref)

            if start_col is not None:
                grid_range["startColumnIndex"] = start_col
            if end_col is not None:
                grid_range["endColumnIndex"] = end_col + 1
            if start_row is not None:
                grid_range["startRowIndex"] = start_row
            if end_row is not None:
                grid_range["endRowIndex"] = end_row + 1

        return grid_range

    @staticmethod
    def _split_cell_ref(ref: str):
        """Split 'B3' into (column_index, row_index), both 0-based. A bare
        column ('B') or row ('3') leaves the other side as None (unbounded)."""
        m = re.match(r"^([A-Za-z]*)(\d*)$", ref)
        if not m:
            raise ValueError(f"Invalid cell reference: {ref!r}")
        col_letters, row_digits = m.groups()

        col = None
        if col_letters:
            idx = 0
            for ch in col_letters.upper():
                idx = idx * 26 + (ord(ch) - ord("A") + 1)
            col = idx - 1

        row = int(row_digits) - 1 if row_digits else None
        return col, row
