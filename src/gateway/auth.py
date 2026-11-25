"""Bearer-token check for AI clients connecting over HTTP.

This guards the top arrow in the architecture diagram (AI client -> gateway).
Every HTTP request must carry `Authorization: Bearer <MCP_CLIENT_TOKEN>`.
Anything else is answered with 401 before the request reaches any MCP code.

Why a small middleware instead of the SDK's built-in auth? The SDK's auth
support is built for OAuth 2.1: it requires the URL of an OAuth authorization
server and publishes OAuth discovery metadata pointing at it. This project
uses one static token and has no authorization server, so advertising one
would be misleading and would make clients attempt an OAuth login that cannot
work. A plain ASGI middleware does exactly the one check we need, and is easy
to replace with real OAuth later (see "Possible next steps" in the README).

ASGI in one paragraph: an ASGI app is an async function
`app(scope, receive, send)`. `scope` describes the request (type, path,
headers). A middleware is an ASGI app that wraps another one and decides
whether to pass the request on.
"""

import json
import logging
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("gateway.auth")

# Paths that skip the token check. Health checks from Docker / App Runner
# cannot send secrets, and /health reveals nothing about the fleet.
PUBLIC_PATHS = frozenset({"/health"})


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            # An empty token would make every "Bearer " header valid.
            raise ValueError("BearerTokenMiddleware needs a non-empty token")
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Step 1: Only HTTP requests carry an Authorization header. "lifespan"
        #         events (server startup/shutdown) pass straight through, so the
        #         MCP session manager inside can still start and stop.
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self._app(scope, receive, send)
            return

        # Step 2: Find the Authorization header. ASGI gives headers as a list of
        #         (name, value) byte pairs with lower-case names.
        auth_header = b""
        for name, value in scope["headers"]:
            if name == b"authorization":
                auth_header = value
                break

        # Step 3: Expect exactly "Bearer <token>" (scheme is case-insensitive per
        #         RFC 6750), then compare in constant time. compare_digest takes
        #         the same time however many characters match, so an attacker
        #         can't guess the token one character at a time by timing us.
        scheme, _, presented = auth_header.partition(b" ")
        if scheme.lower() == b"bearer" and secrets.compare_digest(presented, self._token):
            await self._app(scope, receive, send)
            return

        # Step 4: Reject. Never log the presented token itself.
        logger.warning("rejected request to %s: missing or invalid bearer token", scope["path"])
        await _send_401(send)


async def _send_401(send: Send) -> None:
    body = json.dumps({"error": "unauthorized", "detail": "missing or invalid bearer token"})
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                # Tells the client which auth scheme is expected (RFC 6750).
                (b"www-authenticate", b'Bearer realm="vehicle-mcp-gateway"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body.encode()})
