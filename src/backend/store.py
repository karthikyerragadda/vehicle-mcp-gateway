"""In-memory simulated fleet: the backend's "database".

A real connected-vehicle backend would talk to the cars over a telematics
network. Here, three vehicles live in a Python dict so the project runs
anywhere with no external services. Data resets every time the process
starts; that is a known limitation, documented in the README.

Each `create_app()` call gets its own `FleetStore`, so every test starts from
the same seed data and tests cannot affect each other.
"""

import uuid
from datetime import UTC, datetime

from backend.models import (
    CommandRecord,
    DiagnosticCode,
    Location,
    RemoteCommand,
    Trip,
    VehicleRecord,
)


class VehicleNotFoundError(KeyError):
    """Raised when a VIN is not in the fleet. Routes turn this into HTTP 404."""


def _trip(trip_id: str, start: str, end: str, km: float, kwh: float) -> Trip:
    """Small helper to keep the seed data below readable. Times are UTC."""
    return Trip(
        trip_id=trip_id,
        started_at=datetime.fromisoformat(start).replace(tzinfo=UTC),
        ended_at=datetime.fromisoformat(end).replace(tzinfo=UTC),
        distance_km=km,
        energy_used_kwh=kwh,
    )


def seed_vehicles() -> list[VehicleRecord]:
    """Return the three demo vehicles in their starting state.

    The states are chosen so the tests have something to work with:
    vehicle 1 is locked (for the unlock tests), vehicle 2 is unlocked (for the
    lock test), and vehicle 3 has several trouble codes.
    """
    return [
        VehicleRecord(
            vin="WBA00000000000001",
            nickname="Daily commuter",
            model="i4 eDrive40",
            battery_percent=78,
            range_km=402,
            odometer_km=18_240,
            locked=True,
            cabin_temp_celsius=19.5,
            climate_target_celsius=None,
            location=Location(lat=48.1374, lon=11.5755),  # Munich
            diagnostics=[],
            trips=[
                _trip("t-1003", "2026-11-24T17:40", "2026-11-24T18:15", 24.1, 3.9),
                _trip("t-1002", "2026-11-24T07:55", "2026-11-24T08:30", 23.8, 4.2),
                _trip("t-1001", "2026-11-23T12:10", "2026-11-23T12:35", 11.4, 1.8),
            ],
        ),
        VehicleRecord(
            vin="WBA00000000000002",
            nickname="Weekend tourer",
            model="iX xDrive50",
            battery_percent=41,
            range_km=245,
            odometer_km=42_917,
            locked=False,
            cabin_temp_celsius=22.0,
            climate_target_celsius=21.0,
            location=Location(lat=52.5200, lon=13.4050),  # Berlin
            diagnostics=[
                DiagnosticCode(
                    code="C0035",
                    description="Left front wheel speed sensor circuit",
                    severity="medium",
                ),
            ],
            trips=[
                _trip("t-2002", "2026-11-22T09:00", "2026-11-22T11:45", 212.6, 46.3),
                _trip("t-2001", "2026-11-20T18:20", "2026-11-20T18:50", 17.2, 3.4),
            ],
        ),
        VehicleRecord(
            vin="WBA00000000000003",
            nickname="Fleet van",
            model="eSprinter",
            battery_percent=12,
            range_km=38,
            odometer_km=96_402,
            locked=True,
            cabin_temp_celsius=8.5,
            climate_target_celsius=None,
            location=Location(lat=50.1109, lon=8.6821),  # Frankfurt
            diagnostics=[
                DiagnosticCode(
                    code="P0420",
                    description="Catalyst system efficiency below threshold (bank 1)",
                    severity="medium",
                ),
                DiagnosticCode(
                    code="P0A80",
                    description="Replace hybrid/EV battery pack",
                    severity="high",
                ),
                DiagnosticCode(
                    code="B1318",
                    description="Battery voltage low",
                    severity="low",
                ),
            ],
            trips=[
                _trip("t-3001", "2026-11-24T06:00", "2026-11-24T14:30", 187.0, 61.5),
            ],
        ),
    ]


class FleetStore:
    """Holds the vehicles and applies changes to them.

    Routes call these methods instead of editing the dict directly, so every
    state change happens in one place.
    """

    def __init__(self, vehicles: list[VehicleRecord] | None = None) -> None:
        # Step 1: Index the vehicles by VIN for O(1) lookups.
        records = vehicles if vehicles is not None else seed_vehicles()
        self._vehicles: dict[str, VehicleRecord] = {v.vin: v for v in records}

    def list(self) -> list[VehicleRecord]:
        return list(self._vehicles.values())

    def get(self, vin: str) -> VehicleRecord:
        # A missing VIN becomes our own exception type, so routes can map it to
        # a 404 without catching unrelated KeyErrors.
        try:
            return self._vehicles[vin]
        except KeyError:
            raise VehicleNotFoundError(vin) from None

    def set_climate(self, vin: str, target_celsius: float) -> VehicleRecord:
        vehicle = self.get(vin)
        vehicle.climate_target_celsius = target_celsius
        return vehicle

    def execute_command(self, vin: str, command: RemoteCommand) -> CommandRecord:
        # Step 1: Look up the vehicle (raises VehicleNotFoundError if unknown).
        vehicle = self.get(vin)

        # Step 2: Apply the command's effect. flash_lights changes no stored
        #         state; on a real car it would just blink the lights.
        if command == "lock":
            vehicle.locked = True
        elif command == "unlock":
            vehicle.locked = False

        # Step 3: Return a receipt so the caller can show what happened and when.
        return CommandRecord(
            command_id=str(uuid.uuid4()),
            vin=vin,
            command=command,
            status="executed",
            executed_at=datetime.now(UTC),
        )
