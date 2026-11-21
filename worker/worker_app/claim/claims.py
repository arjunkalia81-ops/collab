"""Claims: submitted history, pending (ready to submit) and single-claim detail."""

import html
import re
from datetime import date

from worker_app.patient.detail import day
from worker_app.tn.version import version

REJECTED, REJECTED_NEW = 4, 7
CLAIM_STATUS = {
    1: "Pending",
    2: "Submitted",
    3: "Sent",
    4: "Rejected",
    5: "Canceled",
    6: "Paid",
    7: "Rejected - New",
}
SEQUENCE = {1: "Primary", 2: "Secondary"}
CLAIM_FREQUENCY = {1: "New Claim", 2: "Amended Claim", 3: "Void Claim"}
SOURCE = {"Update from Payer": "payer", "Update from Clearinghouse": "clearinghouse"}
TN_PAGE_SIZE = 100
MAX_TN_PAGES = 50
TAG = re.compile(r"<[^>]+>")


def tn_date(value: date | None) -> str | None:
    return f"{value.month}/{value.day}/{value.year}" if value else None


def full_name(first: str | None, last: str | None) -> str:
    return f"{first or ''} {last or ''}".strip()


def untag(value):
    # TN embeds links to its own pages in these messages.
    return html.unescape(TAG.sub("", value)) if isinstance(value, str) else value


def plain(messages: list | None) -> list:
    return [untag(m) for m in messages or []]


def view_path(claim_id: int) -> str:
    return f"/app/billing/claims/view/{claim_id}/"


def match(pattern: str, text: str) -> str | None:
    found = re.search(pattern, text)
    return found.group(1).strip() if found else None


def rejection_item(item: dict) -> dict:
    if item.get("RejectedByPayer"):
        source = "payer"
    elif item.get("RejectedByClearingHouse"):
        source = "clearinghouse"
    else:
        source = None
    return {"message": untag(item.get("Message")), "description": untag(item.get("Description")), "source": source}


def rejection(status_code: int | None, activity: list, items: list) -> dict | None:
    if status_code not in (REJECTED, REJECTED_NEW):
        return None
    # e.g. "Claim Status: Rejected, Claim Status Category Code: A7, Claim Reason: Missing or invalid
    # information. Additional Details: Code 21 - Patient eligibility not found with entity., ..."
    event = next(
        (e for e in activity if e.get("IsException") and "Claim Status:" in (e.get("Description") or "")), {},
    )
    text = event.get("Description") or ""
    first = items[0] if items else {}
    details = match(r"Additional Details: (.*?)\.?$", text) or first.get("description")
    return {
        "reviewed": status_code == REJECTED,
        "date": event.get("Date"),
        "source": SOURCE.get(event.get("TypeCategory")) or first.get("source"),
        "category_code": match(r"Category Code: (\w+)", text),
        "reason": match(r"Claim Reason: (.*?)\.?(?: Additional Details:|$)", text) or first.get("message"),
        "details": details,
        "codes": [
            {"code": code, "description": description.strip()}
            for code, description in re.findall(r"Code (\w+) - ([^.,]+)", details or "")
        ],
        "message": text or None,
    }


