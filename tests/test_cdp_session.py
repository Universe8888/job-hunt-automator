"""
Unit tests for CDPSession tab ownership (audit critical #3).

The bug: close() decided whether to close the tab by URL substring ("jobs.bg"
not in url). Our own tab navigates to jobs.bg during the scrape, so the guard was
always False -> our tab leaked into the user's real browser every run, AND a
reused human tab (also a jobs.bg URL) risked being closed. The fix tracks tab
ownership with an explicit flag set in start(), independent of the URL.

These tests use lightweight fakes (no real browser / Playwright needed).
"""

import asyncio

import pytest

from cdp_session import CDPSession


class _FakePage:
    def __init__(self, url=""):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True

    async def evaluate(self, _expr):
        return 1


class _FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.new_pages = 0

    async def new_page(self):
        self.new_pages += 1
        pg = _FakePage(url="about:blank")
        self.pages.append(pg)
        return pg


class _FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    async def close(self):
        self.closed = True


class _FakePlaywright:
    """Stands in for the playwright object so start() can connect_over_cdp."""
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    async def connect_over_cdp(self, _endpoint):
        return self._browser


def _session_for(context):
    """Build a CDPSession whose start() will attach to a fake browser wrapping
    the given context (exercises the real start()/close() logic end to end)."""
    browser = _FakeBrowser(context)
    return CDPSession(playwright=_FakePlaywright(browser))


def test_opened_tab_is_closed_even_after_navigating_to_jobsbg():
    """We opened the tab (no human jobs.bg tab existed) -> close() must close it,
    even though it now carries a jobs.bg URL from the scrape."""
    ctx = _FakeContext(pages=[])  # no existing jobs.bg tab
    sess = _session_for(ctx)

    asyncio.run(sess.start())
    assert sess._opened_own_tab is True
    our_tab = sess._page
    our_tab.url = "https://www.jobs.bg/en/front_job_search.php?..."  # scrape navigates it

    asyncio.run(sess.close())
    assert our_tab.closed is True          # our tab was cleaned up (no leak)
    assert sess._browser.closed is True    # CDP connection detached


def test_reused_human_tab_is_not_closed():
    """A human jobs.bg tab already existed -> we reuse it and must NOT close it."""
    human_tab = _FakePage(url="https://www.jobs.bg/en/front_job_search.php?human")
    ctx = _FakeContext(pages=[human_tab])
    sess = _session_for(ctx)

    asyncio.run(sess.start())
    assert sess._opened_own_tab is False
    assert sess._page is human_tab

    asyncio.run(sess.close())
    assert human_tab.closed is False       # human's tab left intact
    assert sess._browser.closed is True
