"""
Jobs.bg Scraper Engine (v2.0 extension)
Implements identically signatured scraping functions to integrate cleanly with main.py.
"""

import asyncio
import random
import logging
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup
from scraper import human_delay, MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# ────────────────────────────────────────
# Jobs.bg Builders
# ────────────────────────────────────────

def build_jobsbg_search_url(keyword: str, location: dict, offset: int = 0) -> str:
    """Build the Jobs.bg search results URL."""
    # Base params
    params = {
        "subm": "1",
    }
    
    # If using offset (pages are 15 jobs each)
    if offset > 0:
        params["from"] = str(offset)
        
    query = urlencode(params)
    
    # Add keywords[]
    query += f"&keywords%5B%5D={quote_plus(keyword)}"
    
    # Add location ID if available
    loc_id = location.get("jobsbg_location_id")
    if loc_id:
        query += f"&location_sid={loc_id}"
        
    return f"https://www.jobs.bg/en/front_job_search.php?{query}"

# ────────────────────────────────────────
# HTML Parsing
# ────────────────────────────────────────

def parse_jobsbg_cards(html: str) -> list[dict]:
    """Parse job cards from jobs.bg DOM."""
    soup = BeautifulSoup(html, "lxml")
    jobs = []

    # Job cards
    cards = soup.find_all("div", class_="mdc-card")
    for card in cards:
        job = {}
        
        # Title and URL
        title_el = card.find("a", class_="black-link-b")
        if title_el:
            raw_title = title_el.get("title", "").strip() or title_el.get_text(strip=True)
            # Remove material icon stars if present in get_text
            job["title"] = raw_title.replace("star", "").strip()
            href = title_el.get("href", "")
            # Ensure full url
            if href.startswith("/"):
                href = f"https://www.jobs.bg{href}"
            elif href.startswith("front_job_search.php?") or href.startswith("job/"):
                href = f"https://www.jobs.bg/en/{href}"
            job["url"] = href
            
        # Company
        # company links often have /company/ in the url
        company_elem = card.find("a", href=lambda h: h and "/company/" in h)
        if company_elem:
            job["company"] = company_elem.get("title", company_elem.get_text(strip=True))
        else:
            # Fallback for company name
            company_fallback = card.find("div", class_="secondary-text")
            if company_fallback:
                job["company"] = company_fallback.get_text(strip=True)
                
        # Only add valid jobs
        if job.get("title") and job.get("url"):
            jobs.append(job)
            
    return jobs

# ────────────────────────────────────────
# Core Functions
# ────────────────────────────────────────

async def scrape_jobs(page, keyword: str, location: dict,
                      fetch_descriptions: bool = True,
                      date_filter: str = "",
                      experience_filters: list[str] | None = None) -> list[dict]:
    """
    Matches the LinkedIn scraper API. 
    Iterates over pages in Jobs.bg and grabs metadata.
    """
    search_label = f'"{keyword}" in {location["name"]}'
    logger.info("🔍 Searching Jobs.bg: %s", search_label)
    
    all_jobs = []
    from config import MAX_PAGES_PER_SEARCH
    
    # Jobs.bg uses 15 results per page
    RESULTS_PER_PAGE = 15
    
    for page_num in range(MAX_PAGES_PER_SEARCH):
        offset = page_num * RESULTS_PER_PAGE
        url = build_jobsbg_search_url(keyword, location, offset)
        logger.info("  🌐 Page %d — %s", page_num + 1, url[:120])
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await human_delay(2, 4)
            
            # Check for Captcha / Cloudflare Challenge
            title = await page.title()
            if "Just a moment" in title or "Проверка" in title:
                logger.warning("  ⚠️  DETECTED JOBS.BG CAPTCHA!")
                logger.warning("  ⚠️  Please solve the captcha in the browser window within 60 seconds...")
                # Wait for the user to solve it
                for _ in range(30):
                    await asyncio.sleep(2)
                    title = await page.title()
                    if "Just a moment" not in title and "Проверка" not in title:
                        logger.info("  ✅ Captcha successfully solved! Resuming scrape...")
                        await human_delay(2, 4)
                        break
            
            # Dismiss cookies modal if present
            try:
                cookie_btn = page.locator('button:has-text("ACCEPT"), button.mdc-button--raised').first
                if await cookie_btn.is_visible(timeout=1000):
                    await cookie_btn.click()
            except Exception:
                pass
            
            html = await page.content()
            jobs = parse_jobsbg_cards(html)
            
            if not jobs:
                logger.info("  📭 No more job cards found.")
                break
                
            logger.info("  ✅ Found %d jobs on page %d", len(jobs), page_num + 1)
            
            # Tag metadata
            for j in jobs:
                j["search_keyword"] = keyword
                j["search_location"] = location["name"]
                
            all_jobs.extend(jobs)
            
            # If we returned fewer than 15, we've hit the end of results
            if len(jobs) < RESULTS_PER_PAGE:
                logger.info("  📭 Reached end of total results.")
                break
                
        except Exception as e:
            logger.error("  ❌ Error fetching page %d: %s", page_num + 1, str(e)[:100])
            break

    if not all_jobs:
        logger.warning("  ⚠️  No jobs found for: %s", search_label)
            
    return all_jobs


async def fetch_job_description(page, job_url: str) -> str:
    """
    Navigate to a jobs.bg job detail page and extract the full description.
    """
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
        await human_delay(1.5, 3)
        
        text_blocks = []
        
        # Primary container
        left_col = page.locator(".job-view-left-column")
        if await left_col.count() > 0:
            text = await left_col.first.inner_text()
            if text and len(text.strip()) > 50:
                text_blocks.append(text.strip())
                
        # Fallback container
        main_content = page.locator("#jobViewContent")
        if await main_content.count() > 0:
            text = await main_content.first.inner_text()
            if text and len(text.strip()) > 50:
                text_blocks.append(text.strip())
            
        # Extract from inner iframes (Jobs.bg often embeds job ads in HTML iframes)
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    f_text = await frame.inner_text("body", timeout=1000)
                    if f_text and len(f_text.strip()) > 50:
                        text_blocks.append(f_text.strip())
                except Exception:
                    pass
                    
        final_text = "\n\n".join(text_blocks)
        if final_text:
            return final_text.strip()
            
    except Exception as e:
        logger.warning("  ⚠️  Failed to fetch jobs.bg description %s: %s", job_url[:80], str(e)[:100])
        
    return ""
