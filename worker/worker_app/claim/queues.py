"""Billing item queues behind loadstatement.aspx: secondary, paper (CMS-1500), external, superbill, patient ledger."""

import json
from datetime import date

from worker_app.claim.claims import full_name, tn_date
from worker_app.patient.detail import day, for_patient, read_patient_data

# Every loadstatement filter starts from this and switches item groups on per queue.
BASE_FILTER = {
    "ClosedItems": False,
    "ItemsWithDirectPaymentCompleted": True,
    "InsurancePaymentStatusesToHide": None,
    "PaymentsWithBalancesAssigned": True,
    "InNetworkEdiInsuranceItems": False,
    "InNetworkExternalInsuranceItems": False,
    "InNetworkPaperInsuranceItems": False,
    "OutOfNetworkEdiInsuranceItems": False,
    "OutOfNetworkExternalInsuranceItems": False,
    "OutOfNetworkPaperInsuranceItems": False,
    "DirectPaymentItems": False,
    "Payments": False,
    "DirectPayments": False,
    "InNetworkInsurancePayments": False,
    "OutOfNetworkInsurancePayments": False,
    "IgnoredItems": False,
    "ItemsWithIncompleteNotes": False,
    "ItemsWithCompleteNotes": True,
    "AutomaticModifiers": True,
    "PlaceOfServiceCodes": True,
    "BillingStatementItemType": 1,
    "MinimumDate": "none",
    "MaximumDate": "none",
    "FilterDateRangeType": None,
    "ClinicianToShow": None,
    "ClinicianTypeToShow": None,
    "PatientsToShow": None,
    "LocationToShow": None,
    "LocationTypeToShow": None,
    "PrimaryInsurancePayerToShow": None,
    "SecondaryInsurancePayerToShow": None,
    "InsurancePayersToShow": None,
    "PrimaryInsuranceProviderToShow": None,
    "SecondaryInsuranceProviderToShow": None,
    "InsuranceProvidersToShow": None,
    "ServiceCodeToShow": None,
    "BillableItemsToShow": None,
    "BillableServicesToShow": None,
    "BillableItemsToExclude": None,
    "BillableServicesToExclude": None,
    "PaymentToExcludeFromBalances": None,
    "SinglePayerOnly": False,
    "ShowItemsWithSecondaryInsurancePoliciesOnly": False,
    "FilterItemsByPrimaryInsurancePoliciesOnly": False,
    "TransactionSearch": None,
    "IsCheckNumberSearch": False,
    "Paging": False,
    "PageSize": 100,
    "Page": 1,
    "SortDirection": "asc",
    "SortParameter": 0,
    "FilterMaximumDate": None,
    "ShowWriteOffStatementOnly": None,
    "ShowAutoPayOnly": False,
}

EDI = {"InNetworkEdiInsuranceItems": True, "OutOfNetworkEdiInsuranceItems": True}
PAPER = {"InNetworkPaperInsuranceItems": True, "OutOfNetworkPaperInsuranceItems": True}
EXTERNAL = {"InNetworkExternalInsuranceItems": True, "OutOfNetworkExternalInsuranceItems": True}
EXTERNAL_METHODS = {"external": EXTERNAL, "electronic": EDI, "paper": PAPER}

QUEUES = {
    "secondary": {
        **EDI,
        "InsurancePaymentStatusesToHide": [1, 2, 3, 4, 5, 6, 9, 10],
        "ShowItemsWithSecondaryInsurancePoliciesOnly": True,
        "Paging": True,
    },
    "paper": {
        **PAPER,
        "ClosedItems": True,
        "InsurancePaymentStatusesToHide": [3, 5, 6, 4, 9, 10],
        "Paging": True,
    },
    "external": {
        "InsurancePaymentStatusesToHide": [5, 6, 4, 3, 9, 10],
        "IsExternalSubmission": True,
    },
    "superbill": {
        **EXTERNAL,
        **PAPER,
        "ClosedItems": True,
        "InsurancePaymentStatusesToHide": [4],
        "DirectPaymentItems": True,
        "DirectPayments": True,
        "InNetworkInsurancePayments": True,
        "OutOfNetworkInsurancePayments": True,
        "ItemsWithIncompleteNotes": True,
    },
    "ledger": {
        **EDI,
        **PAPER,
        **EXTERNAL,
        "DirectPaymentItems": True,
        "Payments": True,
        "DirectPayments": True,
        "InNetworkInsurancePayments": True,
        "OutOfNetworkInsurancePayments": True,
        "ItemsWithIncompleteNotes": True,
        "BillingStatementItemType": None,
    },
}


