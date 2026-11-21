"""TherapyNotes gateway — HTTP sessions per user; Chrome only to refresh the Cloudflare clearance."""

import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from worker_app.claim.appointment import get_appointment_billing, update_modifiers
from worker_app.claim.claims import get_claim, get_claim_filters, get_claims, get_pending_claims
from worker_app.claim.eligibility import get_eligibility
from worker_app.claim.queues import get_claim_summary, get_queue
from worker_app.claim.rejections import get_rejections, mark_reviewed, save_comment
from worker_app.core.config import settings
from worker_app.patient.detail import get_patient_billing, get_patient_info
from worker_app.patient.files import delete_file, download_file, list_files, upload_file
from worker_app.patient.insurance import get_patient_insurance, save_policy
from worker_app.patient.draft_header import get_draft_header, update_draft_header
from worker_app.patient.notes import find_diagnoses, get_note, list_notes, update_note
from worker_app.patient.patient import get_patient_list
from worker_app.tn.clearance import CloudflareBlocked, clearance
from worker_app.tn.client import LoginFailed, SessionExpired, TherapyNotesError, TnSession, ValidationFailed
from worker_app.tn.sessions import SessionLimit, registry
from worker_app.tn.version import Conflict, NotVerified

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WORKER] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

WORKER_SECRET = settings.worker_secret.strip()


class CloseBody(BaseModel):
    session_id: str | None = None


class LoginBody(BaseModel):
    practice_code: str
    username: str
    password: str


class PatientsBody(BaseModel):
    session_id: str
    page_num: int = 1
    status: str = "active"
    search: str = ""
    assignment: str = "any"
    sort: str = "name"
    ascending: bool = True


class SessionBody(BaseModel):
    session_id: str


class PatientDetailBody(BaseModel):
    session_id: str
    patient_id: int
    section: Literal["info", "insurance", "billing"]


class InsuranceBody(BaseModel):
    session_id: str
    patient_id: int
    policy_id: int | None = None
    changes: dict
    version: str | None = None


class PatientBody(BaseModel):
    session_id: str
    patient_id: int


class NoteBody(PatientBody):
    note_id: int
    changes: dict | None = None
    version: str | None = None


class FileBody(PatientBody):
    file_id: int


class UploadBody(PatientBody):
    file_name: str
    mime_type: str
    content_base64: str
    name: str
    date: date
    access: Literal["clinical", "administrative", "billing"]


class AppointmentBody(BaseModel):
    session_id: str
    calendar_entry_id: int


class ModifiersBody(AppointmentBody):
    changes: list[dict]
    version: str
    override_warnings: bool = False


class DiagnosisSearchBody(BaseModel):
    session_id: str
    search: str


class ClaimsBody(BaseModel):
    session_id: str
    page_num: int = 1
    page_size: int = 25
    patient_id: int | None = None
    status: int | None = None
    payer_id: int | None = None
    clinician_id: int | None = None
    sequence: int | None = None
    start_date: date | None = None
    end_date: date | None = None


class PendingClaimsBody(BaseModel):
    session_id: str
    items: Literal["newresubmit", "new", "resubmit", "all", "information"] = "newresubmit"
    patient_id: int | None = None
    payer_id: int | None = None
    clinician_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None


class QueueBody(BaseModel):
    session_id: str
    queue: Literal["secondary", "paper", "external", "superbill", "ledger"]
    method: Literal["external", "electronic", "paper"] = "external"
    patient_id: int | None = None
    clinician_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    page_num: int = 1
    page_size: int = 100


class EligibilityBody(BaseModel):
    session_id: str
    patient_id: int | None = None
    status: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    page_num: int = 1
    page_size: int = 50


class ClaimBody(BaseModel):
    session_id: str
    claim_id: int


class CommentBody(ClaimBody):
    comment: str
    version: str


class RejectionsBody(BaseModel):
    session_id: str
    reviewed: bool = False
    page_num: int = 1
    page_size: int = 25
    patient_id: int | None = None
    payer_id: int | None = None
    clinician_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None


PATIENT_SECTIONS = {
    "info": get_patient_info,
    "insurance": get_patient_insurance,
    "billing": get_patient_billing,
}


def require_worker_key(x_worker_key: str | None = Header(default=None)) -> None:
    if not WORKER_SECRET:
        raise HTTPException(status_code=500, detail="WORKER_SECRET is not set on the worker")
    if not x_worker_key or x_worker_key != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Worker-Key")


def get_session(session_id: str) -> TnSession:
    session = registry.get(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="No active session; login first")
    session.touch()
    return session


