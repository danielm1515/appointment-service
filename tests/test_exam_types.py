"""Sub-project 18 task 1: the exam-type catalog, its tables, the booking UI's exam-type select,
and the one-time backfill of the two demo appointments (design D1-D2, §4)."""
import os

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import catalog
from app.catalog import DEPARTMENTS, EXAM_TYPES, EXAMS_BY_CODE
from app.main import create_app
from app.models import Appointment, AppointmentExamType, ExamTypeRow

CODES = [e.code for e in EXAM_TYPES]
NEXT_YEAR = datetime.now(ZoneInfo("Asia/Jerusalem")).year + 1


class FakeRegistry:
    names = {"P-10041": "דנה כהן", "P-20000": "יוסי לוי", "P-30000": "מיכל אברהם"}

    def exists(self, patient_id):
        return patient_id in self.names

    def list_patients(self):
        return sorted(self.names.items())


def make_client(tmp_path, **kwargs):
    app = create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", seed_demo_data=True,
                     patient_registry=FakeRegistry(), **kwargs)
    return app, TestClient(app, follow_redirects=False)


def csrf(client):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text).group(1)


def exam_code_of(app, appointment_id):
    with app.state.SessionLocal() as session:
        return session.get(Appointment, appointment_id).exam_code


# --- the catalog --------------------------------------------------------------------------

def test_the_catalog_has_exactly_the_13_design_exams():
    assert len(EXAM_TYPES) == 13
    assert len(CODES) == len(set(CODES)) == 13


def test_every_department_has_exactly_one_visit_exam():
    for department in DEPARTMENTS:
        visits = [e for e in EXAM_TYPES if e.department == department.value and e.code.endswith("_VISIT")]
        assert len(visits) == 1, department.value
        assert catalog.default_exam(department.value) == visits[0]


def test_every_exams_department_and_documents_are_in_the_catalogs():
    department_values = {d.value for d in DEPARTMENTS}
    for exam in EXAM_TYPES:
        assert exam.department in department_values, exam.code
        assert set(exam.documents) <= catalog.DOCUMENT_TYPE_CODES, exam.code


def test_every_instruction_id_and_version_follow_the_rule():
    for exam in EXAM_TYPES:
        assert exam.instruction_id == "INSTR-" + exam.code.replace("_", "-")
        assert exam.instruction_version == "1"


def test_every_instruction_text_ends_with_the_disclaimer():
    disclaimer = "טיוטת דמו – טעונה אישור רפואי. בכל שאלה רפואית יש לפנות לצוות המטפל."
    for exam in EXAM_TYPES:
        assert exam.instruction_text.endswith(disclaimer), exam.code
        # no medication doses (design D4): a crude but effective guard against a stray "מ\"ג"/"mg"
        assert "מ\"ג" not in exam.instruction_text and "mg" not in exam.instruction_text.lower()


def test_exams_of_filters_by_department():
    cardiology_codes = {e.code for e in catalog.exams_of("Cardiology")}
    assert cardiology_codes == {"CARD_VISIT", "CARD_ECHO", "CARD_STRESS", "CARD_HOLTER"}


def test_exam_type_label_falls_back_to_the_code():
    assert catalog.exam_type_label("CARD_ECHO") == "אקו לב"
    assert catalog.exam_type_label("UNKNOWN") == "UNKNOWN"


def test_card_stress_suggests_ecg():
    """Fix round 1 M6: pin the one exam whose suggested documents are asserted by code."""
    assert EXAMS_BY_CODE["CARD_STRESS"].documents == ("ECG",)


def test_these_three_exams_suggest_no_documents():
    """Fix round 1 M7: CARD_HOLTER, NEURO_EMG and ORTHO_INJECTION no longer pre-tick anything -
    their §4 summaries don't point at any of the 5 catalog document types clearly enough."""
    for code in ("CARD_HOLTER", "NEURO_EMG", "ORTHO_INJECTION"):
        assert EXAMS_BY_CODE[code].documents == (), code


