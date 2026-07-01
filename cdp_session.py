"""
CDP attach mode — scrape through an already-running, human-cleared browser.

jobs.bg fronts with DataDome, which 403s fresh automated browsers regardless of
browser binary, user-agent, or launch flags (verified exhaustively). The one thing
DataDome trusts is a session that a human has already cleared: once you solve the
challenge once in your real Edge/Chrome, the `datadome` clearance cookie lives in
that browser context, and bot-driven navigation INSIDE that context is served real
content (HTTP 200).

This module attaches Playwright to such a browser over the Chrome DevTools Protocol
(CDP) and exposes the same minimal surface main.py's BrowserSession uses (`.page`,
`.context`, `ensure_alive`, `close`), so the scrape loop is unchanged.

Setup (one time, before a run):
  1. Launch your real browser with a debug port. Edge example:
       msedge.exe --remote-debugging-port=9222
     (Chrome: chrome.exe --remote-debugging-port=9222)
  2. In that browser, open jobs.bg and solve the DataDome challenge once.
  3. Run:  python main.py --site jobs.bg --cdp --days 7

Notes:
- close() detaches the CDP connection; it does NOT close your browser or tabs.
- We reuse the human-cleared context (cookies/clearance) and open our own tab in it.
"""

import asyncio
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


async def human_pace(job_index: int) -> None:
    """Sleep a human-like interval between detail fetches in CDP mode.

    DataDome re-challenges a cleared session when it sees machine-speed, regular
    request cadence ("browsing faster than a normal user"). We use a wide random
    delay and an occasional longer "reading" pause to mimic a human triaging jobs.
    Config-driven (CDP_* knobs) with safe fallbacks if they're absent.
    """
    try:
        import config
        lo = float(getattr(config, "CDP_MIN_DELAY", 12.0))
        hi = float(getattr(config, "CDP_MAX_DELAY", 30.0))
        every = int(getattr(config, "CDP_LONG_PAUSE_EVERY", 7))
        plo = float(getattr(config, "CDP_LONG_PAUSE_MIN", 45.0))
        phi = float(getattr(config, "CDP_LONG_PAUSE_MAX", 90.0))
    except Exception:
        lo, hi, every, plo, phi = 12.0, 30.0, 7, 45.0, 90.0

    if every > 0 and job_index > 0 and job_index % every == 0:
        delay = random.uniform(plo, phi)
        logger.info("  ☕ Human pause (%.0fs) to stay under DataDome's rate radar…", delay)
    else:
        delay = random.uniform(lo, hi)
        logger.debug("  ⏳ Pacing %.1fs before next fetch…", delay)
    await asyncio.sleep(delay)


class CDPSession:
    """Minimal stand-in for BrowserSession that attaches to a running browser."""

    def __init__(self, playwright: Any, endpoint: str = DEFAULT_CDP_ENDPOINT):
        self._playwright = playwright
        self._endpoint = endpoint
        # Playwright objects (typed Any — the package's stubs aren't import-safe here).
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._opened_own_tab = False   # True if WE created the tab (so close() owns it)
        self._logger = logging.getLogger(__name__)

    async def start(self):
        """Attach to the running browser and reuse its (cleared) context."""
        self._logger.info("🔌 Attaching to running browser over CDP at %s …", self._endpoint)
        self._browser = await self._playwright.chromium.connect_over_cdp(self._endpoint)

        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError(
                "CDP connect succeeded but the browser has no open context. "
                "Open a normal window/tab in your debug-port browser and retry."
            )
        # Reuse the existing context so we inherit the human-cleared DataDome cookie.
        self._context = contexts[0]

        # Prefer an already-open jobs.bg tab (the one you cleared); else open a new
        # tab IN THE SAME CONTEXT so it still carries the clearance cookie.
        existing = [pg for pg in self._context.pages if "jobs.bg" in (pg.url or "")]
        if existing:
            self._page = existing[0]
            self._opened_own_tab = False
            self._logger.info("✅ Reusing your open jobs.bg tab: %s", (self._page.url or "")[:80])
        else:
            self._page = await self._context.new_page()
            self._opened_own_tab = True
            self._logger.info("✅ Opened a new tab in your cleared session.")

        return self

    @property
    def page(self):
        return self._page

    @property
    def context(self):
        return self._context

    @property
    def owns_page(self) -> bool:
        """True when the active page was opened by this scraper session."""
        return self._opened_own_tab

    async def ensure_alive(self) -> bool:
        """Match BrowserSession.ensure_alive — verify the attached page still responds."""
        try:
            await self._page.evaluate("() => 1")
            return False
        except Exception:
            # Our tab was closed; try to open a fresh one in the same context.
            try:
                self._page = await self._context.new_page()
                self._opened_own_tab = True   # the replacement tab is ours to close
                self._logger.info("  🔄 Reopened a tab in the CDP session.")
                return True
            except Exception:
                self._logger.error("  🛑 CDP session lost (browser closed?). Stopping.")
                raise SystemExit(0)

    async def close(self):
        """Detach from the browser. Does NOT close the user's browser or tabs."""
        # Close ONLY a tab we created (tracked by a flag set in start()/ensure_alive).
        # A URL test is wrong here: our tab navigates to jobs.bg during the scrape, so
        # keying on the URL would leak our tab AND risk closing a reused human tab.
        try:
            if self._page and self._opened_own_tab:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._browser:
                # connect_over_cdp's close() detaches; the real browser keeps running.
                await self._browser.close()
        except Exception:
            pass
