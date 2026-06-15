import asyncio
import logging
import os
from contextlib import asynccontextmanager

from playwright.async_api import Browser, Page, ViewportSize, async_playwright

logger = logging.getLogger("lens.pool")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_playwright = None
_pools: dict[str, "BrowserPool"] = {}


class BrowserPool:
    def __init__(self, playwright, engine: str, size: int) -> None:
        self._playwright = playwright
        self._engine = engine
        self._size = size
        self._queue: asyncio.Queue[Browser] = asyncio.Queue()

    async def _launch(self) -> Browser:
        return await getattr(self._playwright, self._engine).launch(headless=True)

    async def start(self) -> None:
        for _ in range(self._size):
            await self._queue.put(await self._launch())
        logger.info("Browser pool ready (%s ×%d)", self._engine, self._size)

    async def stop(self) -> None:
        while not self._queue.empty():
            browser = self._queue.get_nowait()
            await browser.close()
        logger.info("Browser pool closed (%s)", self._engine)

    @asynccontextmanager
    async def acquire(self):
        browser = await self._queue.get()
        if not browser.is_connected():
            await browser.close()
            browser = await self._launch()
        try:
            yield browser
        finally:
            self._queue.put_nowait(browser)


@asynccontextmanager
async def page(engine: str, viewport: ViewportSize | None = None):
    pool = _pools[engine]
    async with pool.acquire() as browser:
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport=viewport or {"width": 1280, "height": 800},
        )
        try:
            pg: Page = await context.new_page()
            yield pg
        finally:
            await context.close()


async def start() -> None:
    global _playwright, _pools
    _playwright = await async_playwright().start()
    pool_size = int(os.environ.get("LENS_BROWSER_POOL_SIZE", "3"))
    _pools["chromium"] = BrowserPool(_playwright, "chromium", pool_size)
    _pools["firefox"] = BrowserPool(_playwright, "firefox", 1)
    await _pools["chromium"].start()
    await _pools["firefox"].start()


async def stop() -> None:
    global _playwright, _pools
    for pool in _pools.values():
        await pool.stop()
    _pools = {}
    if _playwright:
        await _playwright.stop()
        _playwright = None
