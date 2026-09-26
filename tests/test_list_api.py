"""ListAppointments (hospital-agent sub-project 16, design D1-D3): the patient's appointments in a
window, both statuses, ordered, capped, with the same key and registry checks as CheckAppointment."""
import os

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Appointment, AuditLog
from app.patient_registry import RegistryUnavailable

IL = ZoneInfo("Asia/Jerusalem")
KEY = {"X-API-Key": "test-key"}
URL = "/api/v1/patients/{}/appointments"


class FakeRegistry:
    def __init__(self, known=("P-10041", "P-20000", "P-TIMEOUT"), down=False):
        self.known, self.down = set(known), down

    def exists(self, patient_id):
        if self.down:
            raise RegistryUnavailable
        return patient_id in self.known


def make(tmp_path, registry=None):
    # create_app reads Settings() at call time for most fields, but Settings' own dataclass
    # defaults are captured from the environment when app.config is first imported - which may
    # already have happened (by another test module) before this module's os.environ lines run.
    # test_api.py hits the same issue and passes api_auth_enabled / appointment_api_key straight
    # to create_app instead of relying on the environment; this file does the same.
    app = create_app(f"sqlite:///{(tmp_path / 't.db').as_posix()}", seed_demo_data=True,
                     patient_registry=registry, api_auth_enabled=True,
                     appointment_api_key="test-key")
    return app, TestClient(app)


def add(app, appointment_id, at, status="Scheduled", patient_id="P-10041"):
    with app.state.SessionLocal() as s:
        s.add(Appointment(appointment_id=appointment_id, patient_id=patient_id, department="Neurology",
                          appointment_at=at, status=status))
        s.commit()


def window(start, end):
    return {"from": start.isoformat(), "to": end.isoformat()}


def test_lists_both_statuses_in_order_inside_the_window(tmp_path):
    app, client = make(tmp_path)
    with client:
        add(app, "APT-C", datetime(2026, 10, 3, 12, 0, tzinfo=IL), status="Cancelled")
        add(app, "APT-W", datetime(2026, 12, 1, 9, 0, tzinfo=IL))  # winter time, +02:00
        add(app, "APT-OUT", datetime(2027, 3, 1, 9, 0, tzinfo=IL))
        response = client.get(URL.format("P-10041"), headers=KEY,
                              params=window(datetime(2026, 10, 1, tzinfo=IL), datetime(2027, 1, 1, tzinfo=IL)))
    assert response.status_code == 200
    body = response.json()
    assert [a["appointment_id"] for a in body["appointments"]] == ["APT-8391", "APT-C", "APT-W"]
    assert [a["status"] for a in body["appointments"]] == ["Scheduled", "Cancelled", "Scheduled"]
    assert body["appointments"][2]["appointment_at"] == "2026-12-01T09:00:00+02:00"
    assert body["appointments"][0]["required_documents"] == ["CBC", "COAGULATION_TESTS", "ECG"]
    assert body["truncated"] is False


def test_from_is_inclusive_and_to_is_exclusive(tmp_path):
    app, client = make(tmp_path)
    at = datetime(2026, 10, 3, 10, 30, tzinfo=IL)  # the seeded APT-8391
    with client:
        up_to = client.get(URL.format("P-10041"), headers=KEY, params=window(at - timedelta(days=1), at)).json()
        from_ = client.get(URL.format("P-10041"), headers=KEY, params=window(at, at + timedelta(minutes=1))).json()
    assert up_to["appointments"] == []
    assert [a["appointment_id"] for a in from_["appointments"]] == ["APT-8391"]


def test_another_patients_appointments_are_never_listed(tmp_path):
    app, client = make(tmp_path)
    with client:
        body = client.get(URL.format("P-20000"), headers=KEY,
                          params=window(datetime(2026, 9, 1, tzinfo=IL), datetime(2027, 9, 1, tzinfo=IL))).json()
    assert [a["patient_id"] for a in body["appointments"]] == ["P-20000"]


def test_the_list_is_capped_and_says_so(tmp_path):
    app, client = make(tmp_path)
    start = datetime(2027, 1, 1, 8, 0, tzinfo=IL)
    with client:
        for i in range(101):
            add(app, f"APT-{i:03d}", start + timedelta(hours=i))
        body = client.get(URL.format("P-10041"), headers=KEY,
                          params=window(start, start + timedelta(days=30))).json()
    assert len(body["appointments"]) == 100 and body["truncated"] is True
    assert body["appointments"][-1]["appointment_id"] == "APT-099"


