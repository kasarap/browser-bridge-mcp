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

# Arc Relay spawns this as a stdio MCP server (same shape as the other
# arc-relay-* servers) -- no network port needs publishing.
CMD ["python", "server.py"]
