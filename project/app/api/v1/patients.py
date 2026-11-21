"""Patient list and single-patient details — proxied to the TherapyNotes gateway worker."""

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import require_session_id
from app.worker_client import worker_claim_queue, worker_patient_detail, worker_patients

router = APIRouter(tags=["Patients"])

PatientId = Path(gt=0, description="TherapyNotes patient ID (`patient_id` in the patient list)")
DETAIL_SECTIONS = ("info", "insurance", "billing")

Status = Literal[
    "any",
    "active",
    "inactive",
    "needsDataEntry",
    "aptComingWeek",
    "intComingWeek",
    "patientsWithFirstAppointmentInLast30Days",
    "openBalance",
    "birthday",
]


@router.get(
    "/patients",
    summary="List patients",
    description=(
        "Patients as on the TherapyNotes Patients page, with search, filters, sorting and paging. "
        "Use `patient_id` from here in every other patient API."
    ),
)
async def patients_endpoint(
    search: str = Query("", max_length=100, description="Name, account #, phone, or insurance member ID"),
    status: Status = Query("active", description="TherapyNotes list filter"),
    assignment: str = Query(
        "any",
        pattern=r"^(any|clinician-(none|\d+)|provider-(none|outofnetwork|\d+))$",
        description=(
            "`any`, `clinician-none`, `clinician-{clinician id}`, `provider-none`, "
            "`provider-outofnetwork`, or `provider-{insurance group id}`"
        ),
    ),
    sort: Literal["name", "dob", "lastAppt"] = Query("name", description="Name, date of birth or last appointment"),
    ascending: bool = True,
    page: int = Query(1, ge=1),
    session_id: str = Depends(require_session_id),
):
    data = await worker_patients(
        session_id,
        page_num=page,
        status=status,
        search=search.strip(),
        assignment=assignment,
        sort=sort,
        ascending=ascending,
    )
    # for patient in data.get("patients", []):
    #     pid = patient["patient_id"]
    #     patient["links"] = {s: f"/api/v1/patients/{pid}/{s}" for s in DETAIL_SECTIONS}
    return data


@router.get(
    "/patients/{patient_id}/info",
    summary="Patient info",
    description="Info tab: name, date of birth, address, phones, demographics and appointments.",
)
async def patient_info(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_patient_detail(session_id, patient_id, "info")


@router.get(
    "/patients/{patient_id}/billing",
    summary="Patient billing settings",
    description=(
        "Billing comments, cash rates, responsible party, referral, claim info and prior authorizations. "
        "Insurance policies have their own API under Insurance."
    ),
)
async def patient_billing(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_patient_detail(session_id, patient_id, "billing")


@router.get(
    "/patients/{patient_id}/ledger",
    summary="Patient ledger",
    description="Every billing item for the patient with its insurance status, plus balance owed and unassigned credit.",
)
async def patient_ledger(patient_id: int = PatientId, session_id: str = Depends(require_session_id)):
    return await worker_claim_queue(session_id, "ledger", patient_id=patient_id)