def test_a_missing_or_wrong_key_is_401(tmp_path):
    _app, client = make(tmp_path)
    params = window(datetime(2026, 9, 1, tzinfo=IL), datetime(2026, 10, 1, tzinfo=IL))
    with client:
        missing = client.get(URL.format("P-10041"), params=params)
        wrong = client.get(URL.format("P-10041"), params=params, headers={"X-API-Key": "nope"})
    assert (missing.status_code, missing.json()["error"]) == (401, "unauthorized")
    assert wrong.status_code == 401


PARAMS = window(datetime(2026, 9, 1, tzinfo=IL), datetime(2026, 10, 1, tzinfo=IL))


def test_an_unknown_patient_is_404_like_check_appointment(tmp_path):
    _app, client = make(tmp_path, FakeRegistry(known=("P-20000",)))
    with client:
        unknown = client.get(URL.format("P-10041"), headers=KEY, params=PARAMS)
    assert (unknown.status_code, unknown.json()["error"]) == (404, "patient_not_found")


def test_an_unreachable_registry_is_503_like_check_appointment(tmp_path):
    _app, client = make(tmp_path, FakeRegistry(down=True))
    with client:
        down = client.get(URL.format("P-10041"), headers=KEY, params=PARAMS)
    assert (down.status_code, down.json()["error"]) == (503, "patient_registry_unavailable")


def test_the_timeout_simulation_applies(tmp_path):
    _app, client = make(tmp_path)
    with client:
        response = client.get(URL.format("P-TIMEOUT"), headers=KEY,
                              params=window(datetime(2026, 9, 1, tzinfo=IL), datetime(2026, 10, 1, tzinfo=IL)))
    assert (response.status_code, response.json()["error"]) == (504, "timeout")


def test_a_bad_window_is_400(tmp_path):
    _app, client = make(tmp_path)
    good = datetime(2026, 9, 1, tzinfo=IL)
    cases = [
        {"from": "2026-09-01T00:00:00", "to": "2026-10-01T00:00:00+03:00"},  # naive from
        window(good, good),                                                   # empty
        window(good + timedelta(days=1), good),                               # reversed
        window(good, good + timedelta(days=367)),                             # over 366 days
        {"from": "yesterday", "to": "2026-10-01T00:00:00+03:00"},             # not a date
        {"to": "2026-10-01T00:00:00+03:00"},                                  # missing from
    ]
    with client:
        for params in cases:
            response = client.get(URL.format("P-10041"), headers=KEY, params=params)
            assert (response.status_code, response.json()["error"]) == (400, "validation_error"), params


def test_each_call_writes_a_list_audit_row(tmp_path):
    app, client = make(tmp_path)
    with client:
        client.get(URL.format("P-10041"), headers=KEY,
                   params=window(datetime(2026, 9, 1, tzinfo=IL), datetime(2026, 11, 1, tzinfo=IL)))
        client.get(URL.format("P-10041"), headers=KEY,
                   params=window(datetime(2020, 1, 1, tzinfo=IL), datetime(2020, 2, 1, tzinfo=IL)))
        with app.state.SessionLocal() as s:
            rows = s.scalars(select(AuditLog).where(AuditLog.operation == "ListAppointments")
                             .order_by(AuditLog.timestamp)).all()
    # Compared sorted, not in call order: sqlite's default timestamp resolution can tie two
    # rows written within the same test, making an order-sensitive comparison flaky (M7).
    assert sorted(r.result for r in rows) == sorted(["found", "not_found"])


def last_audit(app):
    with app.state.SessionLocal() as s:
        row = s.scalars(select(AuditLog).order_by(AuditLog.timestamp.desc())).first()
        return (row.operation, row.result)


def test_a_year_0001_window_is_400_not_a_crash(tmp_path):
    """I1: from/to close to datetime's own range limits pass the from<to/366-day checks, but
    converting them to Israel time can overflow past year 1 - that must be a 400, not a 500."""
    _app, client = make(tmp_path)
    with client:
        response = client.get(URL.format("P-10041"), headers=KEY,
                              params={"from": "0001-01-01T00:00:00+05:00",
                                      "to": "0001-01-02T00:00:00+05:00"})
    assert (response.status_code, response.json()["error"]) == (400, "validation_error")


def test_a_year_9999_window_is_400_not_a_crash(tmp_path):
    """I1: the same overflow, the other direction (past year 9999)."""
    _app, client = make(tmp_path)
    with client:
        response = client.get(URL.format("P-10041"), headers=KEY,
                              params={"from": "9999-12-30T23:00:00-12:00",
                                      "to": "9999-12-31T23:00:00-12:00"})
    assert (response.status_code, response.json()["error"]) == (400, "validation_error")