# --- the exam_types table (upserted every start, like document_types) ----------------------

def test_the_exam_type_table_is_seeded_on_every_start_even_without_demo_data(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", seed_demo_data=False,
                     patient_registry=FakeRegistry())
    with TestClient(app):
        with app.state.SessionLocal() as session:
            rows = {r.code: (r.department, r.label_he, r.instruction_id, r.instruction_version)
                    for r in session.scalars(select(ExamTypeRow))}
    assert set(rows) == set(CODES)
    for exam in EXAM_TYPES:
        assert rows[exam.code] == (exam.department, exam.label, exam.instruction_id, exam.instruction_version)


def test_seeding_the_exam_types_twice_changes_nothing(tmp_path):
    db = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    for _ in range(2):
        app = create_app(db, seed_demo_data=True, patient_registry=FakeRegistry())
        with TestClient(app):
            pass
    with app.state.SessionLocal() as session:
        assert session.scalar(text("SELECT count(*) FROM exam_types")) == 13


def test_a_changed_catalog_row_is_upserted_on_the_next_start(tmp_path, monkeypatch):
    """The update branch of _seed_exam_types (M6): an existing row is brought in line with a
    changed app/catalog.py, not just inserted once and left alone."""
    db = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    app1 = create_app(db, seed_demo_data=True, patient_registry=FakeRegistry())
    with TestClient(app1):
        pass

    import dataclasses
    from app import main as app_main
    changed = tuple(
        dataclasses.replace(e, label="תווית חדשה לבדיקה", instruction_title="כותרת חדשה",
                            instruction_text="טקסט חדש לבדיקה")
        if e.code == "CARD_ECHO" else e
        for e in EXAM_TYPES
    )
    monkeypatch.setattr(app_main.catalog, "EXAM_TYPES", changed)

    app2 = create_app(db, seed_demo_data=True, patient_registry=FakeRegistry())
    with TestClient(app2):
        with app2.state.SessionLocal() as session:
            row = session.get(ExamTypeRow, "CARD_ECHO")
    assert row.label_he == "תווית חדשה לבדיקה"
    assert row.instruction_title == "כותרת חדשה"
    assert row.instruction_text == "טקסט חדש לבדיקה"


# --- the backfill (design D2) ---------------------------------------------------------------

