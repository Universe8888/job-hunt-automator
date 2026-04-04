"""
LinkedIn Jobs Scraper — Stealth Browser Configuration
Sets up Playwright with stealth patches and realistic browser fingerprinting.
"""

import random
from config import USER_AGENTS, PROXY_URL, HEADLESS_DEFAULT


def get_random_user_agent() -> str:
    """Return a random User-Agent string from the pool."""
    return random.choice(USER_AGENTS)


def get_launch_options(headless: bool | None = None) -> dict:
    """Return Playwright browser launch options."""
    use_headless = headless if headless is not None else HEADLESS_DEFAULT
    options = {
        "headless": use_headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-extensions",
            "--start-maximized",
        ],
    }
    if use_headless:
        options["args"].append("--disable-gpu")

    if PROXY_URL:
        options["proxy"] = {"server": PROXY_URL}

    return options


def get_context_options(user_agent: str | None = None) -> dict:
    """Return browser context options with a realistic fingerprint."""
    ua = user_agent or get_random_user_agent()
    # Random realistic viewport to avoid fixed headless fingerprints
    width = random.choice([1366, 1440, 1536, 1920])
    height = random.choice([768, 900, 864, 1080])
    return {
        "user_agent": ua,
        "viewport": {"width": width, "height": height},
        "locale": "en-US",
        "timezone_id": "Europe/Sofia",
        "color_scheme": "light",
        "java_script_enabled": True,
        "bypass_csp": False,
        "ignore_https_errors": True,
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9,bg;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    }
