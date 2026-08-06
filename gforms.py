from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# question_type -> Sheets-style ChoiceQuestion.type enum
_CHOICE_API_TYPES = {"RADIO": "RADIO", "CHECKBOX": "CHECKBOX", "DROPDOWN": "DROP_DOWN"}

SUPPORTED_QUESTION_TYPES = (
    "SHORT_TEXT", "PARAGRAPH", "RADIO", "CHECKBOX", "DROPDOWN",
    "SCALE", "DATE", "TIME", "RATING",
)


class FormsService:
    def __init__(self, credentials: Credentials, account_name: str = ""):
        self.service = build("forms", "v1", credentials=credentials)
        self.drive_service = build("drive", "v3", credentials=credentials)
        self.account_name = account_name

    # ------------------------------------------------------------------ create

    def create_form(self, title: str, document_title: Optional[str] = None) -> Dict[str, Any]:
        """Create a new blank Google Form. The Forms API only allows setting
        the title (and optionally the Drive file name) at creation time —
        description, questions, and settings must be added afterward via
        add_question / update_info / update_settings."""
        if not title:
            raise ValueError("create_form requires a non-empty title.")

        info: Dict[str, Any] = {"title": title}
        if document_title:
            info["documentTitle"] = document_title

        form = self.service.forms().create(body={"info": info}).execute()
        return self._parse_form(form)

    def add_question(
        self,
        form_id: str,
        title: str,
        question_type: str,
        options: Optional[List[str]] = None,
        required: bool = False,
        description: Optional[str] = None,
        low: Optional[int] = None,
        high: Optional[int] = None,
        low_label: Optional[str] = None,
        high_label: Optional[str] = None,
        include_year: bool = True,
        include_time: bool = False,
        duration: bool = False,
        rating_icon: str = "STAR",
        rating_levels: int = 5,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Add a question to a form. question_type is one of SHORT_TEXT,
        PARAGRAPH, RADIO, CHECKBOX, DROPDOWN, SCALE, DATE, TIME, RATING.
        RADIO/CHECKBOX/DROPDOWN require options. index places the question
        at a specific position (0-based); omit to append at the end."""
        question = self._build_question(
            question_type, options, required, low, high, low_label, high_label,
            include_year, include_time, duration, rating_icon, rating_levels,
        )
        item: Dict[str, Any] = {"title": title, "questionItem": {"question": question}}
        if description:
            item["description"] = description

        if index is None:
            form_meta = self.service.forms().get(formId=form_id, fields="items").execute()
            index = len(form_meta.get("items", []))

        result = self.service.forms().batchUpdate(
            formId=form_id,
            body={"requests": [{"createItem": {"item": item, "location": {"index": index}}}]},
        ).execute()
        item_id = result["replies"][0]["createItem"]["itemId"]
        return {"form_id": form_id, "item_id": item_id, "index": index}

    # ------------------------------------------------------------------ read

    def get_form(self, form_id: str) -> Dict[str, Any]:
        form = self.service.forms().get(formId=form_id).execute()
        return self._parse_form(form)

    def list_responses(self, form_id: str, max_results: int = 20) -> List[Dict[str, Any]]:
        result = self.service.forms().responses().list(
            formId=form_id, pageSize=min(max_results, 200)
        ).execute()
        return [self._parse_response(r) for r in result.get("responses", [])]

    def get_response(self, form_id: str, response_id: str) -> Dict[str, Any]:
        r = self.service.forms().responses().get(
            formId=form_id, responseId=response_id
        ).execute()
        return self._parse_response(r)

    # ------------------------------------------------------------------ update

    def update_info(
        self,
        form_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the form's visible title and/or description."""
        if title is None and description is None:
            raise ValueError("update_info requires title and/or description.")

        info: Dict[str, Any] = {}
        fields = []
        if title is not None:
            info["title"] = title
            fields.append("title")
        if description is not None:
            info["description"] = description
            fields.append("description")

        self.service.forms().batchUpdate(
            formId=form_id,
            body={"requests": [{"updateFormInfo": {"info": info, "updateMask": ",".join(fields)}}]},
        ).execute()
        return self.get_form(form_id)

    def update_settings(
        self,
        form_id: str,
        email_collection_type: Optional[str] = None,
        is_quiz: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update form-level settings. email_collection_type is one of
        DO_NOT_COLLECT, VERIFIED, RESPONDER_INPUT. is_quiz turns quiz
        grading on/off (turning it off deletes any existing grading)."""
        if email_collection_type is None and is_quiz is None:
            raise ValueError("update_settings requires email_collection_type and/or is_quiz.")

        settings: Dict[str, Any] = {}
        fields = []
        if email_collection_type is not None:
            settings["emailCollectionType"] = email_collection_type.upper()
            fields.append("emailCollectionType")
        if is_quiz is not None:
            settings["quizSettings"] = {"isQuiz": is_quiz}
            fields.append("quizSettings.isQuiz")

        self.service.forms().batchUpdate(
            formId=form_id,
            body={"requests": [{"updateSettings": {"settings": settings, "updateMask": ",".join(fields)}}]},
        ).execute()
        return self.get_form(form_id)

    def set_publish_settings(
        self,
        form_id: str,
        is_published: bool,
        is_accepting_responses: bool,
    ) -> Dict[str, Any]:
        """Publish/unpublish a form and control whether it currently accepts
        responses. is_accepting_responses=True requires is_published=True."""
        if is_accepting_responses and not is_published:
            raise ValueError(
                "set_publish_settings: is_accepting_responses=True requires is_published=True."
            )

        result = self.service.forms().setPublishSettings(
            formId=form_id,
            body={
                "publishSettings": {
                    "publishState": {
                        "isPublished": is_published,
                        "isAcceptingResponses": is_accepting_responses,
                    }
                }
            },
        ).execute()
        state = result.get("publishSettings", {}).get("publishState", {})
        return {
            "form_id": form_id,
            "is_published": state.get("isPublished"),
            "is_accepting_responses": state.get("isAcceptingResponses"),
        }

    def update_question(
        self,
        form_id: str,
        item_id: str,
        title: Optional[str] = None,
        question_type: Optional[str] = None,
        options: Optional[List[str]] = None,
        required: Optional[bool] = None,
        description: Optional[str] = None,
        low: Optional[int] = None,
        high: Optional[int] = None,
        low_label: Optional[str] = None,
        high_label: Optional[str] = None,
        include_year: Optional[bool] = None,
        include_time: Optional[bool] = None,
        duration: Optional[bool] = None,
        rating_icon: Optional[str] = None,
        rating_levels: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Update an existing question item, found by item_id (from
        add_question or get_form). Any field left as None keeps its current
        value. Pass question_type (plus options, for choice types) to change
        the question's kind — otherwise it stays the same kind and only
        required/options (if given) are patched in place."""
        index, current_item = self._find_item(form_id, item_id)
        current_q = current_item.get("questionItem", {}).get("question", {})

        new_item: Dict[str, Any] = {"itemId": item_id}
        fields = ["title"]
        new_item["title"] = title if title is not None else current_item.get("title", "")

        if description is not None:
            new_item["description"] = description
            fields.append("description")
        elif current_item.get("description"):
            new_item["description"] = current_item["description"]

        if question_type is not None:
            new_question = self._build_question(
                question_type, options,
                required if required is not None else current_q.get("required", False),
                low, high, low_label, high_label,
                include_year if include_year is not None else True,
                include_time if include_time is not None else False,
                duration if duration is not None else False,
                rating_icon or "STAR", rating_levels or 5,
            )
        else:
            new_question = dict(current_q)
            if required is not None:
                new_question["required"] = required
            if options is not None and "choiceQuestion" in new_question:
                new_question["choiceQuestion"] = dict(new_question["choiceQuestion"])
                new_question["choiceQuestion"]["options"] = [{"value": o} for o in options]

        new_question["questionId"] = current_q.get("questionId", "")
        new_item["questionItem"] = {"question": new_question}
        fields.append("questionItem")

        self.service.forms().batchUpdate(
            formId=form_id,
            body={
                "requests": [
                    {
                        "updateItem": {
                            "item": new_item,
                            "location": {"index": index},
                            "updateMask": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute()
        return {"form_id": form_id, "item_id": item_id, "status": "updated"}

    def move_question(self, form_id: str, item_id: str, new_index: int) -> Dict[str, Any]:
        """Move a question/item to a new 0-based position in the form."""
        index, _ = self._find_item(form_id, item_id)

        self.service.forms().batchUpdate(
            formId=form_id,
            body={
                "requests": [
                    {
                        "moveItem": {
                            "originalLocation": {"index": index},
                            "newLocation": {"index": new_index},
                        }
                    }
                ]
            },
        ).execute()
        return {"form_id": form_id, "item_id": item_id, "new_index": new_index, "status": "moved"}

    def rename_form(self, form_id: str, title: str) -> Dict[str, Any]:
        """Rename the form's Drive file name (the "document title"). This is
        NOT the same as the visible title respondents see on the form itself
        — use update_info for that."""
        if not title:
            raise ValueError("rename_form requires a non-empty title.")

        self.drive_service.files().update(fileId=form_id, body={"name": title}).execute()
        return self.get_form(form_id)

    # ------------------------------------------------------------------ delete

    def delete_question(self, form_id: str, item_id: str) -> Dict[str, Any]:
        index, _ = self._find_item(form_id, item_id)

        self.service.forms().batchUpdate(
            formId=form_id,
            body={"requests": [{"deleteItem": {"location": {"index": index}}}]},
        ).execute()
        return {"form_id": form_id, "item_id": item_id, "status": "deleted"}

    def delete_form(self, form_id: str) -> Dict[str, str]:
        """Move a form to Drive trash (the Forms API has no delete-form call —
        a form is a Drive file, so deletion goes through the Drive API, same
        as Docs/Sheets)."""
        self.drive_service.files().update(fileId=form_id, body={"trashed": True}).execute()
        return {"status": "trashed", "form_id": form_id}

    # ------------------------------------------------------------------ internals

    def _find_item(self, form_id: str, item_id: str):
        form = self.service.forms().get(formId=form_id, fields="items").execute()
        items = form.get("items", [])
        index = next((i for i, it in enumerate(items) if it.get("itemId") == item_id), None)
        if index is None:
            raise ValueError(f"No item found with item_id {item_id!r} in this form.")
        return index, items[index]

    def _build_question(
        self,
        question_type: str,
        options: Optional[List[str]],
        required: bool,
        low: Optional[int],
        high: Optional[int],
        low_label: Optional[str],
        high_label: Optional[str],
        include_year: bool,
        include_time: bool,
        duration: bool,
        rating_icon: str,
        rating_levels: int,
    ) -> Dict[str, Any]:
        qt = question_type.upper()
        question: Dict[str, Any] = {"required": required}

        if qt == "SHORT_TEXT":
            question["textQuestion"] = {"paragraph": False}
        elif qt == "PARAGRAPH":
            question["textQuestion"] = {"paragraph": True}
        elif qt in _CHOICE_API_TYPES:
            if not options:
                raise ValueError(f"{qt} questions require a non-empty options list.")
            question["choiceQuestion"] = {
                "type": _CHOICE_API_TYPES[qt],
                "options": [{"value": o} for o in options],
            }
        elif qt == "SCALE":
            question["scaleQuestion"] = {
                "low": low if low is not None else 1,
                "high": high if high is not None else 5,
            }
            if low_label:
                question["scaleQuestion"]["lowLabel"] = low_label
            if high_label:
                question["scaleQuestion"]["highLabel"] = high_label
        elif qt == "DATE":
            question["dateQuestion"] = {"includeYear": include_year, "includeTime": include_time}
        elif qt == "TIME":
            question["timeQuestion"] = {"duration": duration}
        elif qt == "RATING":
            question["ratingQuestion"] = {
                "iconType": rating_icon.upper(),
                "ratingScaleLevel": rating_levels,
            }
        else:
            raise ValueError(
                f"Unsupported question_type: {question_type!r}. Use one of: "
                + ", ".join(SUPPORTED_QUESTION_TYPES)
                + ". (FILE_UPLOAD questions cannot be created via the Forms API.)"
            )
        return question

    def _parse_form(self, form: Dict[str, Any]) -> Dict[str, Any]:
        info = form.get("info", {})
        items = [self._parse_item(i) for i in form.get("items", [])]
        publish = form.get("publishSettings", {}).get("publishState", {})
        form_id = form.get("formId", "")
        return {
            "id": form_id,
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "document_title": info.get("documentTitle", ""),
            "items": items,
            "responder_url": form.get("responderUri", ""),
            "edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
            "linked_sheet_id": form.get("linkedSheetId"),
            "is_published": publish.get("isPublished"),
            "is_accepting_responses": publish.get("isAcceptingResponses"),
            "settings": form.get("settings", {}),
        }

    def _parse_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "item_id": item.get("itemId", ""),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
        }

        q_item = item.get("questionItem")
        if q_item:
            q = q_item.get("question", {})
            parsed["question_id"] = q.get("questionId", "")
            parsed["required"] = q.get("required", False)
            if "textQuestion" in q:
                parsed["type"] = "PARAGRAPH" if q["textQuestion"].get("paragraph") else "SHORT_TEXT"
            elif "choiceQuestion" in q:
                cq = q["choiceQuestion"]
                parsed["type"] = "DROPDOWN" if cq.get("type") == "DROP_DOWN" else cq.get("type", "")
                parsed["options"] = [o.get("value", "") for o in cq.get("options", [])]
            elif "scaleQuestion" in q:
                sq = q["scaleQuestion"]
                parsed["type"] = "SCALE"
                parsed["low"] = sq.get("low")
                parsed["high"] = sq.get("high")
                parsed["low_label"] = sq.get("lowLabel", "")
                parsed["high_label"] = sq.get("highLabel", "")
            elif "dateQuestion" in q:
                parsed["type"] = "DATE"
            elif "timeQuestion" in q:
                parsed["type"] = "TIME"
            elif "ratingQuestion" in q:
                rq = q["ratingQuestion"]
                parsed["type"] = "RATING"
                parsed["rating_icon"] = rq.get("iconType", "")
                parsed["rating_levels"] = rq.get("ratingScaleLevel")
            elif "fileUploadQuestion" in q:
                parsed["type"] = "FILE_UPLOAD"
            else:
                parsed["type"] = "UNKNOWN"
        elif "pageBreakItem" in item:
            parsed["type"] = "PAGE_BREAK"
        elif "textItem" in item:
            parsed["type"] = "TEXT_BLOCK"
        elif "imageItem" in item:
            parsed["type"] = "IMAGE"
        elif "questionGroupItem" in item:
            parsed["type"] = "QUESTION_GROUP"
        else:
            parsed["type"] = "UNKNOWN"

        return parsed

    def _parse_response(self, r: Dict[str, Any]) -> Dict[str, Any]:
        answers: Dict[str, Any] = {}
        for qid, ans in r.get("answers", {}).items():
            if "textAnswers" in ans:
                values = [a.get("value", "") for a in ans["textAnswers"].get("answers", [])]
                answers[qid] = values if len(values) > 1 else (values[0] if values else "")
            elif "fileUploadAnswers" in ans:
                answers[qid] = [
                    {
                        "file_id": a.get("fileId"),
                        "file_name": a.get("fileName"),
                        "mime_type": a.get("mimeType"),
                    }
                    for a in ans["fileUploadAnswers"].get("answers", [])
                ]
            else:
                answers[qid] = None

        return {
            "response_id": r.get("responseId", ""),
            "form_id": r.get("formId", ""),
            "create_time": r.get("createTime", ""),
            "last_submitted_time": r.get("lastSubmittedTime", ""),
            "respondent_email": r.get("respondentEmail", ""),
            "answers": answers,
        }
