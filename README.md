# 🔍 LinkedIn & Jobs.bg Scraper v3.2

A local Python-based job scraper built with **Playwright** and **playwright-stealth**. Supports **LinkedIn** and **Jobs.bg** as dual scraping targets, then runs each posting through a configurable **3-gate filter** that decides **keep / manual-review / reject** — so you surface the rare strong matches instead of drowning in noise — and exports the results to CSV.

## The 3-Gate Filter

Earlier versions only *scored* every job and surfaced everything. v3.2 turns the post-processing into a **filter**: each posting is graded on three independent gates and assigned a verdict.

| Gate | Question | Source |
|------|----------|--------|
| **1 — Lane** | Is this the *kind* of role you want? | Title + body keyword match (`LANE_ALLOW` strong/weak vs `LANE_DENY`) |
| **2 — Geo** | Is the location/work-arrangement acceptable? | Location field + body (`LOCATION_ALLOW` / `DENY` / `SOFT`) |
| **3 — Ceiling** | Does the comp clear your floor? | Parsed salary, FX-normalised to EUR/yr gross, vs configurable thresholds |

**Verdict precedence:** `reject` if **any** gate hard-fails → else `manual_review` if **any** gate is soft (e.g. undisclosed comp, hybrid geo) → else `keep`. Results are ranked (`w_lane·lane + w_geo·geo + w_comp·comp`) so apply-first roles sit on top.

- **Nothing is silently dropped** — rejects go to a separate audit CSV with the failing gate + reason.
- **The skill score is informational only** — the gates decide the verdict, not the legacy match percentage.
- **Everything is configurable** in `config.py` — keyword lists, geo rules, and salary thresholds are yours to define.

## Features

### Core Engine
- 🔍 **Multi-Site Scraping** — Toggle between LinkedIn and Jobs.bg with `--site`
- 🚦 **3-Gate Filter** — Lane × Geo × Ceiling → keep / manual_review / reject, with ranked output
- 🕵️ **Stealth Mode** — `playwright-stealth`, realistic User-Agent rotation, randomised viewports, human-like delays
- 🛡️ **Anti-Blocking** — Auto-dismisses modals, handles rate limits with backoff
- 🧠 **Persistent Sessions** — Solve a captcha once; cookies are saved locally for future runs
- 🔌 **CDP Attach Mode** (`--cdp`) — Scrape jobs.bg through your own already-open, human-cleared browser to get past DataDome's anti-bot wall (see [Jobs.bg & DataDome](#jobsbg--datadome))
- 🔑 **Authenticated Scraping** — One-time login mode (`--login`) saves cookies for authenticated scraping. **(⚠️ USE AT YOUR OWN RISK: Logging in for automation may lead to account suspension/banning by LinkedIn).**
- 🛑 **Graceful Shutdown** — Close the browser window to stop the program instantly

### LinkedIn-Specific
- 🔗 **Dual Strategy** — Guest API (fast) with full-browser fallback
- 📄 **Guest API Detail Endpoint** — Fetches full job descriptions without navigating pages or dismissing modals
- 🔗 **URL Validation** — Only processes valid `/jobs/view/` URLs; filters out company pages
- 🧹 **Content Sanitisation** — Detects and discards sign-in modal garbage in descriptions
- 📊 **Enhanced Card Parsing** — Extracts seniority, employment type, and applicant count from search results

### Jobs.bg-Specific
- 🇧🇬 **Location Mapping** — Bulgarian city IDs (Plovdiv = `2`) mapped in config
- 🔄 **Captcha Detection** — Auto-detects Cloudflare/slider challenges, pauses and waits for manual solve
- 🖼️ **Iframe Extraction** — Grabs job descriptions embedded in HTML iframes

### Matching & Export
- 🎯 **AI Profile Matching** — Scores jobs against your PDF resume (info-only column; the gates decide the verdict)
- 📊 **Two-CSV Output** — A **leads** CSV (`keep` + `manual_review`, sorted by rank) and a separate **rejects** audit CSV (failing gate + reasons) so a false-reject is never lost
- 📊 **Rich CSV Export** — Deduped, UTF-8 BOM (Excel-friendly), includes verdict, gate statuses, parsed comp, matched skills, salary info, seniority & employment type
- 📈 **Progress Bars** — Live tracking during description fetching (via tqdm)
- 📅 **Date Filters** — Only see jobs from the past day, week, or month

## Quick Start

### 1. Python Environment Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate on Windows:
.venv\Scripts\activate

# Activate on macOS/Linux:
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Validate Installation

