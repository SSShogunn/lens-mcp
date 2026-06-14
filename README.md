# Lens MCP

A [FastMCP](https://github.com/jlowin/fastmcp) server that gives an MCP client browser-grade web access: render pages to markdown, screenshot them, and fetch images — backed by headless Chromium (Playwright) and httpx.

## Tools

| Tool | Signature | Returns | Description |
| --- | --- | --- | --- |
| `fetch_page` | `url, wait_for_selector=None, timeout_ms=15000` | markdown `str` | Renders the page in headless Chromium (waits for `networkidle`, optionally for a selector) and converts the HTML to markdown with links and images preserved. |
| `screenshot_page` | `url, full_page=False, timeout_ms=15000` | PNG `Image` | Screenshots the page in a 1280×800 viewport. Set `full_page=True` for the full scrollable page. |
| `fetch_image` | `image_url, referer=None` | `Image` | Downloads an image over HTTP with a desktop Chrome User-Agent and optional `Referer` (handy for hotlink-protected images). Format is inferred from `Content-Type`. |

## Requirements

- Python `>=3.14`
- [uv](https://github.com/astral-sh/uv)
- Playwright Chromium browser (`uv run playwright install chromium`)

## Configuration

All configuration is via environment variables (see `.env`):

| Variable | Default | Description |
| --- | --- | --- |
| `LENS_PORT` | `8788` | HTTP port the server listens on. |
| `LENS_AUTH_TOKEN` | _(unset)_ | Bearer token required on requests. **If unset, auth is disabled.** When set, every tool call must send `Authorization: Bearer <token>`. |

## Running locally

```bash
uv sync
uv run playwright install chromium
uv run python app/server.py
```

The server listens on `http://0.0.0.0:8788` using FastMCP's HTTP transport.

## Running with Docker

```bash
docker compose up --build
```

The image is based on `mcr.microsoft.com/playwright/python`, so Chromium and its system dependencies are preinstalled.

## Graceful shutdown

The server handles `SIGINT` (Ctrl-C) and `SIGTERM` (sent by `docker stop` / Kubernetes), letting the underlying uvicorn server drain in-flight requests before exiting. Each request launches and closes its own browser within the call, so resources are released even if a request is cancelled mid-flight.

## Authentication

When `LENS_AUTH_TOKEN` is set, clients must include the matching bearer token:

```
Authorization: Bearer <LENS_AUTH_TOKEN>
```

A missing or mismatched token raises a `ToolError`. Leave the variable unset for local/trusted use to disable the check entirely.
