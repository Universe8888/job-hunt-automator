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

# Detail-page DOM: jobs.bg carries the work location ONLY as bare <span> leaves
# with no class / itemprop / location-anchor hook (verified against live detail
# pages 8514748 / 8514545 / 8514820 on 2026-06-29 — the earlier selector list
# below matched NOTHING, so structured-location extraction was silently 0/30).
# We therefore grab the short leaf-span texts and classify them by CONTENT.
GEO_SPAN_SELECTOR = "span"
# Allowlist phrases (lower-cased) that mark a genuine WORK location. BG towns +
# remote/hybrid phrasing the gate's vocabulary already understands.
_GEO_TOWN_TOKENS = (
    "sofia", "софия", "plovdiv", "пловдив", "varna", "варна",
    "burgas", "бургас", "ruse", "русе", "stara zagora", "стара загора",
    "bulgaria", "българия",
)
_GEO_REMOTE_PHRASES = (
    "fully remote work", "fully remote", "remote work", "remote position",
    "remote", "дистанционно", "work from home", "home office", "hybrid",
    "дистанционна работа", "изцяло дистанционно", "home-based",
)
# Single ambiguous tokens matched STANDALONE only: the token, then end-of-string or
# a non-word char that is NOT a hyphen — so 'Remote', 'Remote (CET team)', 'Hybrid,
# Sofia' pass, while compounds/derivatives ('remotely', 'remote sensing',
# 'remote-controlled', 'hybrid car parts') do NOT. A following space+word or hyphen
# means the token modifies a noun (a compound), not a standalone work-arrangement tag.
_GEO_REMOTE_STANDALONE = {
    tok: re.compile(r"\b" + re.escape(tok) + r"(?![\w\- ]*\w)", re.IGNORECASE)
    for tok in ("remote", "hybrid", "дистанционно")
}


def _geo_remote_hit(low: str) -> bool:
    """True if a remote/hybrid phrase is present. Single ambiguous tokens
    ('remote'/'hybrid'/'дистанционно') must be STANDALONE (not part of a longer
    remote-*/hybrid-* compound or the adverb 'remotely'); multi-word phrases
    ('work from home', 'fully remote') match as substrings — no homograph risk."""
    for ph in _GEO_REMOTE_PHRASES:
        rx = _GEO_REMOTE_STANDALONE.get(ph)
        if rx is not None:
            if rx.search(low):
                return True
        elif ph in low:
            return True
    return False
# False friends: spans that CONTAIN a geo word but do NOT describe the work
# location. 'Remote interview' is a hiring-process note, not a remote job.
_GEO_TRAP_PHRASES = (
    "remote interview", "remote interviewing", "online interview",
    "video interview", "remote onboarding",
)
# Company-name false-matches (live 309-run): the "IT Jobs of <Company> EOOD"
# other-jobs link text matched because BG legal names embed a city/country token
# ('Akkodis Bulgaria EOOD', 'ФЕСТО БЪЛГАРИЯ ЕООД'). Skip any span that is a
# company-jobs link OR carries a legal-entity token — those are never the work
# location. The entity token is matched as a STANDALONE WORD anywhere in the span
# (leading/mid/trailing), not just at end-of-string: a 60-case adversarial
# stress-test (2026-06-30) showed 'Akkodis Bulgaria EOOD - Sofia office' and
# leading 'Ad Astra Bulgaria' / 'ЕАД Русе' slipping through an end-anchored regex.
_GEO_COMPANY_PREFIXES = ("jobs of", "it jobs of")
# Legal-entity tokens come in two safety classes:
#   - UNAMBIGUOUS (>=3 chars / clearly corporate): may appear anywhere in the span.
#   - AMBIGUOUS short tokens (ad/sa/ag/od and their Cyrillic forms) collide with
#     ordinary words ("ad-tech district", "AG campus") — they mark a company ONLY
#     in a company-NAME POSITION: the FIRST or LAST token of the span ("Ad Astra
#     Bulgaria", "...Bulgaria EAD", "ЕАД Русе"). A mid-string occurrence is treated
#     as incidental text and kept, which removes the false-drops the audit found.
_GEO_ENTITY_ANY = re.compile(
    r"\b(?:eood|jsc|ltd|llc|gmbh|gbr|srl|еоод|оод|еад)\b\.?",
    re.IGNORECASE | re.UNICODE,
)
# Ambiguous short entity token in company-NAME position. Leading form requires the
# token to be a STANDALONE word (followed by whitespace, not a hyphen) so a
# hyphenated compound ('ad-tech', 'ag-grid') is NOT treated as a company token;
# trailing form is the legal suffix at end-of-string ('...Bulgaria EAD').
_GEO_ENTITY_EDGE = re.compile(
    r"(?:^(?:ood|ead|ad|ag|sa|ад)(?=\s)|\b(?:ood|ead|ad|ag|sa|ад)\.?\s*$)",
    re.IGNORECASE | re.UNICODE,
)
# Negation / on-site markers that DISQUALIFY a remote phrase ('No remote work —
# strictly on-site'): the span mentions remote only to deny it.
_GEO_REMOTE_NEGATORS = (
    "no remote", "not remote", "without remote", "on-site", "onsite",
    "on site", "strictly", "no home office", "no work from home",
)
# Region/hub tokens that make a geo span DENY-SCOPED (US / NAMER / APAC / LATAM). A
# small, CLOSED set of region NAMES (not open-ended like salary numbers — so a regex
# is acceptable here where it was not for comp). When a remote/town span ALSO carries
# one of these, it is allow-able only as a LAST RESORT, ranking below any clean
# (deny-free) sibling.
#
# 'us'/'usa' are NEVER matched as a bare word (R2 audit: a bare \bus\b matched the
# English PRONOUN in 'Join us in Sofia' / 'work with us', demoting a clean BG-town span
# and discarding a real lead). They count as a region ONLY in unambiguous region
# position: parenthesized '(US)', dotted 'U.S.', a 'US-based / US remote / US only'
# compound, or after a region preposition ('within/based in/located in/… (the) US').
# Verb-object pronoun forms ('join us', 'work with us', 'contact us') never match.
_GEO_DENY_SCOPE_RX = re.compile(
    r"\(usa?\b|\[usa?\b"                                   # (US) / (USA) / [US]
    r"|\bu\.s\.a?\.?"                                      # U.S. / U.S.A.
    r"|\b(?:namer|apac|latam)\b"                           # region acronyms
    r"|united states|north america|americas"              # spelled-out regions
    r"|\busa?[\s-]+(?:only|based|remote|region|market|citizen|resident)"  # US-based / US remote / US only
    r"|(?:within|based\s+in|located\s+in|reside\s+in|residing\s+in|relocat\w*\s+to)"
    r"\s+(?:the\s+)?usa?\b",                               # within / based in (the) US
    re.IGNORECASE,
)