def history_row(claim: dict) -> dict:
    return {
        "claim_id": claim["Id"],
        "status": CLAIM_STATUS.get(claim.get("StatusCode"), claim.get("StatusCode")),
        "sequence": SEQUENCE.get(claim.get("InsurancePriority"), claim.get("InsurancePriority")),
        "created": claim.get("DateCreated"),
        "updated": claim.get("DateLastUpdated"),
        "patient": {
            "patient_id": claim.get("PatientId"),
            "first_name": claim.get("PatientFirstName"),
            "last_name": claim.get("PatientLastName"),
        },
        "payer": {
            "name": claim.get("InsuranceProviderName"),
            "payer_id": claim.get("InsuranceProviderPayerId"),
            "group_id": claim.get("InsuranceProviderGroupId"),
        },
        "secondary_payer": claim.get("SecondaryInsuranceProviderName"),
        "total_billed": claim.get("TotalAmountBilled"),
        "location": claim.get("Location"),
        "resubmission_of": claim.get("ResubmissionClaimId"),
        "comment": claim.get("Comment"),
        "line_items": [
            {
                "date": item.get("Date"),
                "service_code": item.get("ServiceCodeText"),
                "modifiers": [item[f"ProcedureModifierCode{i}"] for i in range(1, 5) if item.get(f"ProcedureModifierCode{i}")],
                "units": item.get("UnitsQuantity"),
                "amount_billed": item.get("AmountBilled"),
                "amount_to_insurance": item.get("AmountToInsurance"),
                "insurance_paid": item.get("InsuranceAmountPaid"),
                "patient_cost": item.get("PatientCost"),
                "prior_authorization": item.get("PriorAuthorizationCode"),
                "diagnosis": item.get("Diagnosis"),
                "clinician": {
                    "id": item.get("ClinicianId"),
                    "name": full_name(item.get("ClinicianFirstName"), item.get("ClinicianLastName")),
                },
                "calendar_entry_id": item.get("CalendarEntryId"),
            }
            for item in claim.get("LineItems") or []
        ],
    }


def insurer(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "name": raw.get("Name"),
        "payer_id": raw.get("PayerId"),
        "group_id": raw.get("ProviderGroupId"),
        "type": raw.get("Type"),
        "processing_type": raw.get("ProcessingType"),
    }


def insured(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "relationship": raw.get("RelationshipToInsured"),
        "first_name": raw.get("FirstName"),
        "last_name": raw.get("LastName"),
        "date_of_birth": day(raw.get("DateOfBirth")),
        "member_id": raw.get("MemberId"),
        "policy_group": raw.get("PolicyGroup"),
        "plan_name": raw.get("PlanName"),
        "employer": raw.get("EmployerName"),
    }


def service_line(service: dict) -> dict:
    code = service.get("ServiceCode") or {}
    return {
        "service_code": code.get("Code"),
        "units": code.get("Units"),
        "charge": round((service.get("InsuranceRate") or 0) + (service.get("PatientCost") or 0), 2),
        "insurance_rate": service.get("InsuranceRate"),
        "insurance_paid": service.get("InsuranceAmountPaid"),
        "patient_paid": service.get("PatientAmountPaid"),
        "patient_cost": service.get("PatientCost"),
        "modifiers": service.get("ModifierCodes") or [],
        "authorization": service.get("AuthorizationCode"),
        "payer_claim_control_number": service.get("PayerClaimControlNumber"),
        "errors": plain(service.get("Errors")),
        "warnings": plain(service.get("Warnings")),
    }


def claim_body(claim: dict) -> dict:
    patient = claim.get("Patient") or {}
    appointments = []
    for appt in claim.get("Appointments") or []:
        clinician = appt.get("Clinician") or {}
        appointments.append({
            "calendar_entry_id": appt.get("CalendarEntryId"),
            "billable_item_id": appt.get("BillableItemId"),
            "date": appt.get("Date"),
            "session_start": appt.get("SessionStartDate"),
            "session_end": appt.get("SessionEndDate"),
            "clinician": {
                "id": clinician.get("Id"),
                "name": full_name(clinician.get("FirstName"), clinician.get("LastName")),
                "npi": clinician.get("Npi"),
            },
            "prior_authorization": appt.get("PriorAuthorizationCode") or None,
            "services": [service_line(s) for s in appt.get("Services") or []],
            "errors": plain(appt.get("Errors")),
            "warnings": plain(appt.get("Warnings")),
        })
    return {
        "patient": {
            "patient_id": patient.get("Id"),
            "first_name": patient.get("FirstName"),
            "last_name": patient.get("LastName"),
            "date_of_birth": day(patient.get("DateOfBirth")),
            "sex": patient.get("Sex"),
            "phone": patient.get("Phone"),
            "address": {
                "address1": patient.get("Address1"),
                "address2": patient.get("Address2"),
                "city": patient.get("City"),
                "state": patient.get("State"),
                "postal_code": patient.get("PostalCode"),
            },
            "signature_on_file": patient.get("SignatureOnFile"),
            "first_appointment": patient.get("DateOfFirstAppointment"),
        },
        "sequence": SEQUENCE.get(claim.get("InsurancePriority"), claim.get("InsurancePriority")),
        "payer": insurer(claim.get("PrimaryInsurer")),
        "insured": insured(claim.get("PrimaryInsuredPerson")),
        "secondary_payer": insurer(claim.get("SecondaryInsurer")),
        "secondary_insured": insured(claim.get("SecondaryInsuredPerson")),
        "is_resubmission": claim.get("IsResubmission"),
        "place_of_service": claim.get("PlaceOfServiceCode"),
        "diagnoses": claim.get("Diagnoses") or [],
        "payment_method": claim.get("PaymentMethod"),
        "total_charge": round(sum(s["charge"] for a in appointments for s in a["services"]), 2),
        "appointments": appointments,
        "errors": plain(claim.get("Errors")),
        "warnings": plain(claim.get("Warnings")),
    }


