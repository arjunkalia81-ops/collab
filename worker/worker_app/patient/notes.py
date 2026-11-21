"""Notes on the Documents tab: service codes and diagnoses, editable on notes the author has signed."""

import json
import re
from datetime import datetime

from worker_app.tn.client import TherapyNotesError, ValidationFailed
from worker_app.tn.version import check_version, verify, version

DOCUMENTS = "/app/patients/edit/getpatientdocuments.aspx?msg=1"
PATIENT = "/app/patients/getpatient.aspx?msg=1"
GET_NOTE = "/app/notes/api/getnote.aspx?msg=1"
SAVE_NOTE = "/app/notes/api/savenote.aspx?msg=1"
CODE_SEARCH = "/app/settings/diagnosiscodes/codesearch.aspx?msg=1"

NOTE_STATUSES = {1: "Draft", 2: "Signed"}
MAIN_CODE = 1
ADD_ON_CODE = 2
DIAGNOSIS_ELEMENT = 1003
SIGNATURE_ELEMENT = 53
DSM_VERSION = 5


async def note_documents(session, patient_id: int) -> list[dict]:
    items = await session.post(DOCUMENTS, {"request": json.dumps({"Id": patient_id})}, "/app/patients/")
    return [d for d in items or [] if d.get("NoteType")]


def summary(doc: dict) -> dict:
    signed = bool(doc.get("AuthorHasSignedDocument"))
    return {
        "note_id": doc["PatientDocumentKey"],
        "title": doc.get("Title"),
        "status": NOTE_STATUSES.get(doc.get("NoteStatus"), doc.get("NoteStatus")),
        "signed_by_author": signed,
        "editable": signed and doc.get("IsMostRecentNoteRevision", True),
        "author": f"{doc.get('UserFirstName', '')} {doc.get('UserLastName', '')}".strip(),
        "service_codes": doc.get("ServiceCodes") or [],
    }


async def list_notes(session, patient_id: int) -> dict:
    return {"patient_id": patient_id, "notes": [summary(d) for d in await note_documents(session, patient_id)]}


def check_signed(doc: dict) -> None:
    # Draft notes of some types (e.g. Psychological Evaluation) make getnote fail with a 500.
    if not doc.get("AuthorHasSignedDocument"):
        raise ValidationFailed(["Only notes signed by the author can be opened or edited"])


async def find_note(session, patient_id: int, note_id: int) -> dict | None:
    docs = await note_documents(session, patient_id)
    return next((d for d in docs if d["PatientDocumentKey"] == note_id), None)


async def load_note(session, doc: dict, editing: bool) -> dict:
    # Note pages address the patient by its tamper id, which only getpatient returns.
    patient = await session.post(PATIENT, {"patientid": doc["PatientId"]}, "/app/patients/")
    if editing:
        note_id, revision = doc["TamperDocumentKey"], doc["TamperRevisionId"]
        referer = f"/app/notes/edit/{doc['NoteType']}/{note_id}/{revision}/"
    else:
        note_id, revision = doc["EncryptedId"], doc["EncryptedRevisionId"]
        referer = f"/app/notes/view/{doc['NoteType']}/{note_id}/{revision}/"
    values = {
        "NoteId": note_id, "NoteRevision": revision, "CalendarEntryId": None,
        "PatientId": patient.get("TamperId"), "CustomFormId": None,
    }
    note = await session.post(GET_NOTE, {
        "notetype": doc["NoteType"],
        "isediting": int(editing),
        "encryptednotevalues": json.dumps(values),
        "notetemplateid": None,
        "customformid": None,
        "customformversionid": None,
        "wassaved": 0,
        "isgroupnoteworkflow": 0,
    }, referer)
    if not (note.get("Form") or {}).get("Data"):
        raise TherapyNotesError(f"TherapyNotes did not return note {doc['PatientDocumentKey']}")
    note["referer"] = referer
    return note


def element(form: dict, kind: int) -> dict | None:
    return next((e for e in form.get("FormElementValues") or [] if e.get("FormElementType") == kind), None)


def available_codes(form: dict) -> dict:
    return {c["Id"]: c for c in form["SupplementalData"].get("ServiceCodes") or [] if c.get("Status") == 1}


def code_view(item: dict, codes: dict) -> dict:
    return {
        "id": item["Id"],
        "code": item.get("Code"),
        "description": codes.get(item["Id"], {}).get("Description"),
        "units": item.get("Units", 1),
    }


