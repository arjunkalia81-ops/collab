# TherapyNotes POC

Logs into TherapyNotes over plain HTTP (the same calls its web app makes) and serves patients, claims and eligibility through a small JWT-protected API. Chrome is opened only when Cloudflare asks for a fresh clearance.

The work is split into two services:

```
Client (frontend / curl / Swagger)
   │  JWT only
   ▼
project/  FastAPI API                      :8001   ← clients call this
   │  HTTP + X-Worker-Key (private network)
   ▼
worker/   TN gateway                       :8002   ← internal, never public
   │  one httpx session per user
   │  Chrome only to refresh cf_clearance
   ▼
TherapyNotes
```

## Why two services

TherapyNotes sits behind Cloudflare. When Cloudflare challenges a plain HTTP client with a 403, only a real browser can get the `cf_clearance` cookie. So the worker keeps a headed Google Chrome (a desktop on Windows, Xvfb on Linux) for that one job and talks to TherapyNotes with `httpx`, using Chrome's cookies and User-Agent. The API never touches TherapyNotes directly, so it needs no browser.

## What each part does

### `worker/` — TherapyNotes gateway

- Holds one HTTP session (cookie jar + `TNRAC`/`tnv` tokens) per logged-in TherapyNotes user. Many users can be logged in at once, up to `TN_MAX_SESSIONS`.
- Logs in with the same two `processlogin.aspx` calls the TN login page makes; the password is sent as the SHA-512 hash TN's own JavaScript produces. Only that hash is kept (for automatic re-login), never the plain password.
- Shares one Cloudflare clearance (`worker/clearance.json`: `cf_*` cookies + User-Agent) across all sessions. If it's missing, or TN answers `403` with a Cloudflare challenge, the worker opens Chrome with the saved profile (`worker/browser_profile/`), waits for the challenge to clear, saves the cookies and closes Chrome. One Chrome at a time, and never more often than `CLEARANCE_MIN_INTERVAL_SECONDS`.
- If TherapyNotes drops a session (401), the worker logs that user in again with the stored hash and retries the request once. At most 3 automatic re-logins per 10 minutes per session; after that the client gets a 401 and must log in again.
- Pings `sessionping.aspx` every `TN_SESSION_PING_SECONDS` per session so TN doesn't time the session out.
- Drops sessions nobody has used for `TN_IDLE_MINUTES`.
- Mirrors sessions to `worker/sessions.json`, so a worker restart keeps everyone logged in.
- Logging in again as a user who already has a session (same practice code, username and password) reuses that session instead of creating a second one.

