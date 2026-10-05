# Appointment Service MVP

A deterministic service that retrieves a patient's relevant appointment for Hospital Patient Agent. The integration API is read-only; an authenticated administration interface supports booking, editing, and cancelling appointments. The service includes FastAPI, an internal SQLite database, demo data, technical audit records, and a single-container deployment.

## Running the service

Prerequisite: Docker Desktop must be running.

```bash
docker compose up --build -d
docker compose ps
```

- API: `http://localhost:8080`
- Appointment administration: `http://localhost:8080/`
- Swagger: `http://localhost:8080/docs`
- Health: `http://localhost:8080/health`

On the first visit, the interface redirects to `/setup`. The local setup token is `local-setup-token`. Choose a password of at least 12 characters, scan the QR code with Microsoft Authenticator, and verify a six-digit code. For a real deployment, replace `UI_SETUP_TOKEN` and `SESSION_SECRET` with random values. Enable `SECURE_COOKIES=true` after configuring HTTPS.

Hospital Agent's `CheckAppointment` calls require `X-API-Key`. The local Docker development key is `local-development-api-key`. For a real deployment, set a strong random `APPOINTMENT_API_KEY`, also store it in Hospital Agent's secrets, and never include it in source code, images, or logs.

Copy `.env.example` to `.env`, replace the sample values with random secrets, and run `docker compose up -d`. The `.env` file is excluded from Git.

## Checking an existing appointment

```bash
curl -i \
  -H "X-API-Key: local-development-api-key" \
  -H "X-Case-ID: CASE-001" \
  -H "X-Execution-ID: EXEC-001" \
  http://localhost:8080/api/v1/patients/P-10041/appointment
```

Expected response: `200`, `found=true`, and appointment `APT-8391`. The `appointment_at` value always includes a timezone: Israel time, `+03:00` in summer and `+02:00` in winter. Hospital Agent rejects timestamps without a timezone.

Another patient with an appointment:

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-20000/appointment
```

Expected response: `200`, `found=true`, and appointment `APT-8392`. The response includes `required_documents`, described under [Booking and cancelling appointments](#booking-and-cancelling-appointments).

It also includes `exam_type` (`{code, label}`) and `instruction` (`{source_id, version, title}`, without the instruction text). These fields use the appointment's linked exam type. Older appointments without an explicit link use their department's default exam type.

If the department or linked exam type is absent from `app/catalog.py`, the service returns `503 service_unavailable` and writes an audit record. This should not normally occur, but not every persistence path validates it. The service never invents a fallback or returns `500`.

To retrieve a specific appointment, supply `?appointment_id=`. It must belong to the requested patient and have status `Scheduled`; otherwise, the result is `found=false`. A specifically selected appointment can be returned even if its date has passed, provided it remains `Scheduled`. This lookup checks ownership and status; rejecting a past appointment is the calling agent's responsibility.

The response includes `upcoming_count`: the patient's `Scheduled` appointments in the next 90 days, independently of the selection parameter. This matches Hospital Agent's appointment selection window.

```bash
curl -i -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/patients/P-10041/appointment?appointment_id=APT-8391"
```

## Retrieving preparation instructions

```bash
curl -i -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/instructions/INSTR-CARD-ECHO?version=1"
```

Expected response: `200` with `{source_id, version, title, text}`. An unknown identifier or version in `app/catalog.py` produces `404 {"error": "instruction_not_found"}`, without a `message` field.

This endpoint accepts and checks no patient information (specification section 11). It uses the same API key as the other endpoints. Calls are recorded in `appointment_audit_logs` with `operation=GetInstruction` and an empty `patient_id`, because the column does not allow `NULL`.

## Listing a patient's appointments

```bash
curl -i \
  -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/patients/P-10041/appointments?from=2026-10-01T00:00:00%2B03:00&to=2027-01-01T00:00:00%2B02:00"