async def call_tn(fn, session_id: str, **kwargs):
    session = get_session(session_id)
    try:
        return await fn(session, **kwargs)
    except SessionExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except LoginFailed as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValidationFailed as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.messages}) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "current": exc.current}) from exc
    except NotVerified as exc:
        raise HTTPException(
            status_code=502, detail={"message": str(exc), "fields": exc.fields, "current": exc.current},
        ) from exc
    except CloudflareBlocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TherapyNotesError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"TherapyNotes unreachable: {exc}") from exc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    clearance.load()
    await registry.start()
    log.info(
        "Worker ready. Clearance: %s. Ping every %ss, idle limit %s min, max %s sessions",
        "loaded" if clearance.ok else "none (Chrome opens only if Cloudflare blocks a call)",
        settings.tn_session_ping_seconds,
        settings.tn_idle_minutes,
        settings.tn_max_sessions,
    )
    if not WORKER_SECRET:
        log.warning("WORKER_SECRET empty; internal routes will reject calls")
    if settings.keep_browser_open:
        try:
            await clearance.show()
            log.info("Chrome kept open on the login page (KEEP_BROWSER_OPEN=true)")
        except Exception as exc:
            log.warning("Could not open Chrome: %s %s", type(exc).__name__, exc)
    yield
    await registry.stop()
    await clearance.close()


app = FastAPI(
    title="TN Gateway Worker",
    version="0.2.0",
    description="Public surface: health + close only. Login/data routes are internal (API only).",
    lifespan=lifespan,
)


@app.get("/worker/health", tags=["public"])
async def health():
    now = time.time()
    return {
        "status": "ok",
        "clearance_age_minutes": round((now - clearance.created_at) / 60) if clearance.ok else None,
        "sessions": {
            sid: {"username": s.username, "practice": s.practice_code, "idle_minutes": round((now - s.last_used) / 60)}
            for sid, s in registry.sessions.items()
        },
    }


@app.post("/worker/close", tags=["public"])
async def worker_close(body: CloseBody | None = None, _: None = Depends(require_worker_key)):
    session_id = body.session_id if body else None
    if session_id:
        await registry.drop(session_id)
    else:
        await registry.drop_all()
    return {"status": "closed"}


@app.post("/internal/login", include_in_schema=False)
async def internal_login(body: LoginBody, _: None = Depends(require_worker_key)):
    try:
        session = await registry.login(body.practice_code.strip(), body.username.strip(), body.password)
    except LoginFailed as exc:
        log.warning("Login rejected for %s (%s): %s", body.username, exc.code, exc)
        raise HTTPException(status_code=401, detail={"code": exc.code, "message": str(exc)}) from exc
    except SessionLimit as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except CloudflareBlocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TherapyNotesError, SessionExpired) as exc:
        log.error("Login failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"TherapyNotes unreachable: {exc}") from exc

    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "practice_name": session.practice_name,
    }


@app.post("/internal/patients", include_in_schema=False)
async def internal_patients(body: PatientsBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_patient_list, **body.model_dump())


