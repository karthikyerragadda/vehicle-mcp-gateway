"""Tests for the vehicle backend REST API (tests 1-7 of the spec).

These call the FastAPI app directly through TestClient. No gateway or MCP is
involved, which proves the backend works as an ordinary REST service.
"""

from tests.conftest import AUTH, UNKNOWN_VIN, UNLOCKED_VIN


def test_health_needs_no_key(backend_client):
    """/health answers 200 without any service key, so health checks can probe it."""
    response = backend_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_vehicles_without_key_is_401(backend_client):
    """A request with no X-Service-Key header is rejected with 401 and a JSON body."""
    response = backend_client.get("/vehicles")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_vehicles_with_wrong_key_is_401(backend_client):
    """A request with the wrong service key is rejected with 401."""
    response = backend_client.get("/vehicles", headers={"X-Service-Key": "wrong-key"})
    assert response.status_code == 401


def test_vehicles_with_right_key_lists_three(backend_client):
    """With the correct key, /vehicles returns the 3 seeded vehicles as summaries."""
    response = backend_client.get("/vehicles", headers=AUTH)
    assert response.status_code == 200
    vehicles = response.json()
    assert len(vehicles) == 3
    # Summaries only: detailed fields like trips must not appear in the list.
    assert "trips" not in vehicles[0]


def test_unknown_vin_status_is_404(backend_client):
    """Asking for the status of a VIN that isn't in the fleet returns 404."""
    response = backend_client.get(f"/vehicles/{UNKNOWN_VIN}/status", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {"detail": "vehicle not found"}


def test_climate_out_of_range_is_422(backend_client):
    """A climate target of 35 °C is outside 16-28 °C and is rejected with 422."""
    response = backend_client.post(
        f"/vehicles/{UNLOCKED_VIN}/climate", json={"target_celsius": 35}, headers=AUTH
    )
    assert response.status_code == 422


def test_lock_command_changes_status(backend_client):
    """Sending `lock` to an unlocked car makes the next status call report locked=true."""
    before = backend_client.get(f"/vehicles/{UNLOCKED_VIN}/status", headers=AUTH).json()
    assert before["locked"] is False

    response = backend_client.post(
        f"/vehicles/{UNLOCKED_VIN}/commands", json={"command": "lock"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["status"] == "executed"

    after = backend_client.get(f"/vehicles/{UNLOCKED_VIN}/status", headers=AUTH).json()
    assert after["locked"] is True
