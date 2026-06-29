"""
Jobs.bg Scraper Engine (v3.0)
Implements identically signatured scraping functions to integrate cleanly with main.py.
"""

import asyncio
import random
import re
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup
from scraper import human_delay, MIN_DELAY, MAX_DELAY

from config import MAX_PAGES_PER_SEARCH, MAX_RETRIES_ON_BLOCK

logger = logging.getLogger(__name__)

RESULTS_PER_PAGE = 15

DEBUG_HTML_DIR = "debug_html"

# Detail-page DOM selectors for structured location extraction (USER DECISION #3).
# Ordered most-specific -> most-general; first plausible non-empty hit wins.
# Each is guarded by count() > 0 and read via inner_text(). Derived defensively from
# the known card DOM (mdc-card, secondary-text) and the job-view containers already
# read by fetch_job_description; confirm/adjust against a real detail page captured
# via _dump_debug_html(). The gate's body keyword scan is the safety net if all miss.
GEO_DETAIL_SELECTORS = [
    # 1. Explicit location anchor (jobs.bg links location to a town search)
    "a[href*='location_sid']",
    "a[href*='towns']",
    # 2. Structured info rows in the left/header column
    ".job-view-location",
    ".mdc-card .location",
    "#jobView .location",
    "span.location",
    # 3. Labelled info row: a secondary-text node carrying the place
    ".job-view-left-column .secondary-text",
    "#jobViewContent .secondary-text",
    # 4. The address / map block jobs.bg renders for office roles
    ".job-view-address",
    "[itemprop='jobLocation']",
    "[itemprop='addressLocality']",
]


# Precompiled once (looks_like_sentence is called per-selector in a loop).
_SENTENCE_MIDDLE = re.compile(r"[.!?]\s+[A-ZА-Я]")
_SENTENCE_END = re.compile(r"[.!?]$")


def looks_like_sentence(text: str) -> bool:
    """Reject description paragraphs masquerading as a location string.

    Returns True when the text reads like prose rather than a short place
    label: more than 8 words, or sentence punctuation (a period/!/? followed
    by a space and a capital letter, or a trailing terminal punctuation mark).
    """
    if not text:
        return False
    if len(text.split()) > 8:
        return True
    if _SENTENCE_MIDDLE.search(text):
        return True
    if _SENTENCE_END.search(text.strip()):
        return True
    return False


def build_jobsbg_search_url(keyword: str, location: dict, offset: int = 0) -> str:
    """Build the Jobs.bg search results URL."""
    params = {
        "subm": "1",
    }

    if offset > 0:
        params["from"] = str(offset)

    query = urlencode(params)
    query += f"&keywords%5B%5D={quote_plus(keyword)}"

    loc_id = location.get("jobsbg_location_id")
    if loc_id:
        query += f"&location_sid={loc_id}"

    return f"https://www.jobs.bg/en/front_job_search.php?{query}"


def parse_jobsbg_cards(html: str) -> list[dict]:
    """Parse job cards from jobs.bg DOM."""
    soup = BeautifulSoup(html, "lxml")
    jobs = []

    cards = soup.find_all("div", class_="mdc-card")
    for card in cards:
        job = {}

        title_el = card.find("a", class_="black-link-b")
        if title_el:
            raw_title = title_el.get("title", "") or title_el.get_text(strip=True)
            if raw_title:
                raw_title = str(raw_title).strip()
            job["title"] = raw_title.replace("star", "").strip() if raw_title else ""
            href = title_el.get("href", "")
            if href:
                href = str(href)
                if href.startswith("/"):
                    href = f"https://www.jobs.bg{href}"
                elif href.startswith("front_job_search.php?") or href.startswith("job/"):
                    href = f"https://www.jobs.bg/en/{href}"
                job["url"] = href

        company_elem = card.find("a", href=lambda h: h and "/company/" in h)
        if company_elem:
            company_text = company_elem.get("title") or company_elem.get_text(strip=True)
            job["company"] = str(company_text) if company_text else ""
        else:
            company_fallback = card.find("div", class_="secondary-text")
            if company_fallback:
                job["company"] = company_fallback.get_text(strip=True)

        # Date extraction
        time_el = card.find("div", class_="secondary-text", text=re.compile(r"\d{2}\.\d{2}\.\d{4}"))
        if not time_el:
            # Fallback for relative dates like "today" or "yesterday"
            time_el = card.find("div", class_="secondary-text")
        
        if time_el:
            date_text = time_el.get_text(strip=True)
            # Clean up: e.g. "20.03.2026, Ref.No:Ps_1" -> "20.03.2026"
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_text)
            if match:
                job["date"] = match.group(1)
            else:
                job["date"] = date_text

        if job.get("title") and job.get("url"):
            jobs.append(job)

    return jobs


