"""Shared pytest fixtures.

Each test gets a brand-new backend app with a freshly seeded fleet, so state
changes in one test (like locking a car) never leak into another.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security import BackendSettings
from backend.store import FleetStore

SERVICE_KEY = "test-service-key"
AUTH = {"X-Service-Key": SERVICE_KEY}

# VINs from the seed data in backend/store.py.
LOCKED_VIN = "WBA00000000000001"
UNLOCKED_VIN = "WBA00000000000002"
UNKNOWN_VIN = "WBA00000000000099"  # valid format, but not in the fleet


@pytest.fixture
def store() -> FleetStore:
    """A fresh seeded fleet that the test can inspect directly."""
    return FleetStore()


@pytest.fixture
def backend_app(store: FleetStore):
    """A backend app wired to the test's store, with no artificial latency."""
    settings = BackendSettings(backend_service_key=SERVICE_KEY, simulated_latency_ms=0)
    return create_app(settings=settings, store=store)


@pytest.fixture
def backend_client(backend_app) -> TestClient:
    """A synchronous HTTP client that calls the backend app in memory."""
    return TestClient(backend_app)
