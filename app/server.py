import asyncio
import logging
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path

import html2text
import httpx
import trafilatura
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.utilities.types import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import browser_pool
import db
import memory

ICON_PATH = Path(__file__).parent / "icons" / "logo.svg"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

HTTP2_ERROR_MARKERS = ("ERR_HTTP2_PROTOCOL_ERROR", "ERR_HTTP2", "ERR_CONNECTION_RESET")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lens")


@asynccontextmanager
async def lifespan(server: "FastMCP"):
    await browser_pool.start()
    await db.start()
    await memory.start()
    try:
        yield {}
    finally:
        await memory.stop()
        await db.stop()
        await browser_pool.stop()


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


mcp = FastMCP(
    "Lens",
    lifespan=lifespan,
    icons=[Icon(src="https://lens.sshogunn.org/icon.svg", mimeType="image/svg+xml")],
)
mcp.add_middleware(RequestLoggingMiddleware())


@mcp.custom_route("/icon.svg", methods=["GET"])
async def serve_app_icon(request: Request) -> Response:
    return Response(ICON_PATH.read_bytes(), media_type="image/svg+xml")



async def _redirect(request: Request) -> Response:
    target = os.environ.get("LENS_REDIRECT_URL")
    if not target:
        return Response("Not Found", status_code=404)
    return RedirectResponse(target)


mcp.custom_route("/", methods=["GET"])(_redirect)


def _image_format_from_content_type(content_type: str) -> str:
    subtype = content_type.split(";", 1)[0].strip().lower()
    fmt = subtype.split("/", 1)[1] if "/" in subtype else subtype
    if fmt in ("jpg", "jpeg"):
        return "jpeg"
    if fmt == "svg+xml":
        return "svg+xml"
    return fmt or "png"


def _is_http2_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker in message for marker in HTTP2_ERROR_MARKERS)


def _concise_error(exc: Exception) -> str:
    text = str(exc)
    match = re.search(r"net::ERR_[A-Z0-9_]+", text)
    if match:
        return match.group(0)
    if isinstance(exc, PlaywrightTimeoutError):
        return "navigation timed out"
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return (first_line.strip() or exc.__class__.__name__)[:200]


@asynccontextmanager
async def _navigated_page(url: str, timeout_ms: int, engine: str):
    async with browser_pool.page(engine) as page:
        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logger.info("networkidle timed out for %s on %s; using current page state", url, engine)
        yield page


async def _goto_and_extract(
    url: str, wait_for_selector: str | None, timeout_ms: int, engine: str
) -> tuple[str, str]:
    async with _navigated_page(url, timeout_ms, engine) as page:
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
        return await page.content(), await page.title()


async def _goto_and_screenshot(
    url: str, full_page: bool, timeout_ms: int, engine: str
) -> bytes:
    async with _navigated_page(url, timeout_ms, engine) as page:
        return await page.screenshot(full_page=full_page, type="png")


async def _with_engine_fallback(run):
    try:
        return await run("chromium")
    except PlaywrightError as exc:
        if not _is_http2_error(exc):
            raise
        logger.info(
            "Chromium navigation failed (%s); retrying with Firefox", _concise_error(exc)
        )
        return await run("firefox")


def _to_markdown(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    return converter.handle(html)


@mcp.tool
async def fetch_page(
    url: str,
    wait_for_selector: str | None = None,
    timeout_ms: int = 15000,
    readability: bool = False,
) -> str:
    """Fetch a web page with a headless browser and return its content as markdown. Set readability=True to strip nav/sidebars and return only the main content."""
    try:
        html, title = await _with_engine_fallback(
            lambda engine: _goto_and_extract(url, wait_for_selector, timeout_ms, engine)
        )
    except PlaywrightError as exc:
        raise ToolError(f"Failed to load {url}: {_concise_error(exc)}")

    if readability:
        extracted = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_images=True,
            favor_recall=True,
        )
        markdown = extracted or _to_markdown(html)
    else:
        markdown = _to_markdown(html)

    return f"# {title}\n\nSource: {url}\n\n{markdown}"


@mcp.tool
async def fetch_pages(
    urls: list[str],
    wait_for_selector: str | None = None,
    timeout_ms: int = 15000,
    readability: bool = False,
) -> str:
    """Fetch multiple web pages concurrently and return all results as markdown, separated by ---."""

    async def _one(url: str) -> str:
        try:
            html, title = await _with_engine_fallback(
                lambda engine: _goto_and_extract(url, wait_for_selector, timeout_ms, engine)
            )
            if readability:
                extracted = trafilatura.extract(html, url=url, output_format="markdown", include_links=True, include_images=True, favor_recall=True)
                markdown = extracted or _to_markdown(html)
            else:
                markdown = _to_markdown(html)
            return f"# {title}\n\nSource: {url}\n\n{markdown}"
        except Exception as exc:
            reason = _concise_error(exc) if isinstance(exc, PlaywrightError) else str(exc)
            return f"Source: {url}\n\nError: {reason}"

    results = await asyncio.gather(*[_one(url) for url in urls])
    return "\n\n---\n\n".join(results)


@mcp.tool
async def screenshot_page(
    url: str,
    full_page: bool = False,
    timeout_ms: int = 15000,
) -> Image:
    """Capture a PNG screenshot of a web page in a 1280x800 viewport."""
    try:
        data = await _with_engine_fallback(
            lambda engine: _goto_and_screenshot(url, full_page, timeout_ms, engine)
        )
    except PlaywrightError as exc:
        raise ToolError(f"Failed to load {url}: {_concise_error(exc)}")

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


@mcp.tool
async def memory_save(name: str, type: str, description: str, content: str) -> str:
    """Save or update a persistent memory entry (e.g. facts about the user, their preferences, or ongoing project context) so it can be recalled later across sessions via memory_search. `name` is a unique slug — saving again with the same name overwrites the existing entry. `type` categorizes the entry (e.g. user, preference, project, reference)."""
    try:
        record = await memory.save(name, type, description, content)
    except httpx.HTTPError as exc:
        raise ToolError(f"Failed to save memory: embedding request failed ({exc})")
    return f"Saved memory '{record['name']}' (type={record['type']})."


@mcp.tool
async def memory_search(query: str, top_k: int = 5, type: str | None = None) -> str:
    """Semantically search saved memory entries and return the most relevant ones with their full content. Optionally filter by `type`."""
    try:
        results = await memory.search(query, top_k=top_k, type=type)
    except httpx.HTTPError as exc:
        raise ToolError(f"Failed to search memory: embedding request failed ({exc})")
    if not results:
        return "No memory entries found."
    blocks = [
        f"## {r['name']} (type={r['type']}, score={r['score']:.3f})\n{r['description']}\n\n{r['content']}"
        for r in results
    ]
    return "\n\n---\n\n".join(blocks)


@mcp.tool
async def memory_list(type: str | None = None) -> str:
    """List all saved memory entries (name, type, description, last updated) without their full content. Optionally filter by `type`."""
    entries = await memory.list_entries(type=type)
    if not entries:
        return "No memory entries found."
    return "\n".join(
        f"- {e['name']} [{e['type']}]: {e['description']} (updated {e['updated_at']})"
        for e in entries
    )


@mcp.tool
async def memory_delete(name: str) -> str:
    """Delete a saved memory entry by name."""
    deleted = await memory.delete(name)
    if not deleted:
        raise ToolError(f"No memory entry named '{name}' found.")
    return f"Deleted memory '{name}'."


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