# Markers that identify an anti-bot interstitial. jobs.bg fronts with DataDome,
# which serves TWO block variants: the interactive "Just a moment…"/"Проверка"
# challenge (detectable by title) AND a static JS-gate page whose <title> is the
# innocuous "jobs.bg" — that one is only detectable by body content. We check both.
BLOCK_TITLE_MARKERS = ("Just a moment", "Проверка")
BLOCK_CONTENT_MARKERS = (
    "captcha-delivery.com",          # DataDome challenge host
    "geo.captcha-delivery.com",
    "Please enable JS",              # static JS-gate block page text
    'id="cmsg"',                     # the block page's message element
    "disable any ad blocker",
)

# How long to give the user to solve a challenge by hand (seconds).
CAPTCHA_SOLVE_TIMEOUT = 120


# A genuine DataDome block page is tiny (~1.5 KB) and carries no real content.
# A successfully-loaded jobs.bg page can STILL contain a "captcha-delivery.com"
# script reference (DataDome's client JS rides along on cleared pages), so a bare
# marker match is a false positive. Treat a page as blocked only when a marker is
# present AND there is no real content (no job cards, tiny body).
_BLOCK_MAX_CONTENT_LEN = 20000  # real list/detail pages are 80KB–450KB


async def _is_blocked_page(page) -> bool:
    """True only if the page is an actual DataDome block — marker present AND no
    real content. The interactive challenge (title "Just a moment…") is always a
    block; the static JS-gate is a block only when no job content rendered."""
    try:
        title = await page.title()
    except Exception:
        title = ""
    # The interactive challenge title is unambiguous — always a block.
    if any(m in title for m in BLOCK_TITLE_MARKERS):
        return True
    try:
        html = await page.content()
    except Exception:
        return False
    if not any(m in html for m in BLOCK_CONTENT_MARKERS):
        return False
    # Marker present: confirm it's a real block, not just lingering DataDome JS on
    # a page that actually loaded. Real content (job cards or a large body) clears it.
    has_cards = "mdc-card" in html
    looks_substantial = len(html) > _BLOCK_MAX_CONTENT_LEN
    return not (has_cards or looks_substantial)


async def _wait_for_captcha(page) -> bool:
    """If the page is blocked, pause for the user to solve it in the visible window.

    Detects BOTH the title-based challenge and the static JS-gate block page
    (title "jobs.bg" + DataDome content markers). Returns True once the block
    clears, False on timeout.
    """
    if not await _is_blocked_page(page):
        return True

    logger.warning("  🛑 JOBS.BG ANTI-BOT BLOCK DETECTED (DataDome).")
    logger.warning("  👉 Solve the challenge in the browser window. Waiting up to %ds…", CAPTCHA_SOLVE_TIMEOUT)
    for _ in range(CAPTCHA_SOLVE_TIMEOUT // 2):
        await asyncio.sleep(2)
        if not await _is_blocked_page(page):
            logger.info("  ✅ Block cleared! Resuming scrape…")
            await human_delay(2, 4)
            return True

    logger.error("  ❌ Block not cleared within %ds — skipping this search.", CAPTCHA_SOLVE_TIMEOUT)
    return False


def _dump_debug_html(html: str, label: str):
    """Save HTML to debug_html/ directory for inspection when scraping fails."""
    os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)[:80]
    filename = os.path.join(DEBUG_HTML_DIR, f"{safe_label}.html")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("  📄 Debug HTML saved to %s", filename)
    except Exception as e:
        logger.debug("  Could not save debug HTML: %s", e)


