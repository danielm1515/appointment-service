import os

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

from fastapi.testclient import TestClient
import pyotp
from sqlalchemy import func, select

from app.main import create_app
from app.models import AdminUser, AuditLog


def make_client(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app(f"sqlite:///{db_path.as_posix()}", seed_demo_data=True)
    return app, TestClient(app)


def test_health(tmp_path):
    _app, client = make_client(tmp_path)
    with client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_dashboard_lists_and_filters_appointments(tmp_path):
    _app, client = make_client(tmp_path)
    with client:
        dashboard = client.get("/")
        filtered = client.get("/", params={"patient_id": "P-20000"})
    assert dashboard.status_code == 200
    assert "P-10041" in dashboard.text
    assert "P-20000" in dashboard.text
    assert "P-20000" in filtered.text
    assert "P-10041" not in filtered.text


def test_microsoft_authenticator_setup_and_login(tmp_path):
    db_path = tmp_path / "auth.db"
    app = create_app(
        f"sqlite:///{db_path.as_posix()}",
        seed_demo_data=True,
        ui_auth_enabled=True,
        ui_setup_token="test-setup-token",
        session_secret="test-session-secret",
    )
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/").headers["location"] == "/login"
        assert client.get("/login").headers["location"] == "/setup"
        setup = client.post(
            "/setup",
            data={
                "setup_token": "test-setup-token",
                "username": "admin",
                "password": "strong-password-123",
                "password_confirm": "strong-password-123",
            },
        )
        assert setup.status_code == 303
        assert setup.headers["location"] == "/setup/authenticator"
        assert client.get("/setup/qr").headers["content-type"] == "image/png"
        with app.state.SessionLocal() as session:
            admin = session.scalar(select(AdminUser))
            code = pyotp.TOTP(admin.totp_secret).now()
        verified = client.post("/setup/verify", data={"code": code})
        assert verified.status_code == 303
        login = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "strong-password-123",
                "code": pyotp.TOTP(admin.totp_secret).now(),
            },
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/"
        assert client.get("/").status_code == 200


def test_api_key_protects_agent_endpoint(tmp_path):
    db_path = tmp_path / "api-key.db"
    app = create_app(
        f"sqlite:///{db_path.as_posix()}",
        seed_demo_data=True,
        api_auth_enabled=True,
        appointment_api_key="agent-secret-key",
    )
    with TestClient(app) as client:
        missing = client.get("/api/v1/patients/P-10041/appointment")
        invalid = client.get(
            "/api/v1/patients/P-10041/appointment",
            headers={"X-API-Key": "wrong-key"},
        )
        valid = client.get(
            "/api/v1/patients/P-10041/appointment",
            headers={"X-API-Key": "agent-secret-key"},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert valid.json()["appointment"]["appointment_id"] == "APT-8391"


def test_existing_appointment_and_trace(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        response = client.get(
            "/api/v1/patients/P-10041/appointment",
            headers={"X-Case-ID": "CASE-1", "X-Execution-ID": "EXEC-1"},
        )
        with app.state.SessionLocal() as session:
            audit_count = session.scalar(select(func.count()).select_from(AuditLog))
    assert response.status_code == 200
    assert response.json()["found"] is True
    assert response.json()["appointment"]["appointment_id"] == "APT-8391"
    assert response.headers["x-case-id"] == "CASE-1"
    assert audit_count == 1


def test_missing_appointment_is_business_result(tmp_path):
    _app, client = make_client(tmp_path)
    with client:
        response = client.get("/api/v1/patients/P-99999/appointment")
    assert response.status_code == 200
    assert response.json() == {"found": False, "appointment": None}


def test_invalid_patient_id(tmp_path):
    _app, client = make_client(tmp_path)
    with client:
        response = client.get("/api/v1/patients/%20/appointment")
    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


def test_timeout_is_not_not_found(tmp_path):
    _app, client = make_client(tmp_path)
    with client:
        response = client.get("/api/v1/patients/P-TIMEOUT/appointment")
    assert response.status_code == 504
    assert response.json()["error"] == "timeout"
    assert "found" not in response.json()


def test_appointment_at_carries_israel_time_zone(tmp_path):
    """The Hospital Agent's guard refuses a naive time (HospitalAgent sub-project 10, design §2.7)."""
    _app, client = make_client(tmp_path)
    with client:
        body = client.get("/api/v1/patients/P-10041/appointment").json()
    assert body["appointment"]["appointment_at"] == "2026-10-03T10:30:00+03:00"


def test_a_winter_appointment_gets_the_winter_offset(tmp_path):
    from datetime import datetime
    from app.schemas import AppointmentOut
    out = AppointmentOut(appointment_id="A", patient_id="P", department="D", doctor_name=None,
                         appointment_at=datetime(2026, 12, 1, 9, 0), location=None, status="Scheduled")
    assert out.model_dump(mode="json")["appointment_at"] == "2026-12-01T09:00:00+02:00"
