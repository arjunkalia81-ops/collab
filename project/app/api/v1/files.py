"""Files on the patient's Documents tab: list, upload, download and delete."""

import base64
import datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field

from app.api.deps import require_session_id
from app.worker_client import worker_delete_file, worker_download_file, worker_files, worker_upload_file

router = APIRouter(tags=["Documents"])

PatientId = Path(gt=0, description="TherapyNotes patient ID (`patient_id` in the patient list)")
FileId = Path(gt=0, description="`file_id` from GET /patients/{patient_id}/files")


class Upload(BaseModel):
    file_name: str = Field(
        min_length=3, max_length=200, pattern=r"\.\w{1,10}$", description="Original file name with extension, e.g. card.pdf",
    )
    mime_type: str = Field(min_length=3, max_length=100, description="e.g. application/pdf, image/png")
    content_base64: str = Field(
        min_length=4, max_length=14_000_000, description="File content as base64 (no `data:` prefix), up to 10 MB",
    )
    name: str = Field(min_length=1, max_length=200, description="Document name shown in TherapyNotes")
    date: datetime.date = Field(description="Document date, YYYY-MM-DD")
    access: Literal["clinical", "administrative", "billing"] = Field(
        description="Who can see it in TherapyNotes: clinical, administrative or billing staff",
    )


@router.get(
    "/patients/{patient_id}/files",
    summary="List files",
    description="Files uploaded to the patient's Documents tab (notes are not included).",
)
async def patient_files(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_files(session_id, patient_id)


@router.post(
    "/patients/{patient_id}/files",
    summary="Upload a file",
    description=(
        "Adds a file to the patient's Documents tab. After the upload TherapyNotes is read again; "
        "if the saved file does not match, you get a 502."
    ),
)
async def upload_file(body: Upload, patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_upload_file(session_id, patient_id, **body.model_dump(mode="json"))


@router.get(
    "/patients/{patient_id}/files/{file_id}/download",
    summary="Download a file",
    description="Returns the file itself with its content type and file name.",
    response_class=Response,
)
async def download_file(
    patient_id: int = PatientId, file_id: int = FileId, session_id: str = Depends(require_session_id),
):
    data = await worker_download_file(session_id, patient_id, file_id)
    return Response(
        content=base64.b64decode(data["content_base64"]),
        media_type=data.get("mime_type") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(data['file_name'] or 'file')}"},
    )


@router.delete(
    "/patients/{patient_id}/files/{file_id}",
    summary="Delete a file",
    description="Deletes the file from TherapyNotes and checks it is gone. Returns the remaining files.",
)
async def delete_file(patient_id: int = PatientId, file_id: int = FileId, session_id: str = Depends(require_session_id)):
    return await worker_delete_file(session_id, patient_id, file_id)
