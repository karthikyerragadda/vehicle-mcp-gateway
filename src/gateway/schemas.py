"""Typed inputs and outputs for the gateway's MCP tools.

**Inputs.** The MCP SDK builds each tool's input schema from the tool
function's parameters. We describe every parameter with a reusable
`Annotated[type, Field(...)]` type defined here. The SDK turns them into a
Pydantic model per tool, so:

- the AI client sees a precise JSON Schema (pattern, min/max, enum), and
- bad arguments are rejected *by code* before the tool body runs. We never
  rely on the prompt to tell the model "please send a valid VIN".

**Outputs.** Each tool returns one of the models below. The SDK publishes the
model's JSON Schema as the tool's `outputSchema` and sends the data as
`structuredContent`, so clients get typed results instead of free text.

These are the gateway's own copies of the backend's shapes. The two services
are deployed separately, so the gateway does not import backend code.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# --- Input types ------------------------------------------------------------------

# 17 characters, digits and capital letters, excluding I, O and Q (which real
# VINs never use because they look like 1 and 0).
VIN_PATTERN = r"^[A-HJ-NPR-Z0-9]{17}$"

Vin = Annotated[
    str,
    Field(
        pattern=VIN_PATTERN,
        description="17-character Vehicle Identification Number, e.g. WBA00000000000001. "
        "Call list_vehicles to get valid VINs.",
    ),
]

TripLimit = Annotated[
    int,
    Field(ge=1, le=20, description="How many of the most recent trips to return (1-20)."),
]


# --- Output models ----------------------------------------------------------------


class Location(BaseModel):
    lat: float
    lon: float


class VehicleSummary(BaseModel):
    vin: str
    nickname: str
    model: str
    battery_percent: int
    locked: bool


class VehicleList(BaseModel):
    """Output of list_vehicles. Wrapped in an object because MCP structured
    output must be a JSON object, not a bare list."""

    vehicles: list[VehicleSummary]


class VehicleStatus(VehicleSummary):
    range_km: float
    odometer_km: float
    cabin_temp_celsius: float
    climate_target_celsius: float | None = Field(
        description="Target cabin temperature, or null if climate control is off."
    )
    location: Location


class DiagnosticCode(BaseModel):
    code: str = Field(description="OBD-II trouble code, e.g. P0420.")
    description: str
    severity: Literal["low", "medium", "high"]


class DiagnosticReport(BaseModel):
    vin: str
    codes: list[DiagnosticCode]


class Trip(BaseModel):
    trip_id: str
    started_at: datetime
    ended_at: datetime
    distance_km: float
    energy_used_kwh: float


class TripHistory(BaseModel):
    vin: str
    trips: list[Trip] = Field(description="Most recent trip first.")
