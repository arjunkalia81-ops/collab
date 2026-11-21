"""Billing tab of an appointment (date of service): service lines, units, and procedure modifiers.

Modifiers are saved on the appointment. Units are saved on the linked note (TN ignores
UnitsQuantity changes on saveappointmentsettings).
"""

import json
import re

from worker_app.claim.claims import plain
from worker_app.patient.draft_header import get_draft_header, update_draft_header
from worker_app.patient.notes import find_note, get_note, update_note
from worker_app.tn.client import TherapyNotesError, ValidationFailed
from worker_app.tn.version import check_version, verify, version

LOAD = "/app/billing/settings/loadappointmentsettings.aspx?msg=1"
POLICIES = "/app/billing/calendar/api/CalendarEntryPatientInsurancePolicies.aspx?calendarEntryId={}&patientId={}&msg=1"
SAVE = "/app/billing/settings/saveappointmentsettings.aspx?msg=1"
REFERER = "/app/billing/"

ADD_ON_CODE = 2
PRACTICE_COLLECTS = 0
OUT_OF_NETWORK = {3, 4, 7}
MODIFIER = re.compile(r"^[A-Z0-9]{2}$")
MISSING = object()


async def load(session, calendar_entry_id: int) -> dict | None:
    try:
        data = await session.post(LOAD, {"entryid": calendar_entry_id}, REFERER)
    except TherapyNotesError:
        # TN answers an unknown appointment id with a 500.
        return None
    return data if data and data.get("BillableItems") else None


def modifiers(service: dict) -> list[str]:
    # Same as the TN page: saved codes win per slot, otherwise the automatic (override) codes show.
    saved = service.get("ProcedureModifierCodes") or []
    auto = service.get("ProcedureModifierOverrideCodes") or []
    slots = [(saved[i] if i < len(saved) else "") or (auto[i] if i < len(auto) else "") for i in range(4)]
    return [m.strip() for m in slots if m and m.strip()]


def service_view(service: dict) -> dict:
    return {
        "billable_service_id": service["BillableServiceID"],
        "service_code_id": service.get("ServiceCodeID"),
        "service_code": service.get("ServiceCodeText"),
        "description": service.get("ServiceCodeDesc"),
        "add_on": service.get("ServiceCodeType") == ADD_ON_CODE,
        "units": service.get("UnitsQuantity"),
        "insurance_status": service.get("InsurancePaymentStatusText"),
        "modifiers": modifiers(service),
    }


def item_view(item: dict) -> dict:
    return {
        "billable_item_id": item.get("BillableItemID"),
        "patient_id": item.get("PatientID"),
        "patient_name": f"{item.get('PatientFirstName', '')} {item.get('PatientLastName', '')}".strip(),
        "clinician": item.get("ClinicianName"),
        "note_id": item.get("NoteID"),
        "primary_diagnosis": item.get("PrimaryDiagnosis"),
        "payer": (item.get("Payer") or {}).get("Name"),
        "services": [service_view(s) for s in item.get("Services") or []],
    }


def appointment_view(calendar_entry_id: int, data: dict) -> dict:
    view = {
        "calendar_entry_id": calendar_entry_id,
        "date": data.get("ApptStartDate"),
        "type": data.get("ApptTypeName"),
        "items": [item_view(i) for i in data["BillableItems"]],
    }
    return {**view, "version": version(view)}


async def get_appointment_billing(session, calendar_entry_id: int) -> dict | None:
    data = await load(session, calendar_entry_id)
    return appointment_view(calendar_entry_id, data) if data else None


def js_number(value) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def amount(value=MISSING) -> str:
    if value is MISSING:
        return "-1"
    return "" if value is None or value < 0 else js_number(value)


def default_rate(value) -> float | int:
    return value if value and value > 0 else -1


