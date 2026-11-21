"""Single-patient data (Info and Billing Settings tabs) via patientdataservice.aspx."""

import json

PHONE_TYPES = {1: "Home", 2: "Work", 3: "Mobile", 4: "Other"}
TIME_ZONES = {
    -300: "ET - Eastern Time",
    -360: "CT - Central Time",
    -420: "MT - Mountain Time",
    -480: "PT - Pacific Time",
    -540: "AKT - Alaska Time",
    -600: "HT - Hawaii Time",
}


def lookup(name: str) -> dict:
    return {"Name": name, "Data": {}}


def for_patient(name: str, patient_id: int, **extra) -> dict:
    return {"Name": name, "Data": {"PatientId": patient_id, **extra}, "Key": f"{name}{patient_id}"}


async def read_patient_data(session, payloads: list[dict]) -> dict:
    items = await session.post(
        "/app/patients/edit/patientdataservice.aspx?msg=1", {"payloads": json.dumps(payloads)}, "/app/patients/",
    )
    return {item["Name"]: item.get("Data") for item in items}


def day(value: str | None) -> str | None:
    return value[:10] if value else None


def names_by_id(items: list[dict] | None, id_key: str = "ID", name_key: str = "Name") -> dict:
    return {item[id_key]: item[name_key] for item in items or []}


def selected(items: list | None, options: dict, id_key: str) -> list[str]:
    ids = [item.get(id_key) if isinstance(item, dict) else item for item in items or []]
    return [options.get(i, str(i)) for i in ids]


def time_zone(offset: int | None, dst: bool | None) -> str | None:
    if offset is None:
        return None
    label = TIME_ZONES.get(offset, "UTC offset")
    hours = offset // 60
    return f"{label}, UTC{hours:+d}{' with DST' if dst else ''}"


def address(raw: dict | None) -> dict:
    raw = raw or {}
    return {
        "address1": raw.get("Address1", ""),
        "address2": raw.get("Address2", ""),
        "city": raw.get("City", ""),
        "state": raw.get("State", ""),
        "postal_code": raw.get("PostalCode", ""),
    }


async def get_patient_info(session, patient_id: int) -> dict | None:
    data = await read_patient_data(session, [
        lookup("ReadDemographics"),
        lookup("ReadLanguages"),
        lookup("ReadRaceDemographics"),
        lookup("ReadEthnicities"),
        for_patient("ReadPatient", patient_id),
        for_patient("ReadPatientPortalSettings", patient_id, RedactRestrictedPatientData=True),
        for_patient("ReadPatientLanguages", patient_id),
        for_patient("ReadPatientRaceDemographics", patient_id),
        for_patient("ReadPatientEthnicities", patient_id),
        for_patient("ReadPatientStatistics", patient_id),
    ])
    patient = data.get("ReadPatient")
    if not patient:
        return None

    demo = data.get("ReadDemographics") or {}
    portal = data.get("ReadPatientPortalSettings") or {}
    stats = data.get("ReadPatientStatistics") or {}

    offset = patient.get("TimeZoneOffsetMinutes")
    dst = patient.get("TimeZoneIsDST")
    if offset is None:
        offset = portal.get("PracticeTimeZoneOffset")
        dst = portal.get("IsObservingDaylightSavings")

    phones = []
    for code, kind in PHONE_TYPES.items():
        number = patient.get(f"{kind}Phone")
        if number:
            phones.append({
                "type": kind,
                "number": number,
                "messages_ok": bool(patient.get(f"{kind}PhoneMessages")),
                "preferred": patient.get("PreferredPhone") == code,
            })

    pcp = patient.get("PcpConsent")

    return {
        "patient_id": patient["PatientId"],
        "name": {
            "first": patient.get("FirstName", ""),
            "middle": patient.get("MiddleName", ""),
            "last": patient.get("LastName", ""),
            "suffix": patient.get("Suffix", ""),
            "nickname": patient.get("Nickname", ""),
            "display": patient.get("DisplayName", ""),
        },
        "date_of_birth": day(patient.get("DateOfBirth")),
        "address": address(patient.get("Address")),
        "time_zone": time_zone(offset, dst),
        "phones": phones,
        "email": patient.get("Email", ""),
        "administrative_sex": names_by_id(demo.get("AdministrativeSex")).get(patient.get("Gender")),
        "gender_identity": names_by_id(demo.get("GenderIdentity")).get(patient.get("GenderIdentity")),
        "other_gender_identity": patient.get("OtherGenderIdentity", ""),
        "sexual_orientation": names_by_id(demo.get("SexualOrientation")).get(patient.get("SexualOrientation")),
        "other_sexual_orientation": patient.get("OtherSexualOrientation", ""),
        "preferred_pronouns": patient.get("PreferredPronouns", ""),
        "race": selected(
            data.get("ReadPatientRaceDemographics"),
            names_by_id(data.get("ReadRaceDemographics"), "Value"),
            "Value",
        ),
        "ethnicity": selected(
            data.get("ReadPatientEthnicities"),
            names_by_id(data.get("ReadEthnicities"), "Value"),
            "Value",
        ),
        "languages": selected(
            data.get("ReadPatientLanguages"),
            names_by_id(data.get("ReadLanguages"), "LanguageId", "LanguageName"),
            "LanguageId",
        ),
        "smoking_status": names_by_id(demo.get("SmokerStatus")).get(patient.get("SmokerStatus")),
        "marital_status": names_by_id(demo.get("MaritalStatus")).get(patient.get("MaritalStatus")),
        "employment_status": names_by_id(demo.get("EmploymentStatus")).get(patient.get("EmploymentStatus")),
        "religious_affiliation": names_by_id(demo.get("ReligiousAffiliations")).get(
            patient.get("ReligiousAffiliation")
        ),
        "hipaa_npp_signed": bool(patient.get("Hipaa")),
        "pcp_release": "Not set" if pcp in (None, -1) else pcp,
        "pad": {"date": day(patient.get("PadDate")), "form": patient.get("PadForm", "")},
        "last_appointment": day(stats.get("MostRecentPastAppointmentDate")),
        "next_appointment": day(stats.get("NextFutureAppointmentDate")),
    }


