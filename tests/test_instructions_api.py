"""Sub-project 18 task 2: AppointmentOut's exam_type/instruction, CheckAppointment's optional
?appointment_id= and upcoming_count, and the new GET /api/v1/instructions/{source_id} (design
D3, D6, D8-D9)."""
import os

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.catalog import EXAM_TYPES
from app.main import create_app
from app.models import Appointment, AppointmentExamType, AuditLog

IL = ZoneInfo("Asia/Jerusalem")
KEY = {"X-API-Key": "test-key"}


class FakeRegistry:
    names = {"P-10041": "דנה כהן", "P-20000": "יוסי לוי", "P-30000": "מיכל אברהם"}

    def exists(self, patient_id):
        return patient_id in self.names


def make(tmp_path, registry="default"):
    app = create_app(f"sqlite:///{(tmp_path / 't.db').as_posix()}", seed_demo_data=True,
                     patient_registry=FakeRegistry() if registry == "default" else registry,
                     api_auth_enabled=True, appointment_api_key="test-key")
    return app, TestClient(app)


def add(app, appointment_id, at, status="Scheduled", patient_id="P-10041", exam_code=None):
    with app.state.SessionLocal() as s:
        s.add(Appointment(appointment_id=appointment_id, patient_id=patient_id, department="Neurology",
                          appointment_at=at, status=status,
                          exam_type_link=AppointmentExamType(exam_code=exam_code) if exam_code else None))
        s.commit()


def last_audit(app):
    with app.state.SessionLocal() as s:
        row = s.scalars(select(AuditLog).order_by(AuditLog.timestamp.desc())).first()
        return (row.operation, row.result, row.patient_id)


# --- AppointmentOut's exam_type / instruction -------------------------------------------------

def test_check_appointment_reports_the_linked_exam_and_its_instruction(tmp_path):
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY).json()
    exam = body["appointment"]["exam_type"]
    instruction = body["appointment"]["instruction"]
    assert exam == {"code": "NEURO_VISIT", "label": "ביקור במרפאה נוירולוגית"}
    assert instruction["source_id"] == "INSTR-NEURO-VISIT"
    assert instruction["version"] == "1"
    assert instruction["title"] == "הכנה לביקור במרפאה נוירולוגית"
    assert "text" not in instruction  # the text is not in the appointment answer (design D3)


def test_an_appointment_with_no_exam_link_resolves_to_the_department_default(tmp_path):
    app, client = make(tmp_path)
    with client:
        add(app, "APT-NOLINK", datetime(2030, 1, 1, 9, 0, tzinfo=IL), patient_id="P-30000")
        body = client.get("/api/v1/patients/P-30000/appointment", headers=KEY).json()
    assert body["appointment"]["exam_type"]["code"] == "NEURO_VISIT"  # Neurology's default


def test_a_non_default_linked_exam_is_reported_over_the_api(tmp_path):
    """Fix round 1 I2: P-20000's seeded APT-8392 is linked to CARD_STRESS (not Cardiology's
    default, CARD_VISIT) - the API must report the link, not the department default."""
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-20000/appointment", headers=KEY).json()
    assert body["appointment"]["appointment_id"] == "APT-8392"
    assert body["appointment"]["exam_type"]["code"] == "CARD_STRESS"
    assert body["appointment"]["instruction"]["source_id"] == "INSTR-CARD-STRESS"


def test_a_list_appointments_row_with_a_non_default_link_shows_it(tmp_path):
    """Fix round 1 I2, the other half: ListAppointments rows go through the same AppointmentOut
    resolution, so a non-default link must show up there too."""
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-20000/appointments", headers=KEY,
                          params={"from": "2026-01-01T00:00:00+00:00", "to": "2027-01-01T00:00:00+00:00"}).json()
    [row] = body["appointments"]
    assert row["appointment_id"] == "APT-8392"
    assert row["exam_type"]["code"] == "CARD_STRESS"
    assert row["instruction"]["source_id"] == "INSTR-CARD-STRESS"


# --- CheckAppointment's ?appointment_id= -------------------------------------------------------

