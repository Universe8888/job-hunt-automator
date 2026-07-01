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
EXPERIENCE_LEVEL_FILTERS: list[str] = []  # Default: no filter — e.g. ["3", "4"] for Associate + Mid-Senior

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
# CDP-mode human pacing (jobs.bg / DataDome)
# Sustained machine-speed fetches re-trigger DataDome even in a human-cleared
# session ("browsing faster than a normal user"). In --cdp mode we pace each
# detail fetch with a longer, randomized delay and an occasional "reading" pause.
# ──────────────────────────────────────────────
CDP_MIN_DELAY = 12.0              # min seconds between detail fetches in CDP mode
CDP_MAX_DELAY = 30.0              # max seconds between detail fetches in CDP mode
CDP_LONG_PAUSE_EVERY = 7          # every Nth job, take a longer "reading" pause
CDP_LONG_PAUSE_MIN = 45.0         # min seconds for the periodic long pause
CDP_LONG_PAUSE_MAX = 90.0         # max seconds for the periodic long pause

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
# NOTE (v3.2 / 3-gate rebuild): the skill score below is now INFO-ONLY.
# It is surfaced as CSV columns (Match Score / Matched Skills / Match Flag)
# but NO LONGER decides any verdict. The 3 gates in gatekeeper.py decide
# keep / manual_review / reject. (User decision #1.)
MATCH_THRESHOLD = 35


# ══════════════════════════════════════════════════════════════════════════
# 3-GATE FILTER — gatekeeper.py constants  (v3.2 rebuild, 2026-06-29)
# ══════════════════════════════════════════════════════════════════════════
# These constants are IMPORTED by gatekeeper.py (never re-typed elsewhere).
# The scraper is a FILTER: every posting passes lane × geo × ceiling.
#   - keep + manual_review  -> leads CSV  (LEADS_CSV)
#   - reject                -> rejects audit CSV  (REJECTS_CSV)  [never silently dropped]
# Word-boundary, case-insensitive matching reuses profile_matcher.score_job's
# \b<term>\b idiom. Multi-word phrases are stored lowercased + single-spaced.
# See docs/superpowers/specs/2026-06-29-3gate-filter-rebuild-design.md
# ══════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────
# GATE 1 — LANE  (AI-Governance hybrid: governs AND builds AI)
# ──────────────────────────────────────────────
# LANE_ALLOW: ≥1 distinct match in title+body -> Gate 1 passes (lane_hits>=1).
# lane_hits = count of DISTINCT terms present (presence, not frequency).
# Synonyms / spelling variants are SEPARATE entries so any one counts as a hit.
# Group comments are for traceability only; the runtime list is flat.
LANE_ALLOW = [
    # AI governance core
    "ai governance", "ai risk", "ai risk management", "responsible ai",
    "ai policy", "ai compliance", "model governance", "model risk",
    # AI tool inventory / shadow AI
    "ai tool inventory", "ai inventory", "shadow ai", "shadow it", "ai discovery",
    # GRC / compliance automation
    "grc", "compliance automation", "governance risk and compliance",
    "risk and compliance", "controls automation",
    # Standards (KEEP THE HEDGE: iso 27001-style)
    "soc2", "soc 2", "iso 27001", "iso27001", "iso 27001-style",
    "nist", "control framework", "security controls",
    # Audit evidence / remediation
    "audit evidence", "evidence chain", "evidence collection",
    "remediation tracking", "remediation", "audit readiness",
    # Asset / SaaS governance
    "software asset management", "sam", "saas governance", "saas management",
    "application inventory", "asset inventory", "itam", "it asset management",
    # Agentic AI / tooling
    "agentic ai", "agentic", "mcp", "model context protocol",
    "agent tooling", "ai agents", "ai agent", "tool calling",
    # RAG / eval / observability
    "rag", "agentic rag", "retrieval augmented generation",
    "llm eval", "evals", "eval", "evaluation harness",
    "observability", "llm observability",
    # Automation / platform engineering
    "automation", "python automation", "enterprise automation",
    "fastapi", "playwright", "python",
    # Customer-facing technical lane
    "solutions engineer", "solution engineer",
    "forward deployed engineer", "forward-deployed engineer", "fde",
    # Regulatory lane
    "eu ai act", "ai act",
]

