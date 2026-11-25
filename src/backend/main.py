"""FastAPI app for the simulated vehicle backend.

This is the bottom box in the architecture: a plain REST/JSON service. It has
no idea that an AI might be behind the requests. That is the point of the
design: an existing backend stays unchanged, and the MCP gateway adapts it
for AI clients.

Run it with:

    uvicorn backend.main:create_app --factory --port 8001

`--factory` tells uvicorn to call `create_app()` instead of importing a
module-level `app`. Settings (and therefore the required service key) are
read only when the app is built, which keeps importing this module side-effect
free for the tests.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status

from backend.models import (
    ClimateRequest,
    ClimateState,
    CommandRecord,
    CommandRequest,
    DiagnosticsResponse,
    TripsResponse,
    VehicleStatus,
    VehicleSummary,
)
from backend.security import BackendSettings, require_service_key
from backend.store import FleetStore, VehicleNotFoundError

logger = logging.getLogger("backend")


# --- Dependencies -----------------------------------------------------------
# FastAPI "dependencies" are functions that run before a route. They let us
# share logic (auth, latency, looking up the store) without repeating it.


def get_store(request: Request) -> FleetStore:
    """Give a route the fleet store that belongs to this app instance."""
    return request.app.state.store


async def simulate_latency(request: Request) -> None:
    """Sleep for SIMULATED_LATENCY_MS before handling the request.

    asyncio.sleep (not time.sleep) lets the server keep serving other
    requests while this one waits.
    """
    delay_ms = request.app.state.settings.simulated_latency_ms
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)


# `Store` is a reusable type: "a FleetStore, provided by get_store". Using
# Annotated keeps the dependency out of the default value, which is the style
# FastAPI recommends.
Store = Annotated[FleetStore, Depends(get_store)]


def vehicle_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="vehicle not found")


# --- Routes -------------------------------------------------------------------
# Every route on this router requires the service key, then applies the
# simulated latency. /health is deliberately NOT on this router.

vehicles = APIRouter(
    prefix="/vehicles",
    dependencies=[Depends(require_service_key), Depends(simulate_latency)],
)


@vehicles.get("", response_model=list[VehicleSummary])
async def list_vehicles(store: Store) -> list[VehicleSummary]:
    # response_model trims each full record down to the summary fields.
    return store.list()


@vehicles.get("/{vin}/status", response_model=VehicleStatus)
async def get_status(vin: str, store: Store) -> VehicleStatus:
    try:
        return store.get(vin)
    except VehicleNotFoundError:
        raise vehicle_not_found() from None


@vehicles.get("/{vin}/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(vin: str, store: Store) -> DiagnosticsResponse:
    try:
        vehicle = store.get(vin)
    except VehicleNotFoundError:
        raise vehicle_not_found() from None
    return DiagnosticsResponse(vin=vin, codes=vehicle.diagnostics)


@vehicles.get("/{vin}/trips", response_model=TripsResponse)
async def get_trips(
    vin: str,
    store: Store,
    # Query(ge=1, le=20) makes FastAPI reject limit=0 or limit=50 with a 422.
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> TripsResponse:
    try:
        vehicle = store.get(vin)
    except VehicleNotFoundError:
        raise vehicle_not_found() from None
    # Trips are stored newest first, so the first `limit` are the most recent.
    return TripsResponse(vin=vin, trips=vehicle.trips[:limit])


@vehicles.post("/{vin}/climate", response_model=ClimateState)
async def set_climate(vin: str, body: ClimateRequest, store: Store) -> ClimateState:
    # Step 1: FastAPI has already validated `body` (16.0 <= target <= 28.0).
    # Step 2: Apply the change, turning an unknown VIN into a 404.
    try:
        vehicle = store.set_climate(vin, body.target_celsius)
    except VehicleNotFoundError:
        raise vehicle_not_found() from None
    # Step 3: Report the new climate state back to the caller.
    return ClimateState(
        vin=vin,
        climate_target_celsius=body.target_celsius,
        cabin_temp_celsius=vehicle.cabin_temp_celsius,
    )


@vehicles.post("/{vin}/commands", response_model=CommandRecord)
async def send_command(vin: str, body: CommandRequest, store: Store) -> CommandRecord:
    # Step 1: FastAPI has already checked `command` is lock/unlock/flash_lights.
    # Step 2: Execute it and log it, because remote commands change a real
    #         car's state and are worth an audit trail even in a simulation.
    try:
        record = store.execute_command(vin, body.command)
    except VehicleNotFoundError:
        raise vehicle_not_found() from None
    logger.info("executed command=%s vin=%s id=%s", record.command, vin, record.command_id)
    return record


# --- App factory ----------------------------------------------------------------


def create_app(settings: BackendSettings | None = None, store: FleetStore | None = None) -> FastAPI:
    """Build a backend app.

    Tests pass their own settings and store; production lets both default
    (settings from the environment, a freshly seeded fleet).
    """
    # Step 1: Load settings. BackendSettings() raises a clear validation error
    #         right away if BACKEND_SERVICE_KEY is missing (fail fast).
    settings = settings or BackendSettings()
    logging.basicConfig(level=settings.log_level.upper())

    # Step 2: Create the app and attach per-instance state. Dependencies read
    #         it via request.app.state, so two apps never share a fleet.
    app = FastAPI(title="Vehicle backend (simulated)", version="0.1.0")
    app.state.settings = settings
    app.state.store = store if store is not None else FleetStore()

    # Step 3: The health check sits outside the protected router so Docker and
    #         App Runner can probe it without knowing the service key.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(vehicles)
    return app
