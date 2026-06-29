"""
LinkedIn Jobs Scraper — Core Scraping Engine (v3.0)
Dual strategy: Guest API (primary) + Full browser scrape (fallback).
Includes anti-blocking logic, URL validation, and content sanitization.
"""

import asyncio
import random
import re
import logging
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup

from config import (
    WORK_TYPE_FILTERS,
    DATE_POSTED_FILTER,
    EXPERIENCE_LEVEL_FILTERS,
    RESULTS_PER_PAGE,
    MAX_PAGES_PER_SEARCH,
    MIN_DELAY,
    MAX_DELAY,
    SCROLL_MIN_DELAY,
    SCROLL_MAX_DELAY,
    SIGN_IN_MODAL_WAIT,
    MAX_RETRIES_ON_BLOCK,
    SIGN_IN_MODAL_MARKERS,
)

logger = logging.getLogger(__name__)

JOB_URL_PATTERN = re.compile(r"https?://[a-z]{0,3}\.?linkedin\.com/jobs/view/")


async def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY):
    """Sleep for a random duration to mimic human behaviour."""
    delay = random.uniform(min_s, max_s)
    logger.debug(f"  ⏳ Waiting {delay:.1f}s …")
    await asyncio.sleep(delay)


def is_valid_job_url(url: str) -> bool:
    """Check if a URL is a valid LinkedIn job detail page."""
    if not url:
        return False
    return bool(JOB_URL_PATTERN.match(url))


def is_modal_garbage(text: str) -> bool:
    """Check if scraped text is actually sign-in modal / login prompt garbage."""
    if not text:
        return False
    for marker in SIGN_IN_MODAL_MARKERS:
        if marker.lower() in text.lower():
            return True
    return False


def build_api_url(keyword: str, location: dict, start: int = 0,
                  date_filter: str = "", experience_filters: list[str] | None = None) -> str:
    """Build a LinkedIn Guest API URL."""
    params = {
        "keywords": keyword,
        "location": location["location_text"],
        "geoId": location["geoId"],
        "f_WT": ",".join(WORK_TYPE_FILTERS),
        "start": str(start),
        "sortBy": "DD",
    }

    effective_date = date_filter or DATE_POSTED_FILTER
    if effective_date:
        params["f_TPR"] = effective_date

    effective_exp = experience_filters or EXPERIENCE_LEVEL_FILTERS
    if effective_exp:
        params["f_E"] = ",".join(effective_exp)

    base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    return f"{base}?{urlencode(params, quote_via=quote_plus)}"


def build_search_url(keyword: str, location: dict,
                     date_filter: str = "", experience_filters: list[str] | None = None) -> str:
    """Build a full LinkedIn search page URL (for browser fallback)."""
    params = {
        "keywords": keyword,
        "location": location["location_text"],
        "geoId": location["geoId"],
        "f_WT": ",".join(WORK_TYPE_FILTERS),
        "sortBy": "DD",
    }

    effective_date = date_filter or DATE_POSTED_FILTER
    if effective_date:
        params["f_TPR"] = effective_date

    effective_exp = experience_filters or EXPERIENCE_LEVEL_FILTERS
    if effective_exp:
        params["f_E"] = ",".join(effective_exp)

    base = "https://www.linkedin.com/jobs/search/"
    return f"{base}?{urlencode(params, quote_via=quote_plus)}"


async def dismiss_sign_in_modal(page) -> bool:
    """Detect and dismiss LinkedIn sign-in modals / interstitials."""
    dismiss_selectors = [
        'button[data-tracking-control-name="public_jobs_contextual-sign-in-modal_modal_dismiss"]',
        'button[aria-label="Dismiss"]',
        '.modal__dismiss',
        '.contextual-sign-in-modal__modal-dismiss-btn',
        'button.modal__dismiss',
        '[data-tracking-control-name="public_jobs_contextual-sign-in-modal-cta_sign-in_sign-in"]',
        'button[data-test-modal-close-btn]',
        '.cta-modal__dismiss-btn',
    ]

    modal_visible = False
    try:
        modal_visible = await page.locator('.modal__overlay--visible, .contextual-sign-in-modal, [data-modal="true"]').first.is_visible(timeout=2000)
    except Exception:
        logger.debug("  ⚠️  Failed to check modal overlay visibility")

    if not modal_visible:
        return False

    logger.info("  🔐 Sign-in modal detected — waiting %ds before dismissing…", SIGN_IN_MODAL_WAIT)
    await asyncio.sleep(SIGN_IN_MODAL_WAIT)

    for selector in dismiss_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                logger.info("  ✅ Dismissed modal via: %s", selector)
                await asyncio.sleep(1)
                return True
        except Exception:
            continue

    try:
        await page.keyboard.press("Escape")
        logger.info("  ✅ Dismissed modal via Escape key")
        await asyncio.sleep(1)
        return True
    except Exception:
        logger.debug("  ⚠️  Failed to press Escape key for modal dismissal")

    return False


