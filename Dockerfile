# Matches the base image pattern that worked reliably for garmin-auth-bridge
# (mcr.microsoft.com/playwright/python bundles Chromium + all OS deps, avoiding
# the font-package mismatches hit when installing Playwright browsers on a
# plain python:slim image).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Default target -- override per-deployment via Arc Relay's env var field.
ENV TARGET_URL="https://100.125.211.8:3011"
ENV VIEWPORT_WIDTH="1542"
ENV VIEWPORT_HEIGHT="797"

# Critical for a stdio MCP server: Python block-buffers stdout when it's not
# a tty (i.e. always, when spawned by Arc Relay). Without this, the JSON-RPC
# "initialize" response can sit unflushed in the buffer and the client times
# out waiting for it ("context deadline exceeded") even though the process
# is alive and would have answered eventually.
ENV PYTHONUNBUFFERED=1

# Arc Relay spawns this as a stdio MCP server (same shape as the other
# arc-relay-* servers) -- no network port needs publishing. -u is redundant
# with PYTHONUNBUFFERED above but kept as a belt-and-suspenders guard.
CMD ["python", "-u", "server.py"]
