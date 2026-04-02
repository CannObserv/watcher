"""Tests for src.core.screenshot."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.core.screenshot import ScreenshotResult, capture_screenshot


class TestCaptureScreenshot:
    async def test_returns_none_when_playwright_unavailable(self):
        with patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", False):
            result = await capture_screenshot("https://example.com")
        assert result is None

    async def test_returns_screenshot_result_on_success(self):
        fake_png = b"\x89PNG\r\nfake"
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        mock_browser = AsyncMock()
        mock_browser.version = "130.0.0"
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()
        mock_chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw = MagicMock()
        mock_pw.chromium = mock_chromium
        mock_pw.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", True),
            patch("src.core.screenshot.async_playwright", return_value=mock_pw),
        ):
            result = await capture_screenshot("https://example.com")

        assert isinstance(result, ScreenshotResult)
        assert result.png_bytes == fake_png
        assert result.browser == "Chromium 130.0.0"

    async def test_returns_none_on_exception(self):
        mock_pw = MagicMock()
        mock_pw.__aenter__ = AsyncMock(side_effect=RuntimeError("browser crash"))
        mock_pw.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", True),
            patch("src.core.screenshot.async_playwright", return_value=mock_pw),
        ):
            result = await capture_screenshot("https://example.com")

        assert result is None

    async def test_calls_goto_with_url(self):
        fake_png = b"\x89PNG\r\nfake"
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        mock_browser = AsyncMock()
        mock_browser.version = "130.0.0"
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()
        mock_chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw = MagicMock()
        mock_pw.chromium = mock_chromium
        mock_pw.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw.__aexit__ = AsyncMock(return_value=False)

        url = "https://example.com/page"
        with (
            patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", True),
            patch("src.core.screenshot.async_playwright", return_value=mock_pw),
        ):
            await capture_screenshot(url)

        mock_page.goto.assert_called_once_with(url, wait_until="load", timeout=30_000)

    async def test_uses_default_viewport_dimensions(self):
        """Default viewport is 1280x800 when no kwargs supplied."""
        fake_png = b"\x89PNG\r\nfake"
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        mock_browser = AsyncMock()
        mock_browser.version = "130.0.0"
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()
        mock_chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw = MagicMock()
        mock_pw.chromium = mock_chromium
        mock_pw.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", True),
            patch("src.core.screenshot.async_playwright", return_value=mock_pw),
        ):
            await capture_screenshot("https://example.com")

        mock_browser.new_page.assert_called_once_with(viewport={"width": 1280, "height": 800})

    async def test_uses_custom_viewport_dimensions(self):
        """Custom viewport_width / viewport_height are passed to new_page."""
        fake_png = b"\x89PNG\r\nfake"
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        mock_browser = AsyncMock()
        mock_browser.version = "130.0.0"
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()
        mock_chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw = MagicMock()
        mock_pw.chromium = mock_chromium
        mock_pw.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.screenshot.PLAYWRIGHT_AVAILABLE", True),
            patch("src.core.screenshot.async_playwright", return_value=mock_pw),
        ):
            await capture_screenshot(
                "https://example.com",
                viewport_width=1920,
                viewport_height=1080,
            )

        mock_browser.new_page.assert_called_once_with(viewport={"width": 1920, "height": 1080})
