"""Insurance policies on the Billing Settings tab: read with edit options, add and update."""

import json
import uuid
from datetime import date

from worker_app.patient.detail import day, for_patient, lookup, read_patient_data
from worker_app.tn.client import TherapyNotesError, ValidationFailed
from worker_app.tn.version import NotVerified, check_version, verify, version

SERVICE = "/app/patients/edit/patientdataservice.aspx?msg=1"
ADVICE_ERROR = 3

PRIORITIES = {1: "Primary", 2: "Secondary", 3: "Tertiary", 4: "Quaternary"}
APPOINTMENT_LIMITS = {0: "Unknown", 1: "Unlimited", 2: "Number"}
GENDERS = {0: "Male", 1: "Female"}
MSP_TYPES = {
    1: "Working Aged Beneficiary or Spouse with Employer Group Health Plan",
    2: "End-Stage Renal Disease Beneficiary in the Mandated Coordination Period with an Employer's Group Health Plan",
    3: "No-fault Insurance including Auto is Primary",
    4: "Worker's Compensation",
    5: "Public Health Service (PHS) or Other Federal Agency",
    6: "Black Lung",
    7: "Veteran's Administration",
    8: "Disabled Beneficiary Under Age 65 with Large Group Health Plan (LGHP)",
    9: "Other Liability Insurance is Primary",
}

FIELDS = {
    "payer_setting_id": "InsuranceProviderSettingId",
    "priority": "Priority",
    "msp_type": "SecondaryInsuranceTypeCode",
    "comments": "Comments",
    "signature_on_file": "SignatureOnFile",
    "copay": "Copay",
    "deductible": "Deductible",
    "appointment_limit": "MaxAppointmentsPerYearType",
    "appointments_per_year": "MaxAppointmentsPerYear",
    "member_id": "InsuredsIdNumber",
    "policy_group": "InsuredsPolicyGroup",
    "employer": "InsuredsEmployerName",
    "plan_name": "InsuredsPlanName",
    "relationship": "RelationshipToInsured",
}
DATE_FIELDS = {
    "coverage_start": "CoverageStart",
    "coverage_end": "CoverageEnd",
    "appointments_year_end": "MaxAppointmentsPerYearEnding",
}
INSURED_FIELDS = {
    "first_name": "InsuredsFirstName",
    "middle_name": "InsuredsMiddleName",
    "last_name": "InsuredsLastName",
    "suffix": "InsuredsNameSuffix",
    "gender": "InsuredsGender",
    "phone": "InsuredsPhone",
}
ADDRESS_FIELDS = {
    "address1": "InsuredsAddress1",
    "address2": "InsuredsAddress2",
    "city": "InsuredsCity",
    "state": "InsuredsState",
    "postal_code": "InsuredsPostalCode",
}
LABELS = {"priority_name", "payer_name", "payer_id", "policy_holder"}


def options(items: dict) -> list[dict]:
    return [{"value": value, "label": label} for value, label in items.items()]


def read_policies(patient_id: int) -> dict:
    return for_patient("ReadInsurancePolicies", patient_id, IncludeInsuranceProviderNames=False,
                       IncludeBillableServiceStatuses=True)


def policy_view(policy: dict, payers: dict, relationships: dict) -> dict:
    payer = payers.get(policy.get("InsuranceProviderSettingId"), {})
    relation = policy.get("RelationshipToInsured")
    item = {
        "policy_id": policy.get("Id"),
        "priority": policy.get("Priority"),
        "priority_name": PRIORITIES.get(policy.get("Priority")),
        "payer_setting_id": policy.get("InsuranceProviderSettingId"),
        "payer_name": payer.get("Name") or policy.get("InsuranceProviderSettingName"),
        "payer_id": payer.get("PayerId"),
        "msp_type": policy.get("SecondaryInsuranceTypeCode"),
        "comments": policy.get("Comments"),
        "signature_on_file": policy.get("SignatureOnFile"),
        "coverage_start": day(policy.get("CoverageStart")),
        "coverage_end": day(policy.get("CoverageEnd")),
        "copay": policy.get("Copay"),
        "deductible": policy.get("Deductible"),
        "appointment_limit": policy.get("MaxAppointmentsPerYearType"),
        "appointments_per_year": policy.get("MaxAppointmentsPerYear"),
        "appointments_year_end": day(policy.get("MaxAppointmentsPerYearEnding")),
        "member_id": policy.get("InsuredsIdNumber"),
        "policy_group": policy.get("InsuredsPolicyGroup"),
        "employer": policy.get("InsuredsEmployerName"),
        "plan_name": policy.get("InsuredsPlanName"),
        "relationship": relation,
        "policy_holder": relationships.get(relation, relation),
        "insured": None,
    }
    if relation != 0:
        item["insured"] = {
            **{key: policy.get(tn) for key, tn in INSURED_FIELDS.items()},
            "date_of_birth": day(policy.get("InsuredsDob")),
            "address": {key: policy.get(tn) or "" for key, tn in ADDRESS_FIELDS.items()},
        }
        if item["insured"]["address"]["state"] == "-1":
            item["insured"]["address"]["state"] = ""
    item["version"] = version({k: v for k, v in item.items() if k not in LABELS})
    return item


