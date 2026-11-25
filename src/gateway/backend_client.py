"""HTTP client for the vehicle backend, with error mapping.

This is the arrow between the gateway and the backend in the architecture
diagram. Every tool goes through `BackendClient`, which:

1. adds the `X-Service-Key` header to every request,
2. enforces a timeout, so a slow backend can't hang the AI client, and
3. turns every kind of failure into a short, actionable `BackendError`.

Why the error mapping matters: the reader of a tool error is an AI model. A
raw traceback or an HTML 502 page wastes its context and tells it nothing
useful. "No vehicle with VIN X exists. Call list_vehicles to see valid VINs."
tells it exactly what to do next. Full details still go to the server log.
"""

import logging
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("gateway.backend")


class BackendError(ToolError):
    """A backend failure, already translated into a message for the model.

    It subclasses the SDK's ToolError, so when a tool lets it escape, the SDK
    returns a result with `isError: true` and this message as the content,
    and logs it without a traceback.
    """


class BackendClient:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # One AsyncClient for the whole process: it reuses TCP connections
        # between calls instead of opening a new one per tool call.
        # `transport` is only set by tests, to route requests straight into the
        # backend app in memory (httpx.ASGITransport) with no real network.
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Service-Key": service_key},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- One method per backend route ---------------------------------------------

    async def list_vehicles(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/vehicles")

    async def get_status(self, vin: str) -> dict[str, Any]:
        return await self._request("GET", f"/vehicles/{vin}/status", vin=vin)

    async def get_diagnostics(self, vin: str) -> dict[str, Any]:
        return await self._request("GET", f"/vehicles/{vin}/diagnostics", vin=vin)

    async def get_trips(self, vin: str, limit: int) -> dict[str, Any]:
        return await self._request(
            "GET", f"/vehicles/{vin}/trips", vin=vin, params={"limit": limit}
        )

    async def set_climate(self, vin: str, target_celsius: float) -> dict[str, Any]:
        return await self._request(
            "POST", f"/vehicles/{vin}/climate", vin=vin, json={"target_celsius": target_celsius}
        )

    async def send_command(self, vin: str, command: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/vehicles/{vin}/commands", vin=vin, json={"command": command}
        )

    # --- The shared request + error-mapping logic -----------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        vin: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # Step 1: Send the request. Network-level failures (timeout, refused
        #         connection, DNS) raise here, before any status code exists.
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.TimeoutException:
            logger.warning("backend timeout after %ss: %s %s", self._timeout_s, method, path)
            raise BackendError(
                f"Vehicle backend did not respond within {self._timeout_s:g}s. Try again later."
            ) from None
        except httpx.HTTPError as exc:
            logger.error("backend connection error: %s %s: %r", method, path, exc)
            raise BackendError("Vehicle backend is unavailable.") from None

        # Step 2: Success. Hand the parsed JSON back to the tool.
        if response.is_success:
            return response.json()

        # Step 3: Map each failure status to a message the model can act on.
        #         The full response body goes to the log, never to the model.
        logger.warning(
            "backend returned %s for %s %s: %s",
            response.status_code,
            method,
            path,
            response.text[:500],
        )
        status = response.status_code
        if status == 401:
            # The model can't fix this; a human must fix the gateway's config.
            raise BackendError("Gateway is misconfigured: backend rejected the service key.")
        if status == 404:
            raise BackendError(
                f"No vehicle with VIN {vin} exists. Call list_vehicles to see valid VINs."
            )
        if status == 422:
            raise BackendError(f"Backend rejected the input: {_describe_422(response)}")
        # Anything else (5xx, or an unexpected 4xx) is treated as an outage.
        raise BackendError("Vehicle backend is unavailable.")


def _describe_422(response: httpx.Response) -> str:
    """Turn FastAPI's 422 body into "field: reason; field: reason".

    FastAPI sends {"detail": [{"loc": ["body", "target_celsius"], "msg": "..."}]}.
    We drop the "body"/"query"/"path" prefix because the model only knows the
    field names, not where in the HTTP request they ended up.
    """
    try:
        errors = response.json()["detail"]
        parts = []
        for err in errors:
            field = ".".join(
                str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")
            )
            parts.append(f"{field}: {err.get('msg', 'invalid value')}")
        return "; ".join(parts) or "invalid input"
    except (ValueError, KeyError, TypeError):
        # The body wasn't the shape we expected; stay generic rather than crash.
        return "invalid input"
