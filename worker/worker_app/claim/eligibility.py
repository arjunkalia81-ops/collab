"""Eligibility verification history (read-only)."""

import json
from datetime import datetime, time

from worker_app.claim.claims import full_name
from worker_app.patient.detail import day

ELIGIBILITY_STATUS = {0: "Pending", 1: "Error", 2: "Active", 3: "Inactive", 4: "Rejected", 5: "Deleted"}


def report_row(report: dict) -> dict:
    return {
        "transaction_number": report.get("TransactionNumber"),
        "transaction_date": report.get("TransactionDate"),
        "clearinghouse": report.get("Clearinghouse"),
        "patient": {
            "patient_id": report.get("PatientId"),
            "first_name": report.get("PatientFirstName"),
            "last_name": report.get("PatientLastName"),
        },
        "payer": {"name": report.get("PayerName"), "group_id": report.get("PayerGroupId")},
        "clinician": {
            "id": report.get("ClinicianId"),
            "name": full_name(report.get("ClinicianFirstName"), report.get("ClinicianLastName")),
        },
        "location": report.get("LocationName"),
        "date_of_coverage": day(report.get("DateOfCoverage")),
        "status": ELIGIBILITY_STATUS.get(report.get("Status"), report.get("Status")),
        "reviewed": report.get("Reviewed"),
    }


async def get_eligibility(
    session,
    patient_id: int | None = None,
    status: int | None = None,
    start_date=None,
    end_date=None,
    page_num: int = 1,
    page_size: int = 50,
) -> dict:
    request = {"PageNumber": page_num, "PageSize": page_size, "SortAscending": False, "SortParameter": 0}
    if patient_id:
        request["PatientId"] = patient_id
    if status is not None:
        request["VerificationHistoryStatusFilter"] = status
    if start_date:
        request["StartTransactionDate"] = datetime.combine(start_date, time.min).isoformat() + "Z"
    if end_date:
        request["EndTransactionDate"] = datetime.combine(end_date, time.max).isoformat(timespec="seconds") + "Z"

    data = await session.post(
        "/app/billing/eligibility/api/eligibilitysearch.aspx?msg=3",
        {"request": json.dumps(request)}, "/app/billing/eligibility/history/",
    )
    pages = data.get("PageCount") or 0
    return {
        "page": page_num,
        "page_size": page_size,
        "page_count": pages,
        "has_next": page_num < pages,
        "reports": [report_row(r) for r in data.get("Reports") or []],
    }
