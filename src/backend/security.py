"""Backend settings and the X-Service-Key check.

Only one caller is meant to talk to the backend: the MCP gateway. It proves
who it is by sending a shared secret in the `X-Service-Key` header. This
module holds that secret (read from the environment) and the FastAPI
dependency that checks it on every protected route.

The secret is deliberately *different* from the token AI clients use for the
gateway (`MCP_CLIENT_TOKEN`). Someone who steals the client token can call the
gateway's six safe tools, but cannot talk to this backend directly.
"""

import secrets

from fastapi import Header, HTTPException, Request, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendSettings(BaseSettings):
    """Backend configuration, read from environment variables or `.env`."""

    # extra="ignore": the shared .env also holds gateway settings; skip them.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # No default, so the backend refuses to start without a key. Starting
    # with an empty key would make the auth check meaningless.
    backend_service_key: str = Field(min_length=1)

    # Artificial delay before each protected response. Used by a test to make
    # the gateway time out on purpose.
    simulated_latency_ms: int = Field(default=0, ge=0)

    log_level: str = "INFO"


async def require_service_key(
    request: Request,
    x_service_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: reject the request with 401 unless the key matches.

    FastAPI reads the `X-Service-Key` header into `x_service_key` (it maps
    underscores to dashes for us). A missing header arrives as None.
    """
    expected: str = request.app.state.settings.backend_service_key

    # Step 1: Missing header means 401, and we skip the comparison.
    # Step 2: Compare with secrets.compare_digest instead of `==`. `==` stops
    #         at the first wrong character, so response timing could leak how
    #         much of a guess was right. compare_digest takes the same time
    #         whatever the input.
    if x_service_key is None or not secrets.compare_digest(
        x_service_key.encode(), expected.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid service key",
        )