def note_view(doc: dict, note: dict) -> dict:
    form = note["Form"]["Data"]
    header = form.get("FormHeaderData") or {}
    codes = available_codes(form)
    picked = header.get("ServiceCodes") or []
    diagnosis = (element(form, DIAGNOSIS_ELEMENT) or {}).get("Value") or {}
    editable = {
        "service_code": next((code_view(c, codes) for c in picked if not c.get("IsAddOn")), None),
        "add_on_codes": [code_view(c, codes) for c in picked if c.get("IsAddOn")],
        "diagnoses": [
            {"code": d.get("Code"), "description": d.get("Description")}
            for d in diagnosis.get("NoteDiagnoses") or []
        ],
        "diagnosis_justification": diagnosis.get("Explanation"),
    }
    return {
        **summary(doc),
        "date_of_service": header.get("DateAndTime"),
        "duration_minutes": header.get("SessionDuration"),
        **editable,
        "version": version(editable),
        "options": {
            "service_codes": [
                {"id": c["Id"], "code": c["Code"], "description": c.get("Description")}
                for c in codes.values() if c.get("CodeType") == MAIN_CODE
            ],
            "add_on_codes": [
                {"id": c["Id"], "code": c["Code"], "description": c.get("Description"),
                 "multiple_units": bool(c.get("AllowDuplicates"))}
                for c in codes.values() if c.get("CodeType") == ADD_ON_CODE
            ],
        },
    }


async def get_note(session, patient_id: int, note_id: int) -> dict | None:
    doc = await find_note(session, patient_id, note_id)
    if not doc:
        return None
    check_signed(doc)
    return note_view(doc, await load_note(session, doc, editing=False))


async def search_diagnoses(session, search: str, dsm_version: int = DSM_VERSION) -> list[dict]:
    data = await session.post(CODE_SEARCH, {
        "searchquery": search,
        "practiceid": -1,
        "assigneduserid": -1,
        "param": json.dumps({"DsmVersion": dsm_version, "AfterSwitchoverDate": True}),
    }, "/app/notes/")
    return [{"code": m["Name"], "description": m.get("Description")} for m in data.get("Matches") or []]


async def find_diagnoses(session, search: str) -> dict:
    results = await search_diagnoses(session, search)
    return {"search": search, "results": results}


async def resolve_diagnoses(session, wanted: list[dict], dsm_version: int) -> list[dict]:
    picked, errors = [], []
    for item in wanted:
        code = item["code"].strip().upper()
        matches = [m for m in await search_diagnoses(session, code, dsm_version) if m["code"].upper() == code]
        if item.get("description"):
            matches = [m for m in matches if m["description"] == item["description"]]
        if not matches:
            errors.append(f"Diagnosis {item['code']} {item.get('description') or ''}".strip() + " not found")
            continue
        picked.append({"Code": matches[0]["code"], "Description": matches[0]["description"], "Axis": None})
    if errors:
        raise ValidationFailed(errors)
    return picked


def pick_service_codes(header: dict, codes: dict, changes: dict) -> list[dict]:
    current = header.get("ServiceCodes") or []
    units = {c["Id"]: c.get("Units", 1) for c in current}
    main = changes.get("service_code_id", next((c["Id"] for c in current if not c.get("IsAddOn")), None))
    add_ons = changes.get("add_on_codes")
    if add_ons is None:
        add_ons = [{"id": c["Id"], "units": c.get("Units", 1)} for c in current if c.get("IsAddOn")]

    errors = []
    if codes.get(main, {}).get("CodeType") != MAIN_CODE:
        errors.append(f"Service code {main} is not available on this note")
    ids = [a["id"] for a in add_ons]
    for add_on in add_ons:
        code = codes.get(add_on["id"], {})
        wanted = add_on.get("units") or units.get(add_on["id"], 1)
        if code.get("CodeType") != ADD_ON_CODE:
            errors.append(f"Add-on code {add_on['id']} is not available on this note")
        elif ids.count(add_on["id"]) > 1:
            errors.append(f"Add-on code {code['Code']} can only be added once")
        elif wanted != 1 and not code.get("AllowDuplicates"):
            errors.append(f"Add-on code {code['Code']} allows only 1 unit")
        add_on["units"] = wanted
    if errors:
        raise ValidationFailed(sorted(set(errors), key=errors.index))

    return [
        {"Id": main, "Code": codes[main]["Code"], "Units": units.get(main, 1), "IsAddOn": False},
        *({"Id": a["id"], "Code": codes[a["id"]]["Code"], "Units": a["units"], "IsAddOn": True} for a in add_ons),
    ]


