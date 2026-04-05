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
    python main.py --login                  # One-time login to save session cookies
    python main.py --cookies cookies.json   # Load cookies from a JSON file
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import os
import atexit

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from config import KEYWORDS, LOCATIONS, PROFILE_PDF, OUTPUT_CSV, MAX_JOBS_PER_RUN, TARGET_SITE
from stealth_config import get_launch_options, get_context_options, get_random_user_agent, apply_stealth_to_page
from scraper import human_delay
from csv_export import export_to_csv
from profile_matcher import match_jobs, load_skills

import scraper as linkedin_scraper
import jobsbg_scraper

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
                old_pid = f.read().strip()
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

    def __init__(self, playwright, headless: bool = False, cookies: list[dict] | None = None):
        self._playwright = playwright
        self._headless = headless
        self._context = None
        self._page = None
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
    """Open browser for one-time manual LinkedIn login, then save cookies."""
    async with Stealth().use_async(async_playwright()) as p:
        session = await BrowserSession(p, headless=headless).start()
        page = session.page

        logging.info("🔐 Login mode — navigating to LinkedIn login page…")
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

    selected_site = args.site if args.site else TARGET_SITE

    # jobs.bg uses DataDome captcha which blocks headless browsers
    if "jobs" in selected_site.lower() and headless:
        logger.warning("⚠️  jobs.bg requires a visible browser to bypass DataDome captcha.")
        logger.warning("   Forcing non-headless mode...")
        headless = False

    if getattr(args, "output", None):
        output_csv = args.output
        active_scraper = jobsbg_scraper if selected_site == "jobs.bg" else linkedin_scraper
    elif selected_site == "jobs.bg":
        active_scraper = jobsbg_scraper
        output_csv = "Jobs.bg-leads.csv"
    else:
        active_scraper = linkedin_scraper
        output_csv = OUTPUT_CSV

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

    total_combinations = len(keywords) * len(LOCATIONS)
    logger.info("🚀 Starting scraper")
    logger.info("   Keywords: %s", keywords)
    logger.info("   Locations: %s", [loc["name"] for loc in LOCATIONS])
    logger.info("   Work types: Remote + Hybrid")
    logger.info("   Fetch descriptions: %s (Real-Time)", fetch_descriptions)
    logger.info("   Target Site: %s", selected_site)
    logger.info("   Profile PDF: %s", profile_pdf)
    logger.info("   Output: %s", output_csv)
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

    all_jobs = []
    start_time = time.time()
    search_time = 0.0
    desc_time = 0.0
    hit_max = False

    # Launch browser with stealth
    async with Stealth().use_async(async_playwright()) as p:
        session = await BrowserSession(p, headless=headless, cookies=cookies).start()
        page = session.page

        # Warm up: visit target site homepage first (looks more natural)
        home_url = "https://www.linkedin.com/" if selected_site == "linkedin" else "https://www.jobs.bg/en/"
        logger.info("🏠 Warming up — visiting %s homepage…", selected_site)
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning("⚠️  Homepage warm-up failed: %s", str(e)[:100])

        # Iterate searches
        combo_num = 0
        for keyword in keywords:
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

                # Fetch basic jobs (no descriptions yet)
                t0 = time.time()
                basic_jobs = await active_scraper.scrape_jobs(
                    page, keyword, location,
                    fetch_descriptions=False,
                    date_filter=date_filter,
                )
                search_time += time.time() - t0

                if basic_jobs:
                    logger.info("  📖 Found %d jobs — fetching descriptions and matching…", len(basic_jobs))

                    # Progress bar for description fetching
                    job_iter = basic_jobs
                    if HAS_TQDM and fetch_descriptions:
                        job_iter = tqdm(
                            basic_jobs,
                            desc="  Fetching descriptions",
                            unit="job",
                            leave=False,
                            ncols=80,
                        )

                    for i, job in enumerate(job_iter):
                        # Check max jobs limit
                        if max_jobs and len(all_jobs) >= max_jobs:
                            logger.info("  🛑 Reached max jobs limit (%d). Stopping.", max_jobs)
                            hit_max = True
                            break

                        if not HAS_TQDM:
                            logger.info("    [%d/%d] %s", i + 1, len(basic_jobs), job.get("title", "Unknown"))

                        # Ensure browser is alive before each description fetch
                        recovered = await session.ensure_alive()
                        if recovered:
                            page = session.page

                        if fetch_descriptions and job.get("url"):
                            t0 = time.time()
                            job["description"] = await active_scraper.fetch_job_description(page, job["url"])
                            desc_time += time.time() - t0
                            await human_delay(2, 5)
                        else:
                            job["description"] = ""

                        # Match and save immediately (Real-Time Saving)
                        matched_job = match_jobs([job], skills=skills)[0]
                        new_count = export_to_csv([matched_job], output_csv)

                        all_jobs.append(matched_job)
                        if not HAS_TQDM:
                            if new_count > 0:
                                logger.info("      ✅ Saved to %s", output_csv)
                            else:
                                logger.info("      📎 Duplicate skipped")

                    # Close tqdm bar if used
                    if HAS_TQDM and hasattr(job_iter, 'close'):
                        job_iter.close()

                logger.info("  📊 Total collected so far: %d jobs", len(all_jobs))


        await session.close()

    elapsed = time.time() - start_time
    logger.info("")
    logger.info("═" * 60)
    logger.info("  SCRAPING COMPLETE")
    logger.info("═" * 60)
    logger.info("  Total jobs found:     %d", len(all_jobs))
    logger.info("  Time elapsed:         %.1f minutes", elapsed / 60)
    logger.info("  ├─ Search phase:      %.1f minutes", search_time / 60)
    logger.info("  └─ Description phase: %.1f minutes", desc_time / 60)

    if not all_jobs:
        logger.warning("⚠️  No jobs were found in this run.")
        return

    # Print summary
    logger.info("")
    logger.info("═" * 60)
    logger.info("  MATCH SUMMARY")
    logger.info("═" * 60)
    logger.info("  Output file:          %s", output_csv)

    # Show top matches across the whole run
    matched = [j for j in all_jobs if j.get("match_score", 0) >= 35]

    if matched:
        matched.sort(key=lambda j: j.get("match_score", 0), reverse=True)
        logger.info("")
        logger.info("🏆 Top Matches (score ≥ %d%%):", 35)
        for i, job in enumerate(matched[:10], 1):
            skills_preview = ", ".join(job.get("matched_skills", [])[:5])
            logger.info("  %d. [%.0f%%] %s @ %s",
                        i, job["match_score"], job.get("title", "?"), job.get("company", "?"))
            if skills_preview:
                logger.info("      Skills: %s", skills_preview)
            logger.info("      %s", job.get("url", ""))
    else:
        logger.info("  No jobs exceeded the match threshold.")

    logger.info("")
    logger.info("✅ Done! Open %s to review your leads.", output_csv)


# ────────────────────────────────────────
# CLI
# ────────────────────────────────────────

def main():
    if not _acquire_lock():
        print(f"ERROR: Another scraper instance is already running.")
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
  python main.py --verbose                       # Debug logging
  python main.py --login                         # One-time login to save cookies
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
    parser.add_argument("--site", type=str, choices=["linkedin", "jobs.bg"],
                        help="Select the target site to scrape (linkedin or jobs.bg)")
    parser.add_argument("--output", type=str, help="Override output CSV filename")
    parser.add_argument("--log-file", type=str, default="scraper.log", help="Path to runtime log file")
    parser.add_argument("--login", action="store_true",
                        help="One-time login mode: opens browser for you to sign in, then saves cookies")
    parser.add_argument("--cookies", type=str,
                        help="Path to a JSON cookie file for authenticated scraping")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
