"""Patient notes (Documents tab): service codes (CPT) and diagnoses, editable on notes signed by the author."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.api.deps import require_session_id
from app.worker_client import (
    worker_diagnosis_codes, worker_draft_header, worker_note, worker_notes, worker_save_draft_header, worker_save_note,
)

router = APIRouter(tags=["Notes"])

PatientId = Path(gt=0, description="TherapyNotes patient ID (`patient_id` in the patient list)")
NoteId = Path(gt=0, description="`note_id` from GET /patients/{patient_id}/notes")


class Diagnosis(BaseModel):
    code: str = Field(min_length=3, max_length=10, description="ICD-10 code, e.g. F41.1")
    description: str | None = Field(
        None, max_length=300, description="Only needed when a code has several descriptions; defaults to the first",
    )


class AddOnCode(BaseModel):
    id: int = Field(gt=0, description="`options.add_on_codes[].id`")
    units: int | None = Field(
        None, ge=1, le=999, description="Only above 1 when `options.add_on_codes[].multiple_units` is true",
    )


class HeaderAddOn(BaseModel):
    id: int = Field(gt=0, description="`add_on_codes[].id` from the draft header")
    units: int = Field(ge=1, le=999, description="Above 1 only when `multiple_units` is true")


class HeaderChanges(BaseModel):
    add_on_codes: list[HeaderAddOn] = Field(min_length=1, max_length=10)
    version: str = Field(description="`version` from the draft header; 409 if it changed in TherapyNotes since")


class NoteChanges(BaseModel):
    """Only the fields you send are changed. Lists replace what is on the note."""

    service_code_id: int | None = Field(None, gt=0, description="`options.service_codes[].id`")
    add_on_codes: list[AddOnCode] | None = Field(None, max_length=10, description="Send [] to remove all add-ons")
    diagnoses: list[Diagnosis] | None = Field(None, min_length=1, max_length=12, description="Primary first")
    diagnosis_justification: str | None = Field(None, max_length=5000)
    version: str = Field(description="`version` of the note as loaded; 409 if it changed in TherapyNotes since")


@router.get(
    "/patients/{patient_id}/notes",
    summary="List notes",
    description=(
        "Notes on the patient's Documents tab. Only notes with `editable: true` "
        "(signed by the author) can be opened and changed."
    ),
)
async def patient_notes(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_notes(session_id, patient_id)


@router.get(
    "/patients/{patient_id}/notes/{note_id}",
    summary="Get note codes",
    description=(
        "Service code (CPT), add-on codes with units, diagnoses and justification of a signed note, "
        "plus `options` for the code dropdowns and the note's `version`."
    ),
)
async def patient_note(
    patient_id: int = PatientId, note_id: int = NoteId, session_id: str = Depends(require_session_id),
):
    return await worker_note(session_id, patient_id, note_id)


@router.get(
    "/patients/{patient_id}/notes/{note_id}/header",
    summary="Get draft note header",
    description=(
        "Service code and add-on units on a draft note, from the note header "
        "(About this Note). A signed note returns 422; use the signed note GET for that."
    ),
)
async def draft_note_header(
    patient_id: int = PatientId, note_id: int = NoteId, session_id: str = Depends(require_session_id),
):
    return await worker_draft_header(session_id, patient_id, note_id)


@router.patch(
    "/patients/{patient_id}/notes/{note_id}/header",
    summary="Update draft note units",
    description=(
        "Changes add-on units on a draft note and saves the note as a draft. "
        "The rest of the note is left as it is, and the note is not signed. "
        "Send `version` from the draft header GET."
    ),
)
async def update_draft_note_header(
    body: HeaderChanges,
    patient_id: int = PatientId,
    note_id: int = NoteId,
    session_id: str = Depends(require_session_id),
):
    changes = {"add_on_codes": [item.model_dump() for item in body.add_on_codes]}
    return await worker_save_draft_header(session_id, patient_id, note_id, changes, body.version)


@router.patch(
    "/patients/{patient_id}/notes/{note_id}",
    summary="Update note codes",
    description=(
        "Changes the service code, add-on codes and units, diagnoses or justification. "
        "TherapyNotes saves a new revision signed by you. Send the note's `version` from the GET; "
        "returns 409 if the note changed since you loaded it."
    ),
)
async def update_note(
    body: NoteChanges,
    patient_id: int = PatientId,
    note_id: int = NoteId,
    session_id: str = Depends(require_session_id),
):
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"version"})
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    if "service_code_id" in changes and changes["service_code_id"] is None:
        raise HTTPException(status_code=422, detail="service_code_id can't be removed, only changed")
    return await worker_save_note(session_id, patient_id, note_id, changes, body.version)


@router.get(
    "/diagnosis-codes",
    summary="Search diagnosis codes",
    description="ICD-10 search for the diagnosis field, by code or words. Use a result's `code` and `description` when saving a note.",
)
async def diagnosis_codes(
    search: str = Query(min_length=2, max_length=60, description="ICD-10 code or words, e.g. F41 or anxiety"),
    session_id: str = Depends(require_session_id),
):
    return await worker_diagnosis_codes(session_id, search.strip())