def blocked_patient(raw: dict) -> dict:
    patient = raw.get("Patient") or {}
    return {
        "patient_id": patient.get("Id"),
        "first_name": patient.get("FirstName"),
        "last_name": patient.get("LastName"),
        "blocked_claims": raw.get("BlockedClaims"),
        "blocked_appointments": raw.get("BlockedAppts"),
        "errors": plain(raw.get("Errors")),
    }


async def search_history(session, form: dict, page_num: int, page_size: int) -> dict:
    return await session.post("/app/billing/claims/history/searchclaims.aspx?msg=3", {
        "insuranceprovider": None,
        "clinician": None,
        "claimstatus": None,
        "claimdaterangestart": None,
        "claimdaterangeend": None,
        "claimnumber": None,
        "pagenumber": page_num,
        "pagesize": page_size,
        "encryptedpatientidfilter": None,
        "claimsequence": None,
        **form,
    }, "/app/billing/claims/history/")


def history_form(payer_id=None, clinician_id=None, status=None, sequence=None, start_date=None, end_date=None) -> dict:
    return {
        "insuranceprovider": payer_id,
        "clinician": clinician_id,
        "claimstatus": status,
        "claimsequence": sequence,
        "claimdaterangestart": tn_date(start_date),
        "claimdaterangeend": tn_date(end_date),
    }


async def matching_claims(session, form: dict, keep) -> list[dict]:
    matches = []
    for tn_page in range(1, MAX_TN_PAGES + 1):
        data = await search_history(session, form, tn_page, TN_PAGE_SIZE)
        matches += [c for c in data.get("Resources") or [] if keep(c)]
        if tn_page * TN_PAGE_SIZE >= (data.get("Meta") or {}).get("Total", 0):
            break
    return matches