def test_the_two_demo_appointments_are_backfilled(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        assert exam_code_of(app, "APT-8391") == "NEURO_VISIT"
        assert exam_code_of(app, "APT-8392") == "CARD_STRESS"


def test_backfill_never_overwrites_an_exam_type_already_set(tmp_path):
    """A second start (or a start after an edit) must not clobber a link that already exists."""
    app, client = make_client(tmp_path)
    with client:
        with app.state.SessionLocal() as session:
            link = session.get(AppointmentExamType, "APT-8391")
            link.exam_code = "NEURO_EEG"
            session.commit()
    app2, client2 = make_client(tmp_path)  # re-open the same database
    with client2:
        assert exam_code_of(app2, "APT-8391") == "NEURO_EEG"


def test_backfill_skips_an_appointment_moved_to_another_department(tmp_path):
    """Fix round 1 I1: an old-schema APT-8391 that was moved to Cardiology before this service
    ever ran must not be linked to NEURO_VISIT - that would misdescribe a cardiology
    appointment as a neurology one. It stays unlinked and resolves to its own department's
    default (CARD_VISIT) instead."""
    import sqlite3
    db = tmp_path / "moved.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE appointments (appointment_id VARCHAR(64) PRIMARY KEY, patient_id VARCHAR(64) NOT NULL,
                department VARCHAR(120) NOT NULL, doctor_name VARCHAR(120), appointment_at DATETIME NOT NULL,
                location VARCHAR(200), status VARCHAR(32) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
            INSERT INTO appointments (appointment_id, patient_id, department, appointment_at, status)
                VALUES ('APT-8391', 'P-10041', 'Cardiology', '2026-10-03 10:30:00', 'Scheduled');
        """)
    app = create_app(f"sqlite:///{db.as_posix()}", seed_demo_data=True, patient_registry=FakeRegistry())
    with TestClient(app):
        assert exam_code_of(app, "APT-8391") is None


def test_an_appointment_with_no_exam_link_reports_none(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        with app.state.SessionLocal() as session:
            appointment = Appointment(appointment_id="APT-NOLINK", patient_id="P-30000",
                                      department="Cardiology", status="Scheduled",
                                      appointment_at=__import__("datetime").datetime(2030, 1, 1, 9, 0))
            session.add(appointment)
            session.commit()
        assert exam_code_of(app, "APT-NOLINK") is None


def test_the_new_code_starts_on_a_database_made_by_the_old_schema(tmp_path):
    """Going live: the file already has appointments but not the exam-type tables at all."""
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE appointments (appointment_id VARCHAR(64) PRIMARY KEY, patient_id VARCHAR(64) NOT NULL,
                department VARCHAR(120) NOT NULL, doctor_name VARCHAR(120), appointment_at DATETIME NOT NULL,
                location VARCHAR(200), status VARCHAR(32) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
            INSERT INTO appointments (appointment_id, patient_id, department, appointment_at, status)
                VALUES ('APT-8391', 'P-10041', 'Neurology', '2026-10-03 10:30:00', 'Scheduled');
            INSERT INTO appointments (appointment_id, patient_id, department, appointment_at, status)
                VALUES ('APT-8392', 'P-20000', 'Cardiology', '2026-10-07 09:00:00', 'Scheduled');
        """)
    app = create_app(f"sqlite:///{db.as_posix()}", seed_demo_data=True, patient_registry=FakeRegistry())
    with TestClient(app) as client:
        body = client.get("/api/v1/patients/P-10041/appointment").json()
        with app.state.SessionLocal() as session:
            violations = session.execute(text("PRAGMA foreign_key_check")).all()
            exam_count = session.scalar(text("SELECT count(*) FROM exam_types"))
    assert body["appointment"]["appointment_id"] == "APT-8391"
    assert exam_code_of(app, "APT-8391") == "NEURO_VISIT"
    assert exam_code_of(app, "APT-8392") == "CARD_STRESS"
    assert violations == [] and exam_count == 13


# --- the booking form's exam-type select -----------------------------------------------------

