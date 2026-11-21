"""Files uploaded to a patient's Documents tab: list, upload, download and delete."""

import base64
import json
import uuid

import httpx

from worker_app.patient.detail import day
from worker_app.tn.client import SessionExpired, TherapyNotesError, ValidationFailed
from worker_app.tn.version import verify

DOCUMENTS = "/app/patients/edit/getpatientdocuments.aspx?msg=1"
SEGMENT = "/app/common/files/FileSegmentReceiver.ashx"
SAVE = "/app/patients/files/savepatientfile.aspx?msg=1"
DELETE = "/app/patients/files/deletepatientfile.aspx?msg=1"
REFERER = "/app/patients/"
SEGMENT_SIZE = 102400
MAX_BYTES = 10 * 1024 * 1024

ACCESS = {"clinical": 0, "administrative": 1, "billing": 2}


def file_view(doc: dict) -> dict:
    access = doc.get("DocumentAccessType")
    return {
        "file_id": doc["PatientDocumentKey"],
        "name": doc.get("Title"),
        "date": day(doc.get("DocumentDate")),
        "file_name": doc.get("OriginalFileName"),
        "mime_type": doc.get("MimeType"),
        "size": doc.get("FileSize"),
        "access": next((k for k, v in ACCESS.items() if v == access), access),
    }


async def patient_files(session, patient_id: int) -> list[dict]:
    items = await session.post(DOCUMENTS, {"request": json.dumps({"Id": patient_id})}, REFERER)
    return [d for d in items or [] if not d.get("NoteType") and d.get("OriginalFileName")]


async def list_files(session, patient_id: int) -> dict:
    return {"patient_id": patient_id, "files": [file_view(d) for d in await patient_files(session, patient_id)]}


async def send_segments(session, guid: str, file_name: str, mime_type: str, content: bytes) -> None:
    chunks = [content[i:i + SEGMENT_SIZE] for i in range(0, len(content), SEGMENT_SIZE)] or [b""]
    for index, chunk in enumerate(chunks):
        response = await session.request("POST", SEGMENT, content=base64.b64encode(chunk), headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": session.base,
            "Referer": f"{session.base}{REFERER}",
            "X-Upload-Guid": guid,
            "X-Upload-File-Name": file_name,
            "X-Upload-File-Size": str(len(content)),
            "X-Upload-File-Mime-Type": mime_type,
            "X-Upload-Segment": str(index),
            "X-Upload-Segment-Size": str(SEGMENT_SIZE),
            "X-Upload-Segment-Is-Final": "true" if index == len(chunks) - 1 else "false",
        })
        if response.status_code == 401 or "/login" in str(response.url).lower():
            raise SessionExpired("TherapyNotes session expired during the upload")
        if response.status_code != 200:
            raise TherapyNotesError(f"TherapyNotes returned {response.status_code} for a file segment")


async def store(session, patient_id: int, file_name: str, mime_type: str, content: bytes,
                name: str, date: str, access: str) -> dict:
    guid = str(uuid.uuid4())
    await send_segments(session, guid, file_name, mime_type, content)
    metadata = {
        "PatientID": patient_id,
        "UploadGUID": guid,
        "UploadFileName": file_name,
        "UploadFileMimeType": mime_type,
        "Type": ACCESS[access],
        "Name": name,
        "DocumentDate": f"{date}T00:00:00",
        "PortalGenerated": False,
    }
    # Leave the HIPAA, PCP and signature-on-file settings of the patient alone.
    extra = {
        "SetHipaaAgreement": False, "HipaaAgreementSetting": True,
        "SetPcpConsent": False, "PcpConsentSetting": -1,
        "SetSignatureOnFile": False, "SignatureOnFileSetting": False,
    }
    response = await session.send(
        SAVE, {"filemetadata": json.dumps(metadata), "additionalfileinfo": json.dumps(extra)}, REFERER,
    )
    return response.json()


async def upload_file(session, patient_id: int, file_name: str, mime_type: str, content_base64: str,
                      name: str, date: str, access: str) -> dict:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise ValidationFailed(["content_base64 is not valid base64"]) from exc
    if not content:
        raise ValidationFailed(["The file is empty"])
    if len(content) > MAX_BYTES:
        raise ValidationFailed([f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB"])

    args = (session, patient_id, file_name, mime_type, content, name, date, access)
    try:
        result = await store(*args)
    except SessionExpired:
        # Segments sent under the old session are gone, so upload everything again.
        await session.relogin()
        result = await store(*args)

    if not result.get("IsSuccessful"):
        advice = ((result.get("FormAdvice") or {}).get("Data")) or []
        raise ValidationFailed([a.get("Message") for a in advice if a.get("Message")] or
                               ["TherapyNotes did not save the file"])

    files = await list_files(session, patient_id)
    saved = next((f for f in files["files"] if f["file_id"] == result.get("FileID")), None)
    if not saved:
        raise TherapyNotesError(
            "TherapyNotes accepted the file, but it is not in the patient's documents. Check it in TherapyNotes",
        )
    verify(f"File {saved['file_id']}", {"name": name, "date": date, "access": access, "size": len(content)},
           saved, files)
    return {"message": "File uploaded and verified", **saved}


async def fetch(session, doc: dict) -> httpx.Response:
    response = await session.request("GET", f"/app/patients/files/download/{doc['EncryptedId']}/", headers={
        "Referer": f"{session.base}{REFERER}",
    })
    if response.status_code in (301, 302, 401):
        raise SessionExpired("TherapyNotes did not return the file")
    if response.status_code != 200:
        raise TherapyNotesError(f"TherapyNotes returned {response.status_code} for the file download")
    return response


async def download_file(session, patient_id: int, file_id: int) -> dict | None:
    doc = next((d for d in await patient_files(session, patient_id) if d["PatientDocumentKey"] == file_id), None)
    if not doc:
        return None
    try:
        response = await fetch(session, doc)
    except SessionExpired:
        await session.relogin()
        response = await fetch(session, doc)
    return {**file_view(doc), "content_base64": base64.b64encode(response.content).decode()}


async def delete_file(session, patient_id: int, file_id: int) -> dict | None:
    if not any(d["PatientDocumentKey"] == file_id for d in await patient_files(session, patient_id)):
        return None
    await session.post(DELETE, {"fileid": file_id, "patientid": patient_id}, REFERER)
    files = await list_files(session, patient_id)
    if any(f["file_id"] == file_id for f in files["files"]):
        raise TherapyNotesError(f"File {file_id} is still in TherapyNotes after the delete. Check it in TherapyNotes")
    return {"message": "File deleted and verified", **files}
