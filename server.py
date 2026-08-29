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
import json
import os
import socket
import sys
from urllib.parse import urlparse

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
_events: list = []  # rolling diagnostic log: console messages, page errors, failed requests, websocket lifecycle
_EVENTS_MAX = 200


def _log_event(line: str) -> None:
    # Also mirror to stderr (never stdout -- that would corrupt the MCP
    # JSON-RPC framing) so these are visible via plain `docker logs`,
    # independent of whether a client's tool cache has picked up the
    # diagnostics() tool yet.
    print(f"[diag] {line}", file=sys.stderr, flush=True)
    _events.append(line)
    del _events[:-_EVENTS_MAX]


def _wire_diagnostics(page: Page) -> None:
    """Attach listeners so `diagnostics()` can show what actually happened,
    instead of us guessing at why the stream never starts."""
    page.on("console", lambda msg: _log_event(f"[console:{msg.type}] {msg.text}"))
    page.on("pageerror", lambda exc: _log_event(f"[pageerror] {exc}"))
    page.on("requestfailed", lambda req: _log_event(
        f"[requestfailed] {req.method} {req.url} -> {req.failure}"
    ))

    def _on_websocket(ws):
        _log_event(f"[websocket:open] {ws.url}")
        ws.on("close", lambda: _log_event(f"[websocket:close] {ws.url}"))
        ws.on("socketerror", lambda err: _log_event(f"[websocket:error] {ws.url} -> {err}"))

    page.on("websocket", _on_websocket)


async def _connect() -> Page:
    """Launch (or relaunch) a headless Chromium that loads the noVNC page."""
    _events.clear()
    pw: PlaywrightContextManager = await async_playwright().start()
    browser: Browser = await pw.chromium.launch(
        headless=True,
        # "chromium" (open-source Chromium, no headless-shell) fixed page
        # rendering but NOT the video stream: diagnostics showed KasmVNC's
        # WebCodecs H.264 decoder ("avc1...") failing to configure no matter
        # what GPU/software-rendering flags were added, which turned out to
        # be a codec-licensing issue, not a hardware one -- open-source
        # Chromium builds typically don't compile in proprietary H.264 at
        # all. channel="chrome" uses real Google Chrome (installed via
        # `playwright install chrome` in the Dockerfile), which does.
        channel="chrome",
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
            # Root cause found via diagnostics(): KasmVNC's client decodes its
            # video stream through the WebCodecs VideoDecoder API (H.264 /
            # avc1...), and that was failing with "Config not supported" --
            # not a cert or rendering-engine issue, a video-decode-pipeline
            # one. There's no GPU in this container, and even *software*
            # WebCodecs decode in Chromium is routed through the GPU process,
            # which doesn't come up usable by default in headless+Docker.
            # Forcing Chromium onto SwiftShader (software GL/Vulkan) gets a
            # working GPU process up without real hardware, which is what
            # the decoder needs to configure successfully.
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            "--disable-gpu-sandbox",
            "--in-process-gpu",
        ],
    )
    context = await browser.new_context(
        ignore_https_errors=True,  # avoids the native cert-warning interstitial entirely
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
    )
    page = await context.new_page()
    _wire_diagnostics(page)
    _log_event(f"navigating to {TARGET_URL}")
    await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
    _log_event("page.goto returned (networkidle) -- settling before first use")
    # Give the KasmVNC websocket a moment to finish connecting and paint the
    # remote desktop before anyone tries to click on it.
    await asyncio.sleep(CONNECT_SETTLE_SECONDS)
    _log_event("settle wait complete")

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
    """Force a fresh connection to the remote desktop. Use this if the view looks frozen or stale.
    Returns the connection events captured during this attempt (console/network/websocket
    activity) inline, since a separate diagnostics tool has proven unreliable to reach from
    some clients -- this way the info always comes back on a tool we know works."""
    await _teardown()
    await get_page()
    body = "\n".join(_events) if _events else "(no events captured)"
    return f"reconnected\n\n--- events ({len(_events)}) ---\n{body}"


@mcp.tool()
async def evaluate(js: str) -> str:
    """Evaluate a JavaScript expression on the remote page and return its result as text.
    For diagnosing things the page's own console logging doesn't surface, e.g. calling
    VideoDecoder.isConfigSupported(...) directly to see the real rejection reason."""
    page = await get_page()
    try:
        result = await page.evaluate(js)
        return repr(result)
    except Exception as exc:
        return f"<error: {exc}>"


@mcp.tool()
async def status() -> str:
    """Report whether the bridge currently has a live connection to the remote desktop, the
    target URL, and the events captured since the last connect (console/network/websocket
    activity) -- inlined here for the same reason as in reconnect()."""
    page = _state.get("page")
    alive = page is not None and not page.is_closed()
    body = "\n".join(_events) if _events else "(no events captured)"
    return f"target={TARGET_URL} connected={alive}\n\n--- events ({len(_events)}) ---\n{body}"