def test_the_form_offers_every_exam_type_grouped_by_department(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    select_tag = re.search(r'<select[^>]*name="exam_type"[^>]*>', page).group(0)
    select = re.search(r'<select[^>]*name="exam_type"[^>]*>(.*?)</select>', page, re.S)
    assert select, "the exam-type field must be a dropdown"
    options = re.findall(r'<option value="([^"]*)"', select.group(1))
    assert options == [""] + CODES
    # The select itself (M6): required, and filtered by department like doctor/location.
    assert "required" in select_tag
    assert 'data-filter-by="book_department"' in select_tag
    for exam in EXAM_TYPES:
        option = re.search(rf'<option value="{exam.code}"[^>]*>', select.group(1)).group(0)
        assert f'data-department="{exam.department}"' in option
        assert f'data-documents="{",".join(exam.documents)}"' in option


def test_the_pretick_script_adds_without_unchecking_anything(tmp_path):
    """M6, as far as a template test can (no JS runtime here): the pre-tick script must only
    ever set a box to checked, never uncheck one - so a manually-ticked document that isn't
    among the newly chosen exam's suggestions survives switching the exam type."""
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    script = re.search(r"Choosing an exam type pre-ticks.*?</script>", page, re.S).group(0)
    assert "box.checked = true" in script
    assert "box.checked = false" not in script
    assert re.search(r"required_documents.*forEach", script, re.S)
    # It must not reset every box before applying the suggestion (that would be replace, not add).
    assert not re.search(r"box\.checked\s*=\s*false", script)


def test_an_unlinked_appointment_shows_its_resolved_default_on_the_dashboard(tmp_path):
    """Fix round 1 I3: an unlinked appointment isn't blank on the dashboard - it shows the
    department's default exam, marked as a resolved default rather than a real choice."""
    app, client = make_client(tmp_path)
    with client:
        with app.state.SessionLocal() as session:
            session.add(Appointment(appointment_id="APT-UNLINKED", patient_id="P-30000",
                                    department="Ophthalmology", status="Scheduled",
                                    appointment_at=datetime(2030, 1, 1, 9, 0)))
            session.commit()
        page = client.get("/").text
    assert "בדיקת עיניים (ברירת מחדל)" in page  # Ophthalmology's default is OPHTH_VISIT
    row = re.search(r"<tr><td><span class=\"id\">APT-UNLINKED.*?</tr>", page, re.S).group(0)
    assert "(ברירת מחדל)" in row


def test_booking_stores_the_chosen_exam_type(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data={
            "csrf_token": csrf(client), "patient_id": "P-30000", "department": "Cardiology",
            "exam_type": "CARD_ECHO", "appointment_day": "14", "appointment_month": "5",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "09:15",
        })
    assert response.status_code == 303
    booked_id = response.headers["location"].split("=")[1]
    assert exam_code_of(app, booked_id) == "CARD_ECHO"


def test_booking_without_an_exam_type_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data={
            "csrf_token": csrf(client), "patient_id": "P-30000", "department": "Cardiology",
            "exam_type": "", "appointment_day": "14", "appointment_month": "5",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "09:15",
        })
    assert response.status_code == 400
    assert "סוג בדיקה" in response.text


def test_booking_with_an_exam_type_of_another_department_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data={
            "csrf_token": csrf(client), "patient_id": "P-30000", "department": "Cardiology",
            "exam_type": "NEURO_EEG", "appointment_day": "14", "appointment_month": "5",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "09:15",
        })
    assert response.status_code == 400
    assert "סוג הבדיקה שנבחר אינו שייך למחלקת" in response.text


def test_editing_shows_the_stored_exam_type(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/appointments/APT-8391/edit").text
    assert re.search(r'<option value="NEURO_VISIT"[^>]* selected>', page)


def test_saving_an_edit_replaces_the_exam_type(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments/APT-8391", data={
            "csrf_token": csrf(client), "department": "Neurology", "exam_type": "NEURO_EMG",
            "doctor_name": "Dr. Cohen", "appointment_day": "20", "appointment_month": "6",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "13:45",
            "location": "Building B, Floor 2",
        })
    assert response.status_code == 303
    assert exam_code_of(app, "APT-8391") == "NEURO_EMG"


def test_an_invalid_edit_leaves_the_exam_type_as_it_was(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments/APT-8391", data={
            "csrf_token": csrf(client), "department": "Neurology", "exam_type": "CARD_ECHO",
            "doctor_name": "Dr. Cohen", "appointment_day": "20", "appointment_month": "6",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "13:45",
            "location": "Building B, Floor 2",
        })
    assert response.status_code == 400
    assert exam_code_of(app, "APT-8391") == "NEURO_VISIT"


def test_the_dashboard_shows_the_exam_type_column(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    assert "<th>סוג בדיקה</th>" in page
    assert "ביקור במרפאה נוירולוגית" in page  # APT-8391 -> NEURO_VISIT
    assert "מבחן מאמץ" in page  # APT-8392 -> CARD_STRESS
    header = re.search(r"<thead><tr>(.*?)</tr></thead>", page, re.S).group(1)
    for row in re.findall(r"<tr>(.*?)</tr>", re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1), re.S):
        assert row.count("<td") == header.count("<th")
