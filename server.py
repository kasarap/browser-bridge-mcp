"""
browser-bridge MCP server
--------------------------
Drives the noVNC/KasmVNC web UI of a headless Chromium container running on
Unraid (linuxserver/chromium) via Playwright, and exposes that control as a
small set of MCP tools: screenshot, click, type_text, key, scroll, wait,
reconnect.

Why this shape: the remote desktop is only reachable as a web page (the
KasmVNC canvas), not as a raw CDP endpoint (deliberately -- no unauthenticated
debug port is exposed on the network). Playwright loads that page itself,
with certificate errors ignored at the API level (no interstitial to click
through, unlike a real browser), and then drives the canvas with mouse/
keyboard events -- the same actions a human clicking the noVNC page would
perform.

This process is meant to be launched as a stdio MCP server by Arc Relay, the
same way arc-relay-garmin-connect / arc-relay-monarch-money etc. are run.
Configure the target via the TARGET_URL env var (Arc Relay's "Environment
Variables" field for this server).
"""

import asyncio
import base64
import os

from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import async_playwright, Page, Browser
from playwright.async_api import Playwright as PlaywrightContextManager

TARGET_URL = os.environ.get("TARGET_URL", "https://100.125.211.8:3011")
VIEWPORT_WIDTH = int(os.environ.get("VIEWPORT_WIDTH", "1542"))
VIEWPORT_HEIGHT = int(os.environ.get("VIEWPORT_HEIGHT", "797"))
CONNECT_SETTLE_SECONDS = float(os.environ.get("CONNECT_SETTLE_SECONDS", "2.5"))

mcp = FastMCP("browser-bridge")

_state: dict = {"playwright": None, "browser": None, "page": None}
_lock = asyncio.Lock()


async def _connect() -> Page:
    """Launch (or relaunch) a headless Chromium that loads the noVNC page."""
    pw: PlaywrightContextManager = await async_playwright().start()
    browser: Browser = await pw.chromium.launch(
        headless=True,
        # Without this, Playwright launches "chrome-headless-shell" -- a
        # stripped-down binary built for fast DOM/JS automation, not full
        # rendering. That's exactly what the earlier "Executable doesn't
        # exist at .../chromium_headless_shell-.../" error was about, and
        # is almost certainly why the KasmVNC canvas stream never starts:
        # the shell build lacks the canvas/WebGL/media plumbing the video
        # stream decodes into, even though page navigation works fine.
        # channel="chromium" forces the full Chromium binary (still
        # headless, via Chromium's own --headless=new mode) instead.
        channel="chromium",
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # ignore_https_errors (below) covers page navigation/subresources
            # via CDP, but the KasmVNC video stream rides a separate wss://
            # WebSocket that the self-signed cert can silently block at the
            # browser-process network-stack level -- these flags force the
            # trust bypass process-wide, not just at the CDP/context layer,
            # which is what the page load alone doesn't reach.
            "--ignore-certificate-errors",
            "--ignore-certificate-errors-spki-list",
            "--allow-insecure-localhost",
        ],
    )
    context = await browser.new_context(
        ignore_https_errors=True,  # avoids the native cert-warning interstitial entirely
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
    )
    page = await context.new_page()
    await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
    # Give the KasmVNC websocket a moment to finish connecting and paint the
    # remote desktop before anyone tries to click on it.
    await asyncio.sleep(CONNECT_SETTLE_SECONDS)

    _state["playwright"] = pw
    _state["browser"] = browser
    _state["page"] = page
    return page


async def get_page() -> Page:
    async with _lock:
        page = _state.get("page")
        if page is None or page.is_closed():
            page = await _connect()
        return page


async def _teardown() -> None:
    browser = _state.get("browser")
    pw = _state.get("playwright")
    if browser is not None:
        await browser.close()
    if pw is not None:
        await pw.stop()
    _state["playwright"] = _state["browser"] = _state["page"] = None


@mcp.tool()
async def screenshot() -> Image:
    """Take a screenshot of the remote Unraid desktop (the Chromium browser running in the container)."""
    page = await get_page()
    data = await page.screenshot(type="png")
    return Image(data=data, format="png")


@mcp.tool()
async def click(x: int, y: int, button: str = "left", click_count: int = 1) -> str:
    """Click at pixel coordinates (x, y) on the remote desktop. button: left/right/middle."""
    page = await get_page()
    await page.mouse.click(x, y, button=button, click_count=click_count)
    return f"clicked ({x},{y}) button={button} count={click_count}"

@mcp.tool()
async def move_and_hover(x: int, y: int) -> str:
    """Move the mouse to (x, y) without clicking -- useful to reveal hover states/tooltips."""
    page = await get_page()
    await page.mouse.move(x, y)
    return f"hovered ({x},{y})"


@mcp.tool()
async def type_text(text: str) -> str:
    """Type text at the current focus/cursor position on the remote desktop."""
    page = await get_page()
    await page.keyboard.type(text, delay=20)
    return f"typed {len(text)} characters"


@mcp.tool()
async def key(name: str) -> str:
    """Press a key or key combo using Playwright key syntax, e.g. 'Enter', 'Tab', 'Control+A', 'Backspace'."""
    page = await get_page()
    await page.keyboard.press(name)
    return f"pressed {name}"


@mcp.tool()
async def scroll(x: int, y: int, delta_y: int = 300) -> str:
    """Scroll the remote desktop. Positive delta_y scrolls down, negative scrolls up."""
    page = await get_page()
    await page.mouse.move(x, y)
    await page.mouse.wheel(0, delta_y)
    return f"scrolled delta_y={delta_y} at ({x},{y})"


@mcp.tool()
async def wait(seconds: float) -> str:
    """Wait for the given number of seconds (e.g. while a page loads)."""
    seconds = max(0.0, min(seconds, 30.0))
    await asyncio.sleep(seconds)
    return f"waited {seconds}s"


@mcp.tool()
async def reconnect() -> str:
    """Force a fresh connection to the remote desktop. Use this if the view looks frozen or stale."""
    await _teardown()
    await get_page()
    return "reconnected"


@mcp.tool()
async def status() -> str:
    """Report whether the bridge currently has a live connection to the remote desktop, and the target URL."""
    page = _state.get("page")
    alive = page is not None and not page.is_closed()
    return f"target={TARGET_URL} connected={alive}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
