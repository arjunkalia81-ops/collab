"""Appointment (date of service) billing tab: service lines, units, and procedure modifiers."""

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field, model_validator

from app.api.deps import require_session_id
from app.worker_client import worker_appointment_billing, worker_save_modifiers

router = APIRouter(tags=["Modifiers"])

CalendarEntryId = Path(gt=0, description="`calendar_entry_id` from Claims to submit, Claim detail or Patient ledger")


class ServiceModifiers(BaseModel):
    billable_service_id: int = Field(gt=0, description="`items[].services[].billable_service_id`")
    modifiers: list[str] | None = Field(
        None,
        max_length=4,
        description="Full list for the line, up to 4 two-character codes; [] removes all; omit to leave modifiers as they are",
    )
    units: int | None = Field(
        None, ge=1, le=999, description="Units for the line; omit to leave units as they are",
    )

    @model_validator(mode="after")
    def need_modifiers_or_units(self):
        if self.modifiers is None and self.units is None:
            raise ValueError("Provide modifiers and/or units")
        return self


class ModifierChanges(BaseModel):
    services: list[ServiceModifiers] = Field(min_length=1, max_length=20)
    version: str = Field(description="`version` of the appointment billing as loaded; 409 if it changed since")
    override_warnings: bool = Field(False, description="Save even when TherapyNotes shows warnings")


@router.get(
    "/appointments/{calendar_entry_id}/billing",
    summary="Get appointment billing",
    description=(
        "Service lines of one appointment with units and modifiers, as in the Billing section of the claim popup. "
        "Includes the `version` to send when updating modifiers or units."
    ),
)
async def appointment_billing(
    calendar_entry_id: int = CalendarEntryId, session_id: str = Depends(require_session_id),
):
    return await worker_appointment_billing(session_id, calendar_entry_id)


@router.patch(
    "/appointments/{calendar_entry_id}/modifiers",
    summary="Update modifiers and units",
    description=(
        "Sets modifiers and/or units on each service line you send; other lines stay as they are. "
        "Send the `version` from the GET; returns 409 if the appointment changed since you loaded it."
    ),
)
async def update_modifiers(
    body: ModifierChanges,
    calendar_entry_id: int = CalendarEntryId,
    session_id: str = Depends(require_session_id),
):
    changes = [s.model_dump(exclude_unset=True) for s in body.services]
    return await worker_save_modifiers(session_id, calendar_entry_id, changes, body.version, body.override_warnings)