def _geo_has_deny_scope(low: str) -> bool:
    """True if the span names a deny-scoped region (US/NAMER/APAC/LATAM)."""
    return _GEO_DENY_SCOPE_RX.search(low) is not None
# Duty-sentence detection. Words like 'support'/'lead'/'build' are duty VERBS in a
# sentence but also common NOUNS in real labels ('Support Center', 'Lead office',
# 'Build St'). A token-presence OR length test drops those valid labels (a long
# noun-phrase label like 'Sofia Tech Park Support Center' is not a duty sentence) —
# so we detect a duty sentence purely by GRAMMATICAL STRUCTURE, independent of
# length: an unambiguous sentence-opener ('you will…'), OR a duty verb directly
# governing an object via a preposition ('relocate to', 'support for'). A noun-phrase
# label has neither, at any length, so it survives.
_GEO_DUTY_VERBS = (
    "provide", "support", "relocate", "manage", "lead", "build", "deliver",
    "develop", "deploy", "maintain", "design", "implement", "drive",
)
_GEO_DUTY_SENTENCE_MARKERS = (
    "you will", "we are looking for", "we offer", "responsible for",
    "looking for a", "looking for an",
)
# A duty verb DIRECTLY governing an object via a preposition: "relocate to",
# "support for", "build into". Deliberately NOT broadened to allow an object noun
# between verb and preposition ("manage delivery for"): that bridge cannot be
# distinguished from a noun-phrase label ("Lead Engineer in Sofia") and would drop
# real labels. The trade is intentional — a rare verb-noun-prep duty clause that
# slips through is KEPT (then Gate 2 soft → human review), the safe failure
# direction; dropping a real location is the unsafe one we refuse.
_GEO_DUTY_GOVERNS = re.compile(
    r"\b(?:" + "|".join(_GEO_DUTY_VERBS) + r")\s+(?:to|for|into|across|in)\b",
    re.IGNORECASE,
)


def _is_duty_sentence(low: str) -> bool:
    """True if the span reads as a job-duty sentence rather than a location label.

    Purely GRAMMATICAL, independent of length (length wrongly dropped long
    noun-phrase labels like 'Sofia Tech Park Support Center'): a duty sentence has
    an unambiguous sentence-opener ('you will…', 'we offer…'), OR a duty verb that
    directly GOVERNS an object via a preposition ('relocate to', 'support for',
    'build into'). A noun-phrase label whose words merely include a verb-homograph
    ('Support Center Plovdiv', 'Lead Software Engineer Office Sofia') has neither,
    at any length, so it is NOT a duty sentence.
    """
    if any(mark in low for mark in _GEO_DUTY_SENTENCE_MARKERS):
        return True
    return _GEO_DUTY_GOVERNS.search(low) is not None


