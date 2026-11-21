"""Rejected claims: the review list, Mark as Reviewed and the biller comment."""

import html
import re
from datetime import date

from worker_app.claim.claims import (
    CLAIM_STATUS,
    REJECTED,
    REJECTED_NEW,
    claim_events,
    claims_page,
    find_claim,
    history_form,
    history_row,
    load_comment,
    matching_claims,
    rejection,
    view_path,
)
from worker_app.tn.client import SessionExpired, TherapyNotesError, ValidationFailed, find
from worker_app.tn.version import check_version, verify

REVIEW_BUTTON = "ctl00$BodyContent$ButtonMarkAsReviewed"
COMMENT_MAX = 1000


async def get_rejections(
    session,
    reviewed: bool = False,
    page_num: int = 1,
    page_size: int = 25,
    patient_id: int | None = None,
    payer_id: int | None = None,
    clinician_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    status = REJECTED if reviewed else REJECTED_NEW
    form = history_form(payer_id, clinician_id, status, start_date=start_date, end_date=end_date)
    # TN's "Rejected" filter also returns the new rejections, so check each claim's own status.
    matches = await matching_claims(
        session, form, lambda c: c.get("StatusCode") == status and patient_id in (None, c.get("PatientId")),
    )
    start = (page_num - 1) * page_size
    data = claims_page(matches[start:start + page_size], len(matches), page_num, page_size)
    for claim in data["claims"]:
        activity, items = await claim_events(session, claim["claim_id"])
        claim["rejection"] = rejection(status, activity, items)
    return data


async def review_form(session, claim_id: int) -> dict:
    response = await session.request("GET", view_path(claim_id), headers={"Referer": f"{session.base}/app/billing/"})
    if response.status_code in (301, 302, 401):
        raise SessionExpired("TherapyNotes did not open the claim page")
    if response.status_code != 200:
        raise TherapyNotesError(f"TherapyNotes returned {response.status_code} for claim {claim_id}")
    if REVIEW_BUTTON not in response.text:
        raise TherapyNotesError(f"TherapyNotes does not offer Mark as Reviewed on claim {claim_id}")
    form = {}
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', response.text):
        name = find(r'name="([^"]+)"', tag)
        if name:
            form[name] = html.unescape(find(r'value="([^"]*)"', tag))
    return form


async def post_review(session, claim_id: int) -> None:
    # Mark as Reviewed is a WebForms button, so post the page back the way the browser does.
    form = await review_form(session, claim_id)
    response = await session.request(
        "POST", view_path(claim_id), data={**form, REVIEW_BUTTON: "Mark as Reviewed"},
        headers={"Origin": session.base, "Referer": f"{session.base}{view_path(claim_id)}"},
    )
    if "/login" in response.headers.get("location", "").lower() or response.status_code == 401:
        raise SessionExpired("TherapyNotes session expired while marking the claim as reviewed")
    if response.status_code not in (200, 302):
        raise TherapyNotesError(f"TherapyNotes returned {response.status_code} for Mark as Reviewed")


async def mark_reviewed(session, claim_id: int) -> dict | None:
    claim = await find_claim(session, claim_id)
    if not claim:
        return None
    if claim.get("StatusCode") != REJECTED_NEW:
        status = CLAIM_STATUS.get(claim.get("StatusCode"), claim.get("StatusCode"))
        raise ValidationFailed([f"Claim {claim_id} is '{status}'; only 'Rejected - New' claims can be marked as reviewed"])

    try:
        await post_review(session, claim_id)
    except SessionExpired:
        await session.relogin()
        await post_review(session, claim_id)

    claim = await find_claim(session, claim_id)
    if not claim or claim.get("StatusCode") != REJECTED:
        raise TherapyNotesError(f"TherapyNotes did not mark claim {claim_id} as reviewed. Check it in TherapyNotes")
    return {"message": "Claim marked as reviewed and verified", **history_row(claim)}


async def save_comment(session, claim_id: int, comment: str, expected: str) -> dict | None:
    if not await find_claim(session, claim_id):
        return None
    what = f"Comment of claim {claim_id}"
    check_version(expected, await load_comment(session, claim_id), what)

    # TN's own editor trims the comment and cuts it at 1000 characters.
    comment = comment.strip()[:COMMENT_MAX]
    result = await session.post("/app/billing/claims/comments/editclaimcomment.aspx",
                                {"claim": claim_id, "comment": comment}, view_path(claim_id))
    if not (result or {}).get("Successful"):
        raise ValidationFailed(["TherapyNotes did not save the comment"])

    saved = await load_comment(session, claim_id)
    verify(what, {"comment": comment}, saved, saved)
    return {"message": "Comment saved and verified", **saved}