def sign(form: dict) -> None:
    meta = form["FormMetadata"]
    context = meta.get("PracticeSignatureContext") or {}
    signature = element(form, SIGNATURE_ELEMENT)
    if not signature or not context.get("CurrentUserCanSign"):
        raise ValidationFailed(["You are not allowed to sign this note in TherapyNotes"])
    signature["Value"] = [s for s in signature.get("Value") or [] if not s.get("IsNew")] + [{
        "IsNew": True,
        "SignerHasLicenses": bool(form["SupplementalData"].get("UserLicenses")),
        "DateSigned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "SignerId": meta["UserId"],
        "SignerSignatureLicenseInfo": context.get("DefaultLicenseInfo"),
        "SignatureType": context.get("PendingPracticeSignatureType", 1),
    }]


def drop_offset(value):
    return re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", value) if isinstance(value, str) else value


async def update_note(session, patient_id: int, note_id: int, changes: dict, expected: str) -> dict | None:
    """Edit service codes and/or diagnoses on a signed note still at version `expected`;
    TN saves it as a new signed revision."""
    doc = await find_note(session, patient_id, note_id)
    if not doc:
        return None
    check_signed(doc)

    note = await load_note(session, doc, editing=True)
    check_version(expected, note_view(doc, note), f"Note {note_id}")
    form = note["Form"]["Data"]
    header = form["FormHeaderData"]

    if "service_code_id" in changes or "add_on_codes" in changes:
        if "ServiceCodes" not in header:
            raise ValidationFailed(["This note has no service codes"])
        header["ServiceCodes"] = pick_service_codes(header, available_codes(form), changes)

    if "diagnoses" in changes or "diagnosis_justification" in changes:
        diagnosis = element(form, DIAGNOSIS_ELEMENT)
        if not diagnosis:
            raise ValidationFailed(["This note has no diagnosis section"])
        value = diagnosis["Value"] = diagnosis.get("Value") or {"DsmVersion": DSM_VERSION, "NoteDiagnoses": []}
        if "diagnoses" in changes:
            value["NoteDiagnoses"] = await resolve_diagnoses(
                session, changes["diagnoses"], value.get("DsmVersion") or DSM_VERSION,
            )
        if "diagnosis_justification" in changes:
            value["Explanation"] = changes["diagnosis_justification"] or ""

    sign(form)
    # The TN page posts these dates without their UTC offset.
    header["AppointmentDate"] = drop_offset(header.get("AppointmentDate"))
    patient = form["SupplementalData"].get("PatientInfo") or {}
    patient["DateOfBirth"] = drop_offset(patient.get("DateOfBirth"))

    info = note.get("NoteInfo") or {}
    request = {
        "FormData": form,
        "PermissionsModifier": info.get("PermissionsModifier", 1),
        "IsBillable": (info.get("NoteBillingInfo") or {}).get("IsBillable", False),
        "NoteAiData": note.get("NoteAiData"),
    }
    result = await session.post(SAVE_NOTE, {"savenoterequest": json.dumps(request)}, note["referer"])
    validation = result.get("FormValidationResult") or {}
    if result.get("Result") != 1 or validation.get("IsValid") is False:
        messages = [m.get("Message") or m.get("Text") or str(m) if isinstance(m, dict) else str(m)
                    for m in validation.get("Messages") or []]
        raise ValidationFailed(messages or ["TherapyNotes did not save the note"])

    saved = await get_note(session, patient_id, note_id)
    if not saved:
        return None
    verify(f"Note {note_id}", wanted_values(header, form, changes), note_values(saved), saved)
    return {"message": "Note saved, signed and verified", **saved}


def note_values(view: dict) -> dict:
    return {
        "service_code_id": (view["service_code"] or {}).get("id"),
        "add_on_codes": sorted((c["id"], c["units"]) for c in view["add_on_codes"]),
        "diagnoses": [d["code"] for d in view["diagnoses"]],
        "diagnosis_justification": view["diagnosis_justification"],
    }


def wanted_values(header: dict, form: dict, changes: dict) -> dict:
    wanted = {}
    if "service_code_id" in changes or "add_on_codes" in changes:
        picked = header["ServiceCodes"]
        wanted["service_code_id"] = next(c["Id"] for c in picked if not c["IsAddOn"])
        wanted["add_on_codes"] = sorted((c["Id"], c["Units"]) for c in picked if c["IsAddOn"])
    diagnosis = (element(form, DIAGNOSIS_ELEMENT) or {}).get("Value") or {}
    if "diagnoses" in changes:
        wanted["diagnoses"] = [d["Code"] for d in diagnosis["NoteDiagnoses"]]
    if "diagnosis_justification" in changes:
        wanted["diagnosis_justification"] = diagnosis["Explanation"]
    return wanted
