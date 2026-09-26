import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Form, Header, Path as ApiPath, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from . import catalog
from .config import Settings
from .models import AdminUser, Appointment, AppointmentRequiredDocument, AuditLog, Base, DocumentTypeRow
from .patient_registry import PatientRegistry, PostgresPatientRegistry, RegistryUnavailable
from .schemas import AppointmentList, AppointmentResult, ErrorResult, HealthResult

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("appointment-service")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
password_hasher = PasswordHasher()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
# The booking form's time is Israel local time.
CLINIC_TZ = ZoneInfo("Asia/Jerusalem")
PATIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATUS_LABELS = {"Scheduled": "מתוכנן", "Cancelled": "בוטל"}
ISRAEL = ZoneInfo("Asia/Jerusalem")
MAX_LIST = 100
MAX_LIST_WINDOW = timedelta(days=366)


class SimulatedTimeout(Exception):
    pass


def _validate_booking(form: dict[str, str]) -> tuple[str | None, datetime | None]:
    """Returns (error, appointment_at); exactly one of them is None. Everything but the patient
    must come from the catalog: the department, its own doctor and location, and a time slot."""
    if not PATIENT_ID_RE.fullmatch(form["patient_id"]):
        return "יש לבחור מטופל מהרשימה.", None
    department = catalog.BY_VALUE.get(form["department"])
    if department is None:
        return "יש לבחור מחלקה מהרשימה.", None
    if form["doctor_name"] and form["doctor_name"] not in department.doctors:
        return f"הרופא שנבחר אינו שייך למחלקת {department.label}.", None
    if form["location"] and form["location"] not in department.locations:
        return f"המיקום שנבחר אינו שייך למחלקת {department.label}.", None
    parts = (form["appointment_day"], form["appointment_month"], form["appointment_year"])
    if not all(p.isdigit() for p in parts):
        return "יש לבחור תאריך מלא לתור: יום, חודש ושנה.", None
    day_n, month_n, year_n = map(int, parts)
    if not (1 <= day_n <= 31 and 1 <= month_n <= 12):
        return "יש לבחור תאריך מלא לתור: יום, חודש ושנה.", None
    if year_n not in catalog.booking_years(datetime.now(CLINIC_TZ).year):
        return "יש לבחור שנה מהרשימה.", None
    try:
        day = date(year_n, month_n, day_n)
    except ValueError:
        return f"התאריך {day_n:02d}/{month_n:02d}/{year_n} אינו קיים.", None
    if form["appointment_time"] not in catalog.TIME_SLOTS:
        return "יש לבחור שעה מהרשימה (שעון 24 שעות, 08:00-17:45).", None
    hour, minute = map(int, form["appointment_time"].split(":"))
    when = datetime.combine(day, dt_time(hour, minute), tzinfo=CLINIC_TZ)
    if when <= datetime.now(CLINIC_TZ):
        return "מועד התור חייב להיות בעתיד.", None
    return None, when


def _validate_required_documents(codes: list[str]) -> tuple[str | None, list[str]]:
    """(error, the codes to store): blanks dropped, repeats merged, sorted; a code outside the
    catalog refuses the whole form - the database would refuse it anyway (design §3)."""
    cleaned = sorted({code.strip() for code in codes if code.strip()})
    if any(code not in catalog.DOCUMENT_TYPE_CODES for code in cleaned):
        return "יש לבחור מסמכים נדרשים מהרשימה בלבד.", []
    return None, cleaned


def _new_appointment_id(session: Session) -> str:
    while True:
        candidate = f"APT-{secrets.token_hex(3).upper()}"
        if session.get(Appointment, candidate) is None:
            return candidate