def claims_page(rows: list[dict], total: int, page_num: int, page_size: int) -> dict:
    pages = -(-total // page_size)
    return {
        "page": page_num,
        "page_size": page_size,
        "total": total,
        "page_count": pages,
        "has_next": page_num < pages,
        "claims": [history_row(c) for c in rows],
    }


async def get_claims(
    session,
    page_num: int = 1,
    page_size: int = 25,
    patient_id: int | None = None,
    status: int | None = None,
    payer_id: int | None = None,
    clinician_id: int | None = None,
    sequence: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    form = history_form(payer_id, clinician_id, status, sequence, start_date, end_date)
    if patient_id is None:
        data = await search_history(session, form, page_num, page_size)
        return claims_page(data.get("Resources") or [], (data.get("Meta") or {}).get("Total", 0), page_num, page_size)

    # TN's patient filter needs a session-encrypted id, so match on PatientId instead.
    matches = await matching_claims(session, form, lambda c: c.get("PatientId") == patient_id)
    start = (page_num - 1) * page_size
    return claims_page(matches[start:start + page_size], len(matches), page_num, page_size)


async def get_pending_claims(
    session,
    items: str = "newresubmit",
    patient_id: int | None = None,
    payer_id: int | None = None,
    clinician_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    form = {
        "startdate": tn_date(start_date) or "",
        "enddate": tn_date(end_date or date.today()),
        "itemstoshow": items,
    }
    if clinician_id:
        form["clinician"] = clinician_id
    if patient_id:
        form["patient"] = patient_id

    data = await session.post("/app/billing/claims/loadpendingitems.aspx?msg=4", form, "/app/billing/")
    claims = [claim_body(c) for c in data.get("Claims") or []]
    if payer_id:
        # TN's payer filter uses its own item id; the group id matches the rest of the API.
        claims = [c for c in claims if (c["payer"] or {}).get("group_id") == payer_id]
    return {
        "total_claims": len(claims),
        "total_appointments": sum(len(c["appointments"]) for c in claims),
        "patient_errors": [blocked_patient(p) for p in data.get("PatientsErrors") or []],
        "practice_errors": plain(data.get("PracticeErrors")),
        "claims": claims,
    }


async def find_claim(session, claim_id: int) -> dict | None:
    found = await search_history(session, {"claimnumber": claim_id}, 1, 1)
    return next((c for c in found.get("Resources") or [] if c.get("Id") == claim_id), None)


async def claim_events(session, claim_id: int) -> tuple[list, list]:
    activity = await session.post(
        "/app/billing/claims/history/getclaimactivity.aspx?msg=2", {"claim": claim_id}, view_path(claim_id),
    )
    rejections = await session.post(
        "/app/billing/claims/getclaimrejections.aspx?msg=3", {"claim": claim_id}, view_path(claim_id),
    )
    return activity or [], [rejection_item(r) for r in rejections or []]


async def load_comment(session, claim_id: int) -> dict:
    data = await session.post("/app/billing/claims/comments/loadclaimcomment.aspx", {"claim": claim_id},
                              view_path(claim_id))
    comment = (data or {}).get("Comment") or ""
    return {"claim_id": claim_id, "comment": comment, "version": version(comment)}


async def get_claim(session, claim_id: int) -> dict | None:
    claim = await find_claim(session, claim_id)
    if not claim:
        return None

    activity, rejections = await claim_events(session, claim_id)
    comment = await load_comment(session, claim_id)
    view = await session.post(
        "/app/billing/claims/loadclaimview.aspx?msg=4",
        {"claimid": claim_id, "practiceid": -1, "includeancillarydiagnoses": "false"},
        view_path(claim_id),
    )

    frequency = view.get("ClaimFrequency")
    return {
        **history_row(claim),
        "comment": comment["comment"],
        "comment_version": comment["version"],
        "claim_frequency": CLAIM_FREQUENCY.get(frequency, frequency),
        "payer_claim_control_number": view.get("PayerClaimControlNumber"),
        "claim_note": view.get("ClaimNote") or None,
        "clearinghouse": view.get("Clearinghouse"),
        "details": claim_body(view),
        "rejection": rejection(claim.get("StatusCode"), activity, rejections),
        "rejections": rejections,
        "activity": [
            {
                "date": event.get("Date"),
                "category": event.get("TypeCategory"),
                "type": event.get("TypeName"),
                "description": event.get("Description"),
                "is_exception": bool(event.get("IsException")),
                "user": event.get("SourceUser"),
            }
            for event in activity
        ],
    }


async def get_claim_filters(session) -> dict:
    data = await session.post(
        "/app/billing/claims/history/getclaimsearchfilters.aspx?msg=2",
        {"patient": None}, "/app/billing/claims/history/",
    )

    def payers(key: str, network: str, active: bool) -> list[dict]:
        return [
            {"payer_id": int(p["Id"]), "name": p["Name"], "network": network, "active": active}
            for p in data.get(key) or []
        ]

    def clinicians(key: str, active: bool) -> list[dict]:
        return [{"clinician_id": int(c["Id"]), "name": c["Name"], "active": active} for c in data.get(key) or []]

    return {
        "payers": (
            payers("InNetworkPayers", "in", True)
            + payers("OutOfNetworkPayers", "out", True)
            + payers("DisabledInNetworkPayers", "in", False)
            + payers("DisabledOutOfNetworkPayers", "out", False)
        ),
        "clinicians": clinicians("ActiveClinicians", True) + clinicians("InactiveClinicians", False),
    }