Swagger at `http://127.0.0.1:8002/docs` shows only:

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /worker/health` | none | Is the worker up, clearance age, which sessions exist and how long they've been idle |
| `POST /worker/close` | `X-Worker-Key` | Drop one or all TN sessions |

Login, patients, claims and session ping are internal routes (`/internal/*`). They're hidden from Swagger and reject any call without the correct `X-Worker-Key`. Only the project API calls them.

### `project/` — API

- The only thing clients talk to
- Issues our own JWTs (access + refresh). The JWT carries the worker's session id; TherapyNotes cookies never leave the worker.
- Stateless: no session file, so it can restart or scale without losing anything
- Forwards login / patients / claims / logout to the worker
- Never opens Chrome

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /health` | none | API is up |
| `POST /api/v1/login` | TN credentials in body | Worker logs into TN; returns `access_token` + `refresh_token` |
| `GET /api/v1/patients` | Bearer access | Patient list. Query: `search`, `status`, `assignment`, `sort`, `ascending`, `page`. Each patient has `links` to its detail endpoints |
| `GET /api/v1/patients/{patient_id}/info` | Bearer access | Info tab: name, DOB, address, time zone, phones, demographics, HIPAA, appointments |
| `GET /api/v1/patients/{patient_id}/insurance` | Bearer access | Insurance policies with every editable field, plus `options` (practice payers, priorities, relationships, MSP types, appointment limits, genders) for the edit form |
| `POST /api/v1/patients/{patient_id}/insurance` | Bearer access | Add a policy. Body needs `payer_setting_id` and `member_id`; other fields optional. Returns the updated insurance |
| `PATCH /api/v1/patients/{patient_id}/insurance/{policy_id}` | Bearer access | Update a policy. Send only the fields to change (`null` clears one) plus the policy's `version`; 409 if it changed in TN since. Returns the updated insurance |
| `GET /api/v1/patients/{patient_id}/notes` | Bearer access | Notes on the Documents tab with status, service codes and whether they can be edited (only notes signed by the author) |
| `GET /api/v1/patients/{patient_id}/notes/{note_id}` | Bearer access | One signed note: service code (CPT), add-on codes, diagnoses, justification, plus `options` (service and add-on codes allowed on the note) |
| `PATCH /api/v1/patients/{patient_id}/notes/{note_id}` | Bearer access | Change the service code, add-on codes (and their units) and/or diagnoses of a signed note, with the note's `version`; 409 if it changed in TN since. TN saves a new revision, signed by the logged-in user |
| `GET /api/v1/patients/{patient_id}/notes/{note_id}/header` | Bearer access | Draft note header only: service code and add-on units. A signed note returns 422 |
| `PATCH /api/v1/patients/{patient_id}/notes/{note_id}/header` | Bearer access | Change add-on units on a draft note and save it as a draft, without signing. Send `version` from the header GET |
| `GET /api/v1/diagnosis-codes` | Bearer access | ICD-10 search for the diagnosis dropdown. Query: `search` (code or words) |
| `GET /api/v1/patients/{patient_id}/files` | Bearer access | Files on the patient's Documents tab (notes not included): name, date, size, access (`clinical`, `administrative`, `billing`) |
| `POST /api/v1/patients/{patient_id}/files` | Bearer access | Upload a file (JSON with `content_base64`, up to 10 MB) with `name`, `date`, `access`. Re-reads and verifies the upload |
| `GET /api/v1/patients/{patient_id}/files/{file_id}/download` | Bearer access | The file itself, with its content type and file name |
| `DELETE /api/v1/patients/{patient_id}/files/{file_id}` | Bearer access | Delete a file and verify it's gone; returns the remaining files |
| `GET /api/v1/appointments/{calendar_entry_id}/billing` | Bearer access | Billing tab of an appointment: service lines with units and procedure modifiers. `calendar_entry_id` comes with each pending claim and claim detail |
| `PATCH /api/v1/appointments/{calendar_entry_id}/modifiers` | Bearer access | Set modifiers (up to 4) and/or units on service lines, with the appointment's `version`; 409 if it changed in TN since. Re-reads and verifies the save |
| `GET /api/v1/patients/{patient_id}/billing` | Bearer access | Billing comments, cash rates, responsible party, referring provider/referral, claim condition flags, prior authorizations |
| `GET /api/v1/claims` | Bearer access | Submitted claims (Electronic Claim History), paged. Query: `patient_id`, `status`, `payer_id`, `clinician_id`, `sequence`, `start_date`, `end_date`, `page`, `page_size` |
| `GET /api/v1/claims/pending` | Bearer access | Claims ready to submit or resubmit (Submit Electronic Claims). Query: `items`, `patient_id`, `payer_id`, `clinician_id`, `start_date`, `end_date` |
| `GET /api/v1/claims/filters` | Bearer access | Payer and clinician ids to use in the claim filters |
| `GET /api/v1/claims/{claim_id}` | Bearer access | One submitted claim: status, line items, full claim details, biller comment, the parsed rejection reason and the payer/clearinghouse activity timeline |
| `GET /api/v1/claims/rejections` | Bearer access | Rejected claims with the payer's reason. Query: `reviewed` (`false` Rejected - New, `true` Rejected), `patient_id`, `payer_id`, `clinician_id`, `start_date`, `end_date`, `page`, `page_size` (max 25) |
| `POST /api/v1/claims/{claim_id}/review` | Bearer access | Mark a Rejected - New claim as reviewed (no undo in TN); verifies it moved to Rejected |
| `PATCH /api/v1/claims/{claim_id}/comment` | Bearer access | Save the biller comment with the claim's `comment_version`; 409 if it changed in TN since. Re-reads and verifies |
| `GET /api/v1/claims/summary` | Bearer access | Counts shown on the Billing page: rejected, primary/secondary to submit, external, paper, new eligibility reports, new ERAs |
| `GET /api/v1/claims/secondary` | Bearer access | Submit Secondary Claims: items waiting for the secondary payer. Query: `patient_id`, `clinician_id`, `start_date`, `end_date`, `page`, `page_size` |
| `GET /api/v1/claims/paper` | Bearer access | Create CMS-1500: items to bill on paper. Same query as secondary |
| `GET /api/v1/claims/external` | Bearer access | Mark External Claims. Query: `method` (`external`, `electronic`, `paper`), `patient_id`, `clinician_id`, `start_date`, `end_date` |
| `GET /api/v1/claims/superbill` | Bearer access | Create Superbill: out-of-network items for one patient. Query: `patient_id` (required), `start_date`, `end_date` |
| `GET /api/v1/eligibility` | Bearer access | Eligibility verification history. Query: `patient_id`, `status` (`pending`, `new`, `reviewed`), `start_date`, `end_date`, `page`, `page_size` |
| `GET /api/v1/patients/{patient_id}/ledger` | Bearer access | Every billing item for one patient with insurance status, plus balance owed and unassigned credit |
| `POST /api/v1/refresh` | refresh token in body | New access token; pings TN first and returns 401 if the worker no longer has the session |
| `POST /api/v1/session-ping` | Bearer access token | Pings TN right now (the worker also pings on its own every `TN_SESSION_PING_SECONDS`) |
| `POST /api/v1/logout` | Bearer access | Drop this TN session |
| `POST /api/v1/logout/force` | none | Drop every TN session on the worker |
| `GET /api/v1/worker/health` | none | Proxy to worker health |

## Folder layout

```
Therapy_notes_POC/
├── requirements.txt          Python packages for both services
├── .env                      settings for both services, git-ignored
├── start.sh                  start / stop both on the Linux VM
├── worker_start.sh           worker in the foreground, for PM2
├── project_start.sh          API in the foreground, for PM2
├── docs/                     VM setup, frontend and testing guides
├── project/                  API
│   ├── app/
│   │   ├── api/v1/           login.py, patients.py, insurance.py, notes.py, files.py, appointments.py, claims.py
│   │   ├── auth/jwt_tokens.py
│   │   ├── core/config.py
│   │   ├── worker_client.py  HTTP calls to the worker
│   │   └── main.py
│   └── run.py                entrypoint
└── worker/                   TN gateway
    ├── worker_app/
    │   ├── tn/client.py         TnSession: HTTP login, tokens, calls with re-login / clearance retry
    │   ├── tn/clearance.py      cf_clearance + User-Agent, refreshed with Chrome
    │   ├── tn/sessions.py       session registry, pings, idle sweeper, sessions.json
    │   ├── tn/version.py        conflict check before a save (409), verification after it (502)
    │   ├── patient/patient.py   patient list
    │   ├── patient/detail.py    single patient: info, billing
    │   ├── patient/insurance.py insurance policies: read, add, update
    │   ├── patient/notes.py     notes: service codes and diagnoses, ICD-10 search, edit signed notes
    │   ├── patient/draft_header.py  draft note header: add-on units, saved without signing
    │   ├── patient/files.py     patient documents: list, upload in 100 KB segments, download, delete
    │   ├── claim/appointment.py appointment billing: service lines, procedure modifiers
    │   ├── claim/claims.py      claim history, pending claims, claim detail with the rejection reason
    │   ├── claim/rejections.py  rejected claims list, Mark as Reviewed, biller comment
    │   ├── claim/queues.py      secondary, paper, external, superbill, ledger, summary counts
    │   ├── claim/eligibility.py eligibility history
    │   ├── playwright/browser.py   open/close Chrome (clearance only)
    │   └── core/config.py
    ├── browser_worker.py     entrypoint
    ├── setup_profile.py      creates browser_profile/ and clearance.json
    ├── browser_profile/      Chrome profile, git-ignored
    ├── clearance.json        Cloudflare cookies + UA, created on first login, git-ignored
    └── sessions.json         live TN sessions, git-ignored
```

## Prerequisites

- Windows, or Ubuntu with Xvfb, with Google Chrome installed (for the worker)
- Python 3.12+

For the Linux VM, follow [docs/linux_vm_setup.md](docs/linux_vm_setup.md) instead of the Windows steps below.

## Setup (first time)

### Install and settings

```powershell
cd D:\Anurag\POC\Therapy_notes_POC
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chrome
```

Create `.env` in the project folder. Both services read it:

```env
# API (project/)
JWT_SECRET=pick-a-long-random-string
JWT_ACCESS_MINUTES=5
JWT_REFRESH_DAYS=7
WORKER_URL=http://127.0.0.1:8002
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
HOST=0.0.0.0
PORT=8001

# Worker (worker/)
THERAPYNOTES_LOGIN_URL=https://www.therapynotes.com/app/login/
THERAPYNOTES_BASE_URL=https://www.therapynotes.com
THERAPYNOTES_HEADLESS=false
KEEP_BROWSER_OPEN=false
TN_SESSION_PING_SECONDS=120
WORKER_HOST=127.0.0.1
WORKER_PORT=8002

# Both services, must be the same
WORKER_SECRET=pick-a-different-long-random-string
```

Optional worker settings, with defaults: `TN_MAX_SESSIONS=50`, `TN_IDLE_MINUTES=60`, `CLEARANCE_MIN_INTERVAL_SECONDS=60`, `BROWSER_PROFILE=browser_profile`.

`THERAPYNOTES_HEADLESS` must stay `false` — Cloudflare blocks headless Chrome.

`CORS_ORIGINS` lists the frontend origins allowed to call the API from a browser (comma-separated, exact scheme + host + port, no trailing slash). Use your real frontend domain in production.

`JWT_SECRET` is required — the API won't start without it. Use at least 32 characters for both secrets.

TherapyNotes credentials are not stored in `.env`. They come from each `/api/v1/login` request.

Create the Chrome profile and Cloudflare clearance on this machine, in a desktop session:

```powershell
cd worker
python setup_profile.py
```

It deletes any existing `browser_profile/` and `clearance.json`, then opens Chrome for a few seconds; if Cloudflare shows a checkbox, click it. It saves a new `browser_profile/` and `clearance.json`, then checks that plain HTTP gets through. Don't copy these from another machine — `cf_clearance` is tied to the IP it was issued to. Run it again if the VM's public IP changes (stop the worker first).

## Run locally

Start the worker first, then the API. Use two terminals.

**Terminal 1 — worker**

```powershell
cd D:\Anurag\POC\Therapy_notes_POC
.\venv\Scripts\activate
cd worker
python browser_worker.py
```

You should see `Uvicorn running on http://127.0.0.1:8002`. Leave it running.

**Terminal 2 — API**

```powershell
cd D:\Anurag\POC\Therapy_notes_POC
.\venv\Scripts\activate
cd project
python run.py
```

**Check both are up**

```powershell
curl.exe http://127.0.0.1:8002/worker/health
curl.exe http://127.0.0.1:8001/health
curl.exe http://127.0.0.1:8001/api/v1/worker/health
```

Then open **http://127.0.0.1:8001/docs**.

## Using the API

Frontend team: see [docs/FRONTEND_INTEGRATION.md](docs/FRONTEND_INTEGRATION.md) for the login/refresh wrapper, error handling, the insurance form mapping and note editing.

1. `POST /api/v1/login` with the TherapyNotes login details:

   ```json
   {
     "practice_code": "YourPracticeCode",
     "username": "your-username",
     "password": "..."
   }
   ```

   The very first login on a machine opens a Chrome window for about 10 seconds to collect the Cloudflare clearance, then closes it. Every login after that is plain HTTP and takes a second or two. Copy `access_token` and `refresh_token`.

   If TherapyNotes rejects the login, the API returns **401** with TN's own message and a `code` the frontend can switch on:

   ```json
   { "detail": { "code": "invalid_credentials", "message": "The username and password you entered did not match any account." } }
   ```

   | `code` | When | Example `message` (from TherapyNotes) |
   |--------|------|------|
   | `invalid_practice_code` | Practice code doesn't exist | The practice code you entered did not match any practice. |
   | `invalid_practice_code` | Practice is disabled | The account with practice code "DFDFD" has been disabled. Please create a new practice, … |
   | `invalid_credentials` | Wrong username or password | The username and password you entered did not match any account. |
   | `too_many_attempts` | TN throttles after failed tries | This account cannot be logged into at this time. Please wait 12 seconds and try again. |
   | `two_factor_required` | The account has 2FA enabled (not supported yet) | — |
   | `login_failed` | Anything else | — |
2. Click **Authorize** in Swagger and paste the `access_token`.
3. `GET /api/v1/patients` — returns the patient list. Use a patient's `patient_id` (or its `links`) with `/api/v1/patients/{patient_id}/info`, `/insurance` or `/billing`.

   | Query | Values | Default |
   |---|---|---|
   | `search` | Name, account #, phone, or insurance member ID | empty |
   | `status` | `any`, `active`, `inactive`, `needsDataEntry`, `aptComingWeek`, `intComingWeek`, `patientsWithFirstAppointmentInLast30Days`, `openBalance`, `birthday` | `active` |
   | `assignment` | `any`, `clinician-none`, `clinician-{clinician id}`, `provider-none`, `provider-outofnetwork`, `provider-{insurance group id}` | `any` |
   | `sort` / `ascending` | `name`, `dob`, `lastAppt` / `true`, `false` | `name` / `true` |
   | `page` | 1, 2, … — the response has `page`, `page_count`, `has_next` | `1` |

   Clinician ids and insurance group ids come from the list itself (`clinicians[].id`, `insurance.group_id`).
4. Claims. `GET /api/v1/claims` lists submitted claims; add `patient_id` for one patient's claims. `GET /api/v1/claims/{claim_id}` shows one claim with its rejection reason and history. `GET /api/v1/claims/pending` lists claims that haven't been sent yet. `GET /api/v1/claims/rejections` lists the rejections to review; `POST /api/v1/claims/{claim_id}/review` marks one as reviewed and `PATCH /api/v1/claims/{claim_id}/comment` (body `{"comment": "...", "version": "<comment_version>"}`) saves the biller comment.

   | Query | Values | Default |
   |---|---|---|
   | `status` (`/claims`) | `pending`, `submitted`, `sent`, `rejected`, `canceled`, `paid`, `rejected_new` | any |
   | `sequence` (`/claims`) | `primary`, `secondary` | any |
   | `items` (`/claims/pending`) | `newresubmit`, `new`, `resubmit`, `all`, `information` | `newresubmit` |
   | `payer_id` / `clinician_id` | ids from `GET /api/v1/claims/filters` (payer id = insurance group id) | any |
   | `start_date` / `end_date` | `YYYY-MM-DD` | all dates (pending: up to today) |
   | `page` / `page_size` (`/claims`) | page size 1–100 | `1` / `25` |
5. Insurance. `GET /api/v1/patients/{patient_id}/insurance` returns the policies and the dropdown `options`. Use the same field names to add (`POST .../insurance`) or change (`PATCH .../insurance/{policy_id}`) a policy:

   | Field | Values |
   |---|---|
   | `payer_setting_id` | `options.payers[].payer_setting_id` (only payers already set up in the practice) |
   | `priority` | 1 Primary, 2 Secondary, 3 Tertiary, 4 Quaternary (add defaults to the next free one) |
   | `msp_type` | 1–9 from `options.msp_types`; TN requires it on secondary and later policies |
   | `comments`, `member_id`, `policy_group`, `employer`, `plan_name` | text |
   | `signature_on_file` | `true` / `false` |
   | `coverage_start`, `coverage_end`, `appointments_year_end` | `YYYY-MM-DD` |
   | `copay`, `deductible` | number |
   | `appointment_limit` / `appointments_per_year` | 0 Unknown, 1 Unlimited, 2 Number / count when 2 |
   | `relationship` | 0 Self, 1 Spouse, 2 Child, 3 Other, 4 Life Partner |
   | `insured` | Policy holder when not Self: `first_name`, `middle_name`, `last_name`, `suffix`, `gender` (0 Male, 1 Female), `date_of_birth`, `phone`, `address` {`address1`, `address2`, `city`, `state`, `postal_code`} |

   Updates (`PATCH`) need the policy's `version` from the GET; see "Conflict check" below.

   When TherapyNotes refuses the save, the API returns 422 with TN's own messages, e.g. `{"detail": {"message": "Please select a valid MSP qualification type from the dropdown.", "errors": [...]}}`. Nothing is saved in that case.
6. Notes. `GET /api/v1/patients/{patient_id}/notes` lists the notes; only those with `editable: true` (signed by the author) can be opened with `GET .../notes/{note_id}` or changed with `PATCH .../notes/{note_id}`:

   | Field | Values |
   |---|---|
   | `service_code_id` | `options.service_codes[].id` from the note (CPT, e.g. 90837) |
   | `add_on_codes` | full list `[{"id": 7324818, "units": 1}]`, ids from `options.add_on_codes`; `units` above 1 only when the code has `multiple_units`; `[]` removes all add-ons |
   | `diagnoses` | full list, primary first: `[{"code": "F41.1"}]`; add `description` only when a code has several (pick from `GET /api/v1/diagnosis-codes?search=F32.9`) |
   | `diagnosis_justification` | text |
   | `version` | required; `version` from the GET |

   Saving works like "Save Changes to Note" with "Sign this Form" ticked in TherapyNotes: a new revision, signed by the logged-in user. Drafts return 422 on these two routes.

   A draft note's add-on units use a different pair of routes, `GET` and `PATCH .../notes/{note_id}/header`. Send `{"add_on_codes": [{"id": 7324821, "units": 15}], "version": "<version from the header GET>"}`. Units above 1 are only accepted when that add-on has `multiple_units`. The rest of the note is left as it is, and the note stays a draft. The signed-note PATCH is unchanged.
7. Modifiers and units. They live on the appointment's billing (Submit Primary Claims → charge → Billing). Take `calendar_entry_id` from `GET /api/v1/claims/pending`, read the lines with `GET /api/v1/appointments/{calendar_entry_id}/billing`, then `PATCH .../modifiers` with `{"services": [{"billable_service_id": 1202149231, "modifiers": ["95"], "units": 1}], "version": "<version from the GET>"}`. Omit `modifiers` or `units` to leave that field alone. When TN warns about the change, the API returns 422 with the warnings; resend with `"override_warnings": true` to save anyway.

   Conflict check: the insurance policy, note and appointment billing GETs return a `version`, a hash of the editable fields. Every `PATCH` must send it back. The worker reads the record from TN again before saving; if the version differs (someone changed it in TN since it was loaded) nothing is saved and the API returns 409 with `detail.current`, the record as TN has it now. After the save the worker re-reads TN and compares every field that was sent with what TN now shows. If they match it returns the new data and `version` (`message` ends with "verified"); if not, the API returns 502 with `detail.fields` (what didn't match) and `detail.current`, and the change counts as failed and needing review.
8. Documents. `POST /api/v1/patients/{patient_id}/files` with `{"file_name": "card.pdf", "mime_type": "application/pdf", "content_base64": "...", "name": "Insurance card", "date": "2026-10-07", "access": "billing"}`. The worker sends the file to TN in 100 KB base64 segments (as TN's own uploader does), saves it, then lists the documents again and checks the new file's name, date, access and size (502 if they differ). TN names the stored file `name` + the extension. Files go on the patient only; TN has no claim attachments.
9. Before 5 minutes pass, call `POST /api/v1/refresh` with `{"refresh_token": "..."}` to get a new access token.
10. `POST /api/v1/logout` when done — drops the TN session.

With curl:

```powershell
curl.exe -X POST http://127.0.0.1:8001/api/v1/login -H "Content-Type: application/json" --data-binary "@login.json"
# login.json: {"practice_code": "...", "username": "...", "password": "..."}

curl.exe http://127.0.0.1:8001/api/v1/patients -H "Authorization: Bearer <access_token>"

curl.exe -X POST http://127.0.0.1:8001/api/v1/refresh -H "Content-Type: application/json" --data-binary "@refresh.json"
# refresh.json: {"refresh_token": "<refresh_token>"}

curl.exe -X POST http://127.0.0.1:8001/api/v1/logout -H "Authorization: Bearer <access_token>"
```

## Tokens and session lifetime

There are two separate sessions:

| Session | Lifetime | Kept alive by |
|---------|----------|---------------|
| Our access JWT | `JWT_ACCESS_MINUTES` (5) | Client calling `/refresh` |
| Our refresh JWT | `JWT_REFRESH_DAYS` (7) | Logging in again |
| TherapyNotes session in the worker | TN's inactivity timeout, or `TN_IDLE_MINUTES` without any client call | Worker ping every `TN_SESSION_PING_SECONDS`, plus one ping on each `/refresh`; automatic re-login if TN drops it anyway |
| Cloudflare clearance | About a year, tied to this machine's IP and Chrome User-Agent | Chrome re-opens automatically when TN answers with a Cloudflare challenge |

The TN session survives a worker restart (`sessions.json`). It goes away on `/logout`, after `TN_IDLE_MINUTES` without client calls, or if TN keeps rejecting it and the re-login guard trips (3 re-logins in 10 minutes) — then `/refresh` and every data call return 401 and the client should call `/login` again.

## Stop

Ctrl+C in each terminal. On the Linux VM, `./start.sh stop`.

## Production

The worker and the API run together on one Ubuntu VM, started by `start.sh`. Every command is in [docs/linux_vm_setup.md](docs/linux_vm_setup.md), and what is installed on the VM is listed in [docs/vm_for_it_team.md](docs/vm_for_it_team.md). Keep the worker port on 127.0.0.1 and open only the API port.

Before going live:

- Use strong random values for `JWT_SECRET` and `WORKER_SECRET`
- Protect or remove `POST /api/v1/logout/force` — it has no auth today
- `worker/sessions.json` holds TN cookies and password hashes; keep the worker disk encrypted and the file readable only by the worker's account
- One worker handles many users; if one Windows box is not enough, run more workers and route each session id to the same worker

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Worker: `error while attempting to bind on address ('127.0.0.1', 8002)` | An old worker is still running | Close the old terminal, or `Get-NetTCPConnection -LocalPort 8002` and stop that process |
| API: `Browser worker unreachable` (503) | Worker not running, or wrong `WORKER_URL` | Start the worker. `WORKER_URL` must be `http://127.0.0.1:<worker port>`, because the worker only listens on 127.0.0.1 |
| API: `Invalid or missing X-Worker-Key` (401) | The API and worker were started with different `WORKER_SECRET` values | Restart both after editing `.env` |
| Login: `Cloudflare challenge did not clear` (503) | Chrome could not get a `cf_clearance` | Make sure the worker runs on Windows in a logged-on desktop with `THERAPYNOTES_HEADLESS=false`; solve the challenge once in the Chrome window |
| Any call: `Cloudflare is blocking TherapyNotes requests` (502) | Clearance refreshed but plain HTTP still gets 403 | Public IP or Chrome changed; delete `worker/clearance.json` and log in again |
| Login: 401 with `invalid_practice_code` / `invalid_credentials` | TherapyNotes rejected the values | Show `detail.message` to the user; check the request body |
| Login: 401 with `too_many_attempts` | Several failed logins in a row | Wait the number of seconds in the message, then retry |
| Login: 429 `Worker already holds N sessions` | `TN_MAX_SESSIONS` reached | Raise the limit or lower `TN_IDLE_MINUTES` |
| Chrome: profile already in use | Another Chrome has `browser_profile/` open | Close it (or set `BROWSER_PROFILE` to another folder) and retry |
| Any call: `No active session` / `login again` (401) | Session dropped: idle limit, logout, or TN kept rejecting it | `POST /api/v1/login` again |