# WEAK lane terms: broad words that ALSO appear in generic operator roles, so a
# match on these ALONE is not a confident lane signal. Live jobs.bg data showed
# "Data Engineer" / "Monday.com Specialist" passing Gate 1 on just "python" or
# "automation". Rule: a PASS needs >=1 STRONG lane term (a LANE_ALLOW term NOT in
# this list) OR >=2 WEAK terms; a lone weak hit -> manual_review (never a clean
# pass, never a reject). Keep this a SUBSET of LANE_ALLOW.
LANE_ALLOW_WEAK = [
    "python", "automation", "observability", "eval", "evals",
    "remediation", "agentic", "sam", "solutions engineer", "solution engineer",
]

# LANE_DENY: operator-lane BODY signals. PENALTY ONLY — each distinct hit
# subtracts DENY_PENALTY from lane_hits (floored at 0.0). NEVER flips
# pass -> hard_fail on its own (per contract). QUALIFIED/disambiguated forms
# so an in-lane JD that merely mentions a word in passing isn't penalized.
LANE_DENY = [
    "generic business analyst", "requirements-only ba",
    "requirements gathering only",
    "l1 support", "level 1 support", "first-line support",
    "helpdesk", "help desk", "service desk agent",
    "desktop support", "deskside support",
    "pos support", "point of sale support", "cash register", "till support",
    "sysadmin", "system administrator", "server administration",
    "network administrator", "field technician", "field engineer",
    "inventory specialist", "stock controller", "warehouse",
    "data entry", "logistics coordinator", "procurement clerk",
    "junior developer", "junior js developer", "generic backend developer",
    "branch systems", "store systems", "retail systems support",
]

# DENY_PENALTY: subtracted from lane_hits per distinct LANE_DENY body hit
# (de-rank only; never flips the verdict). Tunable; sits with RANK_WEIGHTS.
DENY_PENALTY = 0.5

# TITLE_HARD_DENY: UNAMBIGUOUS operator TITLES only -> immediate Gate 1
# hard_fail (matched against the TITLE only, word-boundary phrase).
# FIX #1: contains the QUALIFIED BA form ("generic business analyst",
# "requirements-only ba") and DELIBERATELY OMITS bare "business analyst" —
# a "Business Analyst, AI Governance" must survive on body keywords.
TITLE_HARD_DENY = [
    "inventory specialist", "stock controller",
    "warehouse associate", "warehouse operative", "warehouse manager",
    "pos support", "point of sale support", "cashier", "till operator",
    "helpdesk technician", "help desk technician", "service desk agent",
    "desktop support technician", "deskside support",
    "field technician", "field service technician",
    "data entry clerk", "data entry operator",
    "procurement clerk", "logistics coordinator",
    "store systems technician",
    # FIX #1: the QUALIFIED BA form ONLY — never bare "business analyst"
    "generic business analyst", "requirements-only ba",
]

# ──────────────────────────────────────────────
# GATE 2 — GEO  (stay BG-taxed while reaching a Western payer)
# ──────────────────────────────────────────────
# Precedence inside the gate: DENY > ALLOW > SOFT > unknown.
# Matched on a normalized haystack = location + title + description.
# Multi-word phrases -> substring; bare ambiguous tokens -> word-boundary.
#
# LOCATION_ALLOW: clean pass, geo_certainty 1.0.
# CAVEAT: a BARE BG city (sofia/plovdiv/...) is NOT an automatic clean allow —
# a local-Sofia on-site role is dead. BG city alone -> demoted to SOFT by the
# gate's bg_city_without_remote() check; BG city + remote/EOR -> clean pass.
LOCATION_ALLOW = [
    # remote (global / anywhere)
    "remote", "fully remote", "100% remote", "remote-first", "remote first",
    "work from anywhere", "anywhere in the world",
    "remote (global)", "remote - global", "global remote",
    # remote (EMEA / EU / Europe)
    "remote (emea)", "remote emea", "emea remote",
    "remote (eu)", "remote eu", "eu remote",
    "remote (europe)", "remote europe", "europe remote",
    "remote within europe", "anywhere in europe", "anywhere in emea",
    "anywhere in the eu",
    # Bulgaria / BG cities (allow only when paired with a remote/EOR signal —
    # the gate demotes bare-BG-city to SOFT)
    "bulgaria", "bulgarian", "sofia", "plovdiv", "varna", "burgas",
    # EOR / contractor-of-record into BG
    "employer of record", "eor", "hired via deel", "via deel", "deel",
    "remote.com", "hired through remote.com", "contractor of record",
    "we can hire you in bulgaria", "we hire in bulgaria",
    "eligible to work in bulgaria",
]

