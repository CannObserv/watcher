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
