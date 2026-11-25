"""The MCP server: the tools an AI client can call.

This is the middle box in the architecture diagram. An AI client (Claude
Desktop, Claude Code, ...) connects here over MCP, discovers the tools with
`tools/list`, and calls them with `tools/call`. Each tool:

1. receives arguments that the SDK has already validated against the
   types in `schemas.py`,
2. calls the vehicle backend through `BackendClient`, and
3. returns a typed output model, sent to the client as structured content.

The one sensitive action, unlocking a car, needs approval from the HUMAN via
MCP elicitation. See `confirm_unlock_with_user` below for how that works and
why the model cannot skip it.

Two ways to run it (see `main()` at the bottom):

- `--transport streamable-http` (default): an HTTP server at /mcp, protected by
  a bearer token (`auth.py`). Used by Docker, App Runner and Claude Code.
- `--transport stdio`: the client starts this process and talks over
  stdin/stdout. Used by Claude Desktop locally. There is no network, so there
  is no token either: whoever can start the process is trusted.

A note on names: FastMCP, the high-level server from earlier SDK versions, was
renamed to `MCPServer` in version 2 of the official `mcp` SDK. It is the same
decorator-based server.
"""

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import uvicorn
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from gateway.auth import BearerTokenMiddleware
from gateway.backend_client import BackendClient
from gateway.config import GatewaySettings, load_settings
from gateway.schemas import (
    ClimateState,
    ClimateTarget,
    CommandReceipt,
    DiagnosticReport,
    RemoteCommand,
    TripHistory,
    TripLimit,
    UnlockConfirmation,
    VehicleList,
    VehicleStatus,
    Vin,
)

logger = logging.getLogger("gateway")