def _engine_options(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


def _seed_document_types(session: Session) -> None:
    """The catalog is reference data, not demo data: it is written on every start, whatever
    SEED_DEMO_DATA says, and brought in line with app/catalog.py (design §2)."""
    for t in catalog.DOCUMENT_TYPES:
        row = session.get(DocumentTypeRow, t.code)
        if row is None:
            session.add(DocumentTypeRow(code=t.code, label_he=t.label, max_age_days=t.max_age_days))
        else:
            row.label_he, row.max_age_days = t.label, t.max_age_days
    session.commit()


def _seed(session: Session) -> None:
    if session.scalar(select(Appointment.appointment_id).limit(1)) is not None:
        return
    session.add_all(
        [
            Appointment(
                appointment_id="APT-8391",
                patient_id="P-10041",
                department="Neurology",
                doctor_name="Dr. Cohen",
                appointment_at=datetime.fromisoformat("2026-10-03T10:30:00+03:00"),
                location="Building B, Floor 2",
                status="Scheduled",
                required_document_links=[AppointmentRequiredDocument(document_type=c) for c in ("CBC", "COAGULATION_TESTS", "ECG")],
            ),
            Appointment(
                appointment_id="APT-8392",
                patient_id="P-20000",
                department="Cardiology",
                doctor_name=None,
                appointment_at=datetime.fromisoformat("2026-10-07T09:00:00+03:00"),
                location=None,
                status="Scheduled",
            ),
        ]
    )
    session.commit()


def _write_audit(
    session: Session,
    *,
    case_id: str,
    execution_id: str,
    patient_id: str,
    result: str,
    latency_ms: int,
    operation: str = "CheckAppointment",
) -> None:
    """Adds the audit row and commits - together with anything else pending in the session."""
    event = {
        "case_id": case_id,
        "execution_id": execution_id,
        "patient_id": patient_id,
        "operation": operation,
        "result": result,
        "latency_ms": latency_ms,
    }
    logger.info(json.dumps(event, ensure_ascii=False))
    session.add(AuditLog(audit_id=str(uuid4()), **event))
    session.commit()


def _registry_refusal(session, registry, *, patient_id, case_id, execution_id, started, operation):
    """The registry's verdict (design §4 of the registry design): None when the patient may be
    served, else the 404 / 503 answer - with its audit row already written."""
    if registry is None:
        return None
    try:
        known = registry.exists(patient_id)
    except RegistryUnavailable:
        logger.warning("patient registry unavailable")
        _write_audit(session, case_id=case_id, execution_id=execution_id, patient_id=patient_id,
                     result="technical_failure", operation=operation,
                     latency_ms=round((time.perf_counter() - started) * 1000))
        return JSONResponse(status_code=503,
                            content={"error": "patient_registry_unavailable",
                                     "message": "The patient registry could not be reached"},
                            headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id})
    if not known:
        _write_audit(session, case_id=case_id, execution_id=execution_id, patient_id=patient_id,
                     result="patient_not_found", operation=operation,
                     latency_ms=round((time.perf_counter() - started) * 1000))
        return JSONResponse(status_code=404,
                            content={"error": "patient_not_found", "message": "The patient is not in the registry"},
                            headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id})
    return None