def test_without_the_parameter_behavior_is_unchanged(tmp_path):
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY).json()
    assert body["found"] is True
    assert body["appointment"]["appointment_id"] == "APT-8391"


def test_the_chosen_appointment_is_returned_when_it_is_the_patients_own(tmp_path):
    app, client = make(tmp_path)
    with client:
        add(app, "APT-LATER", datetime(2030, 6, 1, 9, 0, tzinfo=IL))
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                          params={"appointment_id": "APT-LATER"}).json()
    assert body["found"] is True
    assert body["appointment"]["appointment_id"] == "APT-LATER"


def test_another_patients_appointment_id_is_never_returned(tmp_path):
    """Ownership: APT-8392 belongs to P-20000, not P-10041."""
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                          params={"appointment_id": "APT-8392"}).json()
    assert body == {"found": False, "appointment": None, "upcoming_count": 1}


def test_a_cancelled_chosen_appointment_is_found_false(tmp_path):
    app, client = make(tmp_path)
    with client:
        add(app, "APT-GONE", datetime(2030, 1, 1, 9, 0, tzinfo=IL), status="Cancelled")
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                          params={"appointment_id": "APT-GONE"}).json()
    assert body["found"] is False and body["appointment"] is None


def test_an_unknown_chosen_appointment_id_is_found_false(tmp_path):
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                          params={"appointment_id": "APT-NOPE"}).json()
    assert body["found"] is False and body["appointment"] is None


def test_a_chosen_appointment_never_substitutes_another_one(tmp_path):
    """A patient with several appointments who asks about one that is not theirs (or not
    Scheduled) gets found=false - never a different, real appointment instead (design D6)."""
    app, client = make(tmp_path)
    with client:
        add(app, "APT-MINE", datetime(2030, 1, 1, 9, 0, tzinfo=IL))
        body = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                          params={"appointment_id": "APT-NOPE"}).json()
    assert body["found"] is False and body["appointment"] is None


def test_a_bad_appointment_id_pattern_is_400(tmp_path):
    app, client = make(tmp_path)
    with client:
        response = client.get("/api/v1/patients/P-10041/appointment", headers=KEY,
                              params={"appointment_id": " bad id "})
    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


# --- upcoming_count -----------------------------------------------------------------------

def test_upcoming_count_counts_only_scheduled_future_appointments(tmp_path):
    app, client = make(tmp_path)
    now = datetime.now(IL)
    with client:
        add(app, "APT-FUTURE-1", now + timedelta(days=10), patient_id="P-30000")
        add(app, "APT-FUTURE-2", now + timedelta(days=40), patient_id="P-30000")
        add(app, "APT-PAST", datetime(2020, 1, 1, 9, 0, tzinfo=IL), patient_id="P-30000")
        add(app, "APT-CANCELLED-FUTURE", now + timedelta(days=20), status="Cancelled", patient_id="P-30000")
        body = client.get("/api/v1/patients/P-30000/appointment", headers=KEY).json()
    assert body["upcoming_count"] == 2


def test_upcoming_count_is_limited_to_the_pickers_90_days(tmp_path):
    """Final review M4: only the appointments the hospital-agent's picker offers (the next 90
    days, NewRequest.tsx PICKER_DAYS) count - one inside the window, one beyond it: 1."""
    app, client = make(tmp_path)
    now = datetime.now(IL)
    with client:
        add(app, "APT-IN-WINDOW", now + timedelta(days=89), patient_id="P-30000")
        add(app, "APT-BEYOND", now + timedelta(days=91), patient_id="P-30000")
        body = client.get("/api/v1/patients/P-30000/appointment", headers=KEY).json()
    assert body["upcoming_count"] == 1


