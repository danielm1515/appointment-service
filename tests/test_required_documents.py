"""Sub-project 11: every appointment stores its own required document types (design §3)."""
import os

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import catalog
from app.main import create_app
from app.models import Appointment, AppointmentRequiredDocument, DocumentTypeRow

CATALOG = [("CBC", "ספירת דם מלאה", 90), ("COAGULATION_TESTS", "בדיקות קרישה", 90),
           ("ECG", "תרשים פעילות חשמלית של הלב", 180), ("URINALYSIS", "בדיקת שתן", 90),
           ("PREOP_SUMMARY", "סיכום טרום ניתוח", 30)]


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


def required_of(app, appointment_id):
    with app.state.SessionLocal() as session:
        return session.get(Appointment, appointment_id).required_documents


# --- the catalog --------------------------------------------------------------------------

def test_the_catalog_is_the_designs_table():
    assert [(t.code, t.label, t.max_age_days) for t in catalog.DOCUMENT_TYPES] == CATALOG
    assert catalog.DOCUMENT_TYPE_CODES == {code for code, _, _ in CATALOG}
    assert catalog.document_type_label("ECG") == "תרשים פעילות חשמלית של הלב"
    assert catalog.document_type_label("UNKNOWN") == "UNKNOWN"


def test_the_catalog_table_is_seeded_on_every_start_even_without_demo_data(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", seed_demo_data=False,
                     patient_registry=FakeRegistry())
    with TestClient(app):
        with app.state.SessionLocal() as session:
            rows = [(r.code, r.label_he, r.max_age_days)
                    for r in session.scalars(select(DocumentTypeRow).order_by(DocumentTypeRow.code))]
    assert rows == sorted(CATALOG)


def test_seeding_the_catalog_twice_changes_nothing(tmp_path):
    db = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    for _ in range(2):
        app = create_app(db, seed_demo_data=True, patient_registry=FakeRegistry())
        with TestClient(app):
            pass
    with app.state.SessionLocal() as session:
        assert session.scalar(text("SELECT count(*) FROM document_types")) == 5


# --- the link table -----------------------------------------------------------------------