def out_of_network_rate(service: dict, assignment: int):
    patient = service.get("OutOfNetworkPatientAmount", MISSING)
    if patient is MISSING:
        return MISSING
    if patient is None or patient == -1:
        return None
    rate = service.get("OutOfNetworkInsuranceRate")
    if rate is not None and rate >= 0 and assignment == PRACTICE_COLLECTS:
        patient += rate
    return patient


def service_request(service: dict, item: dict, change: dict) -> dict:
    """Mirrors CalendarEntryBillingTabDataService.BuildSaveAppointmentSettingsRequestService with rates untouched."""
    oon = out_of_network_rate(service, item.get("PaymentAssignment"))
    rate = service.get("OutOfNetworkInsuranceRate", MISSING)
    coverage = rate if rate is MISSING else (rate if item.get("PaymentMethod") in OUT_OF_NETWORK and rate != -1 else None)
    codes = list(change["modifiers"]) + [""] * (4 - len(change["modifiers"]))
    return {
        "BillableServiceID": service["BillableServiceID"],
        "ServiceCodeID": service.get("ServiceCodeID"),
        "ServiceCodeText": service.get("ServiceCodeText"),
        "ServiceCodeType": service.get("ServiceCodeType"),
        "InNetworkInsuranceRate": amount(service.get("InNetworkInsuranceRate", MISSING)),
        "InNetworkInsuranceRateDefault": (
            default_rate(service.get("InNetworkInsuranceRate")) if service.get("InNetworkInsuranceRateIsDefault") else -1
        ),
        "InNetworkInsuranceCoPay": amount(service.get("InNetworkInsuranceCoPay", MISSING)),
        "InNetworkInsuranceCoPayDefault": (
            default_rate(service.get("InNetworkInsuranceCoPay")) if service.get("InNetworkInsuranceCoPayIsDefault") else -1
        ),
        "OutOfNetworkServiceRate": amount(oon),
        "OutOfNetworkServiceRateDefault": (
            oon if service.get("OutOfNetworkInsuranceRateIsDefault") and oon not in (MISSING, None) and oon >= 0 else -1
        ),
        "OutOfNetworkInsuranceAmount": amount(coverage),
        "OutOfNetworkInsuranceAmountDefault": (
            default_rate(service.get("OutOfNetworkInsuranceRate")) if service.get("OutOfNetworkPatientAmountIsDefault") else -1
        ),
        "DirectRate": amount(service.get("DirectPaymentRate", MISSING)),
        "DirectRateDefault": service.get("DefaultDirectPaymentRate") or -1,
        "InsuranceStatusCode": service.get("InsurancePaymentStatusCode"),
        # TN echoes UnitsQuantity; changing it here does not stick — units go through the note.
        "UnitsQuantity": service.get("UnitsQuantity"),
        "ModifierCode1": codes[0],
        "ModifierCode2": codes[1],
        "ModifierCode3": codes[2],
        "ModifierCode4": codes[3],
        "SaveRateToRateSchedule": False,
        "SaveRateToServiceCode": False,
        "WriteOffDirectBalanceReason": str(service.get("WriteOffReason", 0)),
    }


