"""FastAPI app — client JWT only; TherapyNotes sessions live in the Windows worker."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import appointments as appointment_routes
from app.api.v1 import claims as claim_routes
from app.api.v1 import files as file_routes
from app.api.v1 import insurance as insurance_routes
from app.api.v1 import login as login_routes
from app.api.v1 import notes as note_routes
from app.api.v1 import patients as patient_routes
from app.core.config import settings
from app.worker_client import worker_health

DESCRIPTION = """
Reads and updates TherapyNotes data for Vero.

**How to try it**

1. `POST /api/v1/login` with your TherapyNotes practice code, username and password.
2. Click **Authorize** and paste the `access_token`.
3. Call any API below. When the access token expires, get a new one with `POST /api/v1/refresh`.

**Saving changes**

Insurance, note and modifier records come with a `version`. Send it back when you save.
If someone changed the record in TherapyNotes in the meantime, nothing is saved and you get a 409.
"""

TAGS = [
    {"name": "Auth", "description": "Log in, keep the session alive, log out."},
    {"name": "Patients", "description": "Patient list and the patient's Info, Billing and Ledger tabs."},
    {"name": "Insurance", "description": "View, add and update a patient's insurance policies."},
    {"name": "Notes", "description": "Service codes (CPT), units and diagnoses. Signed notes, and add-on units on a draft note header."},
    {"name": "Documents", "description": "Upload, download and delete files on the patient's Documents tab."},
    {"name": "Modifiers", "description": "Procedure modifiers on an appointment's service lines."},
    {"name": "Claims", "description": "Claim history, claims waiting to be sent and claim details. Read only."},
    # {"name": "Eligibility", "description": "Insurance eligibility check history. Read only."},
    {"name": "System", "description": "Health checks."},
]

app = FastAPI(title="Vero TherapyNotes API", version="0.1.0", description=DESCRIPTION, openapi_tags=TAGS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Disposition"],
)

app.include_router(login_routes.router, prefix="/api/v1")
app.include_router(patient_routes.router, prefix="/api/v1")
app.include_router(insurance_routes.router, prefix="/api/v1")
app.include_router(note_routes.router, prefix="/api/v1")
app.include_router(file_routes.router, prefix="/api/v1")
app.include_router(appointment_routes.router, prefix="/api/v1")
app.include_router(claim_routes.router, prefix="/api/v1")


@app.get("/health", tags=["System"], summary="API status", description="Returns `ok` when the API is running.")
def health():
    return {"status": "ok"}


@app.get(
    "/api/v1/worker/health",
    tags=["System"],
    summary="Gateway status",
    description="Shows whether the TherapyNotes gateway is running and which users are logged in.",
)
async def gateway_health():
    return await worker_health()
