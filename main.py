"""
LinkedIn Jobs Scraper — Main Entry Point (v3.1)
Orchestrates search → scrape → match → export.
Features: real-time incremental saving, browser context recovery, progress bars,
          cookie injection for authenticated description fetching, one-time login mode.

Usage:
    python main.py                          # Run with default settings
    python main.py --profile resume.pdf     # Specify a custom PDF profile
    python main.py --quick                  # Skip full description fetching
    python main.py --keywords "Data Analyst" "AI Developer"  # Override keywords
    python main.py --headless               # Run without visible browser
    python main.py --days 7                 # Only jobs from the past week
    python main.py --max-jobs 50            # Stop after 50 jobs
    python main.py --login                  # One-time login (⚠️ USE AT YOUR OWN RISK)
    python main.py --cookies cookies.json   # Load cookies from a JSON file
"""

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import sys
import time
import os
import atexit
from typing import Any
from urllib.parse import unquote, urlparse

# NOTE: playwright / playwright_stealth are optional, heavyweight scraping
# dependencies. They are imported lazily inside the two coroutines that
# actually launch a browser (run_login_mode, run) so that `import main`
# succeeds for the pure, no-I/O paths — gate evaluation, CSV routing, and
# the BrowserSession/constant smoke tests — without the browser stack
# installed. Do NOT promote these back to module-level imports.

from config import (
    KEYWORDS,
    LOCATIONS,
    PROFILE_PDF,
    MAX_JOBS_PER_RUN,
    TARGET_SITE,
    LEADS_CSV,
    REJECTS_CSV,
)
from stealth_config import get_launch_options, get_context_options, get_random_user_agent, apply_stealth_to_page
from scraper import human_delay
from csv_export import (
    export_to_csv,
    export_rejects_csv,
    sort_leads_csv,
    load_existing_urls,
    remove_url_from_csv,
)
from profile_matcher import match_jobs, load_skills

import gatekeeper

import scraper as linkedin_scraper
import jobsbg_scraper
from cdp_session import CDPSession, human_pace as cdp_human_pace, DEFAULT_CDP_ENDPOINT as CDP_DEFAULT_ENDPOINT

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ────────────────────────────────────────
# Single-Instance Lock
# ────────────────────────────────────────

LOCK_FILE = "scraper.lock"
_lock_fd = None


def _acquire_lock() -> bool:
    """Acquire an exclusive lock to prevent multiple scraper instances."""
    global _lock_fd
    try:
        _lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
        os.write(_lock_fd, str(os.getpid()).encode())
        atexit.register(_release_lock)
        return True
    except FileExistsError:
        try:
            with open(LOCK_FILE, "r") as f:
                f.read()
            return False
        except Exception:
            os.remove(LOCK_FILE)
            return _acquire_lock()


