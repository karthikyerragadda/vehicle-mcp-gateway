"""Pydantic models for the vehicle backend's REST API.

These models define the JSON that goes in and out of the backend routes in
`main.py`. FastAPI uses them for two things:

1. Validating request bodies. A bad body is rejected with HTTP 422 before any
   route code runs.
2. Shaping responses. Only the declared fields are serialised, so internal
   data cannot leak out by accident.

The gateway has its own copies of the output shapes (`gateway/schemas.py`) on
purpose: the backend is an independent service and must not depend on the
gateway.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# A VIN (Vehicle Identification Number) is 17 characters. The letters I, O and Q
# are never used because they are too easy to confuse with 1 and 0.
VIN_PATTERN = r"^[A-HJ-NPR-Z0-9]{17}$"

# Real OBD-II trouble codes: a system letter (P=powertrain, C=chassis,
# B=body, U=network) followed by 4 hex digits, e.g. "P0420".
DTC_PATTERN = r"^[PCBU][0-9A-F]{4}$"

RemoteCommand = Literal["lock", "unlock", "flash_lights"]


class Location(BaseModel):
    """GPS position of a vehicle in decimal degrees."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class DiagnosticCode(BaseModel):
    """One diagnostic trouble code (DTC) reported by the vehicle."""

    code: str = Field(pattern=DTC_PATTERN)
    description: str
    severity: Literal["low", "medium", "high"]


class Trip(BaseModel):
    """One completed trip."""

    trip_id: str
    started_at: datetime
    ended_at: datetime
    distance_km: float = Field(ge=0)
    energy_used_kwh: float = Field(ge=0)


class VehicleSummary(BaseModel):
    """Short view of a vehicle, used by the fleet list."""

    vin: str
    nickname: str
    model: str
    battery_percent: int
    locked: bool


class VehicleStatus(VehicleSummary):
    """Full live status of one vehicle (extends the summary)."""

    range_km: float
    odometer_km: float
    cabin_temp_celsius: float
    # None means climate control is off (no target has been set).
    climate_target_celsius: float | None
    location: Location


class VehicleRecord(VehicleStatus):
    """Everything the in-memory store keeps about a vehicle.

    This is the internal record. Routes never return it directly; they return
    the narrower models above so each endpoint exposes only what it should.
    """

    diagnostics: list[DiagnosticCode]
    trips: list[Trip]  # newest first


class DiagnosticsResponse(BaseModel):
    vin: str
    codes: list[DiagnosticCode]


class TripsResponse(BaseModel):
    vin: str
    trips: list[Trip]


class ClimateRequest(BaseModel):
    """Body of POST /vehicles/{vin}/climate."""

    # The comfortable, safe range. Anything outside it gets a 422 from FastAPI.
    target_celsius: float = Field(ge=16.0, le=28.0)


class ClimateState(BaseModel):
    """Climate settings after a successful change."""

    vin: str
    climate_target_celsius: float
    cabin_temp_celsius: float


class CommandRequest(BaseModel):
    """Body of POST /vehicles/{vin}/commands."""

    # Literal means Pydantic rejects any other string with a 422.
    command: RemoteCommand


class CommandRecord(BaseModel):
    """Receipt for a remote command the backend has executed."""

    command_id: str
    vin: str
    command: RemoteCommand
    status: Literal["executed"]
    executed_at: datetime