@app.post("/internal/patient-detail", include_in_schema=False)
async def internal_patient_detail(body: PatientDetailBody, _: None = Depends(require_worker_key)):
    data = await call_tn(PATIENT_SECTIONS[body.section], body.session_id, patient_id=body.patient_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Patient {body.patient_id} not found")
    return data


@app.post("/internal/insurance/save", include_in_schema=False)
async def internal_insurance_save(body: InsuranceBody, _: None = Depends(require_worker_key)):
    data = await call_tn(
        save_policy, body.session_id, patient_id=body.patient_id, changes=body.changes, policy_id=body.policy_id,
        expected=body.version,
    )
    if data is None:
        target = f"Policy {body.policy_id} for patient" if body.policy_id else "Patient"
        raise HTTPException(status_code=404, detail=f"{target} {body.patient_id} not found")
    return data


@app.post("/internal/notes", include_in_schema=False)
async def internal_notes(body: PatientBody, _: None = Depends(require_worker_key)):
    return await call_tn(list_notes, body.session_id, patient_id=body.patient_id)


@app.post("/internal/note", include_in_schema=False)
async def internal_note(body: NoteBody, _: None = Depends(require_worker_key)):
    data = await call_tn(get_note, body.session_id, patient_id=body.patient_id, note_id=body.note_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Note {body.note_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/note/header", include_in_schema=False)
async def internal_note_header(body: NoteBody, _: None = Depends(require_worker_key)):
    data = await call_tn(get_draft_header, body.session_id, patient_id=body.patient_id, note_id=body.note_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Note {body.note_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/note/header/save", include_in_schema=False)
async def internal_note_header_save(body: NoteBody, _: None = Depends(require_worker_key)):
    data = await call_tn(
        update_draft_header, body.session_id, patient_id=body.patient_id, note_id=body.note_id,
        changes=body.changes or {}, expected=body.version,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Note {body.note_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/note/save", include_in_schema=False)
async def internal_note_save(body: NoteBody, _: None = Depends(require_worker_key)):
    data = await call_tn(
        update_note, body.session_id, patient_id=body.patient_id, note_id=body.note_id, changes=body.changes or {},
        expected=body.version,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Note {body.note_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/files", include_in_schema=False)
async def internal_files(body: PatientBody, _: None = Depends(require_worker_key)):
    return await call_tn(list_files, body.session_id, patient_id=body.patient_id)


@app.post("/internal/files/upload", include_in_schema=False)
async def internal_file_upload(body: UploadBody, _: None = Depends(require_worker_key)):
    return await call_tn(
        upload_file, body.session_id, patient_id=body.patient_id, file_name=body.file_name, mime_type=body.mime_type,
        content_base64=body.content_base64, name=body.name, date=body.date.isoformat(), access=body.access,
    )


@app.post("/internal/files/download", include_in_schema=False)
async def internal_file_download(body: FileBody, _: None = Depends(require_worker_key)):
    data = await call_tn(download_file, body.session_id, patient_id=body.patient_id, file_id=body.file_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"File {body.file_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/files/delete", include_in_schema=False)
async def internal_file_delete(body: FileBody, _: None = Depends(require_worker_key)):
    data = await call_tn(delete_file, body.session_id, patient_id=body.patient_id, file_id=body.file_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"File {body.file_id} for patient {body.patient_id} not found")
    return data


@app.post("/internal/appointment-billing", include_in_schema=False)
async def internal_appointment_billing(body: AppointmentBody, _: None = Depends(require_worker_key)):
    data = await call_tn(get_appointment_billing, body.session_id, calendar_entry_id=body.calendar_entry_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Appointment {body.calendar_entry_id} not found or has no billing")
    return data


@app.post("/internal/appointment-billing/modifiers", include_in_schema=False)
async def internal_modifiers(body: ModifiersBody, _: None = Depends(require_worker_key)):
    data = await call_tn(
        update_modifiers, body.session_id, calendar_entry_id=body.calendar_entry_id,
        changes=body.changes, expected=body.version, override_warnings=body.override_warnings,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Appointment {body.calendar_entry_id} not found or has no billing")
    return data


@app.post("/internal/diagnosis-codes", include_in_schema=False)
async def internal_diagnosis_codes(body: DiagnosisSearchBody, _: None = Depends(require_worker_key)):
    return await call_tn(find_diagnoses, body.session_id, search=body.search)


@app.post("/internal/claims", include_in_schema=False)
async def internal_claims(body: ClaimsBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_claims, **body.model_dump())


@app.post("/internal/claims/pending", include_in_schema=False)
async def internal_pending_claims(body: PendingClaimsBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_pending_claims, **body.model_dump())


@app.post("/internal/claims/filters", include_in_schema=False)
async def internal_claim_filters(body: SessionBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_claim_filters, body.session_id)


@app.post("/internal/claims/queue", include_in_schema=False)
async def internal_claim_queue(body: QueueBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_queue, **body.model_dump())


@app.post("/internal/claims/summary", include_in_schema=False)
async def internal_claim_summary(body: SessionBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_claim_summary, body.session_id)


@app.post("/internal/eligibility", include_in_schema=False)
async def internal_eligibility(body: EligibilityBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_eligibility, **body.model_dump())


@app.post("/internal/claim-detail", include_in_schema=False)
async def internal_claim_detail(body: ClaimBody, _: None = Depends(require_worker_key)):
    data = await call_tn(get_claim, body.session_id, claim_id=body.claim_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Claim {body.claim_id} not found")
    return data


@app.post("/internal/claims/rejections", include_in_schema=False)
async def internal_rejections(body: RejectionsBody, _: None = Depends(require_worker_key)):
    return await call_tn(get_rejections, **body.model_dump())


@app.post("/internal/claim/review", include_in_schema=False)
async def internal_claim_review(body: ClaimBody, _: None = Depends(require_worker_key)):
    data = await call_tn(mark_reviewed, body.session_id, claim_id=body.claim_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Claim {body.claim_id} not found")
    return data


@app.post("/internal/claim/comment", include_in_schema=False)
async def internal_claim_comment(body: CommentBody, _: None = Depends(require_worker_key)):
    data = await call_tn(
        save_comment, body.session_id, claim_id=body.claim_id, comment=body.comment, expected=body.version,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Claim {body.claim_id} not found")
    return data


@app.post("/internal/session-ping", include_in_schema=False)
async def internal_session_ping(body: SessionBody, _: None = Depends(require_worker_key)):
    """Client-driven keep-alive (our /refresh); also confirms the TN session is still valid."""
    session = get_session(body.session_id)
    if not await session.ping():
        await registry.drop(body.session_id)
        raise HTTPException(status_code=401, detail="TherapyNotes session lost; login again")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.worker_host, port=settings.worker_port, reload=False)
