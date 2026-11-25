# One image for both services. The SERVICE environment variable picks which
# one a container runs: SERVICE=backend or SERVICE=gateway (the default).

# Small official Python base image (Debian, without compilers or docs).
FROM python:3.11-slim

# Python/pip behaviour suited to containers: no .pyc files, unbuffered logs
# (so `docker logs` shows lines immediately), and no pip cache in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as an unprivileged user: if the app were ever compromised, the attacker
# would not be root inside the container.
RUN useradd --create-home --uid 1000 app

# Install the dependencies first, from pyproject.toml alone. Docker caches each
# step, so as long as pyproject.toml is unchanged, editing the source code
# reuses this (slow) layer instead of reinstalling everything.
COPY pyproject.toml ./
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; print('\n'.join(deps))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Now copy the source and install the project itself (dependencies are already
# there, hence --no-deps). This also installs the `vehicle-mcp-gateway` command.
COPY README.md ./
COPY src ./src
RUN pip install --no-deps .

USER app

# Defaults; docker-compose / App Runner override them. Both services listen on
# $PORT (compose gives the backend 8001 and the gateway 8000).
ENV SERVICE=gateway \
    PORT=8000
EXPOSE 8000

# Docker marks the container unhealthy if /health stops answering. Python's
# standard library is used because the slim image has no curl.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"

# Start the chosen service. `exec` replaces the shell with the server, so it is
# PID 1 and receives Docker's stop signal directly for a clean shutdown.
# Both bind to 0.0.0.0 so they are reachable from outside the container.
CMD ["sh", "-c", "if [ \"$SERVICE\" = backend ]; then exec uvicorn backend.main:create_app --factory --host 0.0.0.0 --port \"$PORT\"; else exec vehicle-mcp-gateway --transport streamable-http --host 0.0.0.0; fi"]
