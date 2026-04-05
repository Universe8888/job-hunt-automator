"""Async smoke tests for main.py BrowserSession and orchestrator helpers.

These tests use pytest-asyncio and mock Playwright to exercise the
BrowserSession lifecycle and the run() entry point without launching
a real browser.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import BrowserSession, DAYS_TO_FILTER


class TestDaysToFilter:
    def test_one_day(self):
        assert DAYS_TO_FILTER[1] == "r86400"

    def test_seven_days(self):
        assert DAYS_TO_FILTER[7] == "r604800"

    def test_thirty_days(self):
        assert DAYS_TO_FILTER[30] == "r2592000"

    def test_unsupported_day_value(self):
        assert DAYS_TO_FILTER.get(14, "") == ""


class TestBrowserSession:
    """Smoke tests for BrowserSession lifecycle with mocked Playwright."""

    def _make_mock_page(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)
        page.goto = AsyncMock()
        page.close = AsyncMock()
        return page

    def _make_mock_context(self, page):
        context = AsyncMock()
        context.pages = [page]
        context.close = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        return context

    def _make_mock_playwright(self, context):
        pw = AsyncMock()
        pw.chromium = AsyncMock()
        pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
        return pw

    @pytest.mark.asyncio
    async def test_start_creates_context_and_page(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        pw.chromium.launch_persistent_context.assert_awaited_once()
        assert session.page is page

    @pytest.mark.asyncio
    async def test_ensure_alive_returns_false_when_page_is_ok(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        result = await session.ensure_alive()
        assert result is False
        page.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensure_alive_raises_systemExit_when_page_is_dead(self):
        page = self._make_mock_page()
        page.evaluate = AsyncMock(side_effect=Exception("Target closed"))
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        with pytest.raises(SystemExit):
            await session.ensure_alive()

    @pytest.mark.asyncio
    async def test_close_shuts_down_context(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        await session.close()
        context.close.assert_awaited_once()
