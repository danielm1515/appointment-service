"""Sub-project 9: every patient is checked against HospitalAgent's registry (design §4.3)."""
import os
import time

os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
os.environ["MOCK_TIMEOUT_PATIENT_ID"] = "P-TIMEOUT"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Appointment, AuditLog
from app.patient_registry import PostgresPatientRegistry, RegistryUnavailable


class FakeRegistry:
    def __init__(self, known: set[str] | None = None, broken: bool = False) -> None:
        self.known = known or set()
        self.broken = broken
        self.asked: list[str] = []

    def exists(self, patient_id: str) -> bool:
        self.asked.append(patient_id)
        if self.broken:
            raise RegistryUnavailable("down")
        return patient_id in self.known


def make_client(tmp_path, registry):
    app = create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", seed_demo_data=True,
                     patient_registry=registry)
    return app, TestClient(app)


def last_audit_result(app) -> str:
    with app.state.SessionLocal() as session:
        return session.scalars(select(AuditLog.result).order_by(AuditLog.timestamp.desc())).first()


def test_a_known_patient_with_an_appointment_gets_it(tmp_path):
    app, client = make_client(tmp_path, FakeRegistry({"P-10041"}))
    with client:
        response = client.get("/api/v1/patients/P-10041/appointment")
    assert response.status_code == 200
    assert response.json()["appointment"]["appointment_id"] == "APT-8391"


def test_a_known_patient_without_an_appointment_is_still_a_business_result(tmp_path):
    app, client = make_client(tmp_path, FakeRegistry({"P-30000"}))
    with client:
        response = client.get("/api/v1/patients/P-30000/appointment")
    assert response.status_code == 200
    assert response.json() == {"found": False, "appointment": None}


def test_a_patient_the_registry_does_not_know_is_404(tmp_path):
    app, client = make_client(tmp_path, FakeRegistry({"P-10041"}))
    with client:
        response = client.get("/api/v1/patients/P-99999/appointment",
                              headers={"X-Case-ID": "CASE-9", "X-Execution-ID": "EXEC-9"})
        result = last_audit_result(app)
    assert response.status_code == 404
    assert response.json()["error"] == "patient_not_found"
    assert response.headers["x-case-id"] == "CASE-9"
    assert result == "patient_not_found"


def test_an_unreachable_registry_fails_closed(tmp_path):
    """§14: an appointment for a patient nobody could verify is never returned."""
    app, client = make_client(tmp_path, FakeRegistry(broken=True))
    with client:
        response = client.get("/api/v1/patients/P-10041/appointment")
        result = last_audit_result(app)
    assert response.status_code == 503
    assert response.json()["error"] == "patient_registry_unavailable"
    assert "appointment" not in response.json()
    assert result == "technical_failure"


def test_the_timeout_hook_runs_before_the_registry(tmp_path):
    """P-TIMEOUT is a test hook, not a patient: it must still produce its 504 (design §4.3)."""
    registry = FakeRegistry(set())
    app, client = make_client(tmp_path, registry)
    with client:
        response = client.get("/api/v1/patients/P-TIMEOUT/appointment")
    assert response.status_code == 504
    assert registry.asked == []


def test_without_a_registry_the_service_behaves_as_before(tmp_path):
    app, client = make_client(tmp_path, None)
    with client:
        response = client.get("/api/v1/patients/P-99999/appointment")
    assert response.status_code == 200
    assert response.json() == {"found": False, "appointment": None}


def test_a_registry_url_in_the_environment_does_not_reach_the_no_registry_tests(tmp_path, monkeypatch):
    """conftest.py's autouse fixture: create_app(patient_registry=None) must mean no registry
    here, even when the shell running the tests has PATIENT_REGISTRY_URL set."""
    monkeypatch.setenv("PATIENT_REGISTRY_URL", "postgresql+psycopg://x:y@127.0.0.1:1/db")
    app, _client = make_client(tmp_path, None)
    assert app.state.patient_registry is None


def test_the_demo_seed_uses_hospital_agents_patient_ids(tmp_path):
    app, client = make_client(tmp_path, None)
    with client:
        with app.state.SessionLocal() as session:
            owners = dict(session.execute(select(Appointment.appointment_id, Appointment.patient_id)).all())
    assert owners == {"APT-8391": "P-10041", "APT-8392": "P-20000"}


def test_postgres_registry_fails_closed_when_unreachable():
    """An address nothing listens on must still fail closed, and within a bounded time."""
    registry = PostgresPatientRegistry("postgresql+psycopg://x:y@127.0.0.1:1/db")
    started = time.monotonic()
    with pytest.raises(RegistryUnavailable):
        registry.exists("P-10041")
    with pytest.raises(RegistryUnavailable):
        registry.list_patients()
    assert time.monotonic() - started < 10


def test_postgres_registry_pool_stays_under_the_readers_connection_limit():
    """hospital_reader has CONNECTION LIMIT 5 (HospitalAgent 0004): the pool may open at most
    4, leaving one for a manual psql session. No connection is opened here."""
    registry = PostgresPatientRegistry("postgresql+psycopg://x:y@127.0.0.1:1/db")
    pool = registry._engine.pool
    assert pool.size() == 2
    assert pool._max_overflow == 2
    assert pool.size() + pool._max_overflow <= 4
    registry.dispose()


def test_postgres_registry_list_fails_closed_when_unreachable():
    registry = PostgresPatientRegistry("postgresql+psycopg://x:y@127.0.0.1:1/db")
    with pytest.raises(RegistryUnavailable):
        registry.list_patients()
    registry.dispose()


def test_postgres_registry_fails_closed_on_any_exception(monkeypatch):
    """Not just a SQLAlchemyError - any failure to ask the registry must become RegistryUnavailable."""
    registry = PostgresPatientRegistry("postgresql+psycopg://x:y@127.0.0.1:1/db")

    def _raise_runtime_error(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(registry._engine, "connect", _raise_runtime_error)
    with pytest.raises(RegistryUnavailable):
        registry.exists("P-10041")
    with pytest.raises(RegistryUnavailable):
        registry.list_patients()
