# Lens MCP

A [FastMCP](https://github.com/jlowin/fastmcp) server that gives an MCP client browser-grade web access: render pages to markdown, screenshot them, and fetch images — backed by headless Chromium/Firefox (Playwright) and httpx.

## Tools

| Tool | Signature | Returns | Description |
| --- | --- | --- | --- |
| `fetch_page` | `url, wait_for_selector=None, timeout_ms=15000` | markdown `str` | Renders the page in headless Chromium, falls back to Firefox on HTTP/2 or connection errors, and converts HTML to markdown with links and images preserved. |
| `screenshot_page` | `url, full_page=False, timeout_ms=15000` | PNG `Image` | Screenshots the page in a 1280×800 viewport. Same Chromium→Firefox fallback. Set `full_page=True` for the full scrollable page. |
| `fetch_image` | `image_url, referer=None` | `Image` | Downloads an image over HTTP with a desktop Chrome User-Agent and optional `Referer`. Format is inferred from `Content-Type`. |
| `memory_save` | `name, type, description, content` | `str` | Saves or updates a persistent memory entry (slug `name`, free-form `type` like `user`/`preference`/`project`/`reference`). Overwrites if `name` already exists. |
| `memory_search` | `query, top_k=5, type=None` | `str` | Embeds `query` and returns the `top_k` most semantically similar memory entries (full content), optionally filtered by `type`. |
| `memory_list` | `type=None` | `str` | Lists all memory entries (name, type, description, last updated) without full content, optionally filtered by `type`. |
| `memory_delete` | `name` | `str` | Deletes a memory entry by name. |

## Requirements

- Python `>=3.14`
- [uv](https://github.com/astral-sh/uv)
- Playwright browsers (`uv run playwright install chromium firefox`)

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `LENS_PORT` | `8788` | HTTP port the server listens on. |
| `LENS_REDIRECT_URL` | _(unset)_ | If set, `GET /` and any non-MCP URL redirects (302) here. Returns 404 when unset. |
| `LENS_DB_PATH` | `data/lens.db` | Path to the SQLite request log database. |
| `LENS_MEMORY_DB_PATH` | `data/memory.db` | Path to the SQLite memory store database. |
| `LENS_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | [fastembed](https://github.com/qdrant/fastembed) model used to embed memory entries locally (no external service needed). |
| `LENS_AUTH_TOKENS` | _(unset)_ | Comma-separated `user:token` pairs (e.g. `alice:abc123,bob:def456`). If unset, auth is disabled — fine for a private, single-user local instance. **Set this before exposing the server publicly** (e.g. via a tunnel). |

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

Every tool call is logged to a SQLite database (`data/lens.db` by default) via a background queue writer. The `requests` table stores tool name, arguments, status (`ok`/`error`), response preview, error message, and duration in ms.

## Memory

`memory_save`/`memory_search`/`memory_list`/`memory_delete` give an MCP client a persistent, semantically searchable memory store backed by SQLite (`data/memory.db` by default). Each entry has a unique `name` (per owner), a `type` (e.g. `user`, `preference`, `project`, `reference`), a short `description`, and full `content`; `memory_save` embeds `description + content` locally via fastembed and stores the vector alongside the row. `memory_search` embeds the query and ranks entries by cosine similarity. Because this is a single shared server, any MCP client connected to it (Claude web, Claude Code, etc.) reads and writes the same memory.

### Multi-user / shared instance

If `LENS_AUTH_TOKENS` is set, every memory entry is scoped to the `user` identified by the caller's bearer token — each user only ever sees and modifies their own entries, even though they're all stored in the same `data/memory.db`. Adding a new user is just adding another `user:token` pair to `LENS_AUTH_TOKENS` and giving them their token; no other setup needed. Running with `LENS_AUTH_TOKENS` unset keeps the server single-user with auth disabled, which is the right default for a private local self-hosted instance.

**Upgrading an existing single-user database**: pre-multi-tenant memory entries are kept (assigned to the default/no-auth owner) and remain readable once you set `LENS_AUTH_TOKENS`, as long as you keep using the server without a token (or treat that data as belonging to whichever single user you were before). Entry names stay globally unique within a `data/memory.db` created before this feature shipped — recreate the database if you need strict per-user name isolation from a clean slate.

## Resilient navigation

`fetch_page` and `screenshot_page` use a two-level fallback:

1. **Firefox fallback** — if Chromium raises `ERR_HTTP2_PROTOCOL_ERROR`, `ERR_HTTP2`, or `ERR_CONNECTION_RESET`, the request is retried once with Firefox.
2. **networkidle timeout** — if `networkidle` times out (analytics/websockets keeping the page "not idle"), the current page state is used rather than failing the call.
3. **Clean errors** — unhandled navigation failures surface as a single-line `ToolError` with the `net::ERR_*` code, not Playwright's verbose call log.

## Graceful shutdown

The server handles `SIGINT` (Ctrl-C) and `SIGTERM` (`docker stop`) — in-flight requests drain before exit, and the request log writer flushes its queue cleanly.
