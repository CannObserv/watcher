"""Screenshot capture using Playwright (optional dependency).

If the ``browser`` extra is not installed, all functions in this module
are no-ops and return ``None``.  No import error is raised.
"""

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


async def capture_screenshot(url: str) -> bytes | None:
    """Capture a full-viewport PNG screenshot of *url* and return the bytes.

    Returns PNG bytes on success, ``None`` on any failure (including Playwright
    not being installed).  Never raises — failures are logged as warnings so
    they cannot break the check pipeline.

    Args:
        url: The URL to screenshot.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT}
                )
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                png_bytes: bytes = await page.screenshot(type="png")
            finally:
                await browser.close()
        return png_bytes
    except Exception as exc:
        logger.warning("screenshot capture failed for %s: %s", url, exc)
        return None