_cdp_state: dict = {"playwright": None, "browser": None}
_cdp_lock = asyncio.Lock()

# CHROME_DEBUG_HOST/PORT: the *inner* remote-desktop Chromium's own DevTools
# Protocol endpoint, reached over a dedicated `monarch-cdp` docker network
# (created --internal, so it has no route out of the host at all) that only
# the chromium container and this one are attached to -- never the public/
# Tailscale-exposed KasmVNC path. This gives direct DOM/localStorage access
# to whatever's already logged in there (e.g. Monarch Money via the user's
# own SSO), without ever handling a password: the human logs in once on the
# visible remote desktop the normal way, and this just reads the resulting
# session out of that already-authenticated tab. Chromium (M113+) hardcodes
# its debug port to 127.0.0.1 regardless of --remote-debugging-address, so
# the chromium container runs a small socat relay (added via linuxserver's
# custom-cont-init.d convention, not baked into the image) forwarding a
# second port onto this network -- hence CHROME_DEBUG_PORT defaults to that
# relay port (9223), not Chromium's own 9222.
CHROME_DEBUG_HOST = os.environ.get("CHROME_DEBUG_HOST", "chromium")
CHROME_DEBUG_PORT = os.environ.get("CHROME_DEBUG_PORT", "9223")


async def _get_cdp_browser() -> Browser:
    """Connect directly to the remote desktop's own Chromium via CDP (bypassing
    the video/synthetic-input layer entirely), reusing one connection across
    calls. Chromium's DevTools HTTP server does DNS-rebinding protection on the
    Host header -- it only trusts "localhost" or a bare IP, not a hostname --
    so we resolve CHROME_DEBUG_HOST ourselves and connect via IP, which makes
    Chromium report back a webSocketDebuggerUrl using that same IP (usable
    from this container) instead of "localhost" (which would point back at
    this container's own loopback, not the remote Chromium's)."""
    async with _cdp_lock:
        browser = _cdp_state.get("browser")
        if browser is not None and browser.is_connected():
            return browser
        pw = _cdp_state.get("playwright")
        if pw is None:
            pw = await async_playwright().start()
            _cdp_state["playwright"] = pw
        ip = socket.gethostbyname(CHROME_DEBUG_HOST)
        browser = await pw.chromium.connect_over_cdp(f"http://{ip}:{CHROME_DEBUG_PORT}")
        _cdp_state["browser"] = browser
        return browser


@mcp.tool()
async def read_site_storage(url_substring: str) -> str:
    """Read localStorage (and cookies) directly from a tab already open in the
    remote desktop's browser, matched by a substring of its URL (e.g. "monarch"
    or "icloud"), via a direct CDP connection on the internal monarch-cdp
    network -- NOT via the video-streamed KasmVNC path, so this doesn't touch
    whatever a human might currently be doing on the visible desktop.

    Use this to pull a session token/cookie out of a site the user already
    logged into there by hand, instead of ever entering credentials yourself.
    Returns JSON: {"url": ..., "localStorage": {...}, "cookies": {...}}.
    Returns an error string (not JSON) if no matching tab is open -- ask the
    user to open/log into that site on the remote desktop first."""
    browser = await _get_cdp_browser()
    target = None
    for context in browser.contexts:
        for page in context.pages:
            if url_substring.lower() in page.url.lower():
                target = page
                break
        if target:
            break
    if target is None:
        return (
            f"No open tab matching '{url_substring}' found in the remote desktop's "
            "browser. Ask the user to open/log into that site there first."
        )
    local_storage = await target.evaluate(
        "() => Object.fromEntries(Object.entries(localStorage))"
    )
    all_cookies = await target.context.cookies()
    _host = urlparse(target.url).hostname or ""
    _parts = _host.split(".")
    _root = ".".join(_parts[-2:]) if len(_parts) >= 2 else _host
    cookies = [c for c in all_cookies if c["domain"].lstrip(".").endswith(_root)]  # scope to the tab domain only, not the whole browser profile
    cookie_map = {c["name"]: c["value"] for c in cookies}
    return json.dumps(
        {"url": target.url, "localStorage": local_storage, "cookies": cookie_map},
        indent=2,
    )


@mcp.tool()
async def diagnostics() -> str:
    """Show what actually happened on the remote page since the last connect: console messages,
    JS errors, failed network requests, and websocket open/close/error events (KasmVNC's video
    stream rides a websocket, so this is the place to look when the stream never starts)."""
    page = _state.get("page")
    if page is None:
        return "no active page -- call screenshot() or reconnect() first to establish a connection"
    try:
        title = await page.title()
        url = page.url
    except Exception as exc:  # page might be closed/crashed between calls
        title, url = f"<error reading page: {exc}>", "<unknown>"
    body = "\n".join(_events) if _events else "(no console/network/websocket events captured)"
    return f"page url={url}\npage title={title!r}\n\n--- events ({len(_events)}) ---\n{body}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