```bash
python validate_dependencies.py
```

### 4. (Optional) Add Your Profile PDF

Place your resume/CV as `my_resume.pdf` in the project root. The scraper will automatically extract skills for matching.

You can also use a different filename: `--profile custom_resume.pdf`

### 5. (Optional) Authenticate for Full Job Descriptions

> [!CAUTION]
> **USE AT YOUR OWN RISK.** Using your personal LinkedIn account with automation/scraping tools violates LinkedIn's Terms of Service and may result in your account being permanently restricted or banned. If you choose to use authenticated scraping, it is highly recommended to use a burner/test account.

LinkedIn requires authentication to view full job descriptions. Run the one-time login mode:

```bash
python main.py --login
```

This opens a visible browser. Log in to your LinkedIn account, then close the browser. Cookies are saved to `linkedin_cookies.json` and reused automatically on future runs.

> **💡 Tip:** You can also export cookies manually from your browser and save them as `linkedin_cookies.json`.

### 6. Run the Scraper

```bash
# ─── LinkedIn (default) ───────────────────────
python main.py                                    # Default search
python main.py --quick                            # Skip description fetching
python main.py --headless                         # Run without visible browser
python main.py --days 7 --max-jobs 50             # Past week, limit 50

# ─── Jobs.bg ──────────────────────────────────
python main.py --site jobs.bg                     # Scrape Jobs.bg (headed, recommended)
python main.py --site jobs.bg --max-jobs 100      # Limit to 100 jobs
python main.py --site jobs.bg --cdp --days 7      # Attach to your cleared browser (DataDome bypass)

# ─── Common Options ───────────────────────────
python main.py --profile my_resume.pdf            # Custom PDF profile
python main.py --keywords "Data Analyst" "DevOps" # Override keywords
python main.py --verbose                          # Debug logging
python main.py --login                            # One-time LinkedIn login
python main.py --cookies my_cookies.json          # Use custom cookie file
```

