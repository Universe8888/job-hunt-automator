"""
LinkedIn Jobs Scraper — Stealth Browser Configuration
Sets up Playwright with stealth patches and realistic browser fingerprinting.
"""

import random
from config import USER_AGENTS, PROXY_URL, HEADLESS_DEFAULT


STEALTH_SCRIPT = """
() => {
    // Override navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    
    // Mock plugins to look like a real browser
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    
    // Mock languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en', 'bg']
    });
    
    // Override chrome runtime
    window.chrome = { runtime: {} };
    
    // Remove automation-related properties
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );
    
    // Override connection
    Object.defineProperty(navigator, 'connection', {
        get: () => ({ downlink: 10, effectiveType: '4g', rtt: 50 })
    });
    
    // Override hardware concurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 8
    });
    
    // Override device memory
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8
    });
    
    // Override platform
    Object.defineProperty(navigator, 'platform', {
        get: () => 'Win32'
    });
    
    // Override app version and userAgent to remove HeadlessChrome
    Object.defineProperty(navigator, 'appVersion', {
        get: () => navigator.appVersion.replace('HeadlessChrome', 'Chrome')
    });
}
"""


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
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    }
    if use_headless:
        # Use new headless mode (more stealthy)
        options["args"].append("--headless=new")
        options["args"].extend([
            "--window-size=1920,1080",
            "--disable-gpu",
        ])

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


async def apply_stealth_to_page(page) -> None:
    """Apply stealth scripts to a page before navigation."""
    await page.add_init_script(STEALTH_SCRIPT)