def parse_job_cards_from_html(html: str) -> list[dict]:
    """Parse job card data from the Guest API HTML fragment.
    Extracts: title, company, location, date, url, salary,
              seniority, employment_type, industry, job_function, applicants_count.
    """
    soup = BeautifulSoup(html, "lxml")
    jobs = []

    cards = soup.find_all("div", class_=re.compile(r"base-card|base-search-card|job-search-card"))
    if not cards:
        cards = soup.find_all("li")

    for card in cards:
        job = {}

        title_el = card.find("h3", class_=re.compile(r"base-search-card__title"))
        if not title_el:
            title_el = card.find("a", class_=re.compile(r"base-card__full-link"))
        if title_el:
            job["title"] = title_el.get_text(strip=True)

        company_el = card.find("h4", class_=re.compile(r"base-search-card__subtitle"))
        if not company_el:
            company_el = card.find("a", class_=re.compile(r"hidden-nested-link"))
        if company_el:
            job["company"] = company_el.get_text(strip=True)

        loc_el = card.find("span", class_=re.compile(r"job-search-card__location"))
        if loc_el:
            job["location"] = loc_el.get_text(strip=True)

        time_el = card.find("time")
        if time_el:
            job["date"] = str(time_el.get("datetime", time_el.get_text(strip=True)))

        link_el = card.find("a", class_=re.compile(r"base-card__full-link"))
        if not link_el:
            all_links = card.find_all("a", href=True)
            for a in all_links:
                href = a.get("href", "")
                if href and "/jobs/view/" in href:
                    link_el = a
                    break

        if link_el:
            href = str(link_el.get("href", "") or "")
            if href:
                if "?" in href:
                    href = href.split("?")[0]
                if is_valid_job_url(href):
                    job["url"] = href
                else:
                    logger.debug("  🚫 Skipped non-job URL: %s", href[:80])

        salary_el = card.find("span", class_=re.compile(r"job-search-card__salary-info"))
        if salary_el:
            job["salary"] = salary_el.get_text(strip=True)

        metadata_div = card.find("div", class_=re.compile(r"base-search-card__metadata"))
        if not metadata_div:
            metadata_div = card.find("div", class_=re.compile(r"job-search-card__list"))
        if metadata_div:
            list_items = metadata_div.find_all("li")
            for li in list_items:
                text = li.get_text(strip=True)
                if any(kw in text.lower() for kw in ["seniority", "entry", "mid-senior", "associate", "director", "intern", "executive"]):
                    job["seniority"] = text
                elif any(kw in text.lower() for kw in ["full-time", "part-time", "contract", "temporary", "volunteer", "internship"]):
                    job["employment_type"] = text

        for badge in card.find_all("span", class_=re.compile(r"badge")):
            text = badge.get_text(strip=True).lower()
            if "applicants" in text or "applicant" in text:
                job["applicants_count"] = re.sub(r"[^\d]", "", text)

        footer = card.find("div", class_=re.compile(r"base-search-card__footer"))
        if footer:
            posted_text = footer.get_text(strip=True)
            if "ago" in posted_text and "date" not in job:
                job["date"] = posted_text

        if job.get("title"):
            jobs.append(job)

    return jobs


def extract_job_id_from_url(job_url: str) -> str | None:
    """Extract the numeric job ID from a LinkedIn job URL."""
    match = re.search(r"/jobs/view/(\d+)", job_url)
    return match.group(1) if match else None


def build_job_detail_api_url(job_url: str) -> str | None:
    """Build the Guest API URL for a specific job's full description."""
    job_id = extract_job_id_from_url(job_url)
    if not job_id:
        return None
    return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"


