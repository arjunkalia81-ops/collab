"""Patient insurance policies (Billing Settings tab): read, add and update via the TherapyNotes gateway worker."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.api.deps import require_session_id
from app.worker_client import worker_patient_detail, worker_save_insurance

router = APIRouter(tags=["Insurance"])

PatientId = Path(gt=0, description="TherapyNotes patient ID (`patient_id` in the patient list)")


class InsuredAddress(BaseModel):
    address1: str | None = Field(None, max_length=100)
    address2: str | None = Field(None, max_length=100)
    city: str | None = Field(None, max_length=60)
    state: str | None = Field(None, pattern=r"^([A-Z]{2})?$", description="2-letter code, empty to clear")
    postal_code: str | None = Field(None, max_length=10)


class Insured(BaseModel):
    """Policy holder when `relationship` is not Self."""

    first_name: str | None = Field(None, max_length=60)
    middle_name: str | None = Field(None, max_length=60)
    last_name: str | None = Field(None, max_length=60)
    suffix: str | None = Field(None, max_length=10)
    gender: int | None = Field(None, ge=0, le=1, description="`options.genders`")
    date_of_birth: date | None = None
    phone: str | None = Field(None, max_length=20)
    address: InsuredAddress | None = None


class PolicyChanges(BaseModel):
    """Only the fields you send are changed; send `null` to clear a field."""

    payer_setting_id: int | None = Field(None, gt=0, description="`options.payers[].payer_setting_id`")
    priority: int | None = Field(None, ge=1, le=4, description="`options.priorities`")
    msp_type: int | None = Field(None, ge=1, le=9, description="MSP qualification type, needed on secondary policies")
    comments: str | None = Field(None, max_length=2000)
    signature_on_file: bool | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None
    copay: float | None = Field(None, ge=0)
    deductible: float | None = Field(None, ge=0)
    appointment_limit: int | None = Field(None, ge=0, le=2, description="`options.appointment_limits`")
    appointments_per_year: int | None = Field(None, ge=0, description="When `appointment_limit` is 2 (Number)")
    appointments_year_end: date | None = Field(None, description="Date the yearly appointment count resets")
    member_id: str | None = Field(None, max_length=50)
    policy_group: str | None = Field(None, max_length=50)
    employer: str | None = Field(None, max_length=100)
    plan_name: str | None = Field(None, max_length=100)
    relationship: int | None = Field(None, ge=0, le=4, description="`options.relationships`")
    insured: Insured | None = None


class NewPolicy(PolicyChanges):
    payer_setting_id: int = Field(gt=0, description="`options.payers[].payer_setting_id`")
    member_id: str = Field(min_length=1, max_length=50)


class PolicyUpdate(PolicyChanges):
    version: str = Field(description="`version` of the policy as loaded; 409 if it changed in TherapyNotes since")


def check_dates(changes: dict) -> None:
    start, end = changes.get("coverage_start"), changes.get("coverage_end")
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="coverage_start must be on or before coverage_end")


@router.get(
    "/patients/{patient_id}/insurance",
    summary="Get insurance policies",
    description=(
        "The patient's insurance policies with every editable field and a `version` each, "
        "plus `options` to fill the form dropdowns (payers, priorities, relationships and so on)."
    ),
)
async def patient_insurance(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_patient_detail(session_id, patient_id, "insurance")


@router.post(
    "/patients/{patient_id}/insurance",
    status_code=201,
    summary="Add insurance policy",
    description=(
        "Adds a policy to the patient. `payer_setting_id` and `member_id` are required, the rest is optional. "
        "Returns the patient's insurance after the save."
    ),
)
async def add_policy(body: NewPolicy, patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    changes = body.model_dump(mode="json", exclude_unset=True)
    check_dates(changes)
    return await worker_save_insurance(session_id, patient_id, changes)


@router.patch(
    "/patients/{patient_id}/insurance/{policy_id}",
    summary="Update insurance policy",
    description=(
        "Changes only the fields you send; `null` clears a field. Send the policy's `version` from the GET. "
        "Returns 409 if someone changed the policy in TherapyNotes since you loaded it."
    ),
)
async def update_policy(
    body: PolicyUpdate,
    patient_id: int = PatientId,
    policy_id: int = Path(gt=0, description="`policy_id` from GET /patients/{patient_id}/insurance"),
    session_id: str = Depends(require_session_id),
):
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"version"})
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    check_dates(changes)
    return await worker_save_insurance(session_id, patient_id, changes, policy_id, body.version)
