"""Shared fixtures.

create_app(patient_registry=None) falls back to PATIENT_REGISTRY_URL, so a shell that has it
set would silently turn every "no registry" test - in test_api.py and test_patient_registry.py
alike - into one against a real registry. Settings reads the environment when app.config is
first imported, so clearing the variable is not enough on its own: the fixture also hands
create_app a Settings with the URL blanked. A test that wants a registry passes one explicitly.

app is imported inside the fixture, not here: the test modules set ENABLE_FAILURE_SIMULATION
before their own first import of app, and a module-level import in this file would run first.
"""
import dataclasses

import pytest


@pytest.fixture(autouse=True)
def no_patient_registry_from_the_environment(monkeypatch):
    import app.main
    from app.config import Settings

    monkeypatch.delenv("PATIENT_REGISTRY_URL", raising=False)
    monkeypatch.setattr(
        app.main, "Settings", lambda: dataclasses.replace(Settings(), patient_registry_url="")
    )
