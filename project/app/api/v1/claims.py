"""Claims: submitted history, items waiting to be sent, rejections, claim detail and eligibility history."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.api.deps import require_session_id
from app.worker_client import (
    worker_claim_detail,
    worker_claim_filters,
    worker_claim_queue,
    worker_claim_summary,
    worker_claims,
    worker_eligibility,
    worker_mark_reviewed,
    worker_pending_claims,
    worker_rejections,
    worker_save_comment,
)

router = APIRouter()
CLAIMS = ["Claims"]

CLAIM_STATUS = {
    "pending": 1,
    "submitted": 2,
    "sent": 3,
    "rejected": 4,
    "canceled": 5,
    "paid": 6,
    "rejected_new": 7,
}
SEQUENCE = {"primary": 1, "secondary": 2}
ELIGIBILITY_STATUS = {"pending": 1, "new": 2, "reviewed": 3}

PatientFilter = Query(None, gt=0, description="Only this patient's claims (`patient_id` from `/patients`)")
PayerFilter = Query(None, gt=0, description="Payer group id (`payer_id` from `/claims/filters`)")
ClinicianFilter = Query(None, gt=0, description="Clinician id (`clinician_id` from `/claims/filters`)")
StartDate = Query(None, description="Appointments from (YYYY-MM-DD); empty means all")
EndDate = Query(None, description="Appointments to (YYYY-MM-DD); empty means all")
ClaimId = Path(gt=0, description="TherapyNotes claim number")


class Comment(BaseModel):
    comment: str = Field(max_length=1000, description="Biller comment; send an empty string to clear it")
    version: str = Field(description="`comment_version` from GET /claims/{claim_id}; 409 if it changed since")


def check_dates(start_date: date | None, end_date: date | None) -> None:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")


@router.get(
    "/claims",
    tags=CLAIMS,
    summary="Claim history",
    description="Claims already sent (Electronic Claim History), with filters and paging.",
)
async def claims_endpoint(
    patient_id: int | None = PatientFilter,
    status: Literal["pending", "submitted", "sent", "rejected", "canceled", "paid", "rejected_new"] | None = Query(
        None, description="Claim status; empty means any"
    ),
    payer_id: int | None = PayerFilter,
    clinician_id: int | None = ClinicianFilter,
    sequence: Literal["primary", "secondary"] | None = None,
    start_date: date | None = Query(None, description="Claim date from (YYYY-MM-DD); empty means all"),
    end_date: date | None = Query(None, description="Claim date to (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    session_id: str = Depends(require_session_id),
):
    check_dates(start_date, end_date)
    data = await worker_claims(
        session_id,
        page_num=page,
        page_size=page_size,
        patient_id=patient_id,
        status=CLAIM_STATUS.get(status),
        payer_id=payer_id,
        clinician_id=clinician_id,
        sequence=SEQUENCE.get(sequence),
        start_date=start_date and start_date.isoformat(),
        end_date=end_date and end_date.isoformat(),
    )
    for claim in data.get("claims", []):
        claim["link"] = f"/api/v1/claims/{claim['claim_id']}"
    return data


@router.get(
    "/claims/pending",
    tags=CLAIMS,
    summary="Claims to submit",
    description=(
        "Claims not sent yet (Submit Primary Claims). Each line has a `calendar_entry_id` "
        "for the Modifiers APIs."
    ),
)
async def pending_claims(
    items: Literal["newresubmit", "new", "resubmit", "all", "information"] = Query(
        "newresubmit",
        description=(
            "`newresubmit` pending initial or resubmission, `new` initial only, "
            "`resubmit` resubmission only, `all` all open items, `information` information required"
        ),
    ),
    patient_id: int | None = PatientFilter,
    payer_id: int | None = PayerFilter,
    clinician_id: int | None = ClinicianFilter,
    start_date: date | None = Query(None, description="Appointments from (YYYY-MM-DD); empty means all"),
    end_date: date | None = Query(None, description="Appointments to (YYYY-MM-DD); empty means today"),
    session_id: str = Depends(require_session_id),
):
    check_dates(start_date, end_date)
    return await worker_pending_claims(
        session_id,
        items=items,
        patient_id=patient_id,
        payer_id=payer_id,
        clinician_id=clinician_id,
        start_date=start_date and start_date.isoformat(),
        end_date=end_date and end_date.isoformat(),
    )


@router.get(
    "/claims/filters",
    tags=CLAIMS,
    summary="Claim filter options",
    description="Payers and clinicians to fill the `payer_id` and `clinician_id` filters.",
)
async def claim_filters(session_id: str = Depends(require_session_id)):
    return await worker_claim_filters(session_id)


@router.get(
    "/claims/summary",
    tags=CLAIMS,
    summary="Billing dashboard counts",
    description="Counts shown on the Billing page: rejected claims, claims to submit, paper and external claims, new ERAs and eligibility reports.",
)
async def claim_summary(session_id: str = Depends(require_session_id)):
    return await worker_claim_summary(session_id)


@router.get(
    "/claims/rejections",
    tags=CLAIMS,
    summary="Rejected claims",
    description=(
        "Rejected claims with the payer's reason (`rejection`). By default the new ones not reviewed yet "
        "(Rejections to Review); `reviewed=true` lists the reviewed ones."
    ),
)
async def rejected_claims(
    reviewed: bool = Query(False, description="false: Rejected - New, true: Rejected (already reviewed)"),
    patient_id: int | None = PatientFilter,
    payer_id: int | None = PayerFilter,
    clinician_id: int | None = ClinicianFilter,
    start_date: date | None = Query(None, description="Claim date from (YYYY-MM-DD); empty means all"),
    end_date: date | None = Query(None, description="Claim date to (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=25),
    session_id: str = Depends(require_session_id),
):
    check_dates(start_date, end_date)
    data = await worker_rejections(
        session_id,
        reviewed=reviewed,
        page_num=page,
        page_size=page_size,
        patient_id=patient_id,
        payer_id=payer_id,
        clinician_id=clinician_id,
        start_date=start_date and start_date.isoformat(),
        end_date=end_date and end_date.isoformat(),
    )
    for claim in data.get("claims", []):
        claim["link"] = f"/api/v1/claims/{claim['claim_id']}"
    return data


async def claim_queue(session_id: str, queue: str, start_date: date | None, end_date: date | None, **filters):
    check_dates(start_date, end_date)
    return await worker_claim_queue(
        session_id,
        queue,
        start_date=start_date and start_date.isoformat(),
        end_date=end_date and end_date.isoformat(),
        **filters,
    )


@router.get(
    "/claims/secondary",
    tags=CLAIMS,
    summary="Secondary claims to submit",
    description="Items waiting to be billed to the secondary payer (Submit Secondary Claims).",
)
async def secondary_claims(
    patient_id: int | None = PatientFilter,
    clinician_id: int | None = ClinicianFilter,
    start_date: date | None = StartDate,
    end_date: date | None = EndDate,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    session_id: str = Depends(require_session_id),
):
    return await claim_queue(
        session_id, "secondary", start_date, end_date,
        patient_id=patient_id, clinician_id=clinician_id, page_num=page, page_size=page_size,
    )


@router.get(
    "/claims/paper",
    tags=CLAIMS,
    summary="Paper claims to create",
    description="Items to bill on paper (Create CMS-1500).",
)
async def paper_claims(
    patient_id: int | None = PatientFilter,
    clinician_id: int | None = ClinicianFilter,
    start_date: date | None = StartDate,
    end_date: date | None = EndDate,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    session_id: str = Depends(require_session_id),
):
    return await claim_queue(
        session_id, "paper", start_date, end_date,
        patient_id=patient_id, clinician_id=clinician_id, page_num=page, page_size=page_size,
    )


@router.get(
    "/claims/external",
    tags=CLAIMS,
    summary="Claims to mark as sent",
    description="Items billed outside TherapyNotes that still need to be marked as submitted (Mark External Claims).",
)
async def external_claims(
    method: Literal["external", "electronic", "paper"] = Query(
        "external", description="Items pending external, electronic or paper submission"
    ),
    patient_id: int | None = PatientFilter,
    clinician_id: int | None = ClinicianFilter,
    start_date: date | None = StartDate,
    end_date: date | None = EndDate,
    session_id: str = Depends(require_session_id),
):
    return await claim_queue(
        session_id, "external", start_date, end_date,
        method=method, patient_id=patient_id, clinician_id=clinician_id,
    )


@router.get(
    "/claims/superbill",
    tags=CLAIMS,
    summary="Superbill items",
    description="Out-of-network items for one patient that can go on a superbill (Create Superbill).",
)
async def superbill_items(
    patient_id: int = Query(gt=0, description="Superbills are per patient (`patient_id` from `/patients`)"),
    start_date: date | None = StartDate,
    end_date: date | None = EndDate,
    session_id: str = Depends(require_session_id),
):
    return await claim_queue(session_id, "superbill", start_date, end_date, patient_id=patient_id)


@router.get(
    "/eligibility",
    tags=["Eligibility"],
    summary="Eligibility history",
    description="Insurance eligibility checks and their reports, with filters and paging.",
)
async def eligibility_history(
    patient_id: int | None = Query(None, gt=0, description="Only this patient's eligibility reports"),
    status: Literal["pending", "new", "reviewed"] | None = Query(None, description="Empty means any"),
    start_date: date | None = Query(None, description="Transaction date from (YYYY-MM-DD)"),
    end_date: date | None = Query(None, description="Transaction date to (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    session_id: str = Depends(require_session_id),
):
    check_dates(start_date, end_date)
    return await worker_eligibility(
        session_id,
        patient_id=patient_id,
        status=ELIGIBILITY_STATUS.get(status),
        start_date=start_date and start_date.isoformat(),
        end_date=end_date and end_date.isoformat(),
        page_num=page,
        page_size=page_size,
    )


@router.get(
    "/claims/{claim_id}",
    tags=CLAIMS,
    summary="Claim detail",
    description=(
        "One claim with its line items, payer history, biller comment and, for rejected claims, "
        "the rejection reason (`rejection`)."
    ),
)
async def claim_detail(claim_id: int = ClaimId, session_id: str = Depends(require_session_id)):
    return await worker_claim_detail(session_id, claim_id)


@router.post(
    "/claims/{claim_id}/review",
    tags=CLAIMS,
    summary="Mark rejection as reviewed",
    description=(
        "Moves a 'Rejected - New' claim to 'Rejected' and clears the TherapyNotes To-Do notification. "
        "TherapyNotes has no undo for this. Returns 422 if the claim is not 'Rejected - New'."
    ),
)
async def review_claim(claim_id: int = ClaimId, session_id: str = Depends(require_session_id)):
    return await worker_mark_reviewed(session_id, claim_id)


@router.patch(
    "/claims/{claim_id}/comment",
    tags=CLAIMS,
    summary="Update biller comment",
    description=(
        "Saves the claim's biller comment (max 1000 characters, trimmed). Send `comment_version` from "
        "GET /claims/{claim_id}; returns 409 if the comment changed since you loaded it."
    ),
)
async def update_comment(body: Comment, claim_id: int = ClaimId, session_id: str = Depends(require_session_id)):
    return await worker_save_comment(session_id, claim_id, body.comment, body.version)