async def save_units_on_note(session, item: dict, service: dict, units: int) -> None:
    """Write units on the linked note (signed Edit Note, or draft header). Main CPT stays at 1."""
    note_id = item.get("NoteID")
    patient_id = item.get("PatientID")
    code = service.get("ServiceCodeText") or str(service.get("ServiceCodeID"))
    code_id = service.get("ServiceCodeID")
    if not note_id or not patient_id or not code_id:
        raise ValidationFailed([f"Service {service.get('BillableServiceID')} has no note; units cannot be changed"])
    if service.get("ServiceCodeType") != ADD_ON_CODE:
        raise ValidationFailed([
            f"Units for {code} are on the main service code and cannot be changed; "
            "change add-on units, or change the CPT on the note",
        ])

    doc = await find_note(session, patient_id, note_id)
    if not doc:
        raise ValidationFailed([f"Note {note_id} for this appointment was not found"])

    if doc.get("AuthorHasSignedDocument"):
        current = await get_note(session, patient_id, note_id)
        if not current:
            raise ValidationFailed([f"Note {note_id} for this appointment was not found"])
        add_ons = current.get("add_on_codes") or []
        if code_id not in {a["id"] for a in add_ons}:
            raise ValidationFailed([f"Add-on {code} is not on note {note_id}"])
        wanted = [{"id": a["id"], "units": units if a["id"] == code_id else a["units"]} for a in add_ons]
        await update_note(session, patient_id, note_id, {"add_on_codes": wanted}, current["version"])
        return

    header = await get_draft_header(session, patient_id, note_id)
    if not header:
        raise ValidationFailed([f"Draft note {note_id} for this appointment was not found"])
    if code_id not in {a["id"] for a in header.get("add_on_codes") or []}:
        raise ValidationFailed([f"Add-on {code} is not on note {note_id}"])
    await update_draft_header(
        session, patient_id, note_id,
        {"add_on_codes": [{"id": code_id, "units": units}]},
        header["version"],
    )


def is_default(policies: list[dict] | None, policy_id) -> bool:
    return not policies or policies[0].get("PolicyId") == policy_id


def line_change(service: dict, change: dict | None) -> dict:
    """Modifiers and optional units for one service line; missing change keeps the current values."""
    if not change:
        return {"modifiers": modifiers(service), "units": None}
    codes = change.get("modifiers")
    if codes is None:
        codes = modifiers(service)
    else:
        codes = [m.strip().upper() for m in codes]
    return {"modifiers": codes, "units": change.get("units")}


async def save_item(session, calendar_entry_id: int, item: dict, wanted: dict, override_warnings: bool) -> None:
    url = POLICIES.format(calendar_entry_id, item["PatientID"])
    policies = await session.post(url, {}, REFERER) or {}
    request = {
        "SubmissionType": "SaveEntry",
        "PrimaryInsurancePolicyIsDefault": is_default(
            policies.get("PrimaryInsurancePolicies"), item.get("PrimaryInsurancePolicyId"),
        ),
        "SecondaryInsurancePolicyIsDefault": is_default(
            policies.get("SecondaryInsurancePolicies"), item.get("SecondaryInsurancePolicyId"),
        ),
        "SaveRateScheduleToBillableItem": bool(item.get("SaveRateScheduleToBillableItem")),
        "Services": [
            service_request(s, item, line_change(s, wanted.get(s["BillableServiceID"])))
            for s in item.get("Services") or []
        ],
        "CalendarEntry": calendar_entry_id,
        "Patient": item.get("EncryptedPatientID"),
        "PrimaryInsurancePolicyId": item.get("PrimaryInsurancePolicyId") or -1,
        "SecondaryInsurancePolicyId": item.get("SecondaryInsurancePolicyId") or -1,
        "RateSchedule": item.get("RateScheduleId") or -1,
        "BillingAsSupervisor": (item.get("SupervisorID") or 0) > 0,
        "PaymentMethod": item.get("PaymentMethod"),
        "SecondaryPaymentMethod": item.get("SecondaryPaymentMethod"),
        "OriginalPaymentMethod": item.get("PaymentMethod"),
        "PaymentAssignment": item.get("PaymentAssignment"),
        "WriteOffDirectBalance": bool(item.get("WriteOffDirectBalance")),
        "ReimbursementDocsCreated": item.get("ReimbursementDocsCreated", 0),
        "OriginalReimbursementDocsCreated": item.get("ReimbursementDocsCreated", 0),
        "OverrideWarnings": override_warnings,
    }
    result = await session.post(SAVE, {"submission": json.dumps(request)}, REFERER)
    if result.get("Success"):
        return
    errors = plain(result.get("Errors"))
    warnings = plain(result.get("Warnings"))
    if errors:
        raise ValidationFailed(errors)
    if warnings:
        raise ValidationFailed([*warnings, "Send override_warnings: true to save anyway"])
    raise TherapyNotesError("TherapyNotes did not save the appointment billing")


