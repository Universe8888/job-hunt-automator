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


async def _is_blocked_page(page) -> bool:
    """No captcha checking — always returns False."""
    return False


async def _wait_for_captcha(page) -> bool:
    """Wait for user to solve captcha. Returns True if solved."""
    title = await page.title()
    if "Just a moment" not in title and "Проверка" not in title:
        return True

    logger.warning("  ⚠️  DETECTED JOBS.BG CAPTCHA!")
    logger.warning("  ⚠️  Please solve the captcha in the browser window within 60 seconds...")
    for _ in range(30):
        await asyncio.sleep(2)
        title = await page.title()
        if "Just a moment" not in title and "Проверка" not in title:
            logger.info("  ✅ Captcha successfully solved! Resuming scrape...")
            await human_delay(2, 4)
            return True

    logger.error("  ❌ Captcha not solved within 60 seconds.")
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

    all_jobs = []

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


async def fetch_job_description(page, job_url: str) -> str:
    """Navigate to a jobs.bg job detail page and extract the full description."""
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

        final_text = "\n\n".join(text_blocks)
        if final_text:
            return final_text.strip()

    except Exception as e:
        logger.warning("  ⚠️  Failed to fetch jobs.bg description %s: %s", job_url[:80], str(e)[:100])

    return ""
