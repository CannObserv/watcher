"""Screenshot capture using Playwright (optional dependency).

If the ``browser`` extra is not installed, all functions in this module
are no-ops and return ``None``.  No import error is raised.
"""

from typing import NamedTuple

from src.core.logging import get_logger

logger = get_logger(__name__)

# Try-import: Playwright is optional.  The rest of the module guards on
# PLAYWRIGHT_AVAILABLE so callers never need to check themselves.
try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None  # type: ignore[assignment]


SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 800


class ScreenshotResult(NamedTuple):
    """Result of a successful screenshot capture."""

    png_bytes: bytes
    browser: str


async def capture_screenshot(
    url: str,
    *,
    viewport_width: int = SCREENSHOT_WIDTH,
    viewport_height: int = SCREENSHOT_HEIGHT,
) -> ScreenshotResult | None:
    """Capture a full-viewport PNG screenshot of *url*.

    Returns a :class:`ScreenshotResult` on success, ``None`` on any failure
    (including Playwright not being installed).  Never raises — failures are
    logged as warnings so they cannot break the check pipeline.

    Args:
        url: The URL to screenshot.
        viewport_width: Viewport width in pixels (default: 1280).
        viewport_height: Viewport height in pixels (default: 800).
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            browser_version = browser.version
            try:
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": viewport_height}
                )
                await page.goto(url, wait_until="load", timeout=30_000)
                png_bytes: bytes = await page.screenshot(type="png")
            finally:
                await browser.close()
        return ScreenshotResult(png_bytes=png_bytes, browser=f"Chromium {browser_version}")
    except Exception as exc:
        logger.warning("screenshot capture failed for %s: %s", url, exc)
        return None
