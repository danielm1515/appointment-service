"""Booking and cancelling appointments from the dashboard (UI only - the API stays read-only).

A booking is only for a patient HospitalAgent's registry knows, and fails closed: an unknown
patient, an unreachable registry or no registry configured at all books nothing.
"""
import os
import re

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import AdminUser, Appointment, AuditLog
from app.patient_registry import RegistryUnavailable

NAMES = {"P-10041": "דנה כהן", "P-20000": "יוסי לוי", "P-30000": "מיכל אברהם"}
KNOWN = set(NAMES)
from datetime import datetime
from zoneinfo import ZoneInfo

THIS_YEAR = datetime.now(ZoneInfo("Asia/Jerusalem")).year
NEXT_YEAR = THIS_YEAR + 1
FUTURE_DATE = {"appointment_day": "14", "appointment_month": "5", "appointment_year": str(NEXT_YEAR)}
FUTURE_TIME = "09:15"


class FakeRegistry:
    def __init__(self, known: set[str] | None = None, broken: bool = False) -> None:
        self.known = KNOWN if known is None else known
        self.broken = broken

    def exists(self, patient_id: str) -> bool:
        if self.broken:
            raise RegistryUnavailable("down")
        return patient_id in self.known

    def list_patients(self) -> list[tuple[str, str]]:
        if self.broken:
            raise RegistryUnavailable("down")
        return sorted((pid, NAMES.get(pid, pid)) for pid in self.known)


def make_client(tmp_path, registry="default", **kwargs):
    registry = FakeRegistry() if registry == "default" else registry
    app = create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", seed_demo_data=True,
                     patient_registry=registry, **kwargs)
    return app, TestClient(app, follow_redirects=False)


def csrf(client) -> str:
    page = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "the dashboard must render a CSRF token in its forms"
    return match.group(1)


def book(client, **overrides):
    data = {"csrf_token": csrf(client), "patient_id": "P-30000", "department": "Orthopedics",
            "exam_type": "ORTHO_VISIT", "doctor_name": "Dr. Levi", **FUTURE_DATE,
            "appointment_time": FUTURE_TIME, "location": "Building C, Floor 1"}
    data.update(overrides)
    return client.post("/appointments", data=data)


def appointments_of(app, patient_id):
    with app.state.SessionLocal() as session:
        return list(session.scalars(select(Appointment).where(Appointment.patient_id == patient_id)))


def audit(app, operation):
    with app.state.SessionLocal() as session:
        return [r.result for r in session.scalars(
            select(AuditLog).where(AuditLog.operation == operation).order_by(AuditLog.timestamp))]


def count(app):
    with app.state.SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(Appointment))


