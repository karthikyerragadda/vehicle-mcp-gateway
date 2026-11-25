"""Gateway settings, read from environment variables or a `.env` file.

pydantic-settings maps each field to an environment variable of the same name
in upper case (`backend_url` <- `BACKEND_URL`) and converts the type for us
(the string "5" becomes the float 5.0, for example).
"""

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    # extra="ignore": the shared .env also holds backend-only settings; skip them.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where the vehicle backend lives.
    backend_url: str = "http://localhost:8001"

    # Secret sent to the backend as X-Service-Key. Required: without it every
    # backend call would fail with 401, so we refuse to start instead.
    backend_service_key: str = Field(min_length=1)

    # Secret AI clients must present as "Authorization: Bearer <token>".
    # Optional here because the stdio transport (local Claude Desktop) has no
    # HTTP layer to put it in. The HTTP transport refuses to start without it.
    mcp_client_token: str | None = None

    # How long to wait for the backend before giving up, in seconds.
    backend_timeout_s: float = Field(default=5.0, gt=0)

    log_level: str = "INFO"
    port: int = 8000


def load_settings() -> GatewaySettings:
    """Load settings, turning a validation error into a short, readable exit.

    Pydantic's raw error is accurate but noisy. On startup, a person wants to
    read "BACKEND_SERVICE_KEY is missing", so we fail fast with exactly that.
    """
    try:
        return GatewaySettings()
    except ValidationError as exc:
        names = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        raise SystemExit(
            f"Gateway configuration error: missing or invalid setting(s): {names}. "
            "Set them in the environment or in .env (see .env.example)."
        ) from None
