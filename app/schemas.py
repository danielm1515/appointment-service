from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .catalog import EXAMS_BY_CODE, default_exam


class ExamTypeOut(BaseModel):
    code: str
    label: str


class InstructionOut(BaseModel):
    source_id: str
    version: str
    title: str


class InstructionResult(InstructionOut):
    text: str


class ExamCatalogInconsistent(Exception):
    """The appointment's department, or its own linked exam code, is not (or no longer) in the
    exam-type catalog. Raised instead of silently substituting the department default (M4,
    fix round 1) - the route must catch this and fail closed (503 service_unavailable), never
    return a guessed answer and never a 500."""


def _resolve_exam(department: str, exam_code: str | None):
    if exam_code is not None:
        exam = EXAMS_BY_CODE.get(exam_code)
        if exam is None:
            raise ExamCatalogInconsistent(f"exam code {exam_code!r} is not in the catalog")
        return exam
    try:
        return default_exam(department)
    except KeyError as e:
        raise ExamCatalogInconsistent(str(e)) from e


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
    # Never null (design D3): the link's own exam, or else the department's default "…_VISIT" -
    # resolved here so every consumer (the API, the dashboard) reads the same rule once.
    exam_type: ExamTypeOut
    instruction: InstructionOut

    @model_validator(mode="before")
    @classmethod
    def _resolve_exam_and_instruction(cls, data):
        already_resolved = isinstance(data, dict) and "exam_type" in data
        if already_resolved:
            return data
        get = data.get if isinstance(data, dict) else (lambda k, default=None: getattr(data, k, default))
        exam = _resolve_exam(get("department"), get("exam_code"))
        return {
            "appointment_id": get("appointment_id"),
            "patient_id": get("patient_id"),
            "department": get("department"),
            "doctor_name": get("doctor_name"),
            "appointment_at": get("appointment_at"),
            "location": get("location"),
            "status": get("status"),
            "required_documents": get("required_documents") or [],
            "exam_type": {"code": exam.code, "label": exam.label},
            "instruction": {"source_id": exam.instruction_id, "version": exam.instruction_version,
                            "title": exam.instruction_title},
        }

    @field_validator("appointment_at")
    @classmethod
    def _israel_time(cls, value: datetime) -> datetime:
        """SQLite keeps the Israel local time without an offset; say which zone it is."""
        return value.replace(tzinfo=ZoneInfo("Asia/Jerusalem")) if value.tzinfo is None else value


class AppointmentResult(BaseModel):
    found: bool
    appointment: AppointmentOut | None
    upcoming_count: int = 0


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

