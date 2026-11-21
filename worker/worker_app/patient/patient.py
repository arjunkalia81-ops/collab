"""TherapyNotes patient list via loadpatientlist.aspx."""

import re


def patient_row(row: dict) -> dict:
    last_appt = row.get("LastAppt") or {}
    return {
        "patient_id": row["ID"],
        "first_name": row.get("FirstName", ""),
        "middle_name": row.get("MiddleName", ""),
        "last_name": row.get("LastName", ""),
        "nickname": row.get("Nickname", ""),
        "suffix": row.get("Suffix", ""),
        "date_of_birth": (row.get("DOB") or "")[:10] or None,
        "phone": row.get("Phone", ""),
        "insurance": {
            "name": row.get("InsuranceProvider"),
            "sequence": row.get("Sequence"),
            "group_id": row.get("InsuranceProviderGroupID"),
        },
        "clinicians": [
            {"id": c.get("ClinicianID"), "name": f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip()}
            for c in row.get("Clinicians") or []
        ],
        "last_appointment": (last_appt.get("Date") or "")[:10] or None,
        "to_do_items": row.get("ToDoListItems", 0),
        "has_portal_account": row.get("UserHasPortalAccount", False),
    }


async def get_patient_list(
    session,
    page_num: int = 1,
    status: str = "active",
    search: str = "",
    assignment: str = "any",
    sort: str = "name",
    ascending: bool = True,
) -> dict:
    data = await session.post("/app/patients/loadpatientlist.aspx?msg=1", {
        "manual": "true" if search or assignment != "any" or status != "active" else "false",
        "page": page_num,
        "sortparameter": sort,
        "sortascending": "true" if ascending else "false",
        "patient": search,
        "assignment": assignment,
        "status": status,
    }, "/app/patients/")

    pages = data.get("PageCount", 0)
    # TN ignores an out-of-range page and returns the last one instead.
    rows = (data.get("Matches") or []) if page_num <= max(pages, 1) else []
    description = re.sub(r"<[^>]+>", "", data.get("Instructions") or "")
    return {
        "page": page_num,
        "page_count": pages,
        "has_next": page_num < pages,
        "description": " ".join(description.split()),
        "patients": [patient_row(row) for row in rows],
    }
