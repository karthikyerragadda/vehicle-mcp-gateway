# vehicle-mcp-gateway

An MCP server that lets an AI agent (Claude Desktop, Claude Code, or any MCP client) safely read from and send commands to a connected-vehicle backend through 6 typed tools.

[![CI](https://github.com/karthikyerragadda/vehicle-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/karthikyerragadda/vehicle-mcp-gateway/actions/workflows/ci.yml)

## Why this exists

Companies already have backends for their products: REST services with their own auth, validation and business rules. To let an AI agent use one, you shouldn't have to rewrite it or teach it about AI. Nor should you trust the model to "be careful" with commands that affect the physical world.

This project shows the alternative. A small, reusable **gateway** speaks the [Model Context Protocol](https://modelcontextprotocol.io) to AI clients and plain REST to an existing backend. The gateway is where the safety lives:

- typed tool schemas,
- input validation in code,
- short, actionable error messages, and
- a confirmation step for sensitive actions that the **human** answers and the model cannot skip.

The backend here is a simulated fleet of three cars, but it is an ordinary FastAPI service with no AI-specific code.

## Architecture

```
 AI client (Claude etc.)
        │  MCP over streamable HTTP, Authorization: Bearer <MCP_CLIENT_TOKEN>
        ▼
 ┌──────────────────────────┐
 │  MCP gateway (MCPServer) │  6 tools, Pydantic schemas, error mapping,
 │  src/gateway/            │  confirmation step for sensitive actions
 └──────────┬───────────────┘
            │  REST/JSON over httpx, X-Service-Key: <BACKEND_SERVICE_KEY>
            ▼
 ┌──────────────────────────┐
 │ Vehicle backend (FastAPI)│  simulated fleet of 3 vehicles, in-memory
 │  src/backend/            │
 └──────────────────────────┘
```

**How one tool call flows**, for example `get_vehicle_status`:

1. The AI client sends a `tools/call` request to `/mcp` with the bearer token. `auth.py` checks the token before any MCP code runs. A bad token gets 401.
2. The MCP SDK validates the arguments against the tool's schema. An invalid VIN is rejected here, and the tool body never runs.
3. The tool calls `BackendClient`, which sends `GET /vehicles/{vin}/status` with the `X-Service-Key` header and a 5 s deadline.
4. The backend checks the service key and returns JSON, or a 401/404/422 status.
5. The gateway either validates the JSON into the `VehicleStatus` output model or maps the failure to a short `ToolError`.
6. The SDK sends the result back as `structuredContent`, or as `isError: true` with the message.

## The 6 tools

| # | Tool | What it does | Annotations |
|---|---|---|---|
| 1 | `list_vehicles` | Fleet overview: VIN, nickname, model, battery, lock state | `readOnlyHint` |
| 2 | `get_vehicle_status` | Battery, range, odometer, locks, cabin temperature, climate target, GPS | `readOnlyHint` |
| 3 | `get_diagnostic_codes` | Active OBD-II trouble codes (e.g. `P0420`) with severity | `readOnlyHint` |
| 4 | `get_trip_history` | Most recent trips; `limit` 1–20, checked by the schema | `readOnlyHint` |
| 5 | `set_climate` | Set the cabin temperature target; 16–28 °C, checked by the schema | not read-only, not destructive, `idempotentHint` |
| 6 | `send_remote_command` | `lock`, `unlock` or `flash_lights`; **`unlock` asks the human to confirm** | `destructiveHint` |

All six also set `openWorldHint: false`, because they only touch this backend. VINs are checked against `^[A-HJ-NPR-Z0-9]{17}$` (17 characters, never I, O or Q). Every tool publishes an output schema and returns structured content.

## Quick start

Requires Python 3.11+.

### Local (virtual environment)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then replace the two example secrets

# Terminal 1: the backend on :8001
uvicorn backend.main:create_app --factory --port 8001

# Terminal 2: the gateway on :8000 (streamable HTTP at /mcp)
vehicle-mcp-gateway
```

Both services read `.env` from the current directory.

### Docker Compose

```bash
cp .env.example .env          # then replace the two example secrets
docker compose up --build
```

The gateway is then at `http://localhost:8000/mcp`. The backend's port is deliberately not published to the host: only the gateway can reach it.

### Connect Claude Code (HTTP)

With the gateway running locally or in Docker:

```bash
claude mcp add --transport http vehicles http://localhost:8000/mcp \
  --header "Authorization: Bearer <your MCP_CLIENT_TOKEN>"
```

### Connect Claude Desktop (stdio)

Claude Desktop starts the gateway itself and talks to it over stdin/stdout. The backend must already be running (see the local quick start above). Add this to `claude_desktop_config.json` (on macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`), using absolute paths:

```json
{
  "mcpServers": {
    "vehicles": {
      "command": "/absolute/path/to/vehicle-mcp-gateway/.venv/bin/vehicle-mcp-gateway",
      "args": ["--transport", "stdio"],
      "env": {
        "BACKEND_URL": "http://localhost:8001",
        "BACKEND_SERVICE_KEY": "<same value as the backend's>"
      }
    }
  }
}
```

stdio has no network layer, so there is no bearer token: whoever can start the process is trusted. That is the normal model for a local MCP server, but it is why the HTTP transport is the one to expose to a network.

Try asking: *"Which of my cars has trouble codes?"* or *"Unlock the daily commuter."* The second one makes your MCP client ask **you** to confirm, if the client supports elicitation. If it doesn't, the gateway refuses the unlock.

## Configuration

All settings are environment variables (or `.env`). See [`.env.example`](.env.example).

| Variable | Used by | Default | Description |
|---|---|---|---|
| `BACKEND_SERVICE_KEY` | both | **required** | Shared secret the gateway sends as `X-Service-Key` |
| `MCP_CLIENT_TOKEN` | gateway | required for HTTP | Bearer token AI clients must send. Not used with stdio |
| `BACKEND_URL` | gateway | `http://localhost:8001` | Where the backend lives |
| `BACKEND_TIMEOUT_S` | gateway | `5` | Total time allowed for one backend request |
| `PORT` | gateway, Docker | `8000` | Port to listen on (the backend uses `--port`, or `$PORT` in Docker) |
| `LOG_LEVEL` | both | `INFO` | Python logging level |
| `SIMULATED_LATENCY_MS` | backend | `0` | Artificial delay per request, used by the timeout test |

Gateway command-line options: `--transport streamable-http|stdio` (default `streamable-http`) and `--host` (default `127.0.0.1`; Docker passes `0.0.0.0`).

Neither service starts without its required secrets: the gateway exits with a one-line message naming the missing variable, and the backend stops with a Pydantic validation error for `backend_service_key`.

## Running tests

```bash
ruff check .
pytest -q          # 17 passed
```

The tests need no network, no Docker and no running servers. The backend is called through FastAPI's `TestClient`. The gateway is driven by a **real MCP client session** (`mcp.Client`) connected in-process, with the gateway's HTTP calls routed straight into the backend app through `httpx.ASGITransport`.

| File | Tests | What they prove |
|---|---|---|
| `test_backend.py` | 1–7 | `/health` needs no key; a missing or wrong key gets 401; the right key lists 3 vehicles; an unknown VIN gets 404; 35 °C gets 422; `lock` changes the status |
| `test_tools.py` | 8–14 | Exactly 6 tools with schemas, and no confirmation field the model could fill; correct annotations; structured output; an unknown VIN gives an error with a "Call list_vehicles" hint; `limit=50` never reaches the backend; unlock **accepted** by the user unlocks the car; unlock **declined** leaves it locked and sends nothing |
| `test_auth.py` | 15–17 | `/mcp` without a token or with a wrong one gets 401; a backend slower than the timeout gives a clean timeout message, not a traceback |

CI (GitHub Actions) runs lint and tests on Python 3.11 and 3.12. It then builds the Docker image and starts both containers to check the health endpoint and the bearer-token behaviour.

## Design decisions

### Elicitation, not a `confirmed` flag

The obvious way to guard `unlock` is a tool argument like `confirmed: bool`, with an instruction in the description saying "only set this after asking the user". That protects nothing. The model fills in the arguments, so a confused model, or one manipulated by text injected into its context (a prompt injection), can simply send `confirmed=true`.

This gateway uses **MCP elicitation** instead. `send_remote_command` has a `confirmation` parameter filled by a server-side resolver (`Resolve(confirm_unlock_with_user)`), not by the model:

1. Resolver-filled parameters are **not part of the tool's input schema**, so the model has no way to supply them. Test 8 checks that the schema contains only `vin` and `command`.
2. For `unlock`, the resolver returns an `Elicit(...)` request. The SDK sends it to the **MCP client application**, which shows a form to the human. The model is not part of this exchange.
3. The tool body only calls the backend if the human **accepted and ticked** the box. Declined, cancelled, unticked, or a client that doesn't support elicitation all result in "Unlock was not confirmed by the user; no command was sent."

The SDK carries this over both protocol styles. In the 2026-07-28 MCP revision, the question goes back in an `InputRequiredResult` and the client retries the call with the answer. The SDK encrypts the state the client carries between rounds, so a client cannot forge an "accept". In older revisions, the server sends a separate `elicitation/create` request in the middle of the call. Both styles were tested end to end over HTTP.

`lock` and `flash_lights` don't ask, because they can't let anyone into the car.

### Two separate secrets

AI clients authenticate to the gateway with `MCP_CLIENT_TOKEN`. The gateway authenticates to the backend with `BACKEND_SERVICE_KEY`. Leaking the client token gives access only to the six guarded tools: it can't call the backend directly, and it can't skip the unlock confirmation. In Docker Compose the backend isn't even reachable from the host.

### Validation in schemas, not prompts

Every limit is enforced by code: the VIN pattern, `limit` 1–20, 16–28 °C, and the allowed commands. Each tool parameter uses a shared `Annotated[type, Field(...)]` type from `gateway/schemas.py`. The SDK builds a Pydantic model per tool from these types, publishes it as JSON Schema, and rejects bad arguments before the tool body runs (test 12 proves the backend is never called). The backend validates again with its own models, so it would stay safe even if called by a different client.

Tool parameters are flat (`{"vin": ..., "limit": ...}`) rather than a single Pydantic model argument. With a model argument, every call would be wrapped as `{"params": {...}}`, which is clumsier for a model to fill in correctly. Validation happens in code either way.

### Error mapping

The reader of a tool error is a language model. A traceback wastes its context, and an HTML 502 page tells it nothing. `BackendClient` maps every failure to a short message that says what to do next:

| Backend result | Tool error |
|---|---|
| 401 | Gateway is misconfigured: backend rejected the service key. |
| 404 | No vehicle with VIN … exists. Call list_vehicles to see valid VINs. |
| 422 | Backend rejected the input: `<field>`: `<reason>` |
| timeout | Vehicle backend did not respond within 5s. Try again later. |
| connection error / 5xx | Vehicle backend is unavailable. |

These are raised as `ToolError`, so the client receives `isError: true`. Full details go to the server log only.

The timeout is enforced twice, by httpx and by an overall `anyio.fail_after` deadline. httpx's timeout applies to each step separately (connecting, each read), so a backend that sends bytes slowly could otherwise take far longer than 5 s in total.

### The backend knows nothing about AI

The backend is a plain REST service with its own auth and validation. That is the realistic case: the backend already exists, and other teams and apps use it. All the MCP-specific work (tool descriptions written for the model, annotations, elicitation, error wording) lives in the gateway. The same backend could get a different adapter tomorrow without changing.

### Smaller decisions

- **SDK version.** The project uses `mcp==2.3.0`, pinned exactly. In SDK 2.x, `FastMCP` was renamed `MCPServer`. The SDK's `Resolve`/`Elicit` resolvers are used instead of calling `ctx.elicit()` directly, because they work across both protocol styles.
- **Client auth as a small ASGI middleware** (`gateway/auth.py`), not the SDK's built-in auth. The SDK's auth support is built around OAuth 2.1: it requires an authorization server URL and publishes OAuth discovery metadata pointing at it. This project uses one static token and has no authorization server, so advertising one would be misleading, and clients would attempt an OAuth login that cannot work. The middleware compares tokens with `secrets.compare_digest` and exempts only `/health`.
- **Google Cloud Run instead of AWS App Runner.** AWS's documentation states that App Runner is closed to new customers. Cloud Run runs the same Docker image with nearly the same model (container, port, health checks, secrets from a secret manager).

## Deployment

[`deploy/`](deploy/) contains Cloud Run service definitions for the backend and the gateway, with the full `gcloud` steps in each file's header. Secrets come from Google Secret Manager and are never written in the files.

**This is a deployment configuration, not a live deployment.** The project is not running anywhere unless you deploy it to your own Google Cloud project. The Cloud Run YAML has been checked for internal consistency (ports, health checks, secret names) but has not been deployed.

## Known limitations

- **Simulated, in-memory data.** Three seeded cars and no persistence: every restart resets the fleet.
- **Single instance only.** The fleet, the MCP sessions and the key that protects the unlock confirmation's round trip all live in process memory. The Cloud Run configs therefore cap each service at one instance.
- **Static bearer token instead of OAuth.** One shared token, no per-user identity, no expiry, no revocation other than changing it.
- **Single tenant.** Every client with the token sees and controls every vehicle.
- **No rate limiting.**
- **Elicitation needs a client that supports it.** With a client that doesn't, `unlock` is refused (the safe default), so that client can never unlock a car.
- **Host-header (DNS-rebinding) protection** is enabled automatically when the gateway listens on localhost, but not when it binds `0.0.0.0` (Docker, Cloud Run). There, every request still needs the bearer token, which a malicious web page cannot supply.
- **On Cloud Run, the backend has a public HTTPS URL.** It is protected by the service key, but not hidden at the network level.
- **The deployment config is not deployed** (see above).

## Possible next steps

- OAuth 2.1 for AI clients, following the MCP authorization part of the [MCP specification](https://modelcontextprotocol.io/specification), replacing the static token.
- Per-vehicle authorization: each user may only see and control their own cars.
- An audit log of every command, including who confirmed each unlock.
- A real telematics adapter behind the same REST interface.
- An internal-only backend (private networking, or Google identity tokens between the services).
- Shared session storage, so the gateway can run more than one instance.

## License

[MIT](LICENSE)