# LOCATION_DENY: hard_fail, geo_certainty 0.0. DENY ALWAYS WINS.
# Western-hub rule (mirrors Gate 1 FIX #1): a BARE hub city ("London") is NOT
# a deny ("Remote (EMEA), HQ in London" must keep). Deny fires only on the
# QUALIFIED hub phrase (based in / relocation to / on-site in <hub>).
LOCATION_DENY = [
    # forced relocation
    "relocation required", "relocation is required", "must relocate",
    "willing to relocate", "required to relocate",
    "relocation package", "relocation assistance", "relocation support",
    "candidate must move",
    # on-site only
    "on-site presence required in", "on-site only", "onsite only",
    "fully on-site", "fully onsite", "100% on-site", "100% onsite",
    "office-based", "office based", "in-office", "in office daily",
    "5 days in office", "5 days a week in the office",
    "no remote", "remote not available", "remote is not possible",
    # US-only / region-locked
    "remote (us)", "remote us only", "us-remote", "us remote",
    "remote within the us", "remote within us", "remote (united states)",
    "must be based in the us", "must reside in the united states",
    "us-based only", "authorized to work in the us", "us work authorization",
    "namer only", "north america only", "us & canada only",
    "us and canada only", "americas only",
    "apac only", "apac-based", "must be based in apac", "latam only",
    # forced Western-hub relocation (qualified phrases only — bare city is NOT deny)
    "relocation to london", "relocation to dublin", "relocation to zurich",
    "relocation to zürich", "relocation to brussels", "relocation to munich",
    "relocation to münchen", "relocation to paris", "relocation to madrid",
    "relocation to amsterdam",
    "based in london", "based in dublin", "based in zurich", "based in zürich",
    "based in brussels", "based in munich", "based in paris",
    "based in madrid", "based in amsterdam",
    "on-site in london", "on-site in dublin", "on-site in munich",
    "on-site in paris", "on-site in amsterdam",
]

# LOCATION_SOFT: manual_review, geo_certainty 0.5 (FIX #2 — never blanket deny).
# hybrid-EMEA-EOR-eligible = good; hybrid-local-Sofia = dead — only a human
# can tell, so soft. remote (worldwide)/(international) live HERE not in ALLOW
# because some hide a "must be US tax-resident" clause; a clean EMEA/EU/BG
# allow elsewhere upgrades to pass via precedence.
LOCATION_SOFT = [
    "hybrid", "hybrid working", "hybrid work", "hybrid model",
    "hybrid remote", "partially remote",
    "remote-friendly", "remote friendly",
    "flexible location", "flexible work location",
    "occasional travel to office", "days in the office per week",
    "international contractor", "contractor", "freelance",
    "b2b contract", "self-employed contract",
    "global team", "distributed team", "work across time zones",
    "european time zone", "cet time zone", "cet hours",
    "remote (worldwide)", "remote (international)",
]

# ──────────────────────────────────────────────
# GATE 3 — CEILING  (can disclosed comp clear the €137.5k gross goal?)
# ──────────────────────────────────────────────
# Bands on the normalized figure T = top_eur_gross_yr (EUR / yr / GROSS):
#   T <= 72000          -> hard_fail (top of BG IT-ops band)
#   72000 < T < 120000  -> pass + "below floor"      (nets < €10k/mo)
#   120000 <= T < 137500-> pass + "capable but short"
#   T >= 137500         -> pass (clean — meets goal)
SALARY_GOAL_EUR              = 137500   # gross/yr -> ~€10,050/mo net ✅
SALARY_FLOOR_EUR             = 120000   # gross/yr -> ~€8,740/mo net (below €10k net target)
SALARY_AUTO_REJECT_BELOW_EUR = 72000    # top of BG IT-ops band; disclosed <= this -> hard_fail
DISCLOSED_COMP_REQUIRED      = False     # FIX #3: undisclosed -> manual_review, NOT hard reject

