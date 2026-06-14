# Lens MCP

A [FastMCP](https://github.com/jlowin/fastmcp) server that gives an MCP client browser-grade web access: render pages to markdown, screenshot them, and fetch images — backed by headless Chromium/Firefox (Playwright) and httpx.

## Tools

| Tool | Signature | Returns | Description |
| --- | --- | --- | --- |
| `fetch_page` | `url, wait_for_selector=None, timeout_ms=15000` | markdown `str` | Renders the page in headless Chromium, falls back to Firefox on HTTP/2 or connection errors, and converts HTML to markdown with links and images preserved. |
| `screenshot_page` | `url, full_page=False, timeout_ms=15000` | PNG `Image` | Screenshots the page in a 1280×800 viewport. Same Chromium→Firefox fallback. Set `full_page=True` for the full scrollable page. |
| `fetch_image` | `image_url, referer=None` | `Image` | Downloads an image over HTTP with a desktop Chrome User-Agent and optional `Referer`. Format is inferred from `Content-Type`. |

## Requirements

- Python `>=3.14`
- [uv](https://github.com/astral-sh/uv)
- Playwright browsers (`uv run playwright install chromium firefox`)

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `LENS_PORT` | `8788` | HTTP port the server listens on. |
| `LENS_REDIRECT_URL` | _(unset)_ | If set, `GET /` and any non-MCP URL redirects (302) here. Returns 404 when unset. |
| `LENS_DB_PATH` | `lens.db` | Path to the SQLite request log database. |

## Running locally

```bash
uv sync
uv run playwright install chromium firefox
uv run python app/server.py
```

The server listens on `http://0.0.0.0:8788`.

## Running with Docker

```bash
docker compose up --build
```

The image is based on `mcr.microsoft.com/playwright/python` with Chromium and Firefox preinstalled.

## Endpoints

| Path | Description |
| --- | --- |
| `/mcp` | MCP HTTP transport endpoint |
| `/icon.svg` | Server icon |
| `/get` | 302 redirect to `LENS_REDIRECT_URL`, or 404 |

## Request logging

Every tool call is logged to a SQLite database (`lens.db` by default) via a background queue writer. The `requests` table stores tool name, arguments, status (`ok`/`error`), response preview, error message, and duration in ms.

## Resilient navigation

`fetch_page` and `screenshot_page` use a two-level fallback:

1. **Firefox fallback** — if Chromium raises `ERR_HTTP2_PROTOCOL_ERROR`, `ERR_HTTP2`, or `ERR_CONNECTION_RESET`, the request is retried once with Firefox.
2. **networkidle timeout** — if `networkidle` times out (analytics/websockets keeping the page "not idle"), the current page state is used rather than failing the call.
3. **Clean errors** — unhandled navigation failures surface as a single-line `ToolError` with the `net::ERR_*` code, not Playwright's verbose call log.

## Graceful shutdown

The server handles `SIGINT` (Ctrl-C) and `SIGTERM` (`docker stop`) — in-flight requests drain before exit, and the request log writer flushes its queue cleanly.