async def scrape_jobs(page, keyword: str, location: dict,
                      fetch_descriptions: bool = True,
                      date_filter: str = "",
                      experience_filters: list[str] | None = None) -> list[dict]:
    """
    Matches the LinkedIn scraper API.
    Iterates over pages in Jobs.bg and grabs metadata.
    Includes retry logic for transient failures.
    """
    search_label = f'"{keyword}" in {location["name"]}'
    logger.info("🔍 Searching Jobs.bg: %s", search_label)

    all_jobs: list[dict] = []

    for page_num in range(MAX_PAGES_PER_SEARCH):
        offset = page_num * RESULTS_PER_PAGE
        url = build_jobsbg_search_url(keyword, location, offset)
        logger.info("  🌐 Page %d — %s", page_num + 1, url[:120])

        retries = 0
        page_loaded = False

        while retries < MAX_RETRIES_ON_BLOCK and not page_loaded:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await human_delay(2, 4)

                captcha_solved = await _wait_for_captcha(page)
                if not captcha_solved:
                    return all_jobs

                try:
                    cookie_btn = page.locator('button:has-text("ACCEPT"), button.mdc-button--raised').first
                    if await cookie_btn.is_visible(timeout=1000):
                        await cookie_btn.click()
                except Exception:
                    logger.debug("  ⚠️  No cookie consent button found")

                html = await page.content()
                jobs = parse_jobsbg_cards(html)

                if not jobs:
                    search_label_safe = f"{keyword}_{location['name']}_page{page_num+1}"
                    _dump_debug_html(html, search_label_safe)
                    logger.info("  📭 No more job cards found.")
                    return all_jobs

                # Client-side date filtering
                filtered_jobs = []
                if date_filter:
                    # In main.py, date_filter for 30 days is "r2592000" (seconds)
                    days_limit = 30
                    if date_filter == "r86400": days_limit = 1
                    elif date_filter == "r604800": days_limit = 7
                    elif date_filter == "r2592000": days_limit = 30
                    
                    limit_date = datetime.now() - timedelta(days=days_limit)
                    
                    for j in jobs:
                        job_date_str = j.get("date", "")
                        try:
                            # Parse "20.03.2026"
                            job_date = datetime.strptime(job_date_str, "%d.%m.%Y")
                            if job_date >= limit_date:
                                filtered_jobs.append(j)
                        except Exception:
                            # If date is relative like "today", keep it
                            if any(word in job_date_str.lower() for word in ["today", "yesterday", "min", "hour"]):
                                filtered_jobs.append(j)
                            else:
                                filtered_jobs.append(j)
                else:
                    filtered_jobs = jobs

                if not filtered_jobs and jobs:
                    logger.info("  📭 Jobs found but all were older than the date filter (%s).", date_filter)
                    return all_jobs

                logger.info("  ✅ Found %d jobs on page %d", len(filtered_jobs), page_num + 1)

                for j in filtered_jobs:
                    j["search_keyword"] = keyword
                    j["search_location"] = location["name"]

                all_jobs.extend(filtered_jobs)

                if len(jobs) < RESULTS_PER_PAGE:
                    logger.info("  📭 Reached end of total results.")
                    return all_jobs

                page_loaded = True

            except Exception as e:
                retries += 1
                logger.warning("  ⚠️  Error fetching page %d (attempt %d/%d): %s",
                               page_num + 1, retries, MAX_RETRIES_ON_BLOCK, str(e)[:100])
                if retries < MAX_RETRIES_ON_BLOCK:
                    await human_delay(5, 10)

        if not page_loaded:
            logger.error("  ❌ Failed to load page %d after %d retries.", page_num + 1, MAX_RETRIES_ON_BLOCK)
            break

    if not all_jobs:
        logger.warning("  ⚠️  No jobs found for: %s", search_label)

    return all_jobs


async def _extract_detail_location(page, job_url: str) -> str:
    """Pull a structured location string from the jobs.bg detail-page DOM.

    Loops GEO_DETAIL_SELECTORS (first plausible hit wins); each guarded by
    count() > 0 and read via inner_text(). Accepts only short, non-prose
    strings (<= 60 chars, not looks_like_sentence) so a description paragraph
    is never mistaken for a location. Returns "" if nothing usable is found,
    in which case the gate falls back to its body keyword scan -> soft.
    """
    for sel in GEO_DETAIL_SELECTORS:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                t = (await el.first.inner_text()).strip()
                if t and len(t) <= 60 and not looks_like_sentence(t):
                    return t
        except Exception:
            logger.debug("  ⚠️  Geo selector %s failed on %s", sel, job_url[:60])
    return ""


async def fetch_job_description(page, job_url: str, job: dict | None = None) -> str:
    """Navigate to a jobs.bg job detail page and extract the full description.

    USER DECISION #3 (non-breaking, Option B): if a ``job`` dict is supplied and
    it has no location yet, attach a structured location pulled from the
    detail-page DOM (job['location']). The return type stays ``str`` (the
    description) so this scraper remains identically signatured with the
    LinkedIn fetcher; the optional ``job`` mutation is the only added behavior.
    The ``not job.get('location')`` guard means we only fill a blank — a
    location already set by a card parser (LinkedIn) is never clobbered.
    """
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
        await human_delay(1.5, 3)

        text_blocks = []

        left_col = page.locator(".job-view-left-column")
        if await left_col.count() > 0:
            text = await left_col.first.inner_text()
            if text and len(text.strip()) > 50:
                text_blocks.append(text.strip())

        main_content = page.locator("#jobViewContent")
        if await main_content.count() > 0:
            text = await main_content.first.inner_text()
            if text and len(text.strip()) > 50:
                text_blocks.append(text.strip())

        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    f_text = await frame.inner_text("body", timeout=1000)
                    if f_text and len(f_text.strip()) > 50:
                        text_blocks.append(f_text.strip())
                except Exception:
                    logger.debug("  ⚠️  Could not extract text from iframe on %s", job_url[:60])

        # USER DECISION #3 — structured location from the detail-page DOM.
        # Only fill a blank location; never clobber a value the caller already set.
        if job is not None and not job.get("location"):
            location_text = await _extract_detail_location(page, job_url)
            if location_text:
                job["location"] = location_text
                logger.debug("  📍 jobs.bg detail location: %s", location_text)

        final_text = "\n\n".join(text_blocks)
        if final_text:
            return final_text.strip()

    except Exception as e:
        logger.warning("  ⚠️  Failed to fetch jobs.bg description %s: %s", job_url[:80], str(e)[:100])

    return ""