def test_booking_a_registered_patient_creates_a_scheduled_appointment(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = book(client)
        [appointment] = appointments_of(app, "P-30000")
        page = client.get(response.headers["location"])
        lookup = client.get("/api/v1/patients/P-30000/appointment")
    assert response.status_code == 303
    assert response.headers["location"] == f"/?booked={appointment.appointment_id}"
    assert re.fullmatch(r"APT-[0-9A-F]{6}", appointment.appointment_id)
    assert appointment.status == "Scheduled"
    assert appointment.department == "Orthopedics"
    assert appointment.doctor_name == "Dr. Levi"
    assert appointment.location == "Building C, Floor 1"
    # The form's time is Israel local time, stored as such.
    assert appointment.appointment_at.strftime("%Y-%m-%d %H:%M") == f"{NEXT_YEAR}-05-14 09:15"
    assert audit(app, "BookAppointment") == ["booked"]
    assert appointment.appointment_id in page.text and "נקבע" in page.text
    # The Hospital Agent's read-only lookup sees the new appointment.
    assert lookup.json()["appointment"]["appointment_id"] == appointment.appointment_id


def test_optional_fields_may_be_left_empty(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = book(client, doctor_name="", location="")
        [appointment] = appointments_of(app, "P-30000")
    assert response.status_code == 303
    assert appointment.doctor_name is None and appointment.location is None


def test_a_patient_the_registry_does_not_know_is_not_booked(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        before = count(app)
        response = book(client, patient_id="P-99999")
        after = count(app)
    assert response.status_code == 400
    assert "אינו רשום" in response.text
    assert after == before
    assert audit(app, "BookAppointment") == ["patient_not_found"]


def test_an_unreachable_registry_books_nothing(tmp_path):
    app, client = make_client(tmp_path, FakeRegistry(broken=True))
    with client:
        before = count(app)
        response = book(client)
        after = count(app)
    assert response.status_code == 503
    assert "לא ניתן לאמת" in response.text
    assert after == before
    assert audit(app, "BookAppointment") == ["technical_failure"]


def test_without_a_registry_booking_is_refused(tmp_path):
    """Fail closed: with no registry to ask, nobody can be verified, so nothing is booked."""
    app, client = make_client(tmp_path, None)
    with client:
        before = count(app)
        response = book(client)
        after = count(app)
    assert response.status_code == 503
    assert after == before


def test_the_form_keeps_what_was_typed_after_an_error(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = book(client, patient_id="P-20000", department="Dermatology", doctor_name="Dr. Katz",
                        appointment_time="13:30", location="Nowhere")
    assert response.status_code == 400
    assert '<option value="P-20000" selected>' in response.text
    assert '<option value="Dermatology" selected>' in response.text
    assert re.search(r'<option value="Dr. Katz"[^>]* selected>', response.text)
    assert '<option value="13:30" selected>' in response.text
    assert '<option value="14" selected>' in response.text
    assert '<option value="5" selected>' in response.text
    assert f'<option value="{NEXT_YEAR}" selected>' in response.text


def test_the_patient_is_picked_from_a_list_loaded_from_the_registry(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    select = re.search(r'<select[^>]*name="patient_id"[^>]*>(.*?)</select>', page, re.S)
    assert select, "the patient field must be a dropdown"
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select.group(1))
    assert options == [("", "בחרו מטופל…"),
                       ("P-10041", "P-10041 · דנה כהן"),
                       ("P-20000", "P-20000 · יוסי לוי"),
                       ("P-30000", "P-30000 · מיכל אברהם")]


def test_the_appointments_table_shows_each_patients_name(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    assert "דנה כהן" in page and "יוסי לוי" in page


def test_the_list_never_replaces_the_servers_own_check(tmp_path):
    """A request that bypasses the dropdown with an id the registry does not know is refused."""
    app, client = make_client(tmp_path)
    with client:
        response = book(client, patient_id="P-NOT-IN-LIST")
    assert response.status_code == 400
    assert "אינו רשום" in response.text


def test_when_the_registry_is_down_the_dashboard_still_opens_but_cannot_book(tmp_path):
    app, client = make_client(tmp_path, FakeRegistry(broken=True))
    with client:
        page = client.get("/")
    assert page.status_code == 200
    assert "APT-8391" in page.text  # the appointments themselves are local
    assert "לא ניתן לטעון את רשימת המטופלים" in page.text
    assert re.search(r'<select[^>]*name="patient_id"[^>]*disabled', page.text)
    assert re.search(r'<button[^>]*type="submit"[^>]*disabled[^>]*>קביעת תור', page.text)


def test_without_a_registry_the_dashboard_says_booking_is_off(tmp_path):
    app, client = make_client(tmp_path, None)
    with client:
        page = client.get("/")
    assert page.status_code == 200
    assert "PATIENT_REGISTRY_URL" in page.text
    assert re.search(r'<select[^>]*name="patient_id"[^>]*disabled', page.text)


def test_invalid_input_is_refused_with_a_message(tmp_path):
    cases = [
        ({"patient_id": ""}, "מזהה מטופל"),
        ({"patient_id": "P 1"}, "מזהה מטופל"),
        ({"department": ""}, "מחלקה"),
        ({"department": "Astrology"}, "מחלקה"),
        ({"doctor_name": "Dr. Nobody"}, "רופא"),
        ({"doctor_name": "Dr. Cohen"}, "רופא"),  # a real doctor, of another department
        ({"location": "Building B, Floor 2"}, "מיקום"),  # another department's location
        ({"appointment_day": ""}, "תאריך"),
        ({"appointment_month": "13"}, "תאריך"),
        ({"appointment_day": "x"}, "תאריך"),
        ({"appointment_day": "31", "appointment_month": "2"}, "אינו קיים"),  # 31 February
        ({"appointment_year": str(THIS_YEAR + 5)}, "שנה"),  # beyond the offered years
        ({"appointment_time": ""}, "שעה"),
        ({"appointment_time": "9:15 PM"}, "שעה"),
        ({"appointment_time": "07:30"}, "שעה"),  # outside the slots
        ({"appointment_time": "09:10"}, "שעה"),  # not on a 15-minute slot
        ({"appointment_day": "1", "appointment_month": "1", "appointment_year": str(THIS_YEAR)}, "עתיד"),
    ]
    app, client = make_client(tmp_path)
    with client:
        before = count(app)
        for overrides, message in cases:
            response = book(client, **overrides)
            assert response.status_code == 400, overrides
            assert message in response.text, overrides
        after = count(app)
    assert after == before
    assert audit(app, "BookAppointment") == []


def test_a_booking_without_the_csrf_token_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        before = count(app)
        client.get("/")
        missing = client.post("/appointments", data={"patient_id": "P-30000", "department": "X",
                                                     **FUTURE_DATE})
        wrong = book(client, csrf_token="not-the-token")
        after = count(app)
    assert missing.status_code == 403 and wrong.status_code == 403
    assert after == before


def test_with_ui_auth_only_a_logged_in_admin_can_book_or_cancel(tmp_path):
    app, client = make_client(tmp_path, ui_auth_enabled=True, ui_setup_token="tok",
                              session_secret="test-session-secret")
    with client:
        before = count(app)
        anonymous_book = client.post("/appointments", data={"patient_id": "P-30000",
                                                            "department": "X", **FUTURE_DATE})
        anonymous_cancel = client.post("/appointments/APT-8391/cancel")
        after = count(app)
        [seeded] = appointments_of(app, "P-10041")

        client.post("/setup", data={"setup_token": "tok", "username": "admin",
                                    "password": "strong-password-123",
                                    "password_confirm": "strong-password-123"})
        with app.state.SessionLocal() as session:
            secret = session.scalar(select(AdminUser)).totp_secret
        client.post("/setup/verify", data={"code": pyotp.TOTP(secret).now()})
        client.post("/login", data={"username": "admin", "password": "strong-password-123",
                                    "code": pyotp.TOTP(secret).now()})
        booked = book(client)
        actors = {r.case_id for r in app.state.SessionLocal().scalars(
            select(AuditLog).where(AuditLog.operation == "BookAppointment"))}
    assert anonymous_book.headers["location"] == "/login"
    assert anonymous_cancel.headers["location"] == "/login"
    assert after == before
    assert seeded.status == "Scheduled"
    assert booked.status_code == 303
    assert actors == {"ui:admin"}


def test_cancelling_marks_the_appointment_cancelled_and_hides_it_from_the_lookup(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        token = csrf(client)
        response = client.post("/appointments/APT-8392/cancel", data={"csrf_token": token})
        [appointment] = appointments_of(app, "P-20000")
        page = client.get(response.headers["location"])
        lookup = client.get("/api/v1/patients/P-20000/appointment")
        again = client.post("/appointments/APT-8392/cancel", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/?cancelled=APT-8392"
    assert appointment.status == "Cancelled"  # never deleted
    assert "בוטל" in page.text
    assert lookup.json() == {"found": False, "appointment": None, "upcoming_count": 0}
    assert again.status_code == 409
    assert audit(app, "CancelAppointment") == ["cancelled"]


def test_cancelling_an_unknown_appointment_is_404(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.post("/appointments/APT-NOPE/cancel", data={"csrf_token": csrf(client)})
    assert response.status_code == 404
    assert audit(app, "CancelAppointment") == []


def test_a_cancel_without_the_csrf_token_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.get("/")
        response = client.post("/appointments/APT-8392/cancel", data={"csrf_token": "wrong"})
        [appointment] = appointments_of(app, "P-20000")
    assert response.status_code == 403
    assert appointment.status == "Scheduled"


def test_only_scheduled_rows_offer_a_cancel_button(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.post("/appointments/APT-8392/cancel", data={"csrf_token": csrf(client)})
        page = client.get("/").text
    assert 'action="/appointments/APT-8391/cancel"' in page
    assert 'action="/appointments/APT-8392/cancel"' not in page


def test_booking_is_not_part_of_the_api(tmp_path):
    app, _client = make_client(tmp_path)
    paths = app.openapi()["paths"]
    assert "/appointments" not in paths
    assert all(set(ops) == {"get"} for ops in paths.values())


def select_options(page: str, name: str) -> list[tuple[str, str]]:
    select = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', page, re.S)
    assert select, f"{name} must be a dropdown"
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select.group(1))


def test_department_doctor_and_location_are_picked_from_the_catalog(tmp_path):
    from app.catalog import DEPARTMENTS
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    departments = select_options(page, "department")
    assert departments[0][0] == ""
    assert departments[1:] == [(d.value, f"{d.label} · {d.value}") for d in DEPARTMENTS]
    doctors = [v for v, _ in select_options(page, "doctor_name")]
    assert doctors == [""] + [doc for d in DEPARTMENTS for doc in d.doctors]
    locations = [v for v, _ in select_options(page, "location")]
    assert locations == [""] + [loc for d in DEPARTMENTS for loc in d.locations]
    # Each doctor and location says which department it belongs to, so the page can filter.
    assert 'value="Dr. Levi" data-department="Orthopedics"' in page
    assert 'value="Building B, Floor 2" data-department="Neurology"' in page


def test_the_time_is_a_24_hour_dropdown_of_15_minute_slots(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    times = [v for v, _ in select_options(page, "appointment_time")]
    assert times[0] == ""
    assert times[1] == "08:00" and times[-1] == "17:45"
    assert "13:00" in times and "16:45" in times
    assert len(times) == 1 + 10 * 4
    assert all(label == value for value, label in select_options(page, "appointment_time")[1:])
    # Only the time labels: the page as a whole carries a random CSRF token, which can contain "AM".
    labels = [label for _, label in select_options(page, "appointment_time")]
    assert not any("AM" in label or "PM" in label for label in labels)
    assert 'type="datetime-local"' not in page
    assert 'type="date"' not in page


def test_the_date_is_picked_as_day_month_and_year(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    days = select_options(page, "appointment_day")
    months = select_options(page, "appointment_month")
    years = select_options(page, "appointment_year")
    assert [v for v, _ in days] == [""] + [str(d) for d in range(1, 32)]
    assert [label for _, label in days][1:3] == ["01", "02"]
    assert [v for v, _ in months] == [""] + [str(m) for m in range(1, 13)]
    assert months[1][1] == "01 · ינואר" and months[12][1] == "12 · דצמבר"
    assert [v for v, _ in years] == ["", str(THIS_YEAR), str(THIS_YEAR + 1), str(THIS_YEAR + 2)]
    # Day, month, year - in that order in the markup, so under RTL the day is rightmost.
    assert page.index('name="appointment_day"') < page.index('name="appointment_month"') < page.index('name="appointment_year"')


def test_an_afternoon_slot_is_stored_on_the_24_hour_clock(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        book(client, appointment_time="16:45")
        [appointment] = appointments_of(app, "P-30000")
        page = client.get("/").text
    assert appointment.appointment_at.strftime("%H:%M") == "16:45"
    assert f"14/05/{NEXT_YEAR} 16:45" in page


def test_the_table_shows_the_departments_hebrew_label(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/").text
    assert "נוירולוגיה" in page and "קרדיולוגיה" in page


# --- editing an existing appointment ---

def edit(client, appointment_id, **overrides):
    data = {"csrf_token": csrf(client), "department": "Neurology", "exam_type": "NEURO_VISIT",
            "doctor_name": "Dr. Shapiro", "appointment_day": "20", "appointment_month": "6",
            "appointment_year": str(NEXT_YEAR), "appointment_time": "13:45",
            "location": "Building B, Floor 2"}
    data.update(overrides)
    return client.post(f"/appointments/{appointment_id}", data=data)


def one(app, appointment_id):
    with app.state.SessionLocal() as session:
        return session.get(Appointment, appointment_id)


def test_only_scheduled_rows_offer_an_edit_link(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.post("/appointments/APT-8392/cancel", data={"csrf_token": csrf(client)})
        page = client.get("/").text
    assert 'href="/appointments/APT-8391/edit"' in page
    assert 'href="/appointments/APT-8392/edit"' not in page


def test_the_edit_form_is_filled_with_the_appointment(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        page = client.get("/appointments/APT-8391/edit")
    assert page.status_code == 200
    text = page.text
    assert "עריכת תור" in text and "APT-8391" in text
    assert 'action="/appointments/APT-8391"' in text
    assert '<option value="Neurology" selected>' in text
    assert re.search(r'<option value="Dr. Cohen"[^>]* selected>', text)
    assert re.search(r'<option value="Building B, Floor 2"[^>]* selected>', text)
    assert '<option value="3" selected>' in text and '<option value="10" selected>' in text  # 03/10
    assert '<option value="10:30" selected>' in text
    # The patient is shown, not offered for change.
    assert "P-10041" in text and "דנה כהן" in text
    assert not re.search(r'<select[^>]*name="patient_id"', text)
    assert "שמירת שינויים" in text


def test_saving_an_edit_changes_the_appointment_in_place(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = edit(client, "APT-8391")
        appointment = one(app, "APT-8391")
        page = client.get(response.headers["location"]).text
        lookup = client.get("/api/v1/patients/P-10041/appointment").json()
    assert response.status_code == 303
    assert response.headers["location"] == "/?updated=APT-8391"
    assert appointment.status == "Scheduled"
    assert appointment.patient_id == "P-10041"
    assert (appointment.department, appointment.doctor_name, appointment.location) == (
        "Neurology", "Dr. Shapiro", "Building B, Floor 2")
    assert appointment.appointment_at.strftime("%Y-%m-%d %H:%M") == f"{NEXT_YEAR}-06-20 13:45"
    assert "עודכן" in page
    assert lookup["appointment"]["doctor_name"] == "Dr. Shapiro"
    assert audit(app, "UpdateAppointment") == ["updated"]
    assert count(app) == 2  # edited, not added


def test_an_edit_cannot_move_the_appointment_to_another_patient(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        edit(client, "APT-8391", patient_id="P-30000")
    assert one(app, "APT-8391").patient_id == "P-10041"


def test_an_invalid_edit_changes_nothing_and_stays_in_edit_mode(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        before = one(app, "APT-8391")
        response = edit(client, "APT-8391", doctor_name="Dr. Levi")  # an Orthopedics doctor
        after = one(app, "APT-8391")
    assert response.status_code == 400
    assert "רופא" in response.text
    assert 'action="/appointments/APT-8391"' in response.text
    assert (after.doctor_name, after.appointment_at) == (before.doctor_name, before.appointment_at)
    assert audit(app, "UpdateAppointment") == []


def test_a_cancelled_appointment_cannot_be_edited(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.post("/appointments/APT-8392/cancel", data={"csrf_token": csrf(client)})
        page = client.get("/appointments/APT-8392/edit")
        saved = edit(client, "APT-8392", department="Cardiology", doctor_name="", location="")
    assert page.status_code == 409 and saved.status_code == 409
    assert one(app, "APT-8392").status == "Cancelled"


def test_editing_an_unknown_appointment_is_404(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        assert client.get("/appointments/APT-NOPE/edit").status_code == 404
        assert edit(client, "APT-NOPE").status_code == 404


def test_an_edit_without_the_csrf_token_is_refused(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        client.get("/")
        response = edit(client, "APT-8391", csrf_token="wrong")
    assert response.status_code == 403
    assert one(app, "APT-8391").doctor_name == "Dr. Cohen"


def test_with_ui_auth_editing_needs_a_login(tmp_path):
    app, client = make_client(tmp_path, ui_auth_enabled=True, ui_setup_token="tok",
                              session_secret="test-session-secret")
    with client:
        page = client.get("/appointments/APT-8391/edit")
        saved = client.post("/appointments/APT-8391", data={"department": "Neurology"})
        doctor = one(app, "APT-8391").doctor_name
    assert page.headers["location"] == "/login" and saved.headers["location"] == "/login"
    assert doctor == "Dr. Cohen"


def test_every_row_has_as_many_cells_as_the_header_even_when_cancelled(tmp_path):
    """A cancelled row has no actions; its actions cell must still be an ordinary table cell,
    or the row loses a column. Layout (flex) goes on a wrapper inside the cell, never on it."""
    app, client = make_client(tmp_path)
    with client:
        client.post("/appointments/APT-8392/cancel", data={"csrf_token": csrf(client)})
        page = client.get("/").text
    header = re.search(r"<thead><tr>(.*?)</tr></thead>", page, re.S).group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1), re.S)
    assert len(rows) == 2
    for row in rows:
        assert row.count("<td") == header.count("<th")
    assert not re.search(r"<td[^>]*class=\"[^\"]*row-actions", page)
    css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    assert not re.search(r"(^|[}\s])td[^{]*\{[^}]*display\s*:\s*flex", css)
