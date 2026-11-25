"""Tests for the gateway's MCP tools (tests 8-14 of the spec).

Every test talks to the gateway through a real MCP `Client` session, exactly
as Claude would, rather than calling the Python functions directly. That way
the tests also cover schema generation, argument validation, structured
output and error flagging done by the SDK.
"""

import pytest

from gateway.schemas import VehicleStatus
from tests.conftest import LOCKED_VIN, UNKNOWN_VIN

pytestmark = pytest.mark.anyio


async def test_get_vehicle_status_returns_structured_output(mcp_client):
    """get_vehicle_status returns structuredContent that validates against VehicleStatus."""
    result = await mcp_client.call_tool("get_vehicle_status", {"vin": LOCKED_VIN})

    assert result.is_error is False
    status = VehicleStatus.model_validate(result.structured_content)
    assert status.vin == LOCKED_VIN
    assert status.locked is True


async def test_unknown_vin_is_error_with_hint(mcp_client):
    """An unknown VIN gives an isError result that tells the model to call list_vehicles."""
    result = await mcp_client.call_tool("get_vehicle_status", {"vin": UNKNOWN_VIN})

    assert result.is_error is True
    assert "Call list_vehicles" in result.content[0].text


async def test_trip_limit_out_of_range_never_reaches_backend(mcp_client, backend_transport):
    """limit=50 is rejected by schema validation and the backend receives no request."""
    result = await mcp_client.call_tool("get_trip_history", {"vin": LOCKED_VIN, "limit": 50})

    assert result.is_error is True
    assert "limit" in result.content[0].text
    assert backend_transport.requests == []
