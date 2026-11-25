"""Tests for the gateway's MCP tools (tests 8-14 of the spec).

Every test talks to the gateway through a real MCP `Client` session, exactly
as Claude would, rather than calling the Python functions directly. That way
the tests also cover schema generation, argument validation, structured
output and error flagging done by the SDK.
"""

import pytest
from mcp import Client, types

from gateway.schemas import CommandReceipt, VehicleStatus
from tests.conftest import LOCKED_VIN, UNKNOWN_VIN

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_vehicles",
    "get_vehicle_status",
    "get_diagnostic_codes",
    "get_trip_history",
    "set_climate",
    "send_remote_command",
}
READ_ONLY_TOOLS = EXPECTED_TOOLS - {"set_climate", "send_remote_command"}


def human_answers(action: str, confirm: bool | None = None):
    """Build an elicitation callback that plays the HUMAN in the MCP client.

    The real client would show a form; this one answers straight away with
    `action` ("accept", "decline" or "cancel") and records every prompt it got.
    """
    prompts: list[str] = []

    async def callback(context, params: types.ElicitRequestParams) -> types.ElicitResult:
        prompts.append(params.message)
        content = {"confirm": confirm} if action == "accept" else None
        return types.ElicitResult(action=action, content=content)

    callback.prompts = prompts
    return callback


async def test_list_tools_returns_six_tools_with_schemas(mcp_client):
    """tools/list returns exactly the 6 tools, each with an object input schema."""
    tools = (await mcp_client.list_tools()).tools

    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema is not None
    # The confirmation is filled by the server, so the model cannot pass it.
    command_tool = next(t for t in tools if t.name == "send_remote_command")
    assert set(command_tool.input_schema["properties"]) == {"vin", "command"}


async def test_tool_annotations(mcp_client):
    """Tools 1-4 are marked read-only and send_remote_command is marked destructive."""
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}

    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    command = tools["send_remote_command"].annotations
    assert command.read_only_hint is False
    assert command.destructive_hint is True


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


async def test_unlock_with_user_accepting(gateway, store):
    """When the human accepts the confirmation form, unlock executes and the car is unlocked."""
    human = human_answers("accept", confirm=True)
    async with Client(gateway, elicitation_callback=human) as client:
        result = await client.call_tool(
            "send_remote_command", {"vin": LOCKED_VIN, "command": "unlock"}
        )

    assert result.is_error is False
    assert CommandReceipt.model_validate(result.structured_content).status == "executed"
    assert len(human.prompts) == 1  # the human was asked exactly once
    assert store.get(LOCKED_VIN).locked is False


async def test_unlock_with_user_declining(gateway, store, backend_transport):
    """When the human declines, the result is an error and the car stays locked."""
    human = human_answers("decline")
    async with Client(gateway, elicitation_callback=human) as client:
        result = await client.call_tool(
            "send_remote_command", {"vin": LOCKED_VIN, "command": "unlock"}
        )

    assert result.is_error is True
    assert "not confirmed by the user" in result.content[0].text
    assert len(human.prompts) == 1
    assert store.get(LOCKED_VIN).locked is True
    assert backend_transport.requests == []  # nothing was sent to the backend
