"""Tests for client authentication and backend timeouts (tests 15-17 of the spec).

Tests 15 and 16 send raw HTTP requests to the gateway's real HTTP app (the
same one uvicorn serves), through httpx.ASGITransport, so no port is opened.
"""

import httpx
import pytest
from mcp import Client

from backend.main import create_app
from backend.security import BackendSettings
from gateway.config import GatewaySettings
from gateway.server import create_http_app, create_server
from tests.conftest import LOCKED_VIN, SERVICE_KEY, RecordingTransport

pytestmark = pytest.mark.anyio

# A minimal, valid MCP request body. The gateway must reject it on auth
# grounds before even looking at it.
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def http_client(gateway_settings, gateway):
    """An httpx client pointed at the gateway's HTTP app, auth middleware included."""
    app = create_http_app(gateway_settings, gateway, host="127.0.0.1")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def test_mcp_endpoint_without_authorization_is_401(http_client):
    """A request to /mcp with no Authorization header is rejected with 401."""
    async with http_client:
        response = await http_client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_mcp_endpoint_with_wrong_token_is_401(http_client):
    """A request to /mcp with the wrong bearer token is rejected with 401."""
    headers = {**MCP_HEADERS, "Authorization": "Bearer not-the-right-token"}
    async with http_client:
        response = await http_client.post("/mcp", json=INITIALIZE, headers=headers)

    assert response.status_code == 401


async def test_backend_timeout_gives_clean_tool_error():
    """A backend slower than BACKEND_TIMEOUT_S gives a short timeout tool error, no traceback."""
    # Backend sleeps 500 ms per request; the gateway only waits 100 ms.
    slow_backend = create_app(
        BackendSettings(backend_service_key=SERVICE_KEY, simulated_latency_ms=500)
    )
    settings = GatewaySettings(
        backend_url="http://backend.test", backend_service_key=SERVICE_KEY, backend_timeout_s=0.1
    )
    gateway = create_server(settings, backend_transport=RecordingTransport(slow_backend))

    async with Client(gateway) as client:
        result = await client.call_tool("get_vehicle_status", {"vin": LOCKED_VIN})

    assert result.is_error is True
    message = result.content[0].text
    assert "did not respond within 0.1s" in message
    assert "Traceback" not in message
