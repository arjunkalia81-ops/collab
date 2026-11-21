"""HTTP client: project API → TherapyNotes gateway worker (internal routes + key)."""

import httpx
from fastapi import HTTPException

from app.core.config import settings

TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def worker_headers() -> dict[str, str]:
    secret = settings.worker_secret.strip()
    if not secret:
        raise HTTPException(status_code=500, detail="WORKER_SECRET is not configured")
    return {"X-Worker-Key": secret}


async def post_worker(path: str, body: dict | None = None) -> dict:
    url = settings.worker_url.rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(
                f"{url}{path}",
                json=body or {},
                headers=worker_headers(),
            )
        except httpx.ConnectError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Browser worker unreachable at {url}",
            ) from exc

        # 401 session gone, 404 not found, 409 changed in TN since loaded, 422 TN rejected a save,
        # 429 worker full, 503 Cloudflare blocked.
        if response.status_code in (401, 404, 409, 422, 429, 502, 503):
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except Exception:
                detail = response.reason_phrase
            raise HTTPException(status_code=response.status_code, detail=detail)

        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail=f"Worker error {response.status_code}: {response.text[:300]}",
            )
        return response.json()


async def worker_login(practice_code: str, username: str, password: str) -> dict:
    return await post_worker(
        "/internal/login",
        {
            "practice_code": practice_code,
            "username": username,
            "password": password,
        },
    )


async def worker_patients(session_id: str, **filters) -> dict:
    return await post_worker("/internal/patients", {"session_id": session_id, **filters})


async def worker_patient_detail(session_id: str, patient_id: int, section: str) -> dict:
    return await post_worker(
        "/internal/patient-detail",
        {"session_id": session_id, "patient_id": patient_id, "section": section},
    )


async def worker_save_insurance(
    session_id: str, patient_id: int, changes: dict, policy_id: int | None = None, version: str | None = None,
) -> dict:
    return await post_worker(
        "/internal/insurance/save",
        {"session_id": session_id, "patient_id": patient_id, "policy_id": policy_id, "changes": changes,
         "version": version},
    )


async def worker_notes(session_id: str, patient_id: int) -> dict:
    return await post_worker("/internal/notes", {"session_id": session_id, "patient_id": patient_id})


async def worker_note(session_id: str, patient_id: int, note_id: int) -> dict:
    return await post_worker(
        "/internal/note", {"session_id": session_id, "patient_id": patient_id, "note_id": note_id},
    )


async def worker_draft_header(session_id: str, patient_id: int, note_id: int) -> dict:
    return await post_worker(
        "/internal/note/header", {"session_id": session_id, "patient_id": patient_id, "note_id": note_id},
    )


async def worker_save_draft_header(
    session_id: str, patient_id: int, note_id: int, changes: dict, version: str,
) -> dict:
    return await post_worker(
        "/internal/note/header/save",
        {"session_id": session_id, "patient_id": patient_id, "note_id": note_id, "changes": changes,
         "version": version},
    )


async def worker_save_note(session_id: str, patient_id: int, note_id: int, changes: dict, version: str) -> dict:
    return await post_worker(
        "/internal/note/save",
        {"session_id": session_id, "patient_id": patient_id, "note_id": note_id, "changes": changes,
         "version": version},
    )


async def worker_files(session_id: str, patient_id: int) -> dict:
    return await post_worker("/internal/files", {"session_id": session_id, "patient_id": patient_id})


async def worker_upload_file(session_id: str, patient_id: int, **upload) -> dict:
    return await post_worker("/internal/files/upload", {"session_id": session_id, "patient_id": patient_id, **upload})


async def worker_download_file(session_id: str, patient_id: int, file_id: int) -> dict:
    return await post_worker(
        "/internal/files/download", {"session_id": session_id, "patient_id": patient_id, "file_id": file_id},
    )


async def worker_delete_file(session_id: str, patient_id: int, file_id: int) -> dict:
    return await post_worker(
        "/internal/files/delete", {"session_id": session_id, "patient_id": patient_id, "file_id": file_id},
    )


async def worker_appointment_billing(session_id: str, calendar_entry_id: int) -> dict:
    return await post_worker(
        "/internal/appointment-billing", {"session_id": session_id, "calendar_entry_id": calendar_entry_id},
    )


async def worker_save_modifiers(
    session_id: str, calendar_entry_id: int, changes: list[dict], version: str, override_warnings: bool,
) -> dict:
    return await post_worker(
        "/internal/appointment-billing/modifiers",
        {
            "session_id": session_id,
            "calendar_entry_id": calendar_entry_id,
            "changes": changes,
            "version": version,
            "override_warnings": override_warnings,
        },
    )


async def worker_diagnosis_codes(session_id: str, search: str) -> dict:
    return await post_worker("/internal/diagnosis-codes", {"session_id": session_id, "search": search})


async def worker_claims(session_id: str, **filters) -> dict:
    return await post_worker("/internal/claims", {"session_id": session_id, **filters})


async def worker_pending_claims(session_id: str, **filters) -> dict:
    return await post_worker("/internal/claims/pending", {"session_id": session_id, **filters})


async def worker_claim_filters(session_id: str) -> dict:
    return await post_worker("/internal/claims/filters", {"session_id": session_id})


async def worker_claim_queue(session_id: str, queue: str, **filters) -> dict:
    return await post_worker("/internal/claims/queue", {"session_id": session_id, "queue": queue, **filters})


async def worker_claim_summary(session_id: str) -> dict:
    return await post_worker("/internal/claims/summary", {"session_id": session_id})


async def worker_eligibility(session_id: str, **filters) -> dict:
    return await post_worker("/internal/eligibility", {"session_id": session_id, **filters})


async def worker_claim_detail(session_id: str, claim_id: int) -> dict:
    return await post_worker("/internal/claim-detail", {"session_id": session_id, "claim_id": claim_id})


async def worker_rejections(session_id: str, **filters) -> dict:
    return await post_worker("/internal/claims/rejections", {"session_id": session_id, **filters})


async def worker_mark_reviewed(session_id: str, claim_id: int) -> dict:
    return await post_worker("/internal/claim/review", {"session_id": session_id, "claim_id": claim_id})


async def worker_save_comment(session_id: str, claim_id: int, comment: str, version: str) -> dict:
    return await post_worker(
        "/internal/claim/comment",
        {"session_id": session_id, "claim_id": claim_id, "comment": comment, "version": version},
    )


async def worker_close(session_id: str | None = None) -> dict:
    body = {"session_id": session_id} if session_id else {}
    return await post_worker("/worker/close", body)


async def worker_session_ping(session_id: str) -> dict:
    return await post_worker("/internal/session-ping", {"session_id": session_id})


async def worker_health() -> dict:
    url = settings.worker_url.rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        try:
            response = await client.get(f"{url}/worker/health")
            return response.json()
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)}
