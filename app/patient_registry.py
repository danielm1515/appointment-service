"""HospitalAgent's patients registry, as this service sees it (sub-project 9).

The registry is the `patients` table in HospitalAgent's Postgres, read as hospital_reader - a
role that can read that one table and nothing else. This service asks whether a patient
exists, and lists the patients (id and name) for the booking form's dropdown. It never reads a
phone, and never writes a name to a log.
"""
from typing import Protocol

from sqlalchemy import create_engine, text


class RegistryUnavailable(Exception):
    """The registry could not be asked. The caller must fail closed."""


class PatientRegistry(Protocol):
    def exists(self, patient_id: str) -> bool:
        """True if the patient is registered. Raises RegistryUnavailable if it cannot tell."""

    def list_patients(self) -> list[tuple[str, str]]:
        """(patient_id, full_name) for every patient, by id. Raises RegistryUnavailable."""


class PostgresPatientRegistry:
    def __init__(self, url: str) -> None:
        self._engine = create_engine(
            url,
            pool_pre_ping=True,
            # At most 4 connections: hospital_reader has CONNECTION LIMIT 5 (HospitalAgent's
            # migration 0004), and one is left for a manual psql session. Raise neither
            # number without the other, or a burst of lookups turns into 503s.
            pool_size=2,
            max_overflow=2,
            # connect_timeout bounds the TCP connect; statement_timeout bounds the query itself,
            # so a connection that opens and then hangs still fails closed within a few seconds.
            connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
        )

    def exists(self, patient_id: str) -> bool:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT 1 FROM patients WHERE patient_id = :patient_id"),
                    {"patient_id": patient_id},
                ).first()
        except Exception as exc:
            # Any failure to ask the registry - not just a SQLAlchemy-recognized one - must fail
            # closed through this one exception, so the caller's 503 path is the only way out.
            raise RegistryUnavailable(type(exc).__name__) from exc
        return row is not None

    def list_patients(self) -> list[tuple[str, str]]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT patient_id, full_name FROM patients ORDER BY patient_id")
                ).all()
        except Exception as exc:
            raise RegistryUnavailable(type(exc).__name__) from exc
        return [(row.patient_id, row.full_name) for row in rows]

    def dispose(self) -> None:
        self._engine.dispose()
