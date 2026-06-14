import logging
import os
import signal
import time
from contextlib import asynccontextmanager

import html2text
import httpx
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.utilities.types import Image
from playwright.async_api import async_playwright
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import db

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lens")


@asynccontextmanager
async def lifespan(server: "FastMCP"):
    await db.start()
    try:
        yield {}
    finally:
        await db.stop()


class RequestLoggingMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = context.message.name
        arguments = context.message.arguments
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            db.log_request(tool, arguments, "error", error=repr(exc), duration_ms=duration)
            raise
        duration = (time.perf_counter() - started) * 1000
        db.log_request(
            tool,
            arguments,
            "ok",
            response=str(getattr(result, "content", result)),
            duration_ms=duration,
        )
        return result


mcp = FastMCP("Lens", lifespan=lifespan)
mcp.add_middleware(RequestLoggingMiddleware())


@mcp.custom_route("/get", methods=["GET"])
async def get_redirect(request: Request) -> Response:
    target = os.environ.get("LENS_REDIRECT_URL")
    if not target:
        return Response("Not Found", status_code=404)
    return RedirectResponse(target)


def _image_format_from_content_type(content_type: str) -> str:
    subtype = content_type.split(";", 1)[0].strip().lower()
    fmt = subtype.split("/", 1)[1] if "/" in subtype else subtype
    if fmt in ("jpg", "jpeg"):
        return "jpeg"
    if fmt == "svg+xml":
        return "svg+xml"
    return fmt or "png"


@mcp.tool
async def fetch_page(
    url: str,
    wait_for_selector: str | None = None,
    timeout_ms: int = 15000,
) -> str:
    """Fetch a web page with a headless browser and return its content as markdown."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=DEFAULT_USER_AGENT)
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if wait_for_selector:
                await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)

            html = await page.content()
            title = await page.title()
        finally:
            await browser.close()

    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    markdown = converter.handle(html)

    return f"# {title}\n\nSource: {url}\n\n{markdown}"


@mcp.tool
async def screenshot_page(
    url: str,
    full_page: bool = False,
    timeout_ms: int = 15000,
) -> Image:
    """Capture a PNG screenshot of a web page in a 1280x800 viewport."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            data = await page.screenshot(full_page=full_page, type="png")
        finally:
            await browser.close()

    return Image(data=data, format="png")


@mcp.tool
async def fetch_image(image_url: str, referer: str | None = None) -> Image:
    """Download an image over HTTP with a desktop browser User-Agent and optional Referer."""
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(image_url, headers=headers, timeout=15.0)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "image/png")
    fmt = _image_format_from_content_type(content_type)

    return Image(data=response.content, format=fmt)


def _install_signal_handlers() -> None:
    def _handle(signum, _frame):
        logger.info("Received %s — shutting down gracefully.", signal.Signals(signum).name)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _install_signal_handlers()
    port = int(os.environ.get("LENS_PORT", "8788"))
    logger.info("Starting Lens MCP server on 0.0.0.0:%d", port)
    try:
        mcp.run(transport="http", host="0.0.0.0", port=port)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Lens MCP server stopped.")