def test_the_window_is_compared_in_israel_time_not_the_senders_zone(tmp_path):
    """I2: APT-8391 is 2026-10-03T10:30 Israel time = 07:30 UTC. A UTC window must be converted
    before comparison, not compared as naive values."""
    _app, client = make(tmp_path)
    with client:
        found = client.get(URL.format("P-10041"), headers=KEY,
                           params={"from": "2026-10-03T07:30:00+00:00",
                                   "to": "2026-10-03T07:31:00+00:00"}).json()
        not_found = client.get(URL.format("P-10041"), headers=KEY,
                               params={"from": "2026-10-03T07:00:00+00:00",
                                       "to": "2026-10-03T07:30:00+00:00"}).json()
    assert [a["appointment_id"] for a in found["appointments"]] == ["APT-8391"]
    assert not_found["appointments"] == []


def test_a_winter_utc_window_finds_the_winter_appointment(tmp_path):
    """I2: 2026-12-01T09:00 Israel winter time (+02:00) is 07:00 UTC."""
    app, client = make(tmp_path)
    with client:
        add(app, "APT-W", datetime(2026, 12, 1, 9, 0, tzinfo=IL))
        body = client.get(URL.format("P-10041"), headers=KEY,
                          params={"from": "2026-12-01T07:00:00+00:00",
                                  "to": "2026-12-01T07:01:00+00:00"}).json()
    assert [a["appointment_id"] for a in body["appointments"]] == ["APT-W"]


def test_a_reversed_window_without_a_key_is_still_401(tmp_path):
    """M4: auth is checked before the window is, so a bad window never leaks past a bad key."""
    _app, client = make(tmp_path)
    good = datetime(2026, 9, 1, tzinfo=IL)
    with client:
        response = client.get(URL.format("P-10041"), params=window(good + timedelta(days=1), good))
    assert (response.status_code, response.json()["error"]) == (401, "unauthorized")


def test_same_time_rows_tiebreak_on_appointment_id(tmp_path):
    app, client = make(tmp_path)
    at = datetime(2027, 2, 1, 9, 0, tzinfo=IL)
    with client:
        add(app, "APT-B", at)
        add(app, "APT-A", at)
        body = client.get(URL.format("P-10041"), headers=KEY,
                          params=window(at, at + timedelta(minutes=1))).json()
    assert [a["appointment_id"] for a in body["appointments"]] == ["APT-A", "APT-B"]


def test_a_patient_id_starting_with_a_hyphen_is_400(tmp_path):
    _app, client = make(tmp_path)
    with client:
        response = client.get(URL.format("-bad"), headers=KEY, params=PARAMS)
    assert (response.status_code, response.json()["error"]) == (400, "validation_error")


def test_exactly_100_rows_are_not_truncated(tmp_path):
    app, client = make(tmp_path)
    start = datetime(2027, 4, 1, 8, 0, tzinfo=IL)
    with client:
        for i in range(100):
            add(app, f"APT-X{i:03d}", start + timedelta(hours=i))
        body = client.get(URL.format("P-10041"), headers=KEY,
                          params=window(start, start + timedelta(days=30))).json()
    assert len(body["appointments"]) == 100 and body["truncated"] is False


def test_exactly_366_days_apart_is_allowed(tmp_path):
    _app, client = make(tmp_path)
    good = datetime(2026, 9, 1, tzinfo=IL)
    with client:
        response = client.get(URL.format("P-10041"), headers=KEY,
                              params=window(good, good + timedelta(days=366)))
    assert response.status_code == 200


def test_list_route_404_is_audited_as_list_appointments(tmp_path):
    app, client = make(tmp_path, FakeRegistry(known=("P-20000",)))
    with client:
        client.get(URL.format("P-10041"), headers=KEY, params=PARAMS)
    assert last_audit(app) == ("ListAppointments", "patient_not_found")


def test_list_route_503_registry_is_audited_as_list_appointments(tmp_path):
    app, client = make(tmp_path, FakeRegistry(down=True))
    with client:
        client.get(URL.format("P-10041"), headers=KEY, params=PARAMS)
    assert last_audit(app) == ("ListAppointments", "technical_failure")


def test_list_route_504_is_audited_as_list_appointments(tmp_path):
    app, client = make(tmp_path)
    with client:
        client.get(URL.format("P-TIMEOUT"), headers=KEY,
                   params=window(datetime(2026, 9, 1, tzinfo=IL), datetime(2026, 10, 1, tzinfo=IL)))
    assert last_audit(app) == ("ListAppointments", "technical_failure")


def test_a_check_appointment_registry_404_is_audited_as_check_appointment(tmp_path):
    app, client = make(tmp_path, FakeRegistry(known=("P-20000",)))
    with client:
        response = client.get("/api/v1/patients/P-10041/appointment", headers=KEY)
    assert response.status_code == 404
    assert last_audit(app) == ("CheckAppointment", "patient_not_found")
