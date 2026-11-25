"""The MCP server: the tools an AI client can call.

This is the middle box in the architecture diagram. An AI client (Claude
Desktop, Claude Code, ...) connects here over MCP, discovers the tools with
`tools/list`, and calls them with `tools/call`. Each tool:

1. receives arguments that the SDK has already validated against the
   types in `schemas.py`,
2. calls the vehicle backend through `BackendClient`, and
3. returns a typed output model, sent to the client as structured content.

A note on names: the spec for this project says "FastMCP". In version 2 of
the official `mcp` SDK, FastMCP was renamed to `MCPServer`; it is the same
high-level, decorator-based server.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from gateway.backend_client import BackendClient
from gateway.config import GatewaySettings
from gateway.schemas import (
    DiagnosticReport,
    TripHistory,
    TripLimit,
    VehicleList,
    VehicleStatus,
    Vin,
)

# Tool annotations are hints the client can show to the user or use to decide
# how careful to be (for example, auto-approving read-only tools).
# open_world_hint=False: the tools only touch our own fleet backend, not the
# open internet.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

INSTRUCTIONS = (
    "Tools for a connected-vehicle fleet. Start with list_vehicles to learn the VINs, "
    "then pass a VIN to the other tools."
)


def create_server(
    settings: GatewaySettings,
    backend_transport: httpx.AsyncBaseTransport | None = None,
) -> MCPServer:
    """Build the MCP server and register its tools.

    `backend_transport` is for tests: they pass an in-memory transport that
    routes HTTP calls straight into the backend app.
    """
    # Step 1: Create the one backend client this server will use.
    backend = BackendClient(
        base_url=settings.backend_url,
        service_key=settings.backend_service_key,
        timeout_s=settings.backend_timeout_s,
        transport=backend_transport,
    )

    # Step 2: Close the backend client's connections when the server shuts
    #         down. The SDK runs this "lifespan" around the server's lifetime.
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await backend.aclose()

    mcp = MCPServer(
        name="vehicle-mcp-gateway",
        instructions=INSTRUCTIONS,
        log_level=settings.log_level.upper(),
        lifespan=lifespan,
    )

    # Step 3: Register the tools. The docstring of each tool becomes its
    #         description, which is what the MODEL reads to decide when to use
    #         it, so the docstrings are written for the model.
    #
    #         The tools are defined inside this function (as closures) so they
    #         can use `backend` directly, with no global state. That is what
    #         lets each test build its own server against its own backend.

    @mcp.tool(title="List vehicles", annotations=READ_ONLY)
    async def list_vehicles() -> VehicleList:
        """List every vehicle in the fleet with its VIN, nickname, model, battery
        level and lock state. Call this first to find the VIN for other tools."""
        data = await backend.list_vehicles()
        return VehicleList(vehicles=data)

    @mcp.tool(title="Get vehicle status", annotations=READ_ONLY)
    async def get_vehicle_status(vin: Vin) -> VehicleStatus:
        """Get the full live status of one vehicle: battery, range, odometer,
        lock state, cabin temperature, climate target and GPS location."""
        # Step 1: `vin` already matches the VIN pattern (checked by the SDK).
        # Step 2: Fetch from the backend; failures become a BackendError.
        data = await backend.get_status(vin)
        # Step 3: Validate the backend's JSON into our output model. If the
        #         backend ever sends something unexpected, this fails loudly
        #         here instead of passing bad data to the model.
        return VehicleStatus.model_validate(data)

    @mcp.tool(title="Get diagnostic trouble codes", annotations=READ_ONLY)
    async def get_diagnostic_codes(vin: Vin) -> DiagnosticReport:
        """Get the active OBD-II diagnostic trouble codes (DTCs) for one vehicle,
        each with a description and a severity (low, medium, high). An empty
        list means no faults are reported."""
        data = await backend.get_diagnostics(vin)
        return DiagnosticReport.model_validate(data)

    @mcp.tool(title="Get trip history", annotations=READ_ONLY)
    async def get_trip_history(vin: Vin, limit: TripLimit = 5) -> TripHistory:
        """Get the most recent trips of one vehicle (newest first), with start and
        end times, distance in km and energy used in kWh."""
        # `limit` is guaranteed to be 1-20 by the TripLimit type, so an
        # out-of-range value never reaches the backend.
        data = await backend.get_trips(vin, limit)
        return TripHistory.model_validate(data)

    return mcp