def create_app(
    database_url: str | None = None,
    seed_demo_data: bool | None = None,
    ui_auth_enabled: bool | None = None,
    ui_setup_token: str | None = None,
    session_secret: str | None = None,
    api_auth_enabled: bool | None = None,
    appointment_api_key: str | None = None,
    patient_registry: PatientRegistry | None = None,
) -> FastAPI:
    settings = Settings()
    effective_url = database_url or settings.database_url
    should_seed = settings.seed_demo_data if seed_demo_data is None else seed_demo_data
    auth_enabled = settings.ui_auth_enabled if ui_auth_enabled is None else ui_auth_enabled
    setup_token_value = settings.ui_setup_token if ui_setup_token is None else ui_setup_token
    session_secret_value = settings.session_secret if session_secret is None else session_secret
    api_auth = settings.api_auth_enabled if api_auth_enabled is None else api_auth_enabled
    api_key_value = (
        settings.appointment_api_key
        if appointment_api_key is None
        else appointment_api_key
    )
    registry = patient_registry
    if registry is None and settings.patient_registry_url:
        registry = PostgresPatientRegistry(settings.patient_registry_url)
    # Once, at startup, so there is never doubt which mode the service runs in. The URL itself
    # is never logged: it carries a password.
    logger.info("patient registry check: %s", "enabled" if registry is not None else "disabled")
    engine = create_engine(effective_url, **_engine_options(effective_url))
    if effective_url.startswith("sqlite"):
        # SQLite leaves foreign keys off unless every connection asks (design §3).
        @event.listens_for(engine, "connect")
        def _foreign_keys_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)  # creates the two new tables in an existing database too
        with SessionLocal() as session:
            _seed_document_types(session)
            if should_seed:
                _seed(session)
        yield
        engine.dispose()
        if hasattr(registry, "dispose"):
            registry.dispose()

    app = FastAPI(
        title="Appointment Service",
        description="Read-only appointment lookup API for the Hospital Patient Agent (booking is in the admin UI only).",
        version=settings.service_version,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.SessionLocal = SessionLocal
    app.state.settings = settings
    app.state.patient_registry = registry
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret_value,
        max_age=1800,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    def active_admin(session: Session) -> AdminUser | None:
        return session.scalar(select(AdminUser).where(AdminUser.is_active.is_(True)).limit(1))

    def authenticated(request: Request) -> bool:
        return request.session.get("authenticated") is True

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": "validation_error", "message": str(exc)},
        )

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf_token"] = token
        return token

    def csrf_valid(request: Request, supplied: str) -> bool:
        expected = request.session.get("csrf_token")
        return bool(expected and supplied) and secrets.compare_digest(supplied, expected)

    def forbidden() -> HTMLResponse:
        return HTMLResponse("הבקשה נדחתה: טופס לא תקף. רעננו את הדף ונסו שוב.", status_code=403)

    def actor(request: Request) -> str:
        return f"ui:{request.session.get('username') or 'anonymous'}"[:128]

    def render_dashboard(
        request: Request,
        *,
        search_value: str = "",
        error: str | None = None,
        notice: str | None = None,
        form: dict[str, str] | None = None,
        editing: Appointment | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        # The dropdown's list is a convenience only: booking still asks the registry itself.
        patients, patients_error = None, None
        registry = request.app.state.patient_registry
        if registry is None:
            patients_error = ("בדיקת המטופלים אינה מוגדרת (PATIENT_REGISTRY_URL), ולכן קביעת תורים "
                              "כבויה.")
        else:
            try:
                patients = registry.list_patients()
            except RegistryUnavailable:
                logger.warning("patient registry unavailable")
                patients_error = ("לא ניתן לטעון את רשימת המטופלים (מאגר המטופלים אינו זמין), ולכן "
                                  "אי אפשר לקבוע תור כרגע.")
        with request.app.state.SessionLocal() as session:
            query = select(Appointment).order_by(Appointment.appointment_at.asc())
            if search_value:
                query = query.where(Appointment.patient_id.ilike(f"%{search_value}%"))
            appointments = list(session.scalars(query))
            total_count = session.scalar(select(func.count()).select_from(Appointment)) or 0
            scheduled_count = session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.status == "Scheduled")
            ) or 0

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "appointments": appointments,
                "total_count": total_count,
                "scheduled_count": scheduled_count,
                "search_value": search_value,
                "auth_enabled": auth_enabled,
                "csrf_token": csrf_token(request),
                "status_labels": STATUS_LABELS,
                "error": error,
                "notice": notice,
                "form": form or {},
                "patients": patients,
                "patient_names": dict(patients or []),
                "patients_error": patients_error,
                "departments": catalog.DEPARTMENTS,
                "department_label": catalog.department_label,
                "document_types": catalog.DOCUMENT_TYPES,
                "document_type_label": catalog.document_type_label,
                "time_slots": catalog.TIME_SLOTS,
                "editing": editing,
                "months": catalog.MONTHS,
                "years": catalog.booking_years(datetime.now(CLINIC_TZ).year),
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(
        request: Request,
        patient_id: str | None = Query(default=None, max_length=64),
        booked: str | None = Query(default=None, max_length=64),
        cancelled: str | None = Query(default=None, max_length=64),
        updated: str | None = Query(default=None, max_length=64),
    ):
        if auth_enabled and not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        notice = None
        if booked:
            notice = f"התור {booked} נקבע בהצלחה."
        elif cancelled:
            notice = f"התור {cancelled} בוטל."
        elif updated:
            notice = f"התור {updated} עודכן."
        return render_dashboard(request, search_value=(patient_id or "").strip(), notice=notice)

    @app.post("/appointments", response_class=HTMLResponse, include_in_schema=False)
    def book_appointment(
        request: Request,
        csrf_token_field: str = Form("", alias="csrf_token"),
        patient_id: str = Form(""),
        department: str = Form(""),
        doctor_name: str = Form(""),
        appointment_day: str = Form(""),
        appointment_month: str = Form(""),
        appointment_year: str = Form(""),
        appointment_time: str = Form(""),
        location: str = Form(""),
        required_documents: list[str] = Form(default=[]),
    ):
        """Books an appointment for a patient HospitalAgent's registry knows. Fails closed: an
        unknown patient, an unreachable registry or no registry at all books nothing."""
        if auth_enabled and not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        if not csrf_valid(request, csrf_token_field):
            return forbidden()
        form = {
            "patient_id": patient_id.strip(),
            "department": department.strip(),
            "doctor_name": doctor_name.strip(),
            "appointment_day": appointment_day.strip(),
            "appointment_month": appointment_month.strip(),
            "appointment_year": appointment_year.strip(),
            "appointment_time": appointment_time.strip(),
            "location": location.strip(),
            "required_documents": [c for c in required_documents if c in catalog.DOCUMENT_TYPE_CODES],
        }
        error, when = _validate_booking(form)
        if error:
            return render_dashboard(request, error=error, form=form, status_code=400)
        doc_error, codes = _validate_required_documents(required_documents)
        if doc_error:
            return render_dashboard(request, error=doc_error, form=form, status_code=400)

        started = time.perf_counter()
        registry = request.app.state.patient_registry
        with request.app.state.SessionLocal() as session:
            def refuse(result: str, message: str, status_code: int) -> HTMLResponse:
                _write_audit(session, case_id=actor(request), execution_id=str(uuid4()),
                             patient_id=form["patient_id"], result=result,
                             latency_ms=round((time.perf_counter() - started) * 1000),
                             operation="BookAppointment")
                return render_dashboard(request, error=message, form=form, status_code=status_code)

            if registry is None:
                return refuse("technical_failure",
                              "בדיקת המטופלים אינה מוגדרת (PATIENT_REGISTRY_URL), ולכן לא ניתן לאמת את המטופל ולקבוע תור.",
                              503)
            try:
                known = registry.exists(form["patient_id"])
            except RegistryUnavailable:
                logger.warning("patient registry unavailable")
                return refuse("technical_failure",
                              "לא ניתן לאמת את המטופל כרגע (מאגר המטופלים אינו זמין). התור לא נקבע.", 503)
            if not known:
                return refuse("patient_not_found",
                              "המטופל אינו רשום במאגר המטופלים. התור לא נקבע.", 400)

            appointment_id = _new_appointment_id(session)
            session.add(Appointment(
                appointment_id=appointment_id,
                patient_id=form["patient_id"],
                department=form["department"],
                doctor_name=form["doctor_name"] or None,
                appointment_at=when,
                location=form["location"] or None,
                status="Scheduled",
                required_document_links=[AppointmentRequiredDocument(document_type=c) for c in codes],
            ))
            # One commit: the appointment and its audit row land together or not at all.
            _write_audit(session, case_id=actor(request), execution_id=appointment_id,
                         patient_id=form["patient_id"], result="booked",
                         latency_ms=round((time.perf_counter() - started) * 1000),
                         operation="BookAppointment")
        return RedirectResponse(f"/?booked={appointment_id}", status_code=303)

    def form_of(appointment: Appointment) -> dict[str, str]:
        at = appointment.appointment_at
        if at.tzinfo is not None:
            at = at.astimezone(CLINIC_TZ)
        return {
            "patient_id": appointment.patient_id,
            "department": appointment.department,
            "doctor_name": appointment.doctor_name or "",
            "location": appointment.location or "",
            "appointment_day": str(at.day),
            "appointment_month": str(at.month),
            "appointment_year": str(at.year),
            "appointment_time": at.strftime("%H:%M"),
            "required_documents": appointment.required_documents,
        }

    def editable(session: Session, request: Request, appointment_id: str):
        """(appointment, None), or (None, the page that says why it cannot be edited)."""
        appointment = session.get(Appointment, appointment_id)
        if appointment is None:
            return None, render_dashboard(request, error=f"התור {appointment_id} לא נמצא.",
                                          status_code=404)
        if appointment.status != "Scheduled":
            return None, render_dashboard(
                request, error=f"התור {appointment_id} אינו מתוכנן, ולכן אי אפשר לערוך אותו.",
                status_code=409)
        return appointment, None

    @app.get("/appointments/{appointment_id}/edit", response_class=HTMLResponse,
             include_in_schema=False)
    def edit_appointment_page(request: Request, appointment_id: str = ApiPath(max_length=64)):
        if auth_enabled and not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        with request.app.state.SessionLocal() as session:
            appointment, refusal = editable(session, request, appointment_id)
        if refusal:
            return refusal
        return render_dashboard(request, form=form_of(appointment), editing=appointment)

    @app.post("/appointments/{appointment_id}", response_class=HTMLResponse,
              include_in_schema=False)
    def update_appointment(
        request: Request,
        appointment_id: str = ApiPath(max_length=64),
        csrf_token_field: str = Form("", alias="csrf_token"),
        department: str = Form(""),
        doctor_name: str = Form(""),
        appointment_day: str = Form(""),
        appointment_month: str = Form(""),
        appointment_year: str = Form(""),
        appointment_time: str = Form(""),
        location: str = Form(""),
        required_documents: list[str] = Form(default=[]),
    ):
        """Changes a scheduled appointment in place, under the same rules as a booking. The
        patient never changes: a patient_id in the form is ignored, since moving an
        appointment to someone else is a new booking."""
        if auth_enabled and not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        if not csrf_valid(request, csrf_token_field):
            return forbidden()
        started = time.perf_counter()
        with request.app.state.SessionLocal() as session:
            appointment, refusal = editable(session, request, appointment_id)
            if refusal:
                return refusal
            form = {
                "patient_id": appointment.patient_id,
                "department": department.strip(),
                "doctor_name": doctor_name.strip(),
                "appointment_day": appointment_day.strip(),
                "appointment_month": appointment_month.strip(),
                "appointment_year": appointment_year.strip(),
                "appointment_time": appointment_time.strip(),
                "location": location.strip(),
                "required_documents": [c for c in required_documents if c in catalog.DOCUMENT_TYPE_CODES],
            }
            error, when = _validate_booking(form)
            if error:
                return render_dashboard(request, error=error, form=form, editing=appointment,
                                        status_code=400)
            doc_error, codes = _validate_required_documents(required_documents)
            if doc_error:
                return render_dashboard(request, error=doc_error, form=form, editing=appointment,
                                        status_code=400)
            appointment.department = form["department"]
            appointment.doctor_name = form["doctor_name"] or None
            appointment.location = form["location"] or None
            appointment.appointment_at = when
            appointment.required_document_links = [AppointmentRequiredDocument(document_type=c) for c in codes]
            # One commit: the change and its audit row land together or not at all.
            _write_audit(session, case_id=actor(request), execution_id=appointment_id,
                         patient_id=appointment.patient_id, result="updated",
                         latency_ms=round((time.perf_counter() - started) * 1000),
                         operation="UpdateAppointment")
        return RedirectResponse(f"/?updated={appointment_id}", status_code=303)

    @app.post("/appointments/{appointment_id}/cancel", response_class=HTMLResponse,
              include_in_schema=False)
    def cancel_appointment(
        request: Request,
        appointment_id: str = ApiPath(max_length=64),
        csrf_token_field: str = Form("", alias="csrf_token"),
    ):
        """Marks a scheduled appointment Cancelled. Nothing is ever deleted."""
        if auth_enabled and not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        if not csrf_valid(request, csrf_token_field):
            return forbidden()
        started = time.perf_counter()
        with request.app.state.SessionLocal() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                return render_dashboard(request, error=f"התור {appointment_id} לא נמצא.", status_code=404)
            if appointment.status != "Scheduled":
                return render_dashboard(request, error=f"התור {appointment_id} כבר אינו מתוכנן.",
                                        status_code=409)
            appointment.status = "Cancelled"
            _write_audit(session, case_id=actor(request), execution_id=appointment_id,
                         patient_id=appointment.patient_id, result="cancelled",
                         latency_ms=round((time.perf_counter() - started) * 1000),
                         operation="CancelAppointment")
        return RedirectResponse(f"/?cancelled={appointment_id}", status_code=303)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, configured: bool = False):
        if not auth_enabled:
            return RedirectResponse("/", status_code=303)
        if authenticated(request):
            return RedirectResponse("/", status_code=303)
        with request.app.state.SessionLocal() as session:
            has_admin = active_admin(session) is not None
        if not has_admin:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": None, "configured": configured},
        )

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        code: str = Form(...),
    ):
        with request.app.state.SessionLocal() as session:
            admin = session.scalar(
                select(AdminUser).where(
                    AdminUser.username == username.strip(),
                    AdminUser.is_active.is_(True),
                )
            )
            valid = False
            if admin:
                try:
                    password_hasher.verify(admin.password_hash, password)
                    valid = pyotp.TOTP(admin.totp_secret).verify(code.strip(), valid_window=1)
                except VerifyMismatchError:
                    valid = False
            if valid:
                request.session.clear()
                request.session["authenticated"] = True
                request.session["username"] = admin.username
                return RedirectResponse("/", status_code=303)

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "שם המשתמש, הסיסמה או הקוד אינם תקינים.", "configured": False},
            status_code=401,
        )

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page(request: Request):
        if not auth_enabled:
            return RedirectResponse("/", status_code=303)
        with request.app.state.SessionLocal() as session:
            if active_admin(session):
                return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="setup.html",
            context={"error": None},
        )

    @app.post("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_admin(
        request: Request,
        setup_token: str = Form(...),
        username: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
    ):
        error = None
        clean_username = username.strip()
        if not setup_token_value or not secrets.compare_digest(
            setup_token, setup_token_value
        ):
            error = "קוד ההקמה אינו תקין."
        elif len(clean_username) < 3:
            error = "שם המשתמש חייב להכיל לפחות 3 תווים."
        elif len(password) < 12:
            error = "הסיסמה חייבת להכיל לפחות 12 תווים."
        elif password != password_confirm:
            error = "הסיסמאות אינן תואמות."

        if error:
            return templates.TemplateResponse(
                request=request,
                name="setup.html",
                context={"error": error},
                status_code=400,
            )

        with request.app.state.SessionLocal() as session:
            if active_admin(session):
                return RedirectResponse("/login", status_code=303)
            pending = session.scalar(select(AdminUser).where(AdminUser.is_active.is_(False)))
            if pending:
                session.delete(pending)
                session.flush()
            admin = AdminUser(
                admin_id=str(uuid4()),
                username=clean_username,
                password_hash=password_hasher.hash(password),
                totp_secret=pyotp.random_base32(),
                is_active=False,
            )
            session.add(admin)
            session.commit()
            request.session.clear()
            request.session["pending_admin_id"] = admin.admin_id

        return RedirectResponse("/setup/authenticator", status_code=303)

    @app.get("/setup/authenticator", response_class=HTMLResponse, include_in_schema=False)
    def setup_authenticator(request: Request, error: bool = False):
        admin_id = request.session.get("pending_admin_id")
        if not admin_id:
            return RedirectResponse("/setup", status_code=303)
        with request.app.state.SessionLocal() as session:
            admin = session.get(AdminUser, admin_id)
            if not admin or admin.is_active:
                return RedirectResponse("/login", status_code=303)
            manual_key = admin.totp_secret
        return templates.TemplateResponse(
            request=request,
            name="setup_authenticator.html",
            context={"error": error, "manual_key": manual_key},
        )

    @app.get("/setup/qr", include_in_schema=False)
    def setup_qr(request: Request):
        admin_id = request.session.get("pending_admin_id")
        if not admin_id:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        with request.app.state.SessionLocal() as session:
            admin = session.get(AdminUser, admin_id)
            if not admin or admin.is_active:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            uri = pyotp.TOTP(admin.totp_secret).provisioning_uri(
                name=admin.username,
                issuer_name="Appointment Service",
            )
        output = BytesIO()
        segno.make(uri).save(output, kind="png", scale=6, border=2)
        output.seek(0)
        return StreamingResponse(output, media_type="image/png")

    @app.post("/setup/verify", include_in_schema=False)
    def verify_setup(request: Request, code: str = Form(...)):
        admin_id = request.session.get("pending_admin_id")
        if not admin_id:
            return RedirectResponse("/setup", status_code=303)
        with request.app.state.SessionLocal() as session:
            admin = session.get(AdminUser, admin_id)
            if not admin or not pyotp.TOTP(admin.totp_secret).verify(
                code.strip(), valid_window=1
            ):
                return RedirectResponse("/setup/authenticator?error=true", status_code=303)
            admin.is_active = True
            session.commit()
        request.session.clear()
        return RedirectResponse("/login?configured=true", status_code=303)

    @app.get(
        "/health",
        response_model=HealthResult,
        responses={503: {"model": ErrorResult}},
        tags=["Operations"],
    )
    def health(request: Request) -> HealthResult | JSONResponse:
        try:
            with request.app.state.SessionLocal() as session:
                session.execute(text("SELECT 1"))
            return HealthResult(
                status="ok",
                database="ok",
                version=request.app.state.settings.service_version,
            )
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={"error": "service_unavailable", "message": "Database is unavailable"},
            )

    @app.get(
        "/api/v1/patients/{patient_id}/appointment",
        response_model=AppointmentResult,
        responses={
            400: {"model": ErrorResult},
            401: {"model": ErrorResult},
            404: {"model": ErrorResult},
            503: {"model": ErrorResult},
            504: {"model": ErrorResult},
        },
        tags=["Appointments"],
        operation_id="CheckAppointment",
    )
    def check_appointment(
        request: Request,
        response: Response,
        patient_id: str = ApiPath(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        x_case_id: str | None = Header(default=None, alias="X-Case-ID"),
        x_execution_id: str | None = Header(default=None, alias="X-Execution-ID"),
        supplied_api_key: str | None = Security(api_key_header),
    ) -> AppointmentResult | JSONResponse:
        if api_auth and (
            not api_key_value
            or not supplied_api_key
            or not secrets.compare_digest(supplied_api_key, api_key_value)
        ):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "A valid API key is required"},
                headers={"WWW-Authenticate": "ApiKey"},
            )
        started = time.perf_counter()
        case_id = (x_case_id or str(uuid4()))[:128]
        execution_id = (x_execution_id or str(uuid4()))[:128]
        response.headers["X-Case-ID"] = case_id
        response.headers["X-Execution-ID"] = execution_id

        with request.app.state.SessionLocal() as session:
            try:
                cfg: Settings = request.app.state.settings
                if (
                    cfg.enable_failure_simulation
                    and patient_id == cfg.mock_timeout_patient_id
                ):
                    raise SimulatedTimeout

                refusal = _registry_refusal(session, request.app.state.patient_registry,
                                            patient_id=patient_id, case_id=case_id,
                                            execution_id=execution_id, started=started,
                                            operation="CheckAppointment")
                if refusal is not None:
                    return refusal

                appointment = session.scalar(
                    select(Appointment)
                    .where(
                        Appointment.patient_id == patient_id,
                        Appointment.status == "Scheduled",
                    )
                    .order_by(Appointment.appointment_at.asc())
                    .limit(1)
                )
                result = "found" if appointment else "not_found"
                latency = round((time.perf_counter() - started) * 1000)
                _write_audit(
                    session,
                    case_id=case_id,
                    execution_id=execution_id,
                    patient_id=patient_id,
                    result=result,
                    latency_ms=latency,
                )
                return AppointmentResult(found=bool(appointment), appointment=appointment)
            except SimulatedTimeout:
                latency = round((time.perf_counter() - started) * 1000)
                _write_audit(
                    session,
                    case_id=case_id,
                    execution_id=execution_id,
                    patient_id=patient_id,
                    result="technical_failure",
                    latency_ms=latency,
                )
                return JSONResponse(
                    status_code=504,
                    content={"error": "timeout", "message": "Appointment lookup timed out"},
                    headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id},
                )
            except (OperationalError, SQLAlchemyError):
                session.rollback()
                logger.exception("Appointment database operation failed")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Appointment service is temporarily unavailable",
                    },
                    headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id},
                )

    @app.get(
        "/api/v1/patients/{patient_id}/appointments",
        response_model=AppointmentList,
        responses={400: {"model": ErrorResult}, 401: {"model": ErrorResult}, 404: {"model": ErrorResult},
                   503: {"model": ErrorResult}, 504: {"model": ErrorResult}},
        tags=["Appointments"],
        operation_id="ListAppointments",
    )
    def list_appointments(
        request: Request,
        response: Response,
        patient_id: str = ApiPath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        x_case_id: str | None = Header(default=None, alias="X-Case-ID"),
        x_execution_id: str | None = Header(default=None, alias="X-Execution-ID"),
        supplied_api_key: str | None = Security(api_key_header),
    ) -> AppointmentList | JSONResponse:
        """Every appointment of the patient (Scheduled and Cancelled) with from <= at < to, oldest
        first, at most MAX_LIST (then truncated). The rows hold Israel wall-clock time without an
        offset (AppointmentOut._israel_time), so the window is compared in that zone (from/to may
        be sent in any timezone; they are converted here). The DST fall-back hour makes that
        local wall-clock time ambiguous for one hour a year; a window that falls inside it may
        miss a row stored during it - accepted."""
        if api_auth and (
            not api_key_value
            or not supplied_api_key
            or not secrets.compare_digest(supplied_api_key, api_key_value)
        ):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "A valid API key is required"},
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if start.tzinfo is None or end.tzinfo is None or not start < end or end - start > MAX_LIST_WINDOW:
            return JSONResponse(status_code=400, content={"error": "validation_error",
                                "message": "from and to must be timezone-aware, from < to, at most 366 days apart"})
        try:
            # A year near 0001 or 9999 passes the checks above but overflows datetime's own
            # range once converted to Israel time (e.g. year 1 minus a few hours of offset).
            low = start.astimezone(ISRAEL).replace(tzinfo=None)
            high = end.astimezone(ISRAEL).replace(tzinfo=None)
        except (OverflowError, ValueError):
            return JSONResponse(status_code=400, content={"error": "validation_error",
                                "message": "from and to are outside the supported date range"})
        started = time.perf_counter()
        case_id = (x_case_id or str(uuid4()))[:128]
        execution_id = (x_execution_id or str(uuid4()))[:128]
        response.headers["X-Case-ID"] = case_id
        response.headers["X-Execution-ID"] = execution_id
        with request.app.state.SessionLocal() as session:
            try:
                cfg: Settings = request.app.state.settings
                if cfg.enable_failure_simulation and patient_id == cfg.mock_timeout_patient_id:
                    raise SimulatedTimeout
                refusal = _registry_refusal(session, request.app.state.patient_registry, patient_id=patient_id,
                                            case_id=case_id, execution_id=execution_id, started=started,
                                            operation="ListAppointments")
                if refusal is not None:
                    return refusal
                rows = session.scalars(
                    select(Appointment)
                    .where(Appointment.patient_id == patient_id,
                           Appointment.appointment_at >= low, Appointment.appointment_at < high)
                    .order_by(Appointment.appointment_at.asc(), Appointment.appointment_id.asc())
                    .limit(MAX_LIST + 1)
                ).all()
                _write_audit(session, case_id=case_id, execution_id=execution_id, patient_id=patient_id,
                             result="found" if rows else "not_found", operation="ListAppointments",
                             latency_ms=round((time.perf_counter() - started) * 1000))
                return AppointmentList(appointments=rows[:MAX_LIST], truncated=len(rows) > MAX_LIST)
            except SimulatedTimeout:
                latency = round((time.perf_counter() - started) * 1000)
                _write_audit(
                    session,
                    case_id=case_id,
                    execution_id=execution_id,
                    patient_id=patient_id,
                    result="technical_failure",
                    operation="ListAppointments",
                    latency_ms=latency,
                )
                return JSONResponse(
                    status_code=504,
                    content={"error": "timeout", "message": "Appointment lookup timed out"},
                    headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id},
                )
            except (OperationalError, SQLAlchemyError):
                session.rollback()
                logger.exception("Appointment database operation failed")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Appointment service is temporarily unavailable",
                    },
                    headers={"X-Case-ID": case_id, "X-Execution-ID": execution_id},
                )

    return app


app = create_app()