# FX_RATES: convert detected currency to EUR. BGN is pegged to EUR at 1.95583
# (expressed as multiply-by-reciprocal). USD/GBP are STATIC approximations —
# rates as of 2026-06, update periodically.
FX_RATES = {
    "EUR": 1.0,
    "BGN": 1.0 / 1.95583,   # BGN pegged to EUR at 1.95583 (lev)
    "USD": 0.92,            # static approximation — update periodically
    "GBP": 1.17,            # static approximation — update periodically
}

# NET_TO_GROSS_FACTOR: when a NET figure is detected, multiply by this to
# estimate gross-equivalent (derived from the €120k-net <-> €137.5k-gross anchor).
NET_TO_GROSS_FACTOR = 1.146

# ──────────────────────────────────────────────
# RANK  (apply-first sorts on top)
# ──────────────────────────────────────────────
# rank = w_lane*lane_hits + w_geo*geo_certainty + w_comp*comp_headroom
#      + w_fit*fit_signal      (how much of the JD YOU can deliver; 0..1 from match_score)
#      - w_gap*seniority_gap   (docks roles demanding seniority/skills you lack)
# w_fit/w_gap default-safe: both terms are 0 when match_score / SENIORITY_GAP_TERMS
# are absent, so rank reduces to the original lane+geo+comp behaviour.
RANK_WEIGHTS = {
    "w_lane": 1.0, "w_geo": 1.0, "w_comp": 1.0,
    "w_fit": 4.0,   # set 0.0 to disable candidate-fit lift
    "w_gap": 1.5,   # set 0.0 to disable over-seniority de-rank
}

# SENIORITY_GAP_TERMS: phrases signalling a role is ABOVE your record. Each distinct
# hit subtracts w_gap from the rank (DE-RANK ONLY — never flips a verdict). Populate
# in your private config.py with what you'd actually be screened out on. Empty here
# (generic template) so the public example penalizes nothing by default.
# TIP: scope governance-adjacent concepts to DEMAND-FORM ("lead/own/run/perform X")
# so a role that merely *lists* the concept as oversight scope isn't false-penalized;
# and avoid bare ubiquitous phrases (e.g. "at scale") that fire on benign JD language.
# Terms containing punctuation (e.g. "ci/cd security") are matched after the same
# normalizer applied to the posting text, so they fire correctly.
SENIORITY_GAP_TERMS: list[str] = []

# GAP_PENALTY_CAP: max number of distinct gap hits CHARGED to the rank (0 = uncapped).
# Stops a sprawling senior JD from running the penalty far past the +w_fit ceiling
# (which would make the fit lever a no-op). 0 here = legacy uncapped; ~3 recommended.
GAP_PENALTY_CAP = 0

# FIT_NORMALIZER_PCT: the match_score percent treated as a "full" fit (-> fit_signal 1.0).
# match_score divides matched skill weight by the sum of ALL your skills, so real
# postings often score only ~5-16%. Dividing by a flat 100 leaves w_fit a near-no-op
# (+0.2..+0.6) — fine if you want lane/geo/comp to dominate, but if you actually want
# candidate-fit to move the ranking, set this to ~50 so the realistic score band
# rescales to ~0..1 and competes with w_gap.
#   100.0 = conservative default: fit barely nudges rank (back-compatible).
#    ~50  = active: fit becomes a real lever (recommended once a resume PDF is wired in).
FIT_NORMALIZER_PCT = 100.0

# ──────────────────────────────────────────────
# Output CSVs (3-gate rebuild — two destinations, nothing silently dropped)
# ──────────────────────────────────────────────
LEADS_CSV   = "leads.csv"     # keep + manual_review
REJECTS_CSV = "rejects.csv"   # hard-reject audit trail