```

Expected response: `200` with `appointments` and `truncated`. Rows are sorted by `appointment_at`, earliest first, then by `appointment_id` for equal timestamps.

Both `from` and `to` are required ISO timestamps with timezones. Missing timezones produce `400`. The start is inclusive and the end exclusive (`from <= appointment_at < to`); the range must not exceed 366 days. The list includes `Cancelled` appointments, as well as `Scheduled` appointments, so patients can see cancellations. At most 100 rows are returned; additional rows set `truncated=true`.

Each row includes `exam_type` and `instruction`, following the same explicit-link or department-default rules as `CheckAppointment`.

Range comparison converts inputs to Israel time. Stored timestamps are local Israel times without timezone information, as in `CheckAppointment`, so callers can send UTC or another timezone. When daylight saving time ends, one local hour is ambiguous; a range covering that hour can miss an appointment. This is a known limitation.

| HTTP status | Code | Meaning |
|---|---|---|
| 400 | `validation_error` | Missing or invalid `from`/`to`, missing timezone, `from >= to`, range over 366 days, or invalid `patient_id`. |
| 401 | `unauthorized` | Missing or incorrect API key. |
| 404 | `patient_not_found` | Patient absent from the configured registry. |
| 503 | `patient_registry_unavailable` | Registry unavailable. |
| 504 | `timeout` | Simulated timeout. |
| 503 | `service_unavailable` | Database failure, or a department or linked exam type missing from the catalog. |

For `operation=ListAppointments`, audit records cover `200` (`found`/`not_found`), `404 patient_not_found`, registry unavailability, catalog mismatch, and simulated timeout. The last three are recorded as `technical_failure`. Validation errors, unauthorized requests, and database failures are not recorded in this audit table. Catalog mismatch fails closed and never produces an invented default or `500`.

## Checking patients against the registry

When `PATIENT_REGISTRY_URL` is configured, as it is by default in `compose.yaml`, every patient identifier is checked against Hospital Agent's `patients` table using the read-only `hospital_reader` role:

- Unregistered patient: `404` with `error=patient_not_found`.
- Unavailable registry: `503` with `error=patient_registry_unavailable`. The service fails closed and does not return an appointment for an unverified patient.
- Registered patient without an appointment: `200` with `found=false`. This is a valid business outcome.

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-30000/appointment
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-99999/appointment
```

Expected results: `P-30000` returns `200` with `found=false`; `P-99999` returns `404` with `patient_not_found`.

The registry is in Hospital Agent's PostgreSQL database `hospital`, published on `127.0.0.1:54322`, so Hospital Agent must be running. Containers connect through `host.docker.internal`, which works with Docker Desktop.

On Linux, this hostname resolves to the Docker bridge address, which cannot reach PostgreSQL when its port is published only on `127.0.0.1`. Requests consequently return `503`. Publish the port on an address reachable from the bridge, such as `172.17.0.1:54322`, or connect both projects to a shared Docker network.

If `PATIENT_REGISTRY_URL` is unset or empty when running without Compose, registry checking is disabled and `P-99999` returns `200` with `found=false`. An empty `.env` value does not disable it in Compose, because Compose substitutes the default. Startup logs report `patient registry check: enabled` or `disabled`.

## Booking and cancelling appointments

An authenticated administrator books an appointment from the main page's new-appointment form. Patient, department, date, and time are required; doctor and location are optional.

Departments, their doctors, and their locations are defined in `app/catalog.py`. Dropdowns show only doctors and locations belonging to the selected department. The date uses separate day, month (number and Hebrew name), and year lists; available years are the current year and the following two years. Invalid dates, such as February 31, are rejected.

Time is selected in 15-minute slots from 08:00 through 17:45. The server accepts only catalog values and supported slots. Doctors or locations from another department are rejected. To add catalog entries, edit `app/catalog.py` and rebuild the image.

