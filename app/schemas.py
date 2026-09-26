from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    appointment_id: str
    patient_id: str
    department: str
    doctor_name: str | None
    appointment_at: datetime
    location: str | None
    status: str
    required_documents: list[str] = []

    @field_validator("appointment_at")
    @classmethod
    def _israel_time(cls, value: datetime) -> datetime:
        """SQLite keeps the Israel local time without an offset; say which zone it is."""
        return value.replace(tzinfo=ZoneInfo("Asia/Jerusalem")) if value.tzinfo is None else value


class AppointmentResult(BaseModel):
    found: bool
    appointment: AppointmentOut | None


class AppointmentList(BaseModel):
    appointments: list[AppointmentOut]
    truncated: bool


class HealthResult(BaseModel):
    status: str
    database: str
    version: str


class ErrorResult(BaseModel):
    error: str
    message: str