def parse_job_detail_from_html(html: str, job_url: str) -> str:
    """Parse the full job description from the Guest API job detail HTML."""
    soup = BeautifulSoup(html, "lxml")

    desc_el = soup.find("div", class_=re.compile(r"show-more-less-html__markup"))
    if desc_el:
        text = desc_el.get_text(separator="\n", strip=True)
        if text and len(text.strip()) > 50:
            return text.strip()

    desc_el = soup.find("div", class_=re.compile(r"description__text"))
    if desc_el:
        text = desc_el.get_text(separator="\n", strip=True)
        if text and len(text.strip()) > 50:
            return text.strip()

    for sel in [".decorated-job-posting__details", ".jobs-description__content", "article"]:
        desc_el = soup.select_one(sel)
        if desc_el:
            text = desc_el.get_text(separator="\n", strip=True)
            if text and len(text.strip()) > 50:
                clean = text.strip()
                if not is_modal_garbage(clean):
                    return clean

    return ""


async def fetch_job_description(page, job_url: str) -> str:
    """Fetch full job description via the Guest API detail endpoint.
    This is the most reliable method — it returns the complete HTML description
    without needing to navigate pages, dismiss modals, or be logged in.
    Falls back to browser-based extraction if the API fails.
    """
    if not is_valid_job_url(job_url):
        logger.debug("  🚫 Skipping invalid URL for description: %s", job_url[:80])
        return ""

    # Primary: Guest API detail endpoint
    detail_api_url = build_job_detail_api_url(job_url)
    if detail_api_url:
        try:
            response = await page.goto(detail_api_url, wait_until="domcontentloaded", timeout=10000)
            if response and response.status == 200:
                html = await page.content()
                if html and len(html.strip()) > 200:
                    desc = parse_job_detail_from_html(html, job_url)
                    if desc and len(desc) > 50 and not is_modal_garbage(desc):
                        logger.debug("  ✅ Got description via Guest API (%d chars)", len(desc))
                        return desc
                    elif desc:
                        logger.debug("  🚫 Guest API description was modal garbage or too short")
                else:
                    logger.debug("  📭 Guest API returned empty response")
            elif response:
                logger.debug("  ⚠️  Guest API returned HTTP %d", response.status)
        except Exception as e:
            logger.debug("  ⚠️  Guest API detail fetch failed: %s", str(e)[:100])

        # Navigate back to prepare for browser fallback
        try:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
        except Exception:
            pass

    # Fallback: Browser-based extraction
    return await _fetch_job_description_via_browser(page, job_url)


async def _fetch_job_description_via_browser(page, job_url: str) -> str:
    """Fallback: Navigate to job page in browser and extract description."""
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
        await human_delay(2, 4)

        await dismiss_sign_in_modal(page)

        try:
            show_more = page.locator('button[aria-label="Show more"], button.show-more-less-html__button--more')
            if await show_more.first.is_visible(timeout=2000):
                await show_more.first.click()
                await asyncio.sleep(1)
        except Exception:
            logger.debug("  ⚠️  Could not click 'Show more' button")

        desc_selectors = [
            ".show-more-less-html__markup",
            ".description__text",
            ".decorated-job-posting__details",
            'div[class*="description"]',
        ]
        for sel in desc_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    text = await el.inner_text()
                    if text and len(text.strip()) > 50:
                        clean_text = text.strip()
                        if is_modal_garbage(clean_text):
                            logger.debug("  🚫 Description was modal garbage — discarding")
                            return ""
                        return clean_text
            except Exception:
                continue

        try:
            text = await page.evaluate("""() => {
                const selectors = [
                    '.show-more-less-html__markup',
                    '.description__text',
                    '.decorated-job-posting__details',
                    '[class*="description"]',
                    '.jobs-description',
                    '.job-details',
                    'article'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim().length > 50) {
                        return el.innerText.trim();
                    }
                }
                return '';
            }""")
            if text and len(text.strip()) > 50:
                clean_text = text.strip()
                if is_modal_garbage(clean_text):
                    logger.debug("  🚫 JS-extracted description was modal garbage — discarding")
                    return ""
                return clean_text
        except Exception:
            logger.debug("  ⚠️  JS extraction failed")

        try:
            body_text = await page.locator("main").first.inner_text()
            if body_text:
                clean_text = body_text.strip()[:3000]
                if is_modal_garbage(clean_text):
                    logger.debug("  🚫 Fallback description was modal garbage — discarding")
                    return ""
                return clean_text
            return ""
        except Exception:
            return ""

    except Exception as e:
        logger.warning("  ⚠️  Failed to fetch description from %s: %s", job_url[:80], str(e)[:100])
        return ""