# Tool annotations are hints the client can show to the user or use to decide
# how careful to be (for example, auto-approving read-only tools).
# open_world_hint=False: the tools only touch our own fleet backend, not the
# open internet.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Changes state, but harmlessly, and setting 21 °C twice is the same as once.
CLIMATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Physically affects a real-world object (a car's doors), so clients should
# treat it with the most caution.
REMOTE_COMMAND = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

NOT_CONFIRMED = "Unlock was not confirmed by the user; no command was sent."
NO_ELICITATION = (
    "Unlock needs confirmation from the user, but this MCP client does not support "
    "elicitation; no command was sent."
)


# --- The confirmation step the model cannot skip -------------------------------------
#
# How it works:
#
# `send_remote_command` declares a parameter `confirmation` annotated with
# `Resolve(confirm_unlock_with_user)`. Parameters filled by a resolver are NOT
# part of the tool's input schema, so the model has no way to pass a value for
# them. There is no `confirmed=true` argument it could set by itself.
#
# Before the tool body runs, the SDK calls the resolver. For "unlock" it
# returns an `Elicit(...)` request, which the SDK sends to the MCP *client*
# (the app, e.g. Claude Desktop), not to the model. The client shows a form to
# the human and sends back their answer: accept, decline or cancel. Only then
# does the tool body run, with that answer in `confirmation`.
#
# The SDK handles both protocol styles: in the 2026-07-28 MCP protocol the
# question travels back in an `InputRequiredResult` and the client retries the
# call with the answer (the SDK encrypts the state the client carries between
# rounds, so a client cannot forge an "accept"); in older protocols the server
# sends a separate `elicitation/create` request in the middle of the call.


async def confirm_unlock_with_user(
    vin: str, command: str, ctx: Context
) -> Elicit[UnlockConfirmation] | None:
    """Resolver: decide whether this call needs the human's approval.

    It takes `vin` and `command` by name from the tool's own (already
    validated) arguments.
    """
    # Step 1: Only unlocking is sensitive. Locking or flashing the lights
    #         can't let anyone into the car, so they need no confirmation.
    if command != "unlock":
        return None

    # Step 2: If the client never declared the elicitation capability, there is
    #         no way to ask the human. Refuse rather than unlock unconfirmed.
    caps = ctx.client_capabilities
    if caps is None or caps.elicitation is None:
        logger.warning("unlock refused for vin=%s: client does not support elicitation", vin)
        raise ToolError(NO_ELICITATION)

    # Step 3: Ask the human. The SDK delivers this to the client and injects the
    #         answer into the tool's `confirmation` parameter.
    return Elicit(
        message=(
            f"An AI assistant is asking to UNLOCK vehicle {vin}. "
            "Anyone near the car will be able to open it. Allow?"
        ),
        schema=UnlockConfirmation,
    )


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

    # Liveness probe for Docker / App Runner (HTTP transport only). It does not
    # call the backend: it answers "is this process up?", nothing more.
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

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

    @mcp.tool(title="Set cabin temperature", annotations=CLIMATE)
    async def set_climate(vin: Vin, target_celsius: ClimateTarget) -> ClimateState:
        """Turn on climate control and set the target cabin temperature of one
        vehicle, in °C between 16 and 28. Use this to pre-heat or pre-cool the car."""
        # Step 1: target_celsius is already 16-28 (ClimateTarget type), so the
        #         backend never sees an out-of-range value from this gateway.
        # Step 2: Send it to the backend and return the new climate state.
        data = await backend.set_climate(vin, target_celsius)
        return ClimateState.model_validate(data)

    @mcp.tool(title="Send remote command", annotations=REMOTE_COMMAND)
    async def send_remote_command(
        vin: Vin,
        command: RemoteCommand,
        # Filled by the resolver, never by the model (see the comment block
        # above confirm_unlock_with_user). `ElicitationResult[...]` means we
        # receive the full outcome (accept / decline / cancel) and decide here.
        confirmation: Annotated[
            ElicitationResult[UnlockConfirmation], Resolve(confirm_unlock_with_user)
        ],
    ) -> CommandReceipt:
        """Send a remote command to one vehicle: lock, unlock, or flash_lights.
        Unlocking asks the human user to confirm in their MCP client first; if
        they do not approve, nothing is sent to the vehicle."""
        # Step 1: Validate input. The SDK has already checked the VIN format and
        #         that `command` is one of the allowed values.
        # Step 2: For unlock, require an explicit "yes" from the HUMAN. Declined,
        #         cancelled, or accepted with the box unticked all count as no.
        if command == "unlock":
            approved = isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm
            if not approved:
                logger.info("unlock not confirmed for vin=%s (%s)", vin, confirmation.action)
                raise ToolError(NOT_CONFIRMED)
            logger.info("unlock confirmed by user for vin=%s", vin)

        # Step 3: Only now call the backend with the service key.
        data = await backend.send_command(vin, command)

        # Step 4: Convert the backend response into the typed output model.
        return CommandReceipt.model_validate(data)

    return mcp


# --- Running the server ------------------------------------------------------------


def create_http_app(settings: GatewaySettings, server: MCPServer, host: str) -> ASGIApp:
    """Wrap the MCP server's HTTP app in the bearer-token check."""
    # Step 1: Refuse to serve HTTP without a client token. Without it anyone who
    #         can reach the port could drive the tools.
    if not settings.mcp_client_token:
        raise SystemExit(
            "Gateway configuration error: MCP_CLIENT_TOKEN is required for the HTTP transport."
        )

    # Step 2: The SDK builds a Starlette app serving MCP at /mcp (plus /health).
    #         Passing `host` lets the SDK switch on its DNS-rebinding protection
    #         automatically when we only listen on localhost.
    starlette_app = server.streamable_http_app(host=host)

    # Step 3: Put the token check in front of everything.
    return BearerTokenMiddleware(starlette_app, settings.mcp_client_token)


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: `python -m gateway.server [--transport ...]`."""
    parser = argparse.ArgumentParser(description="Vehicle MCP gateway")
    parser.add_argument(
        "--transport", choices=["streamable-http", "stdio"], default="streamable-http"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to listen on (HTTP only). Use 0.0.0.0 inside a container.",
    )
    args = parser.parse_args(argv)

    # Step 1: Load settings, exiting with a clear message if a secret is missing.
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper())
    server = create_server(settings)

    # Step 2: Start the chosen transport.
    if args.transport == "stdio":
        # stdout carries the MCP messages, so nothing else may print to it;
        # logging goes to stderr by default.
        server.run("stdio")
    else:
        app = create_http_app(settings, server, host=args.host)
        uvicorn.run(app, host=args.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