def service_row(service: dict) -> dict:
    return {
        "service_code": service.get("ServiceCodeText"),
        "description": service.get("ServiceCodeDesc"),
        "modifiers": [m.strip() for m in service.get("ProcedureModifierCodes") or [] if m and m.strip()],
        "insurance_status": service.get("InsuranceStatus"),
        "amount_to_insurance": service.get("AmountToClaimToInsurance"),
        "insurance_rate": service.get("InsuranceRate"),
        "insurance_owed": service.get("InsuranceAmtOwed"),
        "insurance_paid": service.get("InsurancePaid"),
        "patient_rate": service.get("PatientRate"),
        "patient_owed": service.get("PatientAmtOwed"),
        "patient_paid": service.get("PatientPaid"),
    }


def billing_item(row: dict) -> dict:
    return {
        "billable_item_id": row.get("BillableItemID"),
        "calendar_entry_id": row.get("CalendarEntryID"),
        "date": row.get("Date"),
        "type": row.get("CalendarEntryTypeName"),
        "patient": {
            "patient_id": row.get("PatientID"),
            "first_name": row.get("PatientFirstName"),
            "last_name": row.get("PatientLastName"),
            "date_of_birth": day(row.get("PatientDOB")),
            "member_id": row.get("PatientInsuredsIdNumber"),
        },
        "note": {
            "note_id": row.get("NoteID"),
            "type": row.get("NoteTypeName"),
        } if row.get("NoteID") else None,
        "diagnosis": row.get("PrimaryDiagnosis"),
        "place_of_service": row.get("PlaceOfServiceCode"),
        "clinician": {
            "id": row.get("ClinicianID"),
            "name": full_name(row.get("ClinicianFirstName"), row.get("ClinicianLastName")),
            "npi": row.get("ClinicianNPI"),
        },
        "payer": {
            "name": row.get("InsurancePayerName"),
            "payer_id": row.get("InsurancePayerPayerID"),
            "group_id": row.get("InsuranceProviderGroupID"),
        } if row.get("InsurancePayerName") else None,
        "comments": row.get("Comments") or None,
        "services": [service_row(s) for s in row.get("Services") or []],
    }


async def load_statement(session, visible: dict, referer: str) -> dict:
    return await session.post(
        "/app/billing/statements/loadstatement.aspx?msg=4", {"visibleitems": json.dumps(visible)}, referer,
    )


async def get_queue(
    session,
    queue: str,
    method: str = "external",
    patient_id: int | None = None,
    clinician_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page_num: int = 1,
    page_size: int = 100,
) -> dict:
    visible = {
        **BASE_FILTER,
        **QUEUES[queue],
        "MinimumDate": tn_date(start_date) or "none",
        "MaximumDate": tn_date(end_date) or "none",
        "ClinicianToShow": clinician_id,
        "PatientsToShow": [patient_id] if patient_id else None,
        "PageSize": page_size,
        "Page": page_num,
    }
    if queue == "external":
        visible.update(EXTERNAL_METHODS[method])

    referer = "/app/patients/" if queue in ("superbill", "ledger") else "/app/billing/"
    data = await load_statement(session, visible, referer)
    rows = data.get("Items") or []
    result = {
        "items": [billing_item(r) for r in rows if r.get("RecordType") == 1],
    }
    if queue == "ledger":
        billing = await read_patient_data(session, [for_patient("ReadBillingSettingsAggregate", patient_id)])
        totals = billing.get("ReadBillingSettingsAggregate") or {}
        result = {
            "patient_id": patient_id,
            "balance_owed": totals.get("PatientBalanceOwed"),
            "unassigned_credit": totals.get("PatientUnassignedCredits"),
            **result,
            "payments": [r for r in rows if r.get("RecordType") != 1],
        }
    if visible["Paging"]:
        pages = data.get("TotalPages") or 0
        result = {"page": page_num, "page_size": page_size, "page_count": pages, "has_next": page_num < pages, **result}
    return {"total_items": len(result["items"]), **result}


async def get_claim_summary(session) -> dict:
    data = await session.post("/app/billing/claims/loadpracticeclaimstatistics.aspx?msg=2", {}, "/app/billing/")
    stats = data.get("PracticeClaimStatistics") or {}
    return {
        "rejected_claims": stats.get("RejectedClaims"),
        "primary_claims_to_submit": stats.get("PrimaryEDIClaimsToSubmit"),
        "secondary_claims_to_submit": stats.get("SecondaryEDIClaimsToSubmit"),
        "external_claims_to_mark": stats.get("EDIClaimsToSubmitExternally"),
        "paper_claims_to_print": stats.get("EDIClaimsToSubmitPaper"),
        "new_eligibility_reports": stats.get("EligibilityReports"),
        "new_eras": stats.get("NewERAs"),
    }