async def scrape_via_api(page, keyword: str, location: dict,
                         date_filter: str = "", experience_filters: list[str] | None = None) -> list[dict]:
    """Primary strategy: Fetch job listings from the Guest API endpoint."""
    all_jobs = []
    retries = 0
    consecutive_empty = 0

    for page_num in range(MAX_PAGES_PER_SEARCH):
        start = page_num * RESULTS_PER_PAGE
        url = build_api_url(keyword, location, start, date_filter, experience_filters)
        logger.info("  📡 API page %d — %s", page_num + 1, url[:120])

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)

            if response and response.status == 429:
                logger.warning("  ⚠️  Rate limited (429). Backing off…")
                await asyncio.sleep(random.uniform(15, 30))
                retries += 1
                if retries >= MAX_RETRIES_ON_BLOCK:
                    logger.error("  ❌ Max retries reached. Skipping.")
                    break
                continue

            if response and response.status not in (200, 204):
                logger.warning("  ⚠️  HTTP %d — stopping pagination.", response.status)
                break

            html = await page.content()

            if not html or len(html.strip()) < 100:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    logger.info("  📭 Empty responses — no more results.")
                    break
                logger.info("  📭 Empty response on page %d — trying next page.", page_num + 1)
                await human_delay()
                continue

            consecutive_empty = 0
            jobs = parse_job_cards_from_html(html)
            if not jobs:
                logger.info("  📭 No job cards found — end of results.")
                break

            logger.info("  ✅ Found %d jobs on page %d", len(jobs), page_num + 1)
            all_jobs.extend(jobs)

            await human_delay()

        except Exception as e:
            logger.error("  ❌ Error on API page %d: %s", page_num + 1, str(e)[:150])
            retries += 1
            if retries >= MAX_RETRIES_ON_BLOCK:
                break
            await human_delay(5, 10)

    return all_jobs


async def scroll_page_to_load_all(page, max_scrolls: int = 8):
    """Scroll the page gradually to trigger infinite-scroll loading."""
    for i in range(max_scrolls):
        await page.mouse.wheel(0, random.randint(600, 1200))
        await human_delay(SCROLL_MIN_DELAY, SCROLL_MAX_DELAY)

        try:
            see_more = page.locator('button[aria-label="See more jobs"], button.infinite-scroller__show-more-button')
            if await see_more.first.is_visible(timeout=1000):
                await see_more.first.click()
                logger.info("  📜 Clicked 'See more jobs' button")
                await human_delay(2, 4)
        except Exception:
            logger.debug("  ⚠️  No 'See more jobs' button found")

        await dismiss_sign_in_modal(page)


async def scrape_via_browser(page, keyword: str, location: dict,
                             date_filter: str = "", experience_filters: list[str] | None = None) -> list[dict]:
    """Fallback strategy: Full browser-based scraping with infinite scroll."""
    url = build_search_url(keyword, location, date_filter, experience_filters)
    logger.info("  🌐 Browser fallback — %s", url[:120])

    all_jobs = []

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await human_delay(3, 5)

        await dismiss_sign_in_modal(page)
        await scroll_page_to_load_all(page)

        html = await page.content()
        jobs = parse_job_cards_from_html(html)

        if jobs:
            logger.info("  ✅ Browser scrape found %d jobs", len(jobs))
            all_jobs.extend(jobs)
        else:
            logger.warning("  📭 No jobs found in browser scrape.")

    except Exception as e:
        logger.error("  ❌ Browser scrape error: %s", str(e)[:150])

    return all_jobs


async def scrape_jobs(page, keyword: str, location: dict,
                      fetch_descriptions: bool = True,
                      date_filter: str = "",
                      experience_filters: list[str] | None = None) -> list[dict]:
    """
    Scrape jobs for a given keyword + location.
    Tries the Guest API first; falls back to browser scraping.
    NOTE: Description fetching is handled by main.py for real-time saving.
          This function returns basic job data only.
    """
    search_label = f'"{keyword}" in {location["name"]}'
    logger.info("🔍 Searching: %s", search_label)

    jobs = await scrape_via_api(page, keyword, location, date_filter, experience_filters)

    if not jobs:
        logger.info("  🔄 API returned no results. Trying browser fallback…")
        jobs = await scrape_via_browser(page, keyword, location, date_filter, experience_filters)

    if not jobs:
        logger.warning("  ⚠️  No jobs found for: %s", search_label)
        return []

    for job in jobs:
        job["search_keyword"] = keyword
        job["search_location"] = location["name"]

    return jobs