async def update_modifiers(
    session, calendar_entry_id: int, changes: list[dict], expected: str, override_warnings: bool = False,
) -> dict | None:
    """Set modifiers (appointment billing) and/or units (linked note) on service lines.
    Each change is {billable_service_id, modifiers?, units?} with at least one of them."""
    data = await load(session, calendar_entry_id)
    if not data:
        return None
    check_version(expected, appointment_view(calendar_entry_id, data), f"Appointment {calendar_entry_id}")

    wanted = {}
    errors = []
    for change in changes:
        service_id = change["billable_service_id"]
        has_modifiers = "modifiers" in change and change["modifiers"] is not None
        has_units = change.get("units") is not None
        if not has_modifiers and not has_units:
            errors.append(f"Service {service_id} needs modifiers and/or units")
            continue
        codes = [m.strip().upper() for m in change["modifiers"]] if has_modifiers else None
        if codes is not None:
            bad = [m for m in codes if not MODIFIER.match(m)]
            if bad:
                errors.append(f"Modifiers must be 2 letters or digits: {', '.join(bad)}")
            if len(codes) > 4:
                errors.append(f"Service {service_id} can have at most 4 modifiers")
        if has_units and (not isinstance(change["units"], int) or change["units"] < 1 or change["units"] > 999):
            errors.append(f"Service {service_id} units must be between 1 and 999")
        wanted[service_id] = {"modifiers": codes, "units": change["units"] if has_units else None}

    services_by_id = {s["BillableServiceID"]: (i, s) for i in data["BillableItems"] for s in i.get("Services") or []}
    errors += [f"Service {s} is not on appointment {calendar_entry_id}" for s in wanted if s not in services_by_id]
    if errors:
        raise ValidationFailed(errors)

    for service_id, change in wanted.items():
        if change["units"] is None:
            continue
        item, service = services_by_id[service_id]
        await save_units_on_note(session, item, service, change["units"])

    # Note saves can refresh billing; reload before writing modifiers.
    if any(c["modifiers"] is not None for c in wanted.values()):
        data = await load(session, calendar_entry_id) or data
        items = [
            i for i in data["BillableItems"]
            if any(s["BillableServiceID"] in wanted and wanted[s["BillableServiceID"]]["modifiers"] is not None
                   for s in i.get("Services") or [])
        ]
        for item in items:
            await save_item(session, calendar_entry_id, item, wanted, override_warnings)

    saved = await load(session, calendar_entry_id)
    current_mods = {s["BillableServiceID"]: modifiers(s) for i in saved["BillableItems"] for s in i.get("Services") or []}
    current_units = {
        s["BillableServiceID"]: s.get("UnitsQuantity") for i in saved["BillableItems"] for s in i.get("Services") or []
    }
    view = appointment_view(calendar_entry_id, saved)
    expected_check = {}
    actual_check = {}
    for service_id, change in wanted.items():
        if change["modifiers"] is not None:
            expected_check[f"modifiers of service {service_id}"] = sorted(change["modifiers"])
            actual_check[f"modifiers of service {service_id}"] = sorted(current_mods.get(service_id) or [])
        if change["units"] is not None:
            expected_check[f"units of service {service_id}"] = change["units"]
            actual_check[f"units of service {service_id}"] = current_units.get(service_id)
    verify(f"Appointment {calendar_entry_id}", expected_check, actual_check, view)
    parts = []
    if any(c["modifiers"] is not None for c in wanted.values()):
        parts.append("modifiers")
    if any(c["units"] is not None for c in wanted.values()):
        parts.append("units")
    return {"message": f"{' and '.join(parts).capitalize()} saved and verified", **view}
