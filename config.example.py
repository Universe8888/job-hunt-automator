"""
LinkedIn Jobs Scraper — Configuration
All search parameters, delays, and skill weights are defined here.
Loads .env overrides when available.
"""

import os

# ──────────────────────────────────────────────
# .env Loading (optional — graceful fallback)
# ──────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — no problem

# ──────────────────────────────────────────────
# Target Site Default (linkedin or jobs.bg)
# ──────────────────────────────────────────────
TARGET_SITE = os.getenv("TARGET_SITE", "linkedin")

# ──────────────────────────────────────────────
# Search Keywords
# ──────────────────────────────────────────────
KEYWORDS = [
    "Software Engineer",
    "Data Analyst",
    "AI Agent Developer",
]

# ──────────────────────────────────────────────
# Location Targets
# Each entry: (display_name, geoId, location_text, jobsbg_location_id)
# ──────────────────────────────────────────────
LOCATIONS = [
    {
        "name": "London, United Kingdom",
        "geoId": "102257491",           # LinkedIn geoId
        "location_text": "London",
        "jobsbg_location_id": "",       # Leave blank or set appropriate ID for jobs.bg
    },
    {
        "name": "Remote EMEA",
        "geoId": "91000007",
        "location_text": "European Economic Area",
        "jobsbg_location_id": "",
    },
]

# ──────────────────────────────────────────────
# Work-Type Filters (f_WT)
# 1 = On-site, 2 = Remote, 3 = Hybrid
# ──────────────────────────────────────────────
WORK_TYPE_FILTERS = ["2", "3"]  # Remote + Hybrid

# ──────────────────────────────────────────────
# Date-Posted Filter (f_TPR)
# "r86400" = past 24 hours
# "r604800" = past week
# "r2592000" = past month
# "" = no filter (all time)
# ──────────────────────────────────────────────
DATE_POSTED_FILTER = ""  # Default: no filter

# ──────────────────────────────────────────────
# Experience-Level Filter (f_E)
# 1 = Internship, 2 = Entry level,
# 3 = Associate, 4 = Mid-Senior level,
# 5 = Director, 6 = Executive
# ──────────────────────────────────────────────
EXPERIENCE_LEVEL_FILTERS = []  # Default: no filter — e.g. ["3", "4"] for Associate + Mid-Senior

# ──────────────────────────────────────────────
# Pagination
# ──────────────────────────────────────────────
MAX_PAGES_PER_SEARCH = 4          # 4 pages × ~25 results = ~100 jobs per combo
RESULTS_PER_PAGE = 25

# ──────────────────────────────────────────────
# Delays (seconds) — human-like randomisation
# ──────────────────────────────────────────────
MIN_DELAY = 3
MAX_DELAY = 7
SCROLL_MIN_DELAY = 1.5
SCROLL_MAX_DELAY = 4.0

# ──────────────────────────────────────────────
# Anti-blocking
# ──────────────────────────────────────────────
SIGN_IN_MODAL_WAIT = 5            # seconds to wait before dismissing modal
MAX_RETRIES_ON_BLOCK = 3          # max retries before skipping a search term

# ──────────────────────────────────────────────
# Sign-in modal text markers (content validation)
# If a scraped description contains any of these,
# it's modal garbage — discard it.
# ──────────────────────────────────────────────
SIGN_IN_MODAL_MARKERS = [
    "Join LinkedIn",
    "Agree & Join",
    "Password (6+ characters)",
    "Already on Linkedin? Sign in",
    "Get it in the Microsoft Store",
    "LinkedIn is better on the app",
    "Sign in to view more",
]

# ──────────────────────────────────────────────
# User-Agent pool (rotated per session)
# Updated for 2026 — Chrome 131+, Firefox 133+
# ──────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# ──────────────────────────────────────────────
# Proxy (optional — loaded from .env)
# ──────────────────────────────────────────────
PROXY_URL = os.getenv("PROXY_URL", "")

# ──────────────────────────────────────────────
# Max jobs per run (0 = unlimited)
# ──────────────────────────────────────────────
MAX_JOBS_PER_RUN = int(os.getenv("MAX_JOBS_PER_RUN", "0"))

# ──────────────────────────────────────────────
# Headless browser default
# ──────────────────────────────────────────────
HEADLESS_DEFAULT = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")

# ──────────────────────────────────────────────
# Output & Session Data
# ──────────────────────────────────────────────
OUTPUT_CSV = "linkedin_leads.csv"
SESSION_DIR = ".browser_session"

# ──────────────────────────────────────────────
# Profile PDF for AI matching (optional)
# Place your CV/resume PDF in the project root
# ──────────────────────────────────────────────
PROFILE_PDF = "my_resume.pdf"

# ──────────────────────────────────────────────
# Skill Weights for Profile Matching
# ──────────────────────────────────────────────
SKILL_WEIGHTS = {
    # Primary Skills (Weight 1.0)
    "python": 1.0,
    "sql": 1.0,
    "machine learning": 1.0,
    "ai agents": 1.0,
    "llm": 1.0,

    # Domain Skills (Weight 0.8)
    "data analysis": 0.8,
    "agile": 0.8,
    "project management": 0.8,

    # Tools (Weight 0.5)
    "git": 0.5,
    "docker": 0.5,
    "jira": 0.5,
    "aws": 0.5,
}

# Match score threshold (0–100) — jobs scoring above this are "Good Match"
MATCH_THRESHOLD = 35