async def get_patient_insurance(session, patient_id: int) -> dict | None:
    data = await read_patient_data(session, [
        for_patient("ReadBillingSettingsAggregate", patient_id),
        for_patient("ReadInsuranceProviders", patient_id),
        lookup("ReadInsuredRelationships"),
        read_policies(patient_id),
    ])
    patient = (data.get("ReadBillingSettingsAggregate") or {}).get("Patient")
    if not patient:
        return None

    payers = {p["Id"]: p for p in data.get("ReadInsuranceProviders") or []}
    relationships = {r["Value"]: r["Description"] for r in data.get("ReadInsuredRelationships") or []}
    assignment = patient.get("AcceptPaymentAssignment")
    return {
        "patient_id": patient["PatientId"],
        "policies": [policy_view(p, payers, relationships) for p in data.get("ReadInsurancePolicies") or []],
        "out_of_network_payments": (
            "Use Practice Default Assignment Settings" if assignment is None else
            "Provider" if assignment else "Patient"
        ),
        "options": {
            "payers": [
                {"payer_setting_id": p["Id"], "name": p.get("Name"), "payer_id": p.get("PayerId"),
                 "in_network": p.get("InNetwork")}
                for p in payers.values()
            ],
            "priorities": options(PRIORITIES),
            "relationships": options(relationships),
            "msp_types": options(MSP_TYPES),
            "appointment_limits": options(APPOINTMENT_LIMITS),
            "genders": options(GENDERS),
        },
    }


def tn_date(value: str | None) -> str | None:
    return f"{value}T00:00:00" if value else None


def short_date(value: str | None) -> str:
    if not value:
        return ""
    d = date.fromisoformat(value[:10])
    return f"{d.month}/{d.day}/{d.year}"


def money(value) -> str:
    return "" if value is None else f"{float(value):.2f}"


def apply_changes(policy: dict, changes: dict) -> None:
    for key, tn in FIELDS.items():
        if key in changes:
            policy[tn] = changes[key]
    for key, tn in DATE_FIELDS.items():
        if key in changes:
            policy[tn] = tn_date(changes[key])

    insured = changes.get("insured")
    if insured:
        for key, tn in INSURED_FIELDS.items():
            if key in insured:
                policy[tn] = insured[key]
        if "date_of_birth" in insured:
            policy["InsuredsDob"] = tn_date(insured["date_of_birth"])
        for key, tn in ADDRESS_FIELDS.items():
            if key in (insured.get("address") or {}):
                policy[tn] = insured["address"][key]
        if not policy.get("InsuredsState"):
            policy["InsuredsState"] = "-1"


def wrap(policy: dict) -> dict:
    # TN parses these text copies of the form inputs alongside the typed values.
    year_end = policy.get("MaxAppointmentsPerYearEnding")
    msp = policy.get("SecondaryInsuranceTypeCode")
    return {
        "InsurancePolicy": {**policy, "InsurancePolicyIdentifier": str(uuid.uuid4())},
        "InsuredsDobRequestString": short_date(policy.get("InsuredsDob")),
        "InsuredsCoverageStartRequestString": short_date(policy.get("CoverageStart")),
        "InsuredsCoverageEndRequestString": short_date(policy.get("CoverageEnd")),
        "InsuredsCopayRequestString": money(policy.get("Copay")),
        "InsuredsDeductibleRequestString": money(policy.get("Deductible")),
        "MaxAppointmentsPerYearEndingString": short_date(year_end).rsplit("/", 1)[0] if year_end else "",
        "SecondaryInsuranceTypeCodeRequestInt": -1 if msp is None else msp,
    }


def new_policy(patient: dict, policies: list[dict]) -> dict:
    used = {p.get("Priority") for p in policies if p.get("Status") == 1}
    return {
        "Status": 1,
        "Priority": next((n for n in PRIORITIES if n not in used), 1),
        "SignatureOnFile": False,
        "RelationshipToInsured": 0,
        "MaxAppointmentsPerYearType": 0,
        "PracticeId": patient["PracticeId"],
        "PatientId": patient["PatientId"],
        "HasSubmittedBillableServices": False,
        "InsuredsAddress1": "",
        "InsuredsAddress2": "",
        "InsuredsCity": "",
        "InsuredsState": "-1",
        "InsuredsPostalCode": "",
    }