# Precompiled once (looks_like_sentence runs over every candidate span).
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


def classify_location_spans(span_texts: list[str]) -> str:
    """Pick a work-location label from candidate detail-page <span> texts.

    jobs.bg detail pages have no structured location element — the town /
    remote phrase is just a bare <span>. We score EVERY qualified candidate and
    return the best by ALLOW-OVER-DENY precedence (not first-span-wins, which used
    to discard a qualifying sibling — e.g. ['Remote (US)', 'Remote (EMEA)'] wrongly
    returned the US span, losing the clean EMEA one and triggering a hard-fail):

        tier 3 — clean BG-town  (town hit, no deny-scoped region)
        tier 2 — clean remote/hybrid (remote hit, not negated, no deny-scoped region)
        tier 1 — deny-bearing geo span (town/remote that ALSO names US/NAMER/APAC)

    A span must still clear all the same per-span disqualifiers (prose, traps,
    company names, duty sentences). Highest tier wins; on a tie the FIRST (document
    order) is kept, preserving the existing precedence tests. A lone deny-scoped span
    is still returned (tier 1 > nothing) so Gate 2 can see and deny it — we never
    invent a clean location.

    Returns "" when nothing usable is found, so the caller leaves job['location']
    blank and Gate 2 falls back to its body keyword scan -> soft.
    Pure (no I/O) so it is unit-testable without a browser.
    """
    best_text = ""
    best_tier = 0  # 0 = nothing chosen; 3 town-clean > 2 remote-clean > 1 deny-bearing
    for raw in span_texts:
        t = (raw or "").strip()
        if not t or len(t) > 45 or looks_like_sentence(t):
            continue
        low = t.lower()
        if any(trap in low for trap in _GEO_TRAP_PHRASES):
            continue  # geo word present but not a work location
        # Company-name false-match: a "Jobs of …" link or a legal-entity token
        # (EOOD/ЕООД/GmbH/…) anywhere in the span is a company, never a location.
        if any(low.startswith(p) for p in _GEO_COMPANY_PREFIXES):
            continue
        if _GEO_ENTITY_ANY.search(t) or _GEO_ENTITY_EDGE.search(t):
            continue
        # Job-duty SENTENCE where a geo token is incidental, not a label — detected
        # structurally (verb governing a clause), so noun-phrase labels carrying a
        # verb-homograph ('Support Center Plovdiv', 'Lead Engineer, Sofia') survive.
        if _is_duty_sentence(low):
            continue
        # Town tokens on WORD BOUNDARIES so 'ruse' does not match inside 'Ruseville'.
        town_hit = any(
            re.search(r"\b" + re.escape(tok) + r"\b", low) for tok in _GEO_TOWN_TOKENS
        )
        remote_hit = False
        if not town_hit and _geo_remote_hit(low):
            # Remote phrase — but only if not negated ('No remote work, strictly
            # on-site'). The town branch deliberately does NOT run this check, so a
            # town span carrying a remote caveat ('Sofia (no remote)') is still kept.
            if any(neg in low for neg in _GEO_REMOTE_NEGATORS):
                continue
            remote_hit = True
        if not town_hit and not remote_hit:
            continue
        # ALLOW-OVER-DENY: a clean (deny-free) span outranks a deny-scoped one.
        deny_scoped = _geo_has_deny_scope(low)
        if town_hit:
            tier = 1 if deny_scoped else 3
        else:  # remote_hit
            tier = 1 if deny_scoped else 2
        # Highest tier wins; ties keep the FIRST (document order) — strict '>'.
        if tier > best_tier:
            best_tier, best_text = tier, t
    return best_text


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

        # Date extraction (bs4 4.x: use string=, not the deprecated text=).
        time_el = card.find("div", class_="secondary-text", string=re.compile(r"\d{2}\.\d{2}\.\d{4}"))
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
    """Pull a work-location string from the jobs.bg detail-page DOM.

    jobs.bg exposes the location only as bare <span> leaves (no class/itemprop),
    so we collect the short span texts and hand them to the pure
    classify_location_spans() classifier (allowlist of BG-towns / remote phrases,
    minus known traps like 'Remote interview'). Returns "" if nothing usable is
    found, in which case the gate falls back to its body keyword scan -> soft.
    """
    try:
        # One round-trip for all span texts (vs N sequential nth() reads); the pure
        # classifier does the short-leaf filtering and content matching.
        texts = await page.locator(GEO_SPAN_SELECTOR).all_inner_texts()
        return classify_location_spans([t.strip() for t in texts if t and t.strip()])
    except Exception:
        logger.debug("  ⚠️  Geo span scan failed on %s", job_url[:60])
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