> **💡 First time using Jobs.bg?** Jobs.bg is protected by **DataDome**, which blocks fresh automated browsers. The reliable path is `--cdp` mode — see [Jobs.bg & DataDome](#jobsbg--datadome) below.

### 7. Review Results

Each run writes **two** CSVs per site — a **leads** file (kept + manual-review, ranked) and a **rejects** audit file:

| Site | Leads CSV | Rejects CSV |
|------|-----------|-------------|
| LinkedIn | `linkedin_leads.csv` | `linkedin_leads-rejects.csv` |
| Jobs.bg | `Jobs.bg-leads.csv` | `Jobs.bg-rejects.csv` |

#### Leads CSV Columns

| Column | Description |
|--------|-------------|
| Verdict | `keep` or `manual_review` |
| Rank | Sort key (higher = apply-first) |
| Job Title | Position name |
| Company Name | Hiring company |
| Location | City/country or Remote |
| Posting Date | When the job was posted |
| Salary Info | Raw salary text (when available) |
| Parsed Comp (EUR/yr gross) | Normalised comp used by Gate 3 |
| Gate1 Lane / Gate2 Geo / Gate3 Ceiling | Per-gate status (`pass` / `soft` / `hard_fail`) |
| Gate Reasons | Why each gate decided as it did |
| Seniority / Employment Type / Applicants | Card metadata (when available) |
| Description | Full job description (up to 5000 chars) |
| Job URL | Direct link to the listing |
| Search Keyword / Search Location | Which search found it |
| Match Score / Matched Skills | Legacy skill overlap (info-only) |

The **rejects** CSV carries `Failing Gates` + `Reject Reasons` instead of Verdict/rank, so you can audit anything the filter dropped.

### 8. Run the Tests & Demo

The project includes an isolated test suite and a demo script that won't write to your main CSV files. It uses a mocked "John Doe" engineering profile to demonstrate the AI matching.

```bash
# Run unit tests
python -m pytest tests/

# Run the live demo scrape (Isolated output to tests/ folder)
python tests/demo_scrape.py
```

## CLI Reference

| Flag | Description | Default |
|------|-------------|---------|
| `--site {linkedin,jobs.bg}` | Select target site | `linkedin` |
| `--profile FILE` | Path to PDF resume for matching | `my_resume.pdf` |
| `--quick` | Skip description fetching (faster) | Off |
| `--keywords K1 K2…` | Override search keywords | From config |
| `--headless` | Run browser without GUI | Off |
| `--days N` | Only jobs from past N days (1, 7, 30) | All time |
| `--max-jobs N` | Stop after N jobs | Unlimited |
| `--output FILE` | Override the default output CSV path | `OUTPUT_CSV` |
| `--log-file FILE` | Set a custom log file path | `scraper.log` |
| `--login` | One-time login mode: opens browser for you to sign in, then saves cookies. (**⚠️ Account Ban Risk**) | Off |
| `--cookies FILE` | Path to a JSON cookie file for authenticated scraping | `linkedin_cookies.json` |
| `--cdp` | Attach to a running, human-cleared browser over CDP (jobs.bg / DataDome bypass) | Off |
| `--cdp-endpoint URL` | CDP endpoint to attach to | `http://127.0.0.1:9222` |
| `--verbose` | Debug logging | Off |

## Configuration

Copy `config.example.py` to `config.py` and edit it to customise:

- **Target Site** — Default scraping target (`linkedin` or `jobs.bg`)
- **Keywords** — Job titles to search for
- **Locations** — LinkedIn geoIds + Jobs.bg location IDs
- **Work Types** — Remote (2), Hybrid (3), On-site (1)
- **Date Filter** — Default date range for job postings
- **Experience Levels** — Internship, Entry, Associate, Mid-Senior, Director, Executive
- **Gate 1 — Lane** — `LANE_ALLOW` (strong terms) + `LANE_ALLOW_WEAK` (broad terms; need ≥2 to pass alone) + `LANE_DENY` (operator-lane penalties) + `TITLE_HARD_DENY` (instant-reject titles)
- **Gate 2 — Geo** — `LOCATION_ALLOW` / `LOCATION_DENY` / `LOCATION_SOFT` keyword lists
- **Gate 3 — Ceiling** — `SALARY_GOAL_EUR` / `SALARY_FLOOR_EUR` / `SALARY_AUTO_REJECT_BELOW_EUR`, plus `FX_RATES` and `NET_TO_GROSS_FACTOR` for normalisation
- **Rank Weights** — `RANK_WEIGHTS` ({w_lane, w_geo, w_comp}) for ordering kept leads
- **Skill Weights** — Primary (1.0), Domain (0.8), Tools (0.5) — info-only since v3.2
- **Delays** — Min/max seconds between requests; `CDP_*` knobs pace `--cdp` runs to stay under DataDome's radar
- **Match Threshold** — Minimum skill-overlap score for the info-only "Good Match" label
- **Session Directory** — Where persistent browser cookies are stored

### Environment Variables

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Supported variables:
- `TARGET_SITE` — Default site (`linkedin` or `jobs.bg`)
- `PROXY_URL` — HTTP proxy for rate limiting (e.g., `http://user:pass@proxy:8080`)
- `MAX_JOBS_PER_RUN` — Limit total jobs scraped (overridden by `--max-jobs`)
- `HEADLESS` — Set to `true` to default to headless mode

## Project Structure

```
├── main.py                  # Entry point with CLI & orchestrator (fetch → score → gate → route)
├── config.example.py        # Template: search params, gate keyword lists & thresholds (copy to config.py)
├── gatekeeper.py            # 3-gate filter engine: evaluate(job) → Verdict (keep/manual_review/reject)
├── scraper.py               # LinkedIn scraping engine (API + browser + Guest API detail endpoint)
├── jobsbg_scraper.py        # Jobs.bg scraping engine (+ DataDome detection, detail-page geo)
├── cdp_session.py           # CDP attach mode + human pacing (jobs.bg DataDome bypass)
├── stealth_config.py        # Browser stealth, real-browser channel & proxy configuration
├── csv_export.py            # Two-CSV writer (leads + rejects) with dedup & rank sort
├── profile_matcher.py       # Skill scoring (info-only since v3.2)
├── requirements.txt         # Python dependencies
├── README.md                # This file
├── .gitignore               # Git ignore rules
├── .env.example             # Environment variables template
├── validate_dependencies.py # Dependency validation script
├── tests/                   # Pytest suite + demo
│   ├── demo_scrape.py       # Isolated demo scrape script
│   ├── test_gatekeeper.py   # 3-gate filter unit tests
│   ├── test_cdp_session.py  # CDP tab-ownership tests
│   └── test_profile_matcher.py # Unit tests for skill extraction
├── Jobs.bg-leads.csv        # Jobs.bg leads (generated, git-ignored)
├── Jobs.bg-rejects.csv      # Jobs.bg rejects audit (generated, git-ignored)
├── linkedin_cookies.json    # Saved LinkedIn cookies (generated, git-ignored)
├── .browser_session/        # Persistent browser data (generated, git-ignored)
└── scraper.log              # Runtime log (generated, git-ignored)
```

## Jobs.bg & DataDome

Jobs.bg is fronted by **DataDome**, a commercial anti-bot service. It fingerprints the *browser environment* and *navigation pattern* — not just your IP — so a fresh Playwright browser (bundled Chromium, real Chrome, or Edge) gets a hard block (`403` / "Please enable JS and disable any ad blocker") even with stealth patches and a matching user-agent.

The reliable workaround is **`--cdp` mode**: you solve DataDome once as a human, and the scraper rides that already-cleared session.

```bash
# 1. Launch your real browser with a remote-debugging port (close other windows of it first):
#    Edge:   msedge.exe  --remote-debugging-port=9222
#    Chrome: chrome.exe  --remote-debugging-port=9222

# 2. In that browser, open jobs.bg and solve the DataDome challenge once (until listings show).

# 3. Run the scraper attached to that cleared session:
python main.py --site jobs.bg --cdp --days 7
```

How it works: the scraper connects over the Chrome DevTools Protocol, **reuses your existing browser context** (inheriting the DataDome clearance cookie), and runs the search + description fetches through it. It opens its own tab (and cleans it up afterwards) without touching your other tabs.

**Pacing:** sustained machine-speed requests will make DataDome re-challenge even a cleared session. `--cdp` mode therefore paces each fetch with a randomised delay plus a periodic longer pause (tunable via the `CDP_*` knobs in `config.py`). Expect roughly 20–30s per job — deliberately human.

> **Note:** This is a *personal, small-scale* technique for scraping data you can already see in your own browser. Respect jobs.bg's Terms of Service and keep volumes low.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Rate limited (429) | Increase `MIN_DELAY` / `MAX_DELAY` in config.py, or use `PROXY_URL` |
| Sign-in modal won't dismiss | The scraper retries 3 times, then skips |
| No results for a keyword | Try broader terms, different location, or remove `--days` filter |
| CSV encoding issues | File uses UTF-8 with BOM — open with Excel or set encoding manually |
| Selectors broken (LinkedIn) | LinkedIn changes HTML frequently — update selectors in `scraper.py` |
| Selectors broken (Jobs.bg) | Update selectors in `jobsbg_scraper.py` (`.mdc-card`, `.job-view-left-column`) |
| Browser closed → program hangs | v3.0 detects window close and exits cleanly |
| Description is empty | URL was filtered or modal text was discarded; try `--login` first for authenticated access |
| Jobs.bg blocked / "Please enable JS" / 403 | DataDome is blocking automation — use `--cdp` mode (see [Jobs.bg & DataDome](#jobsbg--datadome)) |
| Jobs.bg re-challenges mid-run | Requests too fast — `--cdp` mode paces automatically; raise the `CDP_*` delays in config |
| Another instance already running | Remove `scraper.lock` file if no other instance is active |

### Rate Limiting (429 Errors)

If you get rate-limited:
1. Increase `MIN_DELAY` and `MAX_DELAY` in `config.py`
2. Reduce `MAX_PAGES_PER_SEARCH`
3. Use a proxy (configure `PROXY_URL` in `.env`)
4. Wait 24 hours before trying again

### Playwright Browser Issues

```bash
# Reinstall browsers
playwright install chromium
playwright install --force chromium

# Check installation
python -c "import playwright; print(playwright.__version__)"
```

### Profile Matching Not Working

If jobs show 0% match score:
1. Ensure your PDF is in the project root
2. Check PDF is readable (not scanned image)
3. Verify skill weights in `config.py` match your resume keywords
4. Try with `--profile your_resume.pdf` flag

### Getting Full Job Descriptions

LinkedIn limits description visibility for non-authenticated users. To get full descriptions:

1. **One-time login (⚠️ RISK):** Run `python main.py --login`, log in, then close the browser
2. **Future runs:** Cookies are loaded automatically — descriptions will be fetched via the authenticated session
3. **Guest API fallback:** The scraper also tries the Guest API detail endpoint (`/jobs-guest/jobs/api/jobPosting/{id}`) which sometimes returns descriptions without login

## Changelog

### v3.2 (2026-06-29)
- **Added**: 3-gate filter (`gatekeeper.py`) — lane × geo × ceiling → `keep` / `manual_review` / `reject`, replacing score-only surfacing
- **Added**: Two-CSV output — a ranked **leads** file and a separate **rejects** audit file (nothing silently dropped)
- **Added**: `--cdp` / `--cdp-endpoint` — attach to a human-cleared browser to scrape jobs.bg past DataDome, with human-paced fetching (`CDP_*` config)
- **Added**: Weighted lane matching (`LANE_ALLOW_WEAK`) so generic roles don't pass on a single broad keyword like "python"
- **Added**: Comp parser — currency/period/net-gross detection, FX-normalisation to EUR/yr gross, day/week/hour-rate annualisation, range → top-of-range
- **Added**: DataDome-aware, content-based block detection for jobs.bg; detail-page location extraction feeding Gate 2
- **Changed**: Skill score demoted to an info-only CSV column (the gates decide the verdict)
- **Changed**: Run summary is now verdict-based (keep / manual_review / reject counts + top-ranked leads)
- **Fixed**: Comp gate — weekly/hourly rates now annualised; cleanly-disclosed below-floor figures correctly hard-fail
- **Fixed**: Removed duplicated dead description-fetch functions in `scraper.py`

### v3.1 (2026-04-05)
- **Added**: `--login` flag for one-time manual login with automatic cookie saving
- **Added**: `--cookies` flag to load cookies from a custom JSON file
- **Added**: Guest API detail endpoint (`/jobs-guest/jobs/api/jobPosting/{id}`) for fetching descriptions without page navigation
- **Added**: Enhanced job card parsing — extracts seniority, employment type, and applicant count
- **Added**: New CSV columns: Seniority, Employment Type, Applicants
- **Added**: Multi-strategy description extraction (Guest API → DOM selectors → JS injection → body fallback)
- **Fixed**: Duplicate lock code block in `main.py` causing false "instance already running" errors
- **Fixed**: Cookie injection into persistent browser context for authenticated scraping
- **Improved**: Description fetcher now tries Guest API first (faster, more reliable) before browser navigation
- **Improved**: Better modal garbage detection and filtering

### v3.0 (2026-04-03)
- **Added**: Jobs.bg scraping engine with `--site jobs.bg` flag
- **Added**: Persistent browser sessions (`.browser_session/`) — solve captcha once
- **Added**: Captcha auto-detection with 60-second manual solve window
- **Added**: Iframe extraction for Jobs.bg embedded job descriptions
- **Added**: Separate output files — `Jobs.bg-leads.csv` for Jobs.bg scrapes
- **Added**: Graceful shutdown — closing the browser stops the program
- **Added**: Randomised viewport sizes for better stealth fingerprinting
- **Fixed**: Job titles corrupted by material "star" icons on Jobs.bg
- **Fixed**: CSV column displacement from icon text injection
- **Fixed**: Final "Done!" message now shows correct output filename
- **Improved**: Browser recovery — full persistent context restart on crash

### v2.0 (2026-04-03)
- **Fixed**: PDF was re-read on every single job — now loaded once at startup
- **Fixed**: Company page URLs (`/company/`) were being scraped as job listings
- **Fixed**: Browser context crash cascade — auto-recovery with `BrowserSession`
- **Fixed**: Sign-in modal text polluting job descriptions — content sanitisation
- **Fixed**: Pagination mismatch with dynamic empty-page detection
- **Added**: `--headless` flag for server/CI usage
- **Added**: `--days` flag to filter by posting date (1, 7, 30 days)
- **Added**: `--max-jobs` flag to limit total jobs collected
- **Added**: `Matched Skills` column in CSV output
- **Added**: `Salary Info` column in CSV output
- **Added**: `tqdm` progress bars for description fetching
- **Added**: Timing breakdown (search phase vs. description phase)
- **Added**: `.env` file loading via `python-dotenv`
- **Added**: Proxy support from `PROXY_URL` env variable
- **Updated**: User-Agent strings to Chrome 131+ / Firefox 133+
- **Improved**: Smart description truncation at sentence boundaries

### v1.0 (2026-03-30)
- Initial release with dual scraping strategy, stealth mode, and CSV export

## Legal Notice

This tool is designed for **personal, small-scale use only**. It scrapes publicly accessible data from LinkedIn's guest pages (no login required) and Jobs.bg's public search results.

> [!WARNING]
> **The use of the `--login` feature is strictly at the user's own risk.**
> Automated interaction with LinkedIn while logged in is a violation of their Terms of Service. The developers of this tool are not responsible for any account restrictions, temporary suspensions, or permanent bans that may occur as a result of using this software.

Always:
- Respect each site's Terms of Service
- Use reasonable delays between requests
- Do not scrape personal profile data
- Comply with applicable data protection laws (GDPR, etc.)