async def save_policy(
    session, patient_id: int, changes: dict, policy_id: int | None = None, expected: str | None = None,
) -> dict | None:
    """Add a policy (no policy_id) or update one whose version is still `expected`.
    Returns None if the patient or policy doesn't exist."""
    data = await read_patient_data(session, [
        for_patient("ReadBillingSettingsAggregate", patient_id),
        for_patient("ReadPatient", patient_id),
        for_patient("ReadPatientCashRates", patient_id),
        for_patient("ReadInsuranceProviders", patient_id),
        for_patient("ReadPriorAuthorizations", patient_id, IncludeUsedOnClaimStatus=True),
        for_patient("ReadPatientCreditCards", patient_id),
        read_policies(patient_id),
    ])
    patient = data.get("ReadPatient")
    aggregate = data.get("ReadBillingSettingsAggregate")
    if not patient or not aggregate:
        return None

    policies = data.get("ReadInsurancePolicies") or []
    existing = {p.get("Id") for p in policies}
    if policy_id is None:
        policy = new_policy(patient, policies)
        policies.append(policy)
    else:
        policy = next((p for p in policies if p.get("Id") == policy_id), None)
        if not policy:
            return None

    payers = {p["Id"]: p for p in data.get("ReadInsuranceProviders") or []}
    if policy_id is not None:
        check_version(expected, policy_view(policy, payers, {}), f"Policy {policy_id}")
    payer_id = changes.get("payer_setting_id", policy.get("InsuranceProviderSettingId"))
    if payer_id not in payers:
        raise ValidationFailed([f"Payer {payer_id} is not set up in this practice"])
    apply_changes(policy, changes)
    policy["InsuranceProviderSettingName"] = payers[payer_id].get("Name")

    # TN fails the save without the aggregate, cash rates, prior authorizations and credit cards,
    # so they all go back exactly as read.
    actions = [
        {"Name": "UpdateBillingSettingsAggregate",
         "Data": {"BillingSettingsAggregate": {**aggregate, "Patient": patient}, "PatientId": patient_id}},
        {"Name": "UpdatePatientCashRates",
         "Data": {"PatientCashRates": data.get("ReadPatientCashRates") or [], "PatientId": patient_id}},
        {"Name": "UpdateInsurancePolicies",
         "Data": {"PracticeId": patient["PracticeId"], "PatientId": patient_id, "UserId": session.user_id,
                  "InsurancePolicies": [wrap(p) for p in policies]}},
        {"Name": "UpdatePriorAuthorizations",
         "Data": {"PracticeId": patient["PracticeId"], "PriorAuthorizations": data.get("ReadPriorAuthorizations") or []}},
        {"Name": "UpdatePatientCreditCards",
         "Data": {"PatientCreditCards": data.get("ReadPatientCreditCards") or [], "PatientId": patient_id,
                  "IsAutoPayChangeOnly": False}},
    ]
    payload = [{"Name": "UpdateBillingSettingsActions", "Data": {"CommandPayloads": actions}, "Key": patient_id}]
    items = await session.post(SERVICE, {"payloads": json.dumps(payload)}, "/app/patients/")
    if not isinstance(items, list):
        raise TherapyNotesError("TherapyNotes returned an unexpected save response")

    # TN reports both errors (Type 3) and the "settings have been updated" notice as form advice.
    advice = next((i.get("Data") for i in items if i.get("Name") == "PatientFormAdvice"), None) or []
    errors = [a.get("Message") for a in advice if a.get("Type") == ADVICE_ERROR]
    if errors:
        raise ValidationFailed(errors)
    result = await get_patient_insurance(session, patient_id)
    if not result:
        return None
    saved = next((p for p in result["policies"]
                  if p["policy_id"] == policy_id or (policy_id is None and p["policy_id"] not in existing)), None)
    what = f"Policy {policy_id}" if policy_id else "New policy"
    if not saved:
        raise NotVerified(what, ["policy"], result)
    wanted = flat(changes)
    if saved["relationship"] == 0:
        # TN ignores insured details when the patient is the policy holder.
        wanted = {k: v for k, v in wanted.items() if not k.startswith("insured.")}
    verify(what, wanted, flat(saved), result)
    notices = [a.get("Message") for a in advice if a.get("Type") != ADVICE_ERROR and a.get("Message")]
    return {"message": " ".join(notices) or "Insurance saved and verified", **result}


def flat(data: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out