async def read_billing_settings(session, patient_id: int) -> dict:
    return await read_patient_data(session, [
        for_patient("ReadBillingSettingsAggregate", patient_id),
        for_patient("ReadInsuranceProviders", patient_id),
        lookup("ReadInsuredRelationships"),
        lookup("ReadServiceCodes"),
        for_patient("ReadPatientContacts", patient_id),
        for_patient("ReadPatientCashRates", patient_id),
        for_patient("ReadPatientAssignedClinicians", patient_id),
        for_patient(
            "ReadInsurancePolicies",
            patient_id,
            IncludeInsuranceProviderNames=False,
            IncludeBillableServiceStatuses=True,
        ),
        for_patient("ReadPriorAuthorizations", patient_id, IncludeUsedOnClaimStatus=True),
    ])


async def get_patient_billing(session, patient_id: int) -> dict | None:
    data = await read_billing_settings(session, patient_id)
    aggregate = data.get("ReadBillingSettingsAggregate") or {}
    patient = aggregate.get("Patient")
    if not patient:
        return None

    codes = {str(c["ID"]): c for c in data.get("ReadServiceCodes") or []}
    cash_rates = []
    for rate in data.get("ReadPatientCashRates") or []:
        code = codes.get(str(rate.get("ServiceCode")), {})
        cash_rates.append({
            "service_code": code.get("Code"),
            "description": code.get("Description"),
            "rate": rate.get("Rate"),
        })

    contacts = data.get("ReadPatientContacts")

    return {
        "patient_id": patient["PatientId"],
        "account_number": patient.get("AccountNumber", ""),
        "billing_comments": patient.get("BillingNotes", ""),
        "balance_owed": aggregate.get("PatientBalanceOwed"),
        "unassigned_credits": aggregate.get("PatientUnassignedCredits"),
        "responsible_party": "The Patient" if not contacts else contacts,
        "cash_rates": cash_rates,
        "additional_claim_info": {
            "referring_provider": {
                "first_name": patient.get("ReferringProviderFirstName", ""),
                "last_name": patient.get("ReferringProviderLastName", ""),
                "npi": patient.get("ReferringProviderNpi", ""),
            },
            "referral_number": patient.get("ReferralNumber", ""),
            "referral_date": day(patient.get("ReferralDate")),
            "referral_expiration_date": day(patient.get("ReferralExpirationDate")),
            "epsdt_referral": patient.get("IsEpsdtReferral", False),
            "date_of_current_illness": day(patient.get("DateOfCurrentIllness")),
            "date_of_similar_illness": day(patient.get("DateOfSimilarIllness")),
            "unable_to_work": {
                "start": day(patient.get("UnableToWorkStartDate")),
                "end": day(patient.get("UnableToWorkEndDate")),
            },
            "hospitalization": {
                "start": day(patient.get("HospitalizationStartDate")),
                "end": day(patient.get("HospitalizationEndDate")),
            },
            "condition_related_to": {
                "employment": patient.get("ConditionFromEmployment", False),
                "auto_accident": patient.get("ConditionFromAutoAccident", False),
                "auto_accident_state": patient.get("RelatedAutoAccidentState", ""),
                "other_accident": patient.get("ConditionFromOtherAccident", False),
            },
        },
        "prior_authorizations": data.get("ReadPriorAuthorizations") or [],
        "assigned_clinician_ids": [
            c["ClinicianId"] for c in data.get("ReadPatientAssignedClinicians") or []
        ],
    }