def test_upcoming_count_is_present_even_when_not_found(tmp_path):
    app, client = make(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-30000/appointment", headers=KEY).json()
    assert body == {"found": False, "appointment": None, "upcoming_count": 0}


def test_upcoming_count_boundary_around_now(tmp_path):
    """Fix round 1 M6: a Scheduled appointment a few seconds in the past does not count; one a
    few seconds in the future does - a real-clock check of the strict '>' boundary. Unequal
    numbers of past/future rows (1 vs 2), so an inverted comparison changes the count, not just
    which row is counted."""
    app, client = make(tmp_path)
    now = datetime.now(IL)
    with client:
        add(app, "APT-JUST-PAST", now - timedelta(seconds=20), patient_id="P-30000")
        add(app, "APT-JUST-FUTURE-1", now + timedelta(seconds=60), patient_id="P-30000")
        add(app, "APT-JUST-FUTURE-2", now + timedelta(seconds=90), patient_id="P-30000")
        body = client.get("/api/v1/patients/P-30000/appointment", headers=KEY).json()
    assert body["upcoming_count"] == 2


# --- fail closed: a catalog inconsistency is 503, never a 500 or a silent default (M4) --------

def window():
    return {"from": "2029-12-01T00:00:00+00:00", "to": "2030-02-01T00:00:00+00:00"}


def test_a_departmentless_appointment_is_503_on_check_appointment(tmp_path):
    app, client = make(tmp_path)
    with client:
        with app.state.SessionLocal() as s:
            s.add(Appointment(appointment_id="APT-BADDEPT", patient_id="P-30000", department="Radiology",
                              appointment_at=datetime(2030, 1, 1, 9, 0), status="Scheduled"))
            s.commit()
        response = client.get("/api/v1/patients/P-30000/appointment", headers=KEY)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert last_audit(app)[:2] == ("CheckAppointment", "technical_failure")


def test_a_departmentless_appointment_is_503_on_list_appointments(tmp_path):
    app, client = make(tmp_path)
    with client:
        with app.state.SessionLocal() as s:
            s.add(Appointment(appointment_id="APT-BADDEPT", patient_id="P-30000", department="Radiology",
                              appointment_at=datetime(2030, 1, 1, 9, 0), status="Scheduled"))
            s.commit()
        response = client.get("/api/v1/patients/P-30000/appointments", headers=KEY, params=window())
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert last_audit(app)[:2] == ("ListAppointments", "technical_failure")


def _add_stale_exam_type_row(session):
    """A DB row for an exam code that _seed_exam_types once wrote but app/catalog.py's EXAM_TYPES
    no longer lists (it never deletes a stale row - only upserts the current ones). The FK from
    appointment_exam_types needs this row to exist; the in-memory catalog is what must not."""
    from app.models import ExamTypeRow
    session.add(ExamTypeRow(code="EXAM_RETIRED", department="Cardiology", label_he="בדיקה שהוסרה",
                            instruction_id="INSTR-EXAM-RETIRED", instruction_version="1",
                            instruction_title="ישן", instruction_text="ישן"))


def test_a_stale_linked_exam_code_is_503_never_a_silent_default(tmp_path):
    """The link says EXAM_RETIRED, which used to be in the catalog and no longer is - this must
    never quietly resolve to the department default instead (M4)."""
    app, client = make(tmp_path)
    with client:
        with app.state.SessionLocal() as s:
            _add_stale_exam_type_row(s)
            s.add(Appointment(appointment_id="APT-STALE", patient_id="P-30000", department="Cardiology",
                              appointment_at=datetime(2030, 1, 1, 9, 0), status="Scheduled",
                              exam_type_link=AppointmentExamType(exam_code="EXAM_RETIRED")))
            s.commit()
        response = client.get("/api/v1/patients/P-30000/appointment", headers=KEY)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_a_stale_linked_exam_code_is_503_on_list_appointments(tmp_path):
    app, client = make(tmp_path)
    with client:
        with app.state.SessionLocal() as s:
            _add_stale_exam_type_row(s)
            s.add(Appointment(appointment_id="APT-STALE", patient_id="P-30000", department="Cardiology",
                              appointment_at=datetime(2030, 1, 1, 9, 0), status="Scheduled",
                              exam_type_link=AppointmentExamType(exam_code="EXAM_RETIRED")))
            s.commit()
        response = client.get("/api/v1/patients/P-30000/appointments", headers=KEY, params=window())
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


# --- GET /api/v1/instructions/{source_id} ----------------------------------------------------

def test_a_known_instruction_is_returned(tmp_path):
    app, client = make(tmp_path)
    exam = next(e for e in EXAM_TYPES if e.code == "CARD_ECHO")
    with client:
        response = client.get(f"/api/v1/instructions/{exam.instruction_id}", headers=KEY,
                              params={"version": exam.instruction_version})
    assert response.status_code == 200
    assert response.json() == {"source_id": exam.instruction_id, "version": exam.instruction_version,
                               "title": exam.instruction_title, "text": exam.instruction_text}


def test_every_catalog_instruction_is_reachable(tmp_path):
    app, client = make(tmp_path)
    with client:
        for exam in EXAM_TYPES:
            response = client.get(f"/api/v1/instructions/{exam.instruction_id}", headers=KEY,
                                  params={"version": exam.instruction_version})
            assert response.status_code == 200, exam.code


def test_an_unknown_source_id_is_404(tmp_path):
    app, client = make(tmp_path)
    with client:
        response = client.get("/api/v1/instructions/INSTR-NOPE", headers=KEY, params={"version": "1"})
    assert response.status_code == 404
    assert response.json() == {"error": "instruction_not_found"}


def test_a_known_source_with_the_wrong_version_is_404(tmp_path):
    app, client = make(tmp_path)
    with client:
        response = client.get("/api/v1/instructions/INSTR-CARD-ECHO", headers=KEY, params={"version": "2"})
    assert response.status_code == 404
    assert response.json()["error"] == "instruction_not_found"


def test_a_missing_version_is_400_not_treated_as_found(tmp_path):
    """Fix round 1 M6: pinned to 400 - this app's global RequestValidationError handler always
    answers 400, never FastAPI's default 422, so the contract should say so exactly."""
    app, client = make(tmp_path)
    with client:
        response = client.get("/api/v1/instructions/INSTR-CARD-ECHO", headers=KEY)
    assert response.status_code == 400


def test_a_bad_source_id_pattern_is_400(tmp_path):
    app, client = make(tmp_path)
    with client:
        response = client.get("/api/v1/instructions/%20bad", headers=KEY, params={"version": "1"})
    assert response.status_code == 400


def test_a_missing_or_wrong_key_is_401(tmp_path):
    app, client = make(tmp_path)
    with client:
        missing = client.get("/api/v1/instructions/INSTR-CARD-ECHO", params={"version": "1"})
        wrong = client.get("/api/v1/instructions/INSTR-CARD-ECHO", params={"version": "1"},
                           headers={"X-API-Key": "nope"})
    assert missing.status_code == 401 and wrong.status_code == 401


def test_a_db_failure_writing_the_audit_row_is_503_not_a_500(tmp_path, monkeypatch):
    """Fix round 1 M5: GetInstruction's audit write is now under the same
    OperationalError/SQLAlchemyError -> 503 handling as CheckAppointment/ListAppointments."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as OrmSession

    app, client = make(tmp_path)

    def broken_commit(self, *a, **kw):
        raise OperationalError("stmt", {}, Exception("boom"))

    with client:
        monkeypatch.setattr(OrmSession, "commit", broken_commit)
        response = client.get("/api/v1/instructions/INSTR-CARD-ECHO", headers=KEY, params={"version": "1"})
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_the_audit_row_has_no_patient_and_the_right_operation(tmp_path):
    app, client = make(tmp_path)
    with client:
        client.get("/api/v1/instructions/INSTR-CARD-ECHO", headers=KEY, params={"version": "1"})
    assert last_audit(app) == ("GetInstruction", "found", "")


def test_an_unknown_instructions_audit_row_says_not_found(tmp_path):
    app, client = make(tmp_path)
    with client:
        client.get("/api/v1/instructions/INSTR-NOPE", headers=KEY, params={"version": "1"})
    assert last_audit(app) == ("GetInstruction", "not_found", "")
