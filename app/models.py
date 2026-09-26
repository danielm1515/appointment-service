from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DocumentTypeRow(Base):
    """The document-type catalog (design §2), seeded from app/catalog.py on every start."""

    __tablename__ = "document_types"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label_he: Mapped[str] = mapped_column(String(120), nullable=False)
    max_age_days: Mapped[int] = mapped_column(Integer, nullable=False)


class AppointmentRequiredDocument(Base):
    """The focused spec's link table: one row per (appointment, required document type)."""

    __tablename__ = "appointment_required_documents"

    appointment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("appointments.appointment_id", ondelete="CASCADE"), primary_key=True)
    document_type: Mapped[str] = mapped_column(
        String(32), ForeignKey("document_types.code"), primary_key=True)


class ExamTypeRow(Base):
    """The exam-type catalog (design §4), seeded from app/catalog.py on every start, like
    document_types. A new table (sub-project 18 D1): create_all adds it to an existing
    database without touching any existing table."""

    __tablename__ = "exam_types"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    label_he: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instruction_version: Mapped[str] = mapped_column(String(16), nullable=False)
    instruction_title: Mapped[str] = mapped_column(String(200), nullable=False)
    instruction_text: Mapped[str] = mapped_column(String(4000), nullable=False)


class AppointmentExamType(Base):
    """One row per appointment: which exam type it was booked for (design D1). A new table -
    an appointment with no row here resolves to its department's default exam."""

    __tablename__ = "appointment_exam_types"

    appointment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("appointments.appointment_id", ondelete="CASCADE"), primary_key=True)
    exam_code: Mapped[str] = mapped_column(String(32), ForeignKey("exam_types.code"), nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"

    appointment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    doctor_name: Mapped[str | None] = mapped_column(String(120))
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Loaded with the appointment (selectin), so the schema and the template can read it after
    # the session closes. Replacing the list replaces the rows (delete-orphan).
    required_document_links: Mapped[list["AppointmentRequiredDocument"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin",
        order_by="AppointmentRequiredDocument.document_type")

    # One row or none (uselist=False): the exam type this appointment was booked for.
    exam_type_link: Mapped["AppointmentExamType | None"] = relationship(
        cascade="all, delete-orphan", lazy="selectin", uselist=False)

    @property
    def required_documents(self) -> list[str]:
        # Sorted here, so the API's promise does not rest on the relationship's order_by alone.
        return sorted(link.document_type for link in self.required_document_links)

    @property
    def exam_code(self) -> str | None:
        return self.exam_type_link.exam_code if self.exam_type_link else None


class AuditLog(Base):
    __tablename__ = "appointment_audit_logs"

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    case_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    execution_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    patient_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(nullable=False)


class AdminUser(Base):
    __tablename__ = "admin_users"

    admin_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