def _release_lock():
    """Release the single-instance lock."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            os.close(_lock_fd)
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
        _lock_fd = None


# ────────────────────────────────────────
# Cookie Management
# ────────────────────────────────────────

COOKIES_FILE = "linkedin_cookies.json"


def load_cookies(filepath: str | None = None) -> list[dict]:
    """Load LinkedIn cookies from a JSON file."""
    path = filepath or COOKIES_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        return cookies
    except Exception as e:
        logging.getLogger(__name__).warning("⚠️  Failed to load cookies from %s: %s", path, str(e))
        return []


def save_cookies(cookies: list[dict], filepath: str | None = None):
    """Save LinkedIn cookies to a JSON file."""
    path = filepath or COOKIES_FILE
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        logging.getLogger(__name__).info("💾 Saved %d cookies to %s", len(cookies), path)
    except Exception as e:
        logging.getLogger(__name__).warning("⚠️  Failed to save cookies to %s: %s", path, str(e))


# ────────────────────────────────────────
# Logging Setup
# ────────────────────────────────────────

def setup_logging(verbose: bool = False, log_file: str = "scraper.log"):
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s │ %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8", mode="a"),
        ],
    )


# ────────────────────────────────────────
# Banner
# ────────────────────────────────────────

BANNER = r"""
╔══════════════════════════════════════════════════════╗
║       LinkedIn Jobs Scraper v3.1                     ║
║       Playwright + Stealth  │  Guest API Mode        ║
║       ⚡ Real-Time Saving  │  Context Recovery      ║
║       🔍 URL Validation   │  Content Sanitization   ║
║       🔑 Cookie Auth      │  Enhanced Parsing       ║
╚══════════════════════════════════════════════════════╝
"""

FAST_TRIAGE_AUDIT_REASON = (
    "FAST_TRIAGE: skipped jobs.bg detail fetch because the title matched an "
    "explicit Gate 1 operator-title hard deny"
)
FAST_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}


# ────────────────────────────────────────
# Date Filter Mapping
# ────────────────────────────────────────

DAYS_TO_FILTER = {
    1: "r86400",
    7: "r604800",
    30: "r2592000",
}


# ────────────────────────────────────────
# Browser Context Manager
# ────────────────────────────────────────

class BrowserSession:
    """
    Manages browser lifecycle with automatic recovery on context death.
    Uses persistent contexts to store cookies and bypass recurring captchas.
    Supports cookie injection for authenticated sessions.
    """

    def __init__(self, playwright: Any, headless: bool = False, cookies: list[dict] | None = None):
        self._playwright = playwright
        self._headless = headless
        # Playwright objects (typed Any — package stubs aren't import-safe here).
        self._context: Any = None
        self._page: Any = None
        self._ua = get_random_user_agent()
        self._cookies = cookies or []
        self._logger = logging.getLogger(__name__)

    async def start(self):
        """Launch browser and create initial persistent context."""
        self._logger.info("🌐 Launching persistent browser (User-Agent: %s…)", self._ua[:60])

        import os
        from config import SESSION_DIR
        os.makedirs(SESSION_DIR, exist_ok=True)

        launch_opts = get_launch_options(self._headless)
        context_opts = get_context_options(self._ua)
        opts = {**launch_opts, **context_opts}

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            **opts
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        # Apply stealth scripts to prevent bot detection
        await apply_stealth_to_page(self._page)

        # Inject cookies if provided
        if self._cookies:
            self._logger.info("🔑 Injecting %d cookies for authenticated session…", len(self._cookies))
            await self._context.add_cookies(self._cookies)
            await self._page.reload(wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

        return self

    async def _new_context(self):
        """Recover by opening a new page within the persistent context."""
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass

        try:
            self._page = await self._context.new_page()
            await apply_stealth_to_page(self._page)
            self._logger.debug("  🔄 Created new page in persistent context")
        except Exception:
            self._logger.info("  🛑 Browser closed. Exiting...")
            raise SystemExit(0)

    @property
    def page(self):
        return self._page

    @property
    def context(self):
        return self._context

    @property
    def owns_page(self) -> bool:
        return True

    async def ensure_alive(self) -> bool:
        """
        Check if the browser page is still alive.
        If dead (e.g. user closed it), stop the program.
        """
        try:
            await self._page.evaluate("() => 1")
            return False
        except Exception:
            self._logger.info("  🛑 Browser window was closed. Stopping scraper…")
            raise SystemExit(0)

    async def close(self):
        """Close the browser context."""
        if self._context:
            await self._context.close()


# ────────────────────────────────────────
# Login Mode
# ────────────────────────────────────────

async def run_login_mode(headless: bool, cookies_file: str | None):
    """
    Open browser for one-time manual LinkedIn login, then save cookies.
    ⚠️ WARNING: Using your profile for automated scraping carries a high risk
    of account suspension or permanent banning. Use at your own risk.
    """
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    async with Stealth().use_async(async_playwright()) as p:
        session = await BrowserSession(p, headless=headless).start()
        page = session.page

        logging.info("🔐 Login mode — navigating to LinkedIn login page…")
        logging.warning("⚠️  RISK WARNING: Logging in and using this account for automated scraping")
        logging.warning("   violates LinkedIn's Terms of Service and may result in an account ban.")
        logging.info("   Please log in to your LinkedIn account in the browser window.")
        logging.info("   Once logged in, close the browser or press Ctrl+C to save cookies.")

        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=20000)

        try:
            # Wait for user to log in and close browser
            while True:
                try:
                    await page.evaluate("() => 1")
                    await asyncio.sleep(2)
                except Exception:
                    break
        except KeyboardInterrupt:
            pass

        # Save cookies before exiting
        try:
            cookies = await session.context.cookies()
            save_cookies(cookies, cookies_file)
            logging.info("✅ Cookies saved successfully!")
        except Exception as e:
            logging.warning("⚠️  Could not save cookies: %s", str(e))
        await session.close()


# ────────────────────────────────────────
# 3-Gate Verdict Pipeline (per-job)
# ────────────────────────────────────────

@dataclass
class RuntimeStats:
    desc_time: float = 0.0
    cdp_pace_time: float = 0.0
    non_cdp_pace_time: float = 0.0
    gate_route_time: float = 0.0
    duplicate_skips: int = 0
    fast_triage_skips: int = 0


@dataclass
class RoutingState:
    leads_csv: str
    rejects_csv: str
    seen_urls: set[str]
    stale_reject_urls: set[str]
    all_jobs: list[dict]


@dataclass
class ResourceBlockState:
    page_ids: set[int]
    counts: dict[str, int]


@dataclass
class BatchContext:
    active_scraper: Any
    is_jobsbg: bool
    fetch_descriptions: bool
    fast_triage: bool
    use_cdp: bool
    skills: dict[str, float]
    routing: RoutingState
    stats: RuntimeStats
    max_jobs: int
    resources: ResourceBlockState
    logger: logging.Logger


JOBSBG_TITLE_SOURCE_FIELD = "_jobsbg_title_source"
JOBSBG_TITLE_SOURCE_URL_SLUG = "url_slug"
JOBSBG_TITLE_SOURCE_CARD = "card"

_SLUG_WORD_OVERRIDES = {
    "ai": "AI",
    "api": "API",
    "bi": "BI",
    "ciso": "CISO",
    "crm": "CRM",
    "cto": "CTO",
    "devops": "DevOps",
    "erp": "ERP",
    "gdpr": "GDPR",
    "grc": "GRC",
    "hr": "HR",
    "it": "IT",
    "kyc": "KYC",
    "ml": "ML",
    "qa": "QA",
    "rpa": "RPA",
    "sap": "SAP",
    "seo": "SEO",
    "sre": "SRE",
    "ui": "UI",
    "ux": "UX",
}


def _attach_verdict(job: dict) -> str:
    """
    Run the 3-gate filter on a single job and attach the Verdict fields onto the
    job dict (per the interface contract §4.3) so the CSV writers can read them.

    The legacy skill score (already on job via match_jobs) is INFO-ONLY — the
    three gates decide the verdict, not the score.

    Attaches:
        job['verdict']           "keep" | "manual_review" | "reject"
        job['rank']              float
        job['gate1_status'] / ['gate2_status'] / ['gate3_status']   gate statuses
        job['gate_reasons']      "; "-joined list of all gate reasons
        job['parsed_comp_eur']   normalized EUR/yr gross figure, or "" if undisclosed

    Returns the verdict string.
    """
    verdict = gatekeeper.evaluate(job)

    job["verdict"] = verdict.verdict
    job["rank"] = round(verdict.rank, 4)
    job["gate1_status"] = verdict.gate1.status
    job["gate2_status"] = verdict.gate2.status
    job["gate3_status"] = verdict.gate3.status
    job["gate_reasons"] = "; ".join(verdict.reasons)

    # Surface the normalized comp number for the CSV column. evaluate() re-parses
    # internally but does not expose the figure, so re-run the pure parser over
    # the same combined text (title + description + salary).
    comp_text = "\n".join(
        filter(None, [job.get("title", ""), job.get("description", ""), job.get("salary", "")])
    )
    parsed = gatekeeper.parse_comp(comp_text)
    job["parsed_comp_eur"] = round(parsed.top_eur_gross_yr) if parsed else ""

    return verdict.verdict


def _fast_triage_reject_reason(job: dict, enabled: bool, is_jobsbg: bool) -> str | None:
    """
    Conservative pre-detail jobs.bg triage.

    Safe rule only: skip the expensive detail page when the TITLE alone hits the
    explicit Gate 1 title-hard-deny path. A title with merely no lane terms is
    NOT skipped because the detail body may contain the in-lane signal.
    """
    if not enabled or not is_jobsbg:
        return None

    if job.get(JOBSBG_TITLE_SOURCE_FIELD) == JOBSBG_TITLE_SOURCE_URL_SLUG:
        return None

    title = (job.get("title") or "").strip()
    if not title:
        return None

    if not gatekeeper.title_hard_deny_hits(title):
        return None
    return FAST_TRIAGE_AUDIT_REASON


async def _enable_fast_resource_blocking(
    page: Any,
    blocked_page_ids: set[int],
    blocked_resource_counts: dict[str, int],
    logger: logging.Logger,
) -> bool:
    """Block non-essential resources on a page we own.

    We only abort images/fonts/media: scripts and styles stay enabled so DataDome
    and page DOM rendering are not starved. The page-id guard prevents stacking
    duplicate route handlers on the same Playwright page.
    """
    page_id = id(page)
    if page_id in blocked_page_ids:
        return False

    async def _route_handler(route):
        try:
            resource_type = route.request.resource_type
            if resource_type in FAST_BLOCKED_RESOURCE_TYPES:
                blocked_resource_counts[resource_type] = (
                    blocked_resource_counts.get(resource_type, 0) + 1
                )
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await page.route("**/*", _route_handler)
        blocked_page_ids.add(page_id)
        logger.info("  🚫 Fast triage: blocking image/font/media requests on scraper-owned tab")
        return True
    except Exception as e:
        logger.debug("  ⚠️  Could not enable fast resource blocking: %s", str(e)[:100])
        return False


async def _maybe_enable_fast_resource_blocking(
    page: Any,
    session: Any,
    *,
    fast_triage: bool,
    is_jobsbg: bool,
    use_cdp: bool,
    blocked_page_ids: set[int],
    blocked_resource_counts: dict[str, int],
    logger: logging.Logger,
) -> bool:
    if not fast_triage or not is_jobsbg:
        return False
    if use_cdp and not getattr(session, "owns_page", False):
        logger.debug("  🚫 Fast triage: not blocking resources on a reused human CDP tab")
        return False
    return await _enable_fast_resource_blocking(page, blocked_page_ids, blocked_resource_counts, logger)


def _log_timing_summary(
    logger: logging.Logger,
    *,
    total_jobs: int,
    elapsed: float,
    search_time: float,
    desc_time: float,
    cdp_pace_time: float,
    non_cdp_pace_time: float,
    gate_route_time: float,
    sort_time: float,
    duplicate_skips: int,
    fast_triage_skips: int,
    blocked_resource_counts: dict[str, int] | None = None,
) -> None:
    logger.info("  Jobs routed this run: %d", total_jobs)
    logger.info("  Time elapsed:         %.1f minutes", elapsed / 60)
    logger.info("  ├─ Search phase:            %.1f minutes", search_time / 60)
    logger.info("  ├─ Detail nav/extract:      %.1f minutes", desc_time / 60)
    logger.info("  ├─ CDP pacing sleep:        %.1f minutes", cdp_pace_time / 60)
    logger.info("  ├─ Non-CDP post-fetch wait: %.1f minutes", non_cdp_pace_time / 60)
    logger.info("  ├─ Gate/route/write:        %.1f minutes", gate_route_time / 60)
    logger.info("  └─ Final leads sort:        %.1f minutes", sort_time / 60)
    logger.info("  Duplicate skips before detail fetch: %d", duplicate_skips)
    logger.info("  Fast-triage detail skips:          %d", fast_triage_skips)
    if blocked_resource_counts:
        blocked = ", ".join(
            f"{resource_type}={count}" for resource_type, count in sorted(blocked_resource_counts.items())
        )
        logger.info("  Fast-triage blocked requests:      %s", blocked)


def _load_detail_skip_state(
    leads_csv: str,
    rejects_csv: str,
    logger: logging.Logger,
) -> tuple[set[str], set[str]]:
    """Return URLs safe to skip before detail fetch and old rejects to recheck."""
    lead_urls = load_existing_urls(leads_csv)
    reject_urls = load_existing_urls(rejects_csv)
    logger.info(
        "📋 Pre-loaded %d lead URLs for pre-detail skip; %d reject URLs will be rechecked",
        len(lead_urls),
        len(reject_urls),
    )
    return set(lead_urls), set(reject_urls)


def _route_job(
    job: dict,
    leads_csv: str,
    rejects_csv: str,
    stale_reject_urls: set[str] | None = None,
) -> tuple[str, int]:
    """
    Route a fully-evaluated job to the correct CSV (real-time saving):
      verdict in {keep, manual_review} -> leads CSV  (export_to_csv)
      verdict == reject                -> rejects CSV (export_rejects_csv)

    Returns (verdict, new_row_count) where new_row_count is 0 for a duplicate.
    """
    verdict = job.get("verdict", "")
    if verdict == "reject":
        new_count = export_rejects_csv([job], rejects_csv)
    else:
        # Incremental save: skip the per-row rank sort (O(n^2) over a run); the
        # leads CSV is sorted once at the end of the run via sort_leads_csv().
        new_count = export_to_csv([job], leads_csv, sort_after=False)
        url = job.get("url", "").strip()
        if new_count > 0 and stale_reject_urls is not None and url in stale_reject_urls:
            remove_url_from_csv(rejects_csv, url)
            stale_reject_urls.discard(url)
    return verdict, new_count


def _route_fast_triage_reject(
    job: dict,
    *,
    skills: dict[str, float],
    leads_csv: str,
    rejects_csv: str,
    fast_triage_reason: str,
) -> tuple[str, int]:
    """Attach title-only verdict fields and write a fast-triaged reject."""
    job.setdefault("description", "")
    match_jobs([job], skills=skills)
    _attach_verdict(job)
    job["gate_reasons"] = "; ".join(
        filter(None, [job.get("gate_reasons", ""), fast_triage_reason])
    )
    return _route_job(job, leads_csv, rejects_csv)


def _normalize_jobsbg_job_url(job_url: str) -> str:
    """Normalize a user-supplied jobs.bg URL or path to the canonical host."""
    value = (job_url or "").strip()
    if not value:
        raise ValueError("--job-url cannot be empty")

    if value.startswith("/"):
        value = f"https://www.jobs.bg{value}"
    elif value.startswith("en/job/"):
        value = f"https://www.jobs.bg/{value}"
    elif value.startswith("job/"):
        value = f"https://www.jobs.bg/en/{value}"

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in {"jobs.bg", "www.jobs.bg"}:
        raise ValueError("--job-url must point to jobs.bg")

    path = parsed.path or "/"
    return f"https://www.jobs.bg{path}"


def _is_jobsbg_detail_url(job_url: str) -> bool:
    path = urlparse(job_url or "").path.lower()
    return "/job/" in path


def _is_jobsbg_search_url(job_url: str) -> bool:
    path = urlparse(job_url or "").path.lower()
    return path.endswith("/front_job_search.php")


def _slug_title_from_jobsbg_url(job_url: str) -> str:
    slug = unquote(urlparse(job_url).path.rstrip("/").split("/")[-1])
    words = [
        part
        for part in slug.replace("_", "-").split("-")
        if part and not part.isdigit()
    ]
    title = " ".join(
        _SLUG_WORD_OVERRIDES.get(word.casefold(), word.capitalize())
        for word in words
    ).strip()
    return title if title else "Jobs.bg listing"


def _build_jobsbg_single_url_job(job_url: str, *, source: str) -> dict:
    normalized_url = _normalize_jobsbg_job_url(job_url)
    if not _is_jobsbg_detail_url(normalized_url):
        raise ValueError("--job-url must be a jobs.bg job detail URL")
    return {
        "title": _slug_title_from_jobsbg_url(normalized_url),
        "company": "",
        "location": "",
        "date": "",
        "url": normalized_url,
        "search_keyword": source,
        "search_location": "direct URL",
        JOBSBG_TITLE_SOURCE_FIELD: JOBSBG_TITLE_SOURCE_URL_SLUG,
    }


async def _jobsbg_jobs_from_current_tab(page: Any, logger: logging.Logger) -> list[dict]:
    """Return job cards from the currently reused jobs.bg tab, or one detail job."""
    current_url = getattr(page, "url", "") or ""
    try:
        normalized_url = _normalize_jobsbg_job_url(current_url)
    except ValueError as exc:
        raise RuntimeError(
            "--current-tab requires the reused CDP tab to be open on jobs.bg"
        ) from exc

    if _is_jobsbg_detail_url(normalized_url):
        logger.info("  Current tab is a jobs.bg detail page; processing that single URL.")
        return [_build_jobsbg_single_url_job(normalized_url, source="current-tab")]

    if not _is_jobsbg_search_url(normalized_url):
        raise RuntimeError(
            "--current-tab requires a jobs.bg search results page or job detail page"
        )

    html = await page.content()
    jobs = jobsbg_scraper.parse_jobsbg_cards(html)
    if not jobs:
        raise RuntimeError(
            "--current-tab found no jobs.bg job cards on the current search results page"
        )

    for job in jobs:
        job.setdefault("search_keyword", "current-tab")
        job.setdefault("search_location", "current tab")
        job.setdefault(JOBSBG_TITLE_SOURCE_FIELD, JOBSBG_TITLE_SOURCE_CARD)
    logger.info("  Parsed %d job card(s) from the current jobs.bg tab.", len(jobs))
    return jobs


async def _ensure_batch_page(page: Any, session: Any, ctx: BatchContext) -> Any:
    """Return a live page, reapplying resource blocking if the session recovered."""
    recovered = await session.ensure_alive()
    if not recovered:
        return page

    page = session.page
    await _maybe_enable_fast_resource_blocking(
        page,
        session,
        fast_triage=ctx.fast_triage,
        is_jobsbg=ctx.is_jobsbg,
        use_cdp=ctx.use_cdp,
        blocked_page_ids=ctx.resources.page_ids,
        blocked_resource_counts=ctx.resources.counts,
        logger=ctx.logger,
    )
    return page


async def _fetch_description_for_batch(page: Any, job: dict, ctx: BatchContext) -> None:
    t0 = time.perf_counter()
    if ctx.is_jobsbg:
        job["description"] = await ctx.active_scraper.fetch_job_description(page, job["url"], job)
    else:
        job["description"] = await ctx.active_scraper.fetch_job_description(page, job["url"])
    ctx.stats.desc_time += time.perf_counter() - t0


def _should_fetch_detail_for_batch(job: dict, ctx: BatchContext) -> bool:
    if not (ctx.fetch_descriptions and job.get("url")):
        return False
    return _fast_triage_reject_reason(
        job,
        enabled=ctx.fast_triage and ctx.fetch_descriptions,
        is_jobsbg=ctx.is_jobsbg,
    ) is None


def _has_later_detail_fetch(
    basic_jobs: list[dict],
    start_index: int,
    ctx: BatchContext,
    current_job_url: str = "",
) -> bool:
    projected_routed = len(ctx.routing.all_jobs) + 1
    future_seen = set(ctx.routing.seen_urls)
    if current_job_url:
        future_seen.add(current_job_url)

    for job in basic_jobs[start_index:]:
        if ctx.max_jobs and projected_routed >= ctx.max_jobs:
            return False

        job_url = job.get("url", "").strip()
        if job_url and job_url in future_seen:
            continue

        if _should_fetch_detail_for_batch(job, ctx):
            return True

        if job_url:
            future_seen.add(job_url)
        projected_routed += 1

    return False


async def _pace_after_detail_fetch(ctx: BatchContext, *, has_later_detail_fetch: bool) -> None:
    if not has_later_detail_fetch:
        return

    t0 = time.perf_counter()
    if ctx.use_cdp:
        await cdp_human_pace(len(ctx.routing.all_jobs))
        ctx.stats.cdp_pace_time += time.perf_counter() - t0
    else:
        await human_delay(2, 5)
        ctx.stats.non_cdp_pace_time += time.perf_counter() - t0


def _remember_routed_job(job: dict, ctx: BatchContext) -> None:
    job_url = job.get("url", "").strip()
    if job_url:
        ctx.routing.seen_urls.add(job_url)
    ctx.routing.all_jobs.append(job)


def _log_batch_route(ctx: BatchContext, verdict: str, new_count: int, *, fast_triage: bool = False) -> None:
    if HAS_TQDM:
        return

    dest = ctx.routing.rejects_csv if verdict == "reject" else ctx.routing.leads_csv
    if fast_triage:
        ctx.logger.info("      -> %s | fast-triage saved to %s", verdict, dest)
    elif new_count > 0:
        ctx.logger.info("      -> %s | saved to %s", verdict, dest)
    else:
        ctx.logger.info("      -> %s | duplicate skipped", verdict)


def _route_fast_triage_if_possible(job: dict, ctx: BatchContext) -> bool:
    fast_triage_reason = _fast_triage_reject_reason(
        job,
        enabled=ctx.fast_triage and ctx.fetch_descriptions,
        is_jobsbg=ctx.is_jobsbg,
    )
    if not fast_triage_reason:
        return False

    t0 = time.perf_counter()
    verdict, new_count = _route_fast_triage_reject(
        job,
        skills=ctx.skills,
        leads_csv=ctx.routing.leads_csv,
        rejects_csv=ctx.routing.rejects_csv,
        fast_triage_reason=fast_triage_reason,
    )
    ctx.stats.gate_route_time += time.perf_counter() - t0
    ctx.stats.fast_triage_skips += 1
    _remember_routed_job(job, ctx)
    _log_batch_route(ctx, verdict, new_count, fast_triage=True)
    return True


def _route_full_job(job: dict, ctx: BatchContext) -> None:
    t0 = time.perf_counter()
    match_jobs([job], skills=ctx.skills)
    _attach_verdict(job)
    verdict, new_count = _route_job(
        job,
        ctx.routing.leads_csv,
        ctx.routing.rejects_csv,
        stale_reject_urls=ctx.routing.stale_reject_urls,
    )
    ctx.stats.gate_route_time += time.perf_counter() - t0
    _remember_routed_job(job, ctx)
    _log_batch_route(ctx, verdict, new_count)


async def _process_job_batch(
    basic_jobs: list[dict],
    page: Any,
    session: Any,
    ctx: BatchContext,
) -> bool:
    """Fetch details, score, gate, and route a batch of already-discovered jobs.

    Returns True when the max-jobs limit was reached.
    """
    total_jobs = len(basic_jobs)
    job_iter: Any = basic_jobs
    if HAS_TQDM and ctx.fetch_descriptions:
        job_iter = tqdm(
            basic_jobs,
            desc="  Fetching descriptions",
            unit="job",
            leave=False,
            ncols=80,
        )

    try:
        for i, job in enumerate(job_iter):
            if ctx.max_jobs and len(ctx.routing.all_jobs) >= ctx.max_jobs:
                ctx.logger.info("  Reached max jobs limit (%d). Stopping.", ctx.max_jobs)
                return True

            job_url = job.get("url", "").strip()
            if job_url and job_url in ctx.routing.seen_urls:
                ctx.stats.duplicate_skips += 1
                if not HAS_TQDM:
                    ctx.logger.info(
                        "    [%d/%d] duplicate skipped: %s",
                        i + 1,
                        total_jobs,
                        job.get("title", "Unknown"),
                    )
                continue

            if not HAS_TQDM:
                ctx.logger.info("    [%d/%d] %s", i + 1, total_jobs, job.get("title", "Unknown"))

            page = await _ensure_batch_page(page, session, ctx)

            if _route_fast_triage_if_possible(job, ctx):
                continue

            if ctx.fetch_descriptions and job.get("url"):
                has_later_detail_fetch = _has_later_detail_fetch(
                    basic_jobs,
                    i + 1,
                    ctx,
                    current_job_url=job_url,
                )
                await _fetch_description_for_batch(page, job, ctx)
                await _pace_after_detail_fetch(
                    ctx,
                    has_later_detail_fetch=has_later_detail_fetch,
                )
            else:
                job.setdefault("description", "")

            _route_full_job(job, ctx)
    finally:
        if HAS_TQDM and hasattr(job_iter, "close"):
            job_iter.close()

    return False


# ────────────────────────────────────────
# Main
# ────────────────────────────────────────

async def run(args):
    # Fix Windows console encoding for Unicode banner
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    print(BANNER)
    log_file = getattr(args, "log_file", "scraper.log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    logger = logging.getLogger(__name__)

    # Handle login mode
    if getattr(args, "login", False):
        cookies_file = getattr(args, "cookies", None) or COOKIES_FILE
        headless = getattr(args, "headless", False)
        if headless:
            logger.warning("⚠️  --login mode requires a visible browser. Disabling headless.")
            headless = False
        await run_login_mode(headless, cookies_file)
        return

    keywords = args.keywords if args.keywords else KEYWORDS
    profile_pdf = args.profile or PROFILE_PDF
    fetch_descriptions = not args.quick
    headless = args.headless
    max_jobs = args.max_jobs or MAX_JOBS_PER_RUN
    fast_triage = bool(getattr(args, "fast_triage", False))
    use_cdp = bool(getattr(args, "cdp", False))
    current_tab = bool(getattr(args, "current_tab", False))
    single_job_url = (getattr(args, "job_url", None) or "").strip()
    continuation_mode = current_tab or bool(single_job_url)

    selected_site = args.site if args.site else TARGET_SITE
    if continuation_mode:
        if args.site and args.site != "jobs.bg":
            raise SystemExit("Jobs.bg continuation flags cannot be used with --site linkedin")
        selected_site = "jobs.bg"

    # jobs.bg uses DataDome captcha which blocks headless browsers
    if "jobs" in selected_site.lower() and headless:
        logger.warning("⚠️  jobs.bg requires a visible browser to bypass DataDome captcha.")
        logger.warning("   Forcing non-headless mode...")
        headless = False

    # Two-file model (user decision #2): leads (keep + manual_review) and a
    # SEPARATE rejects audit CSV (reject) — nothing is silently dropped.
    active_scraper = jobsbg_scraper if selected_site == "jobs.bg" else linkedin_scraper
    is_jobsbg = selected_site == "jobs.bg"

    if current_tab and single_job_url:
        raise SystemExit("Use only one continuation source: --current-tab or --job-url")
    if current_tab and not use_cdp:
        raise SystemExit("--current-tab requires --cdp so main.py can reuse your cleared jobs.bg tab")
    if single_job_url and not use_cdp:
        raise SystemExit("--job-url requires --cdp so jobs.bg runs through your cleared browser session")
    if single_job_url:
        try:
            single_job_url = _normalize_jobsbg_job_url(single_job_url)
            if not _is_jobsbg_detail_url(single_job_url):
                raise ValueError("--job-url must be a jobs.bg job detail URL")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    if getattr(args, "output", None):
        # --output overrides the LEADS path; the rejects path is derived by suffix.
        leads_csv = args.output
        _root, _ext = os.path.splitext(leads_csv)
        rejects_csv = _root + "-rejects" + (_ext or ".csv")
    elif is_jobsbg:
        leads_csv = "Jobs.bg-leads.csv"
        rejects_csv = "Jobs.bg-rejects.csv"
    else:
        leads_csv = LEADS_CSV
        rejects_csv = REJECTS_CSV

    # Load cookies for authenticated session
    cookies_file = getattr(args, "cookies", None) or COOKIES_FILE
    cookies = load_cookies(cookies_file)
    auth_status = f"✅ {len(cookies)} cookies loaded" if cookies else "⚠️  No cookies (guest mode)"

    # Date filter
    date_filter = ""
    if args.days:
        date_filter = DAYS_TO_FILTER.get(args.days, "")
        if not date_filter:
            logger.warning("⚠️  --days %d not supported. Use 1, 7, or 30. Ignoring.", args.days)

    total_combinations = 1 if continuation_mode else len(keywords) * len(LOCATIONS)
    logger.info("🚀 Starting scraper")
    if continuation_mode:
        source = "current CDP tab" if current_tab else single_job_url
        logger.info("   Continuation source: %s", source)
    else:
        logger.info("   Keywords: %s", keywords)
        logger.info("   Locations: %s", [loc["name"] for loc in LOCATIONS])
    logger.info("   Work types: Remote + Hybrid")
    logger.info("   Fetch descriptions: %s (Real-Time)", fetch_descriptions)
    logger.info("   Fast triage: %s", fast_triage if is_jobsbg else "off (jobs.bg only)")
    logger.info("   Target Site: %s", selected_site)
    logger.info("   Profile PDF: %s", profile_pdf)
    logger.info("   Leads CSV (keep + manual_review):   %s", leads_csv)
    logger.info("   Rejects CSV (reject — audit trail): %s", rejects_csv)
    logger.info("   Total search combinations: %d", total_combinations)
    logger.info("   Headless: %s", headless)
    logger.info("   Auth: %s", auth_status)
    if date_filter:
        logger.info("   Date filter: past %d days", args.days)
    if max_jobs:
        logger.info("   Max jobs per run: %d", max_jobs)
    logger.info("")

    # ── Load skills ONCE ─────────────────────────
    skills = load_skills(profile_pdf)

    # ── Pre-load leads only for pre-detail skips. Existing rejects are rechecked
    #    so gate/config fixes can promote them on later runs.
    seen_urls, stale_reject_urls = _load_detail_skip_state(leads_csv, rejects_csv, logger)

    all_jobs = []
    start_time = time.perf_counter()
    search_time = 0.0
    desc_time = 0.0
    cdp_pace_time = 0.0
    non_cdp_pace_time = 0.0
    gate_route_time = 0.0
    sort_time = 0.0
    duplicate_skips = 0
    fast_triage_skips = 0
    hit_max = False

    # Launch browser with stealth
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    resource_blocked_pages: set[int] = set()
    blocked_resource_counts: dict[str, int] = {}

    async with Stealth().use_async(async_playwright()) as p:
        if use_cdp:
            # Attach to a running, human-cleared browser (DataDome bypass). No warm-up:
            # the session is already on jobs.bg and past the challenge.
            session = await CDPSession(p, endpoint=getattr(args, "cdp_endpoint", CDP_DEFAULT_ENDPOINT)).start()
            page = session.page
            logger.info("🔌 CDP mode — scraping through your cleared browser session.")
            await _maybe_enable_fast_resource_blocking(
                page,
                session,
                fast_triage=fast_triage,
                is_jobsbg=is_jobsbg,
                use_cdp=use_cdp,
                blocked_page_ids=resource_blocked_pages,
                blocked_resource_counts=blocked_resource_counts,
                logger=logger,
            )
        else:
            session = await BrowserSession(p, headless=headless, cookies=cookies).start()
            page = session.page
            await _maybe_enable_fast_resource_blocking(
                page,
                session,
                fast_triage=fast_triage,
                is_jobsbg=is_jobsbg,
                use_cdp=use_cdp,
                blocked_page_ids=resource_blocked_pages,
                blocked_resource_counts=blocked_resource_counts,
                logger=logger,
            )

            # Warm up: visit target site homepage first (looks more natural)
            home_url = "https://www.linkedin.com/" if selected_site == "linkedin" else "https://www.jobs.bg/en/"
            logger.info("🏠 Warming up — visiting %s homepage…", selected_site)
            try:
                await page.goto(home_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
            except Exception as e:
                logger.warning("⚠️  Homepage warm-up failed: %s", str(e)[:100])

        if continuation_mode:
            logger.info("")
            logger.info("=" * 60)
            logger.info("  Jobs.bg continuation")
            logger.info("=" * 60)

            await session.ensure_alive()
            page = session.page
            await _maybe_enable_fast_resource_blocking(
                page,
                session,
                fast_triage=fast_triage,
                is_jobsbg=is_jobsbg,
                use_cdp=use_cdp,
                blocked_page_ids=resource_blocked_pages,
                blocked_resource_counts=blocked_resource_counts,
                logger=logger,
            )

            try:
                t0 = time.perf_counter()
                if single_job_url:
                    basic_jobs = [_build_jobsbg_single_url_job(single_job_url, source="single-url")]
                else:
                    basic_jobs = await _jobsbg_jobs_from_current_tab(page, logger)
                search_time += time.perf_counter() - t0
            except Exception:
                await session.close()
                raise

            if basic_jobs:
                logger.info("  Found %d continuation job(s) - fetching descriptions and matching...", len(basic_jobs))
                continuation_stats = RuntimeStats()
                continuation_ctx = BatchContext(
                    active_scraper=active_scraper,
                    is_jobsbg=is_jobsbg,
                    fetch_descriptions=fetch_descriptions,
                    fast_triage=fast_triage,
                    use_cdp=use_cdp,
                    skills=skills,
                    routing=RoutingState(
                        leads_csv=leads_csv,
                        rejects_csv=rejects_csv,
                        seen_urls=seen_urls,
                        stale_reject_urls=stale_reject_urls,
                        all_jobs=all_jobs,
                    ),
                    stats=continuation_stats,
                    max_jobs=max_jobs,
                    resources=ResourceBlockState(
                        page_ids=resource_blocked_pages,
                        counts=blocked_resource_counts,
                    ),
                    logger=logger,
                )
                hit_max = await _process_job_batch(
                    basic_jobs,
                    page,
                    session,
                    continuation_ctx,
                ) or hit_max
                desc_time += continuation_stats.desc_time
                cdp_pace_time += continuation_stats.cdp_pace_time
                non_cdp_pace_time += continuation_stats.non_cdp_pace_time
                gate_route_time += continuation_stats.gate_route_time
                duplicate_skips += continuation_stats.duplicate_skips
                fast_triage_skips += continuation_stats.fast_triage_skips

            logger.info("  Total collected so far: %d jobs", len(all_jobs))

        # Iterate searches
        combo_num = 0
        for keyword in (keywords if not continuation_mode else []):
            if hit_max:
                break
            for location in LOCATIONS:
                if hit_max:
                    break

                combo_num += 1
                logger.info("")
                logger.info("═" * 60)
                logger.info("  Search %d/%d: \"%s\" → %s", combo_num, total_combinations, keyword, location["name"])
                logger.info("═" * 60)

                # Ensure browser is alive before each search
                await session.ensure_alive()
                page = session.page
                await _maybe_enable_fast_resource_blocking(
                    page,
                    session,
                    fast_triage=fast_triage,
                    is_jobsbg=is_jobsbg,
                    use_cdp=use_cdp,
                    blocked_page_ids=resource_blocked_pages,
                    blocked_resource_counts=blocked_resource_counts,
                    logger=logger,
                )

                # Fetch basic jobs (no descriptions yet)
                t0 = time.perf_counter()
                basic_jobs = await active_scraper.scrape_jobs(
                    page, keyword, location,
                    fetch_descriptions=False,
                    date_filter=date_filter,
                )
                search_time += time.perf_counter() - t0

                if basic_jobs:
                    logger.info("  Found %d jobs - fetching descriptions and matching...", len(basic_jobs))
                    batch_stats = RuntimeStats()
                    batch_ctx = BatchContext(
                        active_scraper=active_scraper,
                        is_jobsbg=is_jobsbg,
                        fetch_descriptions=fetch_descriptions,
                        fast_triage=fast_triage,
                        use_cdp=use_cdp,
                        skills=skills,
                        routing=RoutingState(
                            leads_csv=leads_csv,
                            rejects_csv=rejects_csv,
                            seen_urls=seen_urls,
                            stale_reject_urls=stale_reject_urls,
                            all_jobs=all_jobs,
                        ),
                        stats=batch_stats,
                        max_jobs=max_jobs,
                        resources=ResourceBlockState(
                            page_ids=resource_blocked_pages,
                            counts=blocked_resource_counts,
                        ),
                        logger=logger,
                    )
                    hit_max = await _process_job_batch(
                        basic_jobs,
                        page,
                        session,
                        batch_ctx,
                    ) or hit_max
                    desc_time += batch_stats.desc_time
                    cdp_pace_time += batch_stats.cdp_pace_time
                    non_cdp_pace_time += batch_stats.non_cdp_pace_time
                    gate_route_time += batch_stats.gate_route_time
                    duplicate_skips += batch_stats.duplicate_skips
                    fast_triage_skips += batch_stats.fast_triage_skips

                logger.info("  📊 Total collected so far: %d jobs", len(all_jobs))
        await session.close()

    if all_jobs:
        t0 = time.perf_counter()
        # Single final rank sort (rows were written with sort_after=False to avoid an
        # O(n^2) per-row rewrite during incremental saving).
        sort_leads_csv(leads_csv)
        sort_time = time.perf_counter() - t0

    elapsed = time.perf_counter() - start_time
    logger.info("")
    logger.info("═" * 60)
    logger.info("  SCRAPING COMPLETE")
    logger.info("═" * 60)
    _log_timing_summary(
        logger,
        total_jobs=len(all_jobs),
        elapsed=elapsed,
        search_time=search_time,
        desc_time=desc_time,
        cdp_pace_time=cdp_pace_time,
        non_cdp_pace_time=non_cdp_pace_time,
        gate_route_time=gate_route_time,
        sort_time=sort_time,
        duplicate_skips=duplicate_skips,
        fast_triage_skips=fast_triage_skips,
        blocked_resource_counts=blocked_resource_counts,
    )

    if not all_jobs:
        if duplicate_skips:
            logger.warning(
                "⚠️  No new jobs were routed; %d already-known URL(s) skipped before detail fetch.",
                duplicate_skips,
            )
        else:
            logger.warning("⚠️  No jobs were found in this run.")
        return

    # ── Verdict-based summary (the 3 gates decide the verdict, not the score) ──
    keeps = [j for j in all_jobs if j.get("verdict") == "keep"]
    reviews = [j for j in all_jobs if j.get("verdict") == "manual_review"]
    rejects = [j for j in all_jobs if j.get("verdict") == "reject"]

    logger.info("")
    logger.info("═" * 60)
    logger.info("  VERDICT SUMMARY")
    logger.info("═" * 60)
    logger.info("  ✅ keep:           %d", len(keeps))
    logger.info("  🔍 manual_review:  %d", len(reviews))
    logger.info("  ❌ reject:         %d", len(rejects))
    logger.info("")
    logger.info("  Leads CSV   (keep + manual_review): %s", leads_csv)
    logger.info("  Rejects CSV (audit trail):          %s", rejects_csv)

    # Top leads by rank (leads CSV is already sorted by Rank desc on write).
    leads = keeps + reviews
    if leads:
        leads.sort(key=lambda j: j.get("rank", 0.0), reverse=True)
        logger.info("")
        logger.info("🏆 Top leads by rank:")
        for i, job in enumerate(leads[:10], 1):
            comp = job.get("parsed_comp_eur", "")
            comp_str = f"~€{comp:,}/yr" if isinstance(comp, (int, float)) else "comp undisclosed"
            logger.info(
                "  %d. [rank %.2f | %s] %s @ %s (%s)",
                i,
                job.get("rank", 0.0),
                job.get("verdict", "?"),
                job.get("title", "?"),
                job.get("company", "?"),
                comp_str,
            )
            logger.info("      gates: lane=%s geo=%s ceiling=%s",
                        job.get("gate1_status", "?"),
                        job.get("gate2_status", "?"),
                        job.get("gate3_status", "?"))
            logger.info("      %s", job.get("url", ""))
    else:
        logger.info("")
        logger.info("  No leads passed the 3-gate filter — all postings were rejected.")

    logger.info("")
    logger.info("✅ Done! Open %s to review your leads (sorted by rank).", leads_csv)


# ────────────────────────────────────────
# CLI
# ────────────────────────────────────────

def main():
    if not _acquire_lock():
        print("ERROR: Another scraper instance is already running.")
        print(f"Remove {LOCK_FILE} if no other instance is active, then try again.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="LinkedIn Jobs Scraper — Scrape public job listings with AI profile matching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                 # Default search
  python main.py --quick                         # Skip description fetching
  python main.py --profile my_cv.pdf             # Custom PDF profile
  python main.py --keywords "Data Analyst"       # Override keywords
  python main.py --headless                      # Run without visible browser
  python main.py --days 7                        # Only jobs from past week
  python main.py --max-jobs 50                   # Stop after 50 jobs
  python main.py --site jobs.bg --fast-triage    # Skip detail fetch for obvious title rejects
  python main.py --site jobs.bg --cdp --current-tab  # Continue from open jobs.bg tab
  python main.py --site jobs.bg --cdp --job-url URL  # Process one jobs.bg detail URL
  python main.py --verbose                       # Debug logging
  python main.py --login                         # One-time login (⚠️ RISK)
  python main.py --cookies my_cookies.json       # Use custom cookie file
        """,
    )
    parser.add_argument("--profile", type=str, help="Path to PDF resume/profile for matching")
    parser.add_argument("--quick", action="store_true", help="Skip full description fetching (faster)")
    parser.add_argument("--keywords", nargs="+", type=str, help="Override search keywords")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (no GUI)")
    parser.add_argument("--days", type=int, choices=[1, 7, 30],
                        help="Only show jobs posted within N days (1, 7, or 30)")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="Maximum total jobs to collect (0 = unlimited)")
    parser.add_argument("--fast-triage", action="store_true",
                        help="jobs.bg only: skip full detail fetch for explicit title hard-deny rejects, "
                             "and use light resource blocking on scraper-owned tabs. Default behavior is unchanged.")
    parser.add_argument("--current-tab", action="store_true",
                        help="jobs.bg + --cdp: process the currently reused jobs.bg tab instead of keyword/location search.")
    parser.add_argument("--job-url", type=str,
                        help="jobs.bg: process one job detail URL instead of keyword/location search.")
    parser.add_argument("--site", type=str, choices=["linkedin", "jobs.bg"],
                        help="Select the target site to scrape (linkedin or jobs.bg)")
    parser.add_argument("--output", type=str, help="Override output CSV filename")
    parser.add_argument("--log-file", type=str, default="scraper.log", help="Path to runtime log file")
    parser.add_argument("--login", action="store_true",
                        help="One-time login mode: opens browser for you to sign in, then saves cookies. "
                             "⚠️ USE AT YOUR OWN RISK: May lead to account suspension/ban.")
    parser.add_argument("--cookies", type=str,
                        help="Path to a JSON cookie file for authenticated scraping")
    parser.add_argument("--cdp", action="store_true",
                        help="Attach to a running browser over CDP (default %s) instead of "
                             "launching one. Use for jobs.bg: open your real browser with "
                             "--remote-debugging-port=9222, solve DataDome once, then scrape "
                             "through that cleared session." % CDP_DEFAULT_ENDPOINT)
    parser.add_argument("--cdp-endpoint", type=str, default=CDP_DEFAULT_ENDPOINT,
                        help="CDP endpoint to attach to (default: %(default)s)")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