The patient dropdown loads identifiers and names from the registry, without phone numbers. Names also appear beside identifiers in the appointment table. If the registry is unavailable or unconfigured, the page still displays appointments, but patient selection and booking are disabled. The dropdown is only a convenience: the server checks the registry again for every booking. Appointment time is Israel time and must be in the future.

Booking fails closed:

- Unregistered patient: no appointment is created (`400`).
- Unavailable or unconfigured registry: no appointment is created (`503`).

A new appointment receives an `APT-XXXXXX` identifier and status `Scheduled`, making it available to `CheckAppointment` immediately.

Each scheduled appointment has an edit link that opens the same form with its saved values. Department, doctor, location, date, and time can be changed under the same rules, retaining the identifier. The patient cannot be changed; transferring an appointment requires a new booking. Cancelled appointments cannot be edited.

The cancel button changes status to `Cancelled`. Nothing is deleted. Cancelled appointments are excluded from the single-appointment lookup but remain visible in the appointment-list endpoint.

Bookings, rejected booking attempts, updates, and cancellations are recorded in `appointment_audit_logs` with `operation=BookAppointment`, `UpdateAppointment`, or `CancelAppointment`, and `case_id=ui:<username>`.

These actions are available through the administration interface, not the integration API or Swagger. Forms use CSRF tokens and require login when `UI_AUTH_ENABLED=true`.

Each appointment can require documents from the five-type catalog: `CBC`, `COAGULATION_TESTS`, `ECG`, `URINALYSIS`, and `PREOP_SUMMARY`. Requirements are stored in `appointment_required_documents`; they are not derived from the doctor or department. Editing restores the saved selection, and saving replaces it. `CheckAppointment` returns a sorted `required_documents` list, empty when there are no requirements.

Older appointments initially have no requirements. Edit an appointment to select them. The demo example is `APT-8391` for `P-10041`, requiring `CBC`, `COAGULATION_TESTS`, and `ECG`.

## Exam types and preparation instructions

Each appointment has a required exam type selected from `app/catalog.py`: 13 types across five departments. The dropdown is filtered by department. Selecting a type preselects its suggested documents on the client; administrators can change them.

The server rejects a type belonging to another department (`400`). The selection is stored in `appointment_exam_types`, one row per appointment, and shown in the table and edit form.

Each type defines preparation instructions: `instruction_id`, `instruction_version`, title, and Hebrew text. These are demo drafts requiring medical approval. The `exam_types` table is refreshed from the catalog at startup, as is `document_types`.

Appointments without an explicit link use their department's default `..._VISIT` type. On the first startup after this feature was introduced, the two demo appointments were assigned types once: `APT-8391` to a neurology clinic visit, and `APT-8392` to a stress test. Assignment occurs only if the appointment's department matches the type's department; a demo appointment moved elsewhere is not assigned that type.

The administration table marks derived types as default, distinguishing them from explicit selections.

## Simulating a timeout

When `ENABLE_FAILURE_SIMULATION=true`, requests for `P-TIMEOUT` return `504`; this is not converted into `found=false`.

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-TIMEOUT/appointment
```

Set `ENABLE_FAILURE_SIMULATION=false` in production.

## Trace and audit

The Tool Executor supplies `X-Case-ID` and `X-Execution-ID`. If either is missing, the service generates a UUID and returns it in the response headers. Appointment lookups are recorded in `appointment_audit_logs` and emitted as JSON on stdout, including the outcome and response time.

## Data storage

The API and SQLite database run in the same container. `/data/appointments.db` is mounted on the `appointment_sqlite_data` Docker volume, so updating or recreating the container preserves data.

Appointment storage requires no separate database container, PostgreSQL, or RDS. The patient-registry integration reads Hospital Agent's PostgreSQL database with a read-only role and never writes to it.

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

## Stopping and cleanup

Stop while keeping data:

```bash
docker compose down
```

Delete the local SQLite data volume:

```bash
docker compose down -v
```
