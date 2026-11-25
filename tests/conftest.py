"""Shared pytest fixtures.

Each test gets a brand-new backend app with a freshly seeded fleet, so state
changes in one test (like locking a car) never leak into another.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver import MCPServer

from backend.main import create_app
from backend.security import BackendSettings
from backend.store import FleetStore
from gateway.config import GatewaySettings
from gateway.server import create_server

SERVICE_KEY = "test-service-key"
AUTH = {"X-Service-Key": SERVICE_KEY}
CLIENT_TOKEN = "test-client-token"

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


# --- Gateway fixtures ---------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio (the anyio pytest plugin reads this fixture)."""
    return "asyncio"


class RecordingTransport(httpx.AsyncBaseTransport):
    """An httpx transport that forwards to the in-memory backend and records
    every request, so tests can prove whether the backend was called at all."""

    def __init__(self, app) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


@pytest.fixture
def backend_transport(backend_app) -> RecordingTransport:
    return RecordingTransport(backend_app)


@pytest.fixture
def gateway_settings() -> GatewaySettings:
    return GatewaySettings(
        backend_url="http://backend.test",
        backend_service_key=SERVICE_KEY,
        mcp_client_token=CLIENT_TOKEN,
        backend_timeout_s=5,
    )


@pytest.fixture
def gateway(gateway_settings, backend_transport) -> MCPServer:
    """The MCP server, wired to the in-memory backend (no real network)."""
    return create_server(gateway_settings, backend_transport=backend_transport)


@pytest.fixture
async def mcp_client(gateway) -> AsyncIterator[Client]:
    """A real MCP client session connected to the gateway in-process."""
    async with Client(gateway) as client:
        yield client