def test_the_demo_seed_gives_apt_8391_the_focused_specs_three_types(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        assert required_of(app, "APT-8391") == ["CBC", "COAGULATION_TESTS", "ECG"]
        assert required_of(app, "APT-8392") == []


def test_an_unknown_type_is_refused_by_the_database(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        with pytest.raises(IntegrityError):
            with app.state.SessionLocal() as session:
                session.add(AppointmentRequiredDocument(appointment_id="APT-8392", document_type="ELECTRICITY_BILL"))
                session.commit()


def test_a_type_cannot_be_required_twice_for_one_appointment(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        with pytest.raises(IntegrityError):
            with app.state.SessionLocal() as session:
                session.execute(text("INSERT INTO appointment_required_documents (appointment_id, document_type) "
                                     "VALUES ('APT-8391', 'CBC')"))
                session.commit()


def test_a_requirement_for_a_missing_appointment_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        with pytest.raises(IntegrityError):
            with app.state.SessionLocal() as session:
                session.add(AppointmentRequiredDocument(appointment_id="APT-NOPE", document_type="CBC"))
                session.commit()


# --- the API ------------------------------------------------------------------------------

def test_check_appointment_returns_the_required_documents(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment").json()
        none = client.get("/api/v1/patients/P-20000/appointment").json()
    assert body["appointment"]["required_documents"] == ["CBC", "COAGULATION_TESTS", "ECG"]
    assert none["appointment"]["required_documents"] == []
    # Nothing else in the contract changed.
    assert set(body["appointment"]) == {"appointment_id", "patient_id", "department", "doctor_name",
                                        "appointment_at", "location", "status", "required_documents"}


# --- the form (design §3) -----------------------------------------------------------------

import re  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app.models import AuditLog  # noqa: E402

NEXT_YEAR = datetime.now(ZoneInfo("Asia/Jerusalem")).year + 1


def csrf(client):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text).group(1)


def booking(client, **overrides):
    data = {"csrf_token": csrf(client), "patient_id": "P-30000", "department": "Orthopedics",
            "exam_type": "ORTHO_VISIT", "doctor_name": "Dr. Levi", "appointment_day": "14",
            "appointment_month": "5", "appointment_year": str(NEXT_YEAR), "appointment_time": "09:15",
            "location": "Building C, Floor 1"}
    data.update(overrides)
    return data


def only_appointment_of(app, patient_id):
    with app.state.SessionLocal() as session:
        [appointment] = session.scalars(select(Appointment).where(Appointment.patient_id == patient_id))
        return appointment


def test_the_form_offers_every_catalog_type_as_a_checkbox(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    boxes = re.findall(r'<input type="checkbox" name="required_documents" value="([^"]+)"', page)
    assert boxes == [code for code, _, _ in CATALOG]
    for _, label, _ in CATALOG:
        assert label in page


def test_booking_stores_the_chosen_types(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data=booking(client, required_documents=["ECG", "CBC"]))
        appointment = only_appointment_of(app, "P-30000")
        lookup = client.get("/api/v1/patients/P-30000/appointment").json()
    assert response.status_code == 303
    assert appointment.required_documents == ["CBC", "ECG"]
    assert lookup["appointment"]["required_documents"] == ["CBC", "ECG"]


def test_booking_with_no_type_chosen_needs_nothing(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        assert client.post("/appointments", data=booking(client)).status_code == 303
        assert only_appointment_of(app, "P-30000").required_documents == []


@pytest.mark.parametrize("codes", [["ELECTRICITY_BILL"], ["CBC", "cbc"], [" "]])
def test_a_type_outside_the_catalog_books_nothing(tmp_path, codes):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data=booking(client, required_documents=codes))
        with app.state.SessionLocal() as session:
            booked = list(session.scalars(select(Appointment).where(Appointment.patient_id == "P-30000")))
    if codes == [" "]:  # a blank value is ignored, not an error
        assert response.status_code == 303 and booked[0].required_documents == []
    else:
        assert response.status_code == 400
        assert "מהרשימה" in response.text
        assert booked == []


def test_a_repeated_type_is_stored_once(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.post("/appointments", data=booking(client, required_documents=["ECG", "ECG"]))
        assert only_appointment_of(app, "P-30000").required_documents == ["ECG"]


def test_the_form_keeps_the_chosen_types_after_an_error(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments", data=booking(client, location="Nowhere",
                                                             required_documents=["CBC", "ECG"]))
    assert response.status_code == 400
    assert 'value="CBC" checked' in response.text and 'value="ECG" checked' in response.text
    assert 'value="URINALYSIS" checked' not in response.text


def test_editing_shows_the_stored_types_not_a_derived_list(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/appointments/APT-8391/edit").text
    checked = re.findall(r'value="([A-Z_]+)" checked', page)
    assert checked == ["CBC", "COAGULATION_TESTS", "ECG"]


def edit_data(client, **overrides):
    data = {"csrf_token": csrf(client), "department": "Neurology", "exam_type": "NEURO_VISIT",
            "doctor_name": "Dr. Cohen", "appointment_day": "20", "appointment_month": "6",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "13:45",
            "location": "Building B, Floor 2"}
    data.update(overrides)
    return data


def test_saving_an_edit_replaces_the_types(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments/APT-8391", data=edit_data(client, required_documents=["URINALYSIS"]))
        assert response.status_code == 303
        assert required_of(app, "APT-8391") == ["URINALYSIS"]
        client.post("/appointments/APT-8391", data=edit_data(client))  # none ticked
        assert required_of(app, "APT-8391") == []
        with app.state.SessionLocal() as session:
            left = session.scalar(text("SELECT count(*) FROM appointment_required_documents "
                                       "WHERE appointment_id = 'APT-8391'"))
            audit = [r.result for r in session.scalars(select(AuditLog).where(AuditLog.operation == "UpdateAppointment"))]
    assert left == 0
    assert audit == ["updated", "updated"]


def test_an_invalid_edit_leaves_the_types_as_they_were(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments/APT-8391",
                               data=edit_data(client, required_documents=["ELECTRICITY_BILL"]))
        assert response.status_code == 400
        assert required_of(app, "APT-8391") == ["CBC", "COAGULATION_TESTS", "ECG"]


def test_the_table_shows_each_appointments_required_documents(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    assert "<th>מסמכים נדרשים</th>" in page
    assert "ספירת דם מלאה" in page and "בדיקות קרישה" in page
    header = re.search(r"<thead><tr>(.*?)</tr></thead>", page, re.S).group(1)
    for row in re.findall(r"<tr>(.*?)</tr>", re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1), re.S):
        assert row.count("<td") == header.count("<th")


def test_an_edit_that_keeps_some_of_the_types_works(tmp_path):
    """The everyday edit: the same set again, a superset, a subset (final review, minor 2)."""
    app, client = make_client(tmp_path)
    with client:
        for codes in (["CBC", "ECG"], ["CBC", "ECG"], ["CBC", "ECG", "URINALYSIS"], ["ECG"]):
            response = client.post("/appointments/APT-8391", data=edit_data(client, required_documents=codes))
            assert response.status_code == 303, codes
            assert required_of(app, "APT-8391") == sorted(codes)


def test_the_new_code_starts_on_a_database_made_by_the_old_schema(tmp_path):
    """Going live: the file already has appointments but not the two new tables (final review, minor 3)."""
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE appointments (appointment_id VARCHAR(64) PRIMARY KEY, patient_id VARCHAR(64) NOT NULL,
                department VARCHAR(120) NOT NULL, doctor_name VARCHAR(120), appointment_at DATETIME NOT NULL,
                location VARCHAR(200), status VARCHAR(32) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
            INSERT INTO appointments (appointment_id, patient_id, department, appointment_at, status)
                VALUES ('APT-OLD1', 'P-20000', 'Cardiology', '2030-01-01 09:00:00', 'Scheduled');
        """)
    app = create_app(f"sqlite:///{db.as_posix()}", seed_demo_data=True, patient_registry=FakeRegistry())
    with TestClient(app) as client:
        body = client.get("/api/v1/patients/P-20000/appointment").json()
        with app.state.SessionLocal() as session:
            violations = session.execute(text("PRAGMA foreign_key_check")).all()
            types = session.scalar(text("SELECT count(*) FROM document_types"))
    assert body["appointment"]["appointment_id"] == "APT-OLD1"
    assert body["appointment"]["required_documents"] == []
    assert violations == [] and types == 5
