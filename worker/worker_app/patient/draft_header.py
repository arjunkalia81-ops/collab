"""Header of a draft note: service codes and add-on units, saved without signing the note."""

import json
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode

from worker_app.patient.notes import find_note
from worker_app.tn.client import TherapyNotesError, ValidationFailed
from worker_app.tn.version import check_version, verify, version

VALIDATE = "/app/notes/edit/validateheader.aspx"
HEADER_FIELD = "ctl00$BodyContent$ctl00$GenericNoteHeaderFormControl$HiddenFieldHeaderData"
SAVE_BUTTON = "ctl00$BodyContent$ctl00$NoteFooterControl$ButtonSaveChanges"
SAVE_LABEL = "Save Note Changes as Draft"
ADD_ON = 2


class FormFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


def edit_path(doc: dict) -> str:
    return f"/app/notes/edit/{doc['NoteType']}/{doc['TamperDocumentKey']}/{doc['TamperRevisionId']}/"


def form_fields(page: str) -> list[tuple[str, str]]:
    parser = FormFields()
    parser.feed(page)
    rows = []
    for item in parser.inputs:
        name = item.get("name")
        kind = item.get("type", "text")
        if not name or "disabled" in item:
            continue
        if kind in {"submit", "button", "image", "file"}:
            continue
        if kind in {"checkbox", "radio"} and "checked" not in item:
            continue
        rows.append((name, item.get("value", "")))
    return rows


def read_header(rows: list[tuple[str, str]]) -> dict:
    raw = next((value for name, value in rows if name == HEADER_FIELD), None)
    if not raw:
        raise TherapyNotesError("TherapyNotes did not return the note header")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise TherapyNotesError("TherapyNotes returned a note header that could not be read") from exc


def date_texts(header: dict) -> tuple[str, str]:
    raw = header.get("NoteDate") or header.get("AppointmentDateTime")
    if not raw:
        raise ValidationFailed(["This note header has no date"])
    when = datetime.fromisoformat(str(raw)[:19])
    hour = when.hour % 12 or 12
    suffix = "AM" if when.hour < 12 else "PM"
    return f"{when.month}/{when.day}/{when.year}", f"{hour}:{when.minute:02d} {suffix}"


def code_row(item: dict, descriptions: dict) -> dict:
    code = str(item.get("Description") or "").lstrip("+")
    return {
        "id": item["ID"],
        "code": code,
        "description": descriptions.get(code),
        "units": item.get("UnitsQuantity") or 1,
        "multiple_units": bool(item.get("AllowDuplicates")),
    }


def descriptions_for(doc: dict) -> dict:
    codes = [str(code).lstrip("+") for code in doc.get("ServiceCodes") or []]
    text = doc.get("ServiceCodeDescriptions") or []
    return {code: text[index] for index, code in enumerate(codes) if index < len(text)}


def header_view(doc: dict, header: dict) -> dict:
    codes = header.get("ServiceCodes") or []
    named = descriptions_for(doc)
    main = next((code_row(item, named) for item in codes if item.get("CodeType") != ADD_ON), None)
    add_ons = [code_row(item, named) for item in codes if item.get("CodeType") == ADD_ON]
    compared = {
        "service_code_id": (main or {}).get("id"),
        "add_on_codes": sorted((item["id"], item["units"]) for item in add_ons),
    }
    return {
        "note_id": doc["PatientDocumentKey"],
        "patient_id": doc["PatientId"],
        "title": header.get("NoteTitle") or doc.get("Title"),
        "status": "Draft",
        "signed_by_author": False,
        "date_of_service": header.get("NoteDate"),
        "service_code": main,
        "add_on_codes": add_ons,
        "version": version(compared),
    }


def require_draft(doc: dict) -> None:
    if doc.get("AuthorHasSignedDocument"):
        raise ValidationFailed(["Only a draft note header can be opened or updated here"])


