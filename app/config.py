import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./appointments.db")
    seed_demo_data: bool = _as_bool(os.getenv("SEED_DEMO_DATA"), True)
    enable_failure_simulation: bool = _as_bool(
        os.getenv("ENABLE_FAILURE_SIMULATION"), False
    )
    mock_timeout_patient_id: str = os.getenv(
        "MOCK_TIMEOUT_PATIENT_ID", "P-TIMEOUT"
    )
    service_version: str = os.getenv("SERVICE_VERSION", "1.5.0")
    ui_auth_enabled: bool = _as_bool(os.getenv("UI_AUTH_ENABLED"), False)
    ui_setup_token: str = os.getenv("UI_SETUP_TOKEN", "")
    session_secret: str = os.getenv("SESSION_SECRET", "local-session-secret-change-me")
    secure_cookies: bool = _as_bool(os.getenv("SECURE_COOKIES"), False)
    api_auth_enabled: bool = _as_bool(os.getenv("API_AUTH_ENABLED"), False)
    appointment_api_key: str = os.getenv("APPOINTMENT_API_KEY", "")
    patient_registry_url: str = os.getenv("PATIENT_REGISTRY_URL", "")