def apply_units(header: dict, changes: dict) -> None:
    codes = {item["ID"]: item for item in header.get("ServiceCodes") or []}
    seen = []
    errors = []
    for change in changes.get("add_on_codes") or []:
        item = codes.get(change["id"])
        seen.append(change["id"])
        if not item or item.get("CodeType") != ADD_ON:
            errors.append(f"Add-on code {change['id']} is not on this note")
            continue
        units = change["units"]
        if units != 1 and not item.get("AllowDuplicates"):
            errors.append(f"Add-on code {item.get('Description')} allows only 1 unit")
            continue
        item["UnitsQuantity"] = units
    if len(seen) != len(set(seen)):
        errors.append("Each add-on code can only be sent once")
    if errors:
        raise ValidationFailed(errors)


def compared_units(header: dict) -> dict:
    codes = header.get("ServiceCodes") or []
    return {
        "add_on_codes": sorted(
            (item["ID"], item.get("UnitsQuantity") or 1) for item in codes if item.get("CodeType") == ADD_ON
        ),
    }


async def load_page(session, doc: dict) -> tuple[str, list[tuple[str, str]], dict]:
    path = edit_path(doc)
    page = await session.request("GET", path)
    if page.status_code != 200:
        raise TherapyNotesError(f"TherapyNotes returned {page.status_code} for the note header")
    rows = form_fields(page.text)
    return path, rows, read_header(rows)


async def validate_header(session, path: str, header: dict) -> dict:
    date_text, time_text = date_texts(header)
    payload = {**header, "NoteDateText": date_text, "NoteTimeText": time_text}
    response = await session.send(VALIDATE, {"data": json.dumps(payload), "noteId": header.get("NoteId")}, path)
    try:
        body = response.json()
    except ValueError as exc:
        raise TherapyNotesError("TherapyNotes did not validate the note header") from exc
    errors = [item for item in body.get("Errors") or [] if item]
    if errors or not body.get("Data"):
        raise ValidationFailed(errors or ["TherapyNotes did not accept the note header"])
    return body["Data"]


async def save_page(session, path: str, rows: list[tuple[str, str]], header: dict) -> None:
    posted = [(name, json.dumps(header) if name == HEADER_FIELD else value) for name, value in rows]
    posted.append((SAVE_BUTTON, SAVE_LABEL))
    response = await session.request(
        "POST",
        path,
        content=urlencode(posted),
        headers={
            "Origin": session.base,
            "Referer": f"{session.base}{path}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if response.status_code not in {302, 303}:
        raise ValidationFailed(["TherapyNotes did not save the note header"])


async def get_draft_header(session, patient_id: int, note_id: int) -> dict | None:
    doc = await find_note(session, patient_id, note_id)
    if not doc:
        return None
    require_draft(doc)
    _, _, header = await load_page(session, doc)
    return header_view(doc, header)


async def update_draft_header(session, patient_id: int, note_id: int, changes: dict, expected: str) -> dict | None:
    """Change add-on units on a draft note header and save it as a draft."""
    doc = await find_note(session, patient_id, note_id)
    if not doc:
        return None
    require_draft(doc)
    path, rows, header = await load_page(session, doc)
    current = header_view(doc, header)
    check_version(expected, current, f"Note {note_id}")
    apply_units(header, changes)
    accepted = await validate_header(session, path, header)
    accepted_units = compared_units(accepted)
    if accepted_units != compared_units(header):
        raise ValidationFailed(["TherapyNotes did not accept the units on this note"])
    await save_page(session, path, rows, accepted)

    saved_doc = await find_note(session, patient_id, note_id)
    if not saved_doc:
        return None
    require_draft(saved_doc)
    _, _, saved_header = await load_page(session, saved_doc)
    saved = header_view(saved_doc, saved_header)
    verify(f"Note {note_id}", compared_units(header), compared_units(saved_header), saved)
    return {"message": "Note header saved as draft and verified", **saved}
