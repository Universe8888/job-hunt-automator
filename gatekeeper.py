"""
Job-Hunt Automator — Gatekeeper (the verdict engine).

The ONLY module that decides keep / manual_review / reject. Three independent,
PURE gates (no I/O, no scraping, no mutation of the job dict):

    Gate 1 — LANE     (passes_lane_gate)   : is the role in the AI-Governance-hybrid lane?
    Gate 2 — GEO      (passes_geo_gate)    : does it keep the candidate BG-taxed?
    Gate 3 — CEILING  (parse_comp / passes_comp_gate) : can disclosed comp clear the goal?

`evaluate(job)` runs all three and composes a Verdict. Verdict precedence:
reject if ANY gate hard_fails; else manual_review if ANY gate is soft; else keep.
rank = w_lane*lane_hits + w_geo*geo_certainty + w_comp*comp_headroom.

The legacy profile_matcher skill score is INFO-ONLY — it never drives a verdict.
Keyword lists / thresholds are IMPORTED from config at module level so tests may
monkeypatch them as `gatekeeper.<NAME>` (matching logic re-reads the module
globals at call time; compiled regex is cached by list identity for speed).
"""

import re
import unicodedata

import config

logger = None  # gates are pure; no logging side effects by design.


# ──────────────────────────────────────────────────────────────────────────
# Config import (module-level so tests can monkeypatch gatekeeper.<NAME>).
# Pinned-contract constants are imported directly. Red-team-mandated extras
# (DENY_PENALTY, WORKING_DAYS_PER_YEAR, SALARY_FLOOR_FX_TOLERANCE) are read with
# a safe default so gatekeeper.py imports cleanly even against a config.py that
# only carries the pinned contract names.
# ──────────────────────────────────────────────────────────────────────────
LANE_ALLOW = config.LANE_ALLOW
LANE_DENY = config.LANE_DENY
TITLE_HARD_DENY = config.TITLE_HARD_DENY
# Broad terms that also appear in generic operator roles (python, automation, …).
# A lane match on WEAK terms only is not a confident lane hit -> manual_review,
# not pass. Strong terms = LANE_ALLOW minus LANE_ALLOW_WEAK. Default empty so
# pre-existing configs keep the old "any lane hit passes" behaviour.
LANE_ALLOW_WEAK = getattr(config, "LANE_ALLOW_WEAK", [])

LOCATION_ALLOW = config.LOCATION_ALLOW
LOCATION_DENY = config.LOCATION_DENY
LOCATION_SOFT = config.LOCATION_SOFT

SALARY_GOAL_EUR = config.SALARY_GOAL_EUR
SALARY_FLOOR_EUR = config.SALARY_FLOOR_EUR
SALARY_AUTO_REJECT_BELOW_EUR = config.SALARY_AUTO_REJECT_BELOW_EUR
DISCLOSED_COMP_REQUIRED = config.DISCLOSED_COMP_REQUIRED
FX_RATES = config.FX_RATES
NET_TO_GROSS_FACTOR = config.NET_TO_GROSS_FACTOR
RANK_WEIGHTS = config.RANK_WEIGHTS

# Red-team-mandated tunables — default if absent from config.
DENY_PENALTY = getattr(config, "DENY_PENALTY", 0.5)
WORKING_DAYS_PER_YEAR = getattr(config, "WORKING_DAYS_PER_YEAR", 220)
WORKING_WEEKS_PER_YEAR = getattr(config, "WORKING_WEEKS_PER_YEAR", 46)
WORKING_HOURS_PER_DAY = getattr(config, "WORKING_HOURS_PER_DAY", 8)
SALARY_FLOOR_FX_TOLERANCE = getattr(config, "SALARY_FLOOR_FX_TOLERANCE", 0.10)


# ──────────────────────────────────────────────────────────────────────────
# Value objects (verbatim per the pinned interface contract §9)
# ──────────────────────────────────────────────────────────────────────────
from dataclasses import dataclass, field


@dataclass
class GateResult:
    status: str                                   # "pass" | "soft" | "hard_fail"
    signal: float                                 # >= 0.0; gate-specific ranking signal
    reasons: list[str] = field(default_factory=list)


@dataclass
class ParsedComp:
    top_eur_gross_yr: float                       # normalized EUR/yr GROSS — the compared number
    currency: str                                 # "EUR" | "USD" | "GBP" | "BGN"
    period: str                                   # "yr" | "mo" (raw, pre-annualization)
    basis: str                                    # "gross" | "net" | "unknown"
    raw: str                                       # matched raw comp substring (audit)
    # Explicit confidence flags (replaces the old practice of encoding metadata as
    # substrings inside `raw` and reverse-parsing it — that conflated benign assumed
    # defaults with genuine parse uncertainty). The gate reads these directly.
    currency_inferred: bool = False               # no explicit currency symbol/code seen
    period_inferred: bool = False                 # no explicit /yr|/mo|/day cue — period guessed
    basis_inferred: bool = False                  # no explicit gross/net cue — assumed gross
    rate_unhandled: bool = False                  # hourly/weekly rate not confidently annualizable
    equity_present: bool = False                  # equity/OTE markers present (cash is a lower bound)


@dataclass
class Verdict:
    verdict: str                                  # "keep" | "manual_review" | "reject"
    gate1: GateResult                             # lane
    gate2: GateResult                             # geo
    gate3: GateResult                             # ceiling
    reasons: list[str] = field(default_factory=list)
    rank: float = 0.0


# ──────────────────────────────────────────────────────────────────────────
# Regex helpers — word-boundary phrase matching (reuses profile_matcher idiom
# `r'\b' + re.escape(term) + r'\b'`). Compiled patterns are cached keyed on the
# *identity* of the source list, so a monkeypatch (a new list object) triggers a
# rebuild while production (a stable list) compiles once.
# ──────────────────────────────────────────────────────────────────────────
_RX_CACHE: dict[int, list[tuple[str, "re.Pattern"]]] = {}


def _compiled(terms) -> list[tuple[str, "re.Pattern"]]:
    """Return [(term, compiled_word_boundary_rx)] for a phrase list, cached by id()."""
    key = id(terms)
    cached = _RX_CACHE.get(key)
    if cached is None:
        cached = [(t, re.compile(r"\b" + re.escape(t) + r"\b")) for t in terms]
        _RX_CACHE[key] = cached
    return cached


# ──────────────────────────────────────────────────────────────────────────
# GATE 1 — LANE
# ──────────────────────────────────────────────────────────────────────────
_LANE_NORM_PUNCT = re.compile(r"[/|,;()\[\]]")
_LANE_HYPHENS = re.compile(r"[‐-―]")
_WS = re.compile(r"\s+")


def _norm_lane(s: str) -> str:
    """Deterministic, accent/spacing-tolerant normalizer for Gate 1 matching."""
    s = (s or "").lower()
    s = s.replace(" ", " ")               # nbsp -> space
    s = _LANE_HYPHENS.sub("-", s)              # unicode hyphens/dashes -> ascii hyphen
    s = _LANE_NORM_PUNCT.sub(" ", s)           # token-gluing punctuation -> space
    s = _WS.sub(" ", s).strip()
    return s


def passes_lane_gate(job: dict) -> GateResult:
    """GATE 1 (lane). Reads job['title'], job['description']."""
    return _lane_core(job.get("title", ""), job.get("description", ""))


def _lane_core(title: str, body: str) -> GateResult:
    nt = _norm_lane(title)
    nb = _norm_lane(body)
    surface = (nt + " " + nb).strip()
    reasons: list[str] = []

    title_denies = [t for t, rx in _compiled(TITLE_HARD_DENY) if rx.search(nt)]
    allow_hits = [t for t, rx in _compiled(LANE_ALLOW) if rx.search(surface)]

    # STAGE A — title hard-deny (FIX #1 lives here).
    # Red-team #2: a TITLE_HARD_DENY hit is overridden (downgraded to the normal
    # lane scan) if the title ALSO carries a LANE_ALLOW phrase, so genuinely
    # in-lane titles with an embedded operator token survive to Stage B.
    title_allow_hits = [t for t, rx in _compiled(LANE_ALLOW) if rx.search(nt)]
    if title_denies and not title_allow_hits:
        reasons.append(f"GATE1 hard_fail: operator title matched {title_denies}")
        return GateResult(status="hard_fail", signal=0.0, reasons=reasons)
    if title_denies and title_allow_hits:
        reasons.append(
            f"GATE1 note: operator token {title_denies} in title overridden by "
            f"lane term {title_allow_hits} (continuing to body scan)"
        )

    deny_hits = [t for t, rx in _compiled(LANE_DENY) if rx.search(nb)]
    lane_hits = len(allow_hits)

    # STAGE B — lane-allow keyword count, weighted (strong vs weak).
    if lane_hits == 0:
        # A role with no LANE_ALLOW term in title/body is out of lane -> hard_fail
        # (never soft). A co-occurring LANE_DENY operator hit is audit context.
        reason = "GATE1 hard_fail: no ai-governance lane terms found"
        if deny_hits:
            reason += f" (operator-lane signal present {deny_hits})"
        reasons.append(reason)
        return GateResult(status="hard_fail", signal=0.0, reasons=reasons)

    # Live jobs.bg data showed generic roles (Data Engineer, Monday.com Specialist)
    # passing on a single broad term ('python' / 'automation'). Split hits: a PASS
    # needs >=1 STRONG hit OR >=2 WEAK hits; a lone weak hit -> soft (manual_review),
    # never a clean pass and never a reject (stays in the safe holding bucket).
    weak_set = {w.casefold() for w in LANE_ALLOW_WEAK}
    strong_hits = [h for h in allow_hits if h.casefold() not in weak_set]
    weak_hits = [h for h in allow_hits if h.casefold() in weak_set]

    if not strong_hits and len(weak_hits) < 2:
        reason = (f"GATE1 soft: only weak lane term(s) {weak_hits} "
                  f"(no strong ai-governance signal) -> manual_review")
        reasons.append(reason)
        if deny_hits:
            reasons.append(f"GATE1 note: operator-lane signal present {deny_hits}")
        # signal stays low but non-zero so genuine-but-weak rolls rank below strong.
        return GateResult(status="soft", signal=0.5, reasons=reasons)

    # LANE_DENY body penalty — de-rank only, never flips the verdict.
    # Red-team #10: bound the penalty so it can never drive a genuine lane match
    # below half its raw hit count.
    penalized = float(lane_hits) - DENY_PENALTY * len(deny_hits)
    signal = max(penalized, 0.5 * float(lane_hits))

    reasons.append(
        f"GATE1 pass: lane_hits={lane_hits} "
        f"(strong={strong_hits}, weak={weak_hits})"
    )
    if deny_hits:
        reasons.append(
            f"GATE1 note: operator-lane penalty via {deny_hits} "
            f"(signal floored to {signal:.2f}; manual_review advised)"
        )
    return GateResult(status="pass", signal=signal, reasons=reasons)


# ──────────────────────────────────────────────────────────────────────────
# GATE 2 — GEO
# ──────────────────────────────────────────────────────────────────────────
# Bare ambiguous single tokens are matched with word boundaries; multi-word
# phrases are matched as substrings of the normalized haystack.
_GEO_BARE_TOKENS = {
    "remote", "hybrid", "eor", "deel", "contractor", "freelance", "bulgaria",
    "bulgarian", "sofia", "plovdiv", "varna", "burgas",
}
_GEO_BG_CITY_TOKENS = {
    "bulgaria", "bulgarian", "sofia", "plovdiv", "varna", "burgas",
}
_GEO_REMOTE_EOR_HINT = (
    "remote", "anywhere", "work from anywhere", "employer of record", "eor",
    "deel", "remote.com", "contractor of record", "we can hire", "we hire in",
    "eligible to work in bulgaria",
)
# Explicit EOR / remote-EMEA / remote-global allow phrases that justify the
# red-team #7 EOR override (a US-auth deny co-occurring with these -> soft).
_GEO_EOR_OVERRIDE_HINT = (
    "employer of record", "eor", "deel", "remote.com", "contractor of record",
    "remote (emea)", "remote emea", "emea remote", "remote (eu)", "remote eu",
    "eu remote", "remote (global)", "global remote", "remote - global",
    "work from anywhere", "anywhere in the world", "anywhere in europe",
    "anywhere in emea", "anywhere in the eu", "we can hire you in bulgaria",
    "we hire in bulgaria", "eligible to work in bulgaria",
)
# Deny phrases that are work-auth boilerplate rather than exclusive US-only
# requirements — eligible for the EOR override (red-team #7).
_GEO_WORKAUTH_BOILERPLATE = (
    "authorized to work in the us", "us work authorization",
)
_GEO_NEGATORS = ("not", "no longer", "except", "isn't", "isnt", "never", "without")


def _norm_geo(s: str) -> str:
    """NFKC + lower-case + whitespace-collapse. Keeps diacritics (ü/ö)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _WS.sub(" ", s).strip()
    return s


def _geo_matches(phrase: str, hay: str) -> bool:
    """Bare ambiguous token -> word-boundary; multi-word phrase -> substring."""
    if phrase in _GEO_BARE_TOKENS:
        return re.search(r"\b" + re.escape(phrase) + r"\b", hay) is not None
    return phrase in hay


def _geo_is_negated(phrase: str, hay: str) -> bool:
    """Red-team #9: True if a negator sits within ~3 tokens before any occurrence
    of `phrase` in `hay` (e.g. 'not remote within the us')."""
    for m in re.finditer(re.escape(phrase), hay):
        prefix = hay[: m.start()].split()
        window = prefix[-3:]
        if any(neg in window for neg in _GEO_NEGATORS):
            return True
    return False


def _geo_bg_city_without_remote(allow_hits: list[str], hay: str) -> bool:
    """True when the ONLY allow hits are bare BG-city/bulgaria tokens AND no
    remote/EOR allow phrase AND no soft remote signal is present."""
    if not allow_hits:
        return False
    if any(h not in _GEO_BG_CITY_TOKENS for h in allow_hits):
        return False
    if any(hint in hay for hint in _GEO_REMOTE_EOR_HINT):
        # a bare-token "remote" hint still counts even though it's word-bounded
        if re.search(r"\bremote\b", hay) or any(
            hint in hay for hint in _GEO_REMOTE_EOR_HINT if hint != "remote"
        ):
            return False
    return True


def passes_geo_gate(job: dict) -> GateResult:
    """GATE 2 (geo). Reads job['location'] + job['title']+job['description'].
    Precedence deny > allow > soft > unknown. signal = geo_certainty."""
    loc = (job.get("location") or "").strip()
    body = (job.get("title") or "") + "\n" + (job.get("description") or "")
    hay = _norm_geo(loc + "\n" + body)

    deny_hits = [
        p for p, _ in _compiled(LOCATION_DENY)
        if _geo_matches(p, hay) and not _geo_is_negated(p, hay)
    ]
    allow_hits = [p for p, _ in _compiled(LOCATION_ALLOW) if _geo_matches(p, hay)]
    soft_hits = [p for p, _ in _compiled(LOCATION_SOFT) if _geo_matches(p, hay)]

    # Step 1 — deny (highest precedence) ...
    if deny_hits:
        has_clean_remote_allow = any(h not in _GEO_BG_CITY_TOKENS for h in allow_hits)
        eor_override = any(hint in hay for hint in _GEO_EOR_OVERRIDE_HINT)
        only_boilerplate = all(d in _GEO_WORKAUTH_BOILERPLATE for d in deny_hits)

        # Red-team #7: a US-auth/regional deny that co-occurs with an explicit
        # EOR / remote-EMEA / remote-global allow downgrades to soft (the role
        # plausibly hires EMEA via EOR — only a recruiter can confirm).
        if only_boilerplate and (eor_override or has_clean_remote_allow):
            reasons = [
                "GEO manual_review (EOR override): work-auth deny "
                f"{deny_hits[:3]} co-occurs with remote/EOR allow"
            ]
            return GateResult(status="soft", signal=0.5, reasons=reasons)

        # Red-team #9 fallback: a deny phrase AND a clean EMEA/EU/global remote
        # allow both present (contrast sentence) -> soft for human disambiguation.
        if has_clean_remote_allow and eor_override:
            reasons = [
                "GEO manual_review (contrast): deny "
                f"{deny_hits[:3]} co-occurs with clean remote allow {allow_hits[:3]}"
            ]
            return GateResult(status="soft", signal=0.5, reasons=reasons)

        reasons = ["GEO hard_fail: " + ", ".join(deny_hits[:4])]
        return GateResult(status="hard_fail", signal=0.0, reasons=reasons)

    # Step 2 — clean allow (no deny), unless BG-city-only without a remote signal.
    if allow_hits and not _geo_bg_city_without_remote(allow_hits, hay):
        reasons = ["GEO pass: " + ", ".join(allow_hits[:4])]
        return GateResult(status="pass", signal=1.0, reasons=reasons)

    # Step 3 — soft / ambiguous.
    if soft_hits:
        reasons = ["GEO manual_review (ambiguous): " + ", ".join(soft_hits[:4])]
        return GateResult(status="soft", signal=0.5, reasons=reasons)

    # Step 4 — BG-city allow only, no remote/EOR signal -> demote to soft.
    if allow_hits:
        reasons = [
            "GEO manual_review: BG location without remote/EOR signal ("
            + ", ".join(allow_hits[:3]) + ")"
        ]
        return GateResult(status="soft", signal=0.5, reasons=reasons)

    # Step 5 — empty/unknown location, no allow, no deny -> soft (Fix #2).
    reasons = ["GEO manual_review: location unknown/unextracted"]
    return GateResult(status="soft", signal=0.5, reasons=reasons)


# ──────────────────────────────────────────────────────────────────────────
# GATE 3 — CEILING (parser + verdict)
# ──────────────────────────────────────────────────────────────────────────
_COMP_DASHES = re.compile(r"[‐-―−]")
_COMP_SPACES = re.compile(r"[     ]")

_CUR_SYMBOL = {"€": "EUR", "$": "USD", "£": "GBP"}
_CUR_CODE = {"eur": "EUR", "usd": "USD", "gbp": "GBP", "bgn": "BGN"}
_LEVA = {"лв", "лв.", "лев", "лева"}

# A number token: 90,000 / 90000 / 90 000 / 5.000 / 90k / 1.2m.
# Two alternatives, separated-groups first (so "90 000" / "5.000" group cleanly),
# then a plain contiguous run (so bare "72000" matches whole, not just "720").
_NUM = (
    r"(?:\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s?[kKmMкКмМ]?"
)
_MONEY = re.compile(
    r"(?P<pre_cur>[€$£]|\b(?:eur|usd|gbp|bgn)\b)?\s*"
    r"(?P<num>" + _NUM + r")"
    r"\s*(?P<post_cur>[€$£]|\b(?:eur|usd|gbp|bgn)\b|лв\.?|лева|лев)?",
    re.IGNORECASE | re.UNICODE,
)
_RANGE = re.compile(
    r"(?P<lo>" + _NUM + r")\s*(?:-|to|до)\s*(?P<hi>" + _NUM + r")",
    re.IGNORECASE | re.UNICODE,
)

_SALARY_CUES = (
    "salary", "compensation", "comp", "pay", "package", "ote", "/yr", "/year",
    "per annum", "per year", "годишно", "/mo", "/month", "per month", "месечно",
    "заплата", "remuneration", "gross", "net", "бруто", "нето", "wage", "/day",
    "per day", "day rate", "daily rate", "/hr", "/hour", "per hour",
)
# Non-comp number contexts to actively exclude (standards numbers etc.).
_NONCOMP_NEG = ("iso", "soc", "27001", "27002", "42001", "nist", "version", "v.")

_MONTHLY_CUES = (
    "/mo", "/month", "per month", "a month", " pm", "месечно", "на месец",
    "мес", "monthly",
)
_YEARLY_CUES = (
    "/yr", "/year", "/annum", "per annum", "per year", "a year", "годишно",
    "на година", " pa ", "annually", "annual",
)
_DAILY_CUES = ("/day", "per day", "day rate", "p/d", "daily rate")
_HOURLY_CUES = ("/hr", "/hour", "per hour", "hourly", "на час")
_WEEKLY_CUES = ("/week", "per week", "weekly")

_NET_CUES = (
    "net", "take-home", "take home", "in hand", "on hand", "after tax",
    "net of tax", "нето", "на ръка", "чисто",
)
_GROSS_CUES = ("gross", "бруто", "before tax", "pre-tax", "pretax")

_EQUITY_CUES = (
    "ote", "on-target", "on target", "+ equity", "plus equity", "+ commission",
    "plus commission", "rsu", "stock option", "stock options", "variable pay",
    "uncapped commission", "bonus", "equity",
)


def _has_cue(text: str, cues) -> bool:
    """True if any cue appears in `text` on a WORD BOUNDARY.

    Cue lists must NOT be matched with a bare `cue in text` substring test:
    short cues leak into unrelated words ("ote" in "rem**ote**", "net" in
    "**net**work"/"inter**net**", "gross" in "en**gross**ed"), which previously
    mis-tagged every remote role as equity/OTE and could flip a gross salary to
    net. We wrap each cue in \\b…\\b. Cues that begin/end with non-word characters
    (e.g. "/yr", "+ equity", " pm") are matched literally — \\b is meaningless
    there and those tokens don't suffer the substring-leak problem anyway.
    """
    for cue in cues:
        c = cue.strip()
        if not c:
            continue
        # If the cue's outer chars are non-word, a literal substring test is
        # safe (and \b would misbehave); otherwise require word boundaries.
        if c[0].isalnum() and c[-1].isalnum():
            if re.search(r"\b" + re.escape(c) + r"\b", text):
                return True
        elif c in text:
            return True
    return False


def _clean_money_text(text: str) -> str:
    t = _COMP_SPACES.sub(" ", text or "")
    t = _COMP_DASHES.sub("-", t)
    return t


def _to_float(num_str: str) -> float | None:
    """Locale-aware magnitude parse. Handles 90,000 / 90 000 / 5.000 / 90k / 1.2m."""
    s = num_str.strip().lower()
    mult = 1.0
    if s.endswith(("k", "к")):
        mult = 1000.0
        s = s[:-1].strip()
    elif s.endswith(("m", "м")):
        mult = 1_000_000.0
        s = s[:-1].strip()

    s = s.replace(" ", "")
    if not s:
        return None

    has_comma = "," in s
    has_dot = "." in s

    try:
        if has_comma and has_dot:
            # Last separator is the decimal mark.
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif has_comma:
            # comma + exactly 3 trailing digits and no later sep => thousands
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s):
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        elif has_dot:
            if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
                s = s.replace(".", "")
            # else: genuine decimal — leave as-is
        val = float(s)
    except ValueError:
        return None
    return val * mult


def _window(t: str, start: int, end: int, radius: int) -> str:
    return t[max(0, start - radius): min(len(t), end + radius)]


def _detect_currency(pre: str | None, post: str | None, win: str) -> tuple[str, bool]:
    """Returns (currency, explicit). explicit=False means defaulted to EUR."""
    for tok in (pre, post):
        if not tok:
            continue
        low = tok.strip().lower()
        if tok.strip() in _CUR_SYMBOL:
            return _CUR_SYMBOL[tok.strip()], True
        if low in _CUR_CODE:
            return _CUR_CODE[low], True
        if low.rstrip(".") in {"лв", "лев", "лева"} or low in _LEVA:
            return "BGN", True
    # Window fallback.
    for sym, cur in _CUR_SYMBOL.items():
        if sym in win:
            return cur, True
    for code, cur in _CUR_CODE.items():
        if re.search(r"\b" + code + r"\b", win):
            return cur, True
    if any(l in win for l in ("лв", "лев", "лева")):
        return "BGN", True
    return "EUR", False


def parse_comp(text: str) -> ParsedComp | None:
    """GATE 3 parser (pure). Returns None when no comp figure is detected.
    FIX #4 order: currency -> period (mo->*12, day->*WORKING_DAYS) -> range ->
    top-of-range -> net(->*NET_TO_GROSS_FACTOR)/gross(assume gross) -> FX to EUR."""
    if not text:
        return None
    raw_text = _clean_money_text(text)
    t = raw_text.casefold()

    equity_present = _has_cue(t, _EQUITY_CUES)

    candidates: list[dict] = []
    for m in _MONEY.finditer(t):
        num_raw = m.group("num")
        if not num_raw or not re.search(r"\d", num_raw):
            continue
        pre = m.group("pre_cur")
        post = m.group("post_cur")
        win = _window(t, m.start(), m.end(), 25)

        has_currency = bool(pre or post) or any(s in win for s in ("€", "$", "£")) \
            or re.search(r"\b(?:eur|usd|gbp|bgn)\b", win) is not None \
            or any(l in win for l in ("лв", "лев", "лева"))
        has_cue = _has_cue(win, _SALARY_CUES)
        if not has_currency and not has_cue:
            continue  # bare number with no anchor — not comp (red-team #11)

        # Exclude standards/version numbers even if a cue is incidentally nearby.
        tight = _window(t, m.start(), m.end(), 6)
        if any(neg in tight for neg in _NONCOMP_NEG):
            continue

        # A number glued to a degree or percent sign is not money: "360° sales",
        # "9000% growth", "20% bonus". Salary figures never carry these units.
        # (Found in the live IT-category sample: "GROSS offers genuine 360° sales
        # solutions" invented a €360/mo figure because the company name supplied a
        # 'gross' cue.) Look at the char that immediately follows the matched run,
        # skipping a single optional space.
        after = t[m.end():m.end() + 2].lstrip()
        if after[:1] in ("°", "%"):
            continue

        mag = _to_float(num_raw)
        if mag is None or mag < 1.0:
            continue

        candidates.append({
            "match": m,
            "pre": pre,
            "post": post,
            "win": win,
            "mag": mag,
            "num_raw": num_raw,
            "has_currency": has_currency,
        })

    if not candidates:
        return None

    # ---- candidate selection (A.8) ----------------------------------------
    # Prefer explicit-currency over cue-only; prefer an OTE/on-target figure over
    # a base figure when equity markers are present; tie-break on highest EUR.
    def _ote_proximity(c) -> int:
        return 1 if any(k in c["win"] for k in ("ote", "on-target", "on target")) else 0

    best = None
    best_eur = -1.0
    best_parsed: ParsedComp | None = None
    for c in candidates:
        parsed = _normalize_candidate(c, t, raw_text)
        if parsed is None:
            continue
        eur = parsed.top_eur_gross_yr
        score_tuple = (
            int(c["has_currency"]),
            _ote_proximity(c),
            eur,
        )
        cur_best = (
            int(best["has_currency"]) if best else -1,
            _ote_proximity(best) if best else -1,
            best_eur,
        )
        if best is None or score_tuple > cur_best:
            best, best_eur, best_parsed = c, eur, parsed

    if best_parsed is None:
        return None
    # Record equity/OTE presence on the explicit flag (the gate reads the flag, not
    # the string); keep a short display note in raw for the audit trail.
    if equity_present:
        best_parsed.equity_present = True
        best_parsed.raw = (best_parsed.raw + " [equity/OTE markers present]").strip()
    return best_parsed


def _normalize_candidate(c: dict, t: str, raw_text: str) -> ParsedComp | None:
    """Run currency -> period -> range -> net/gross -> annualize -> FX on one
    money candidate. Returns a ParsedComp or None if implausible."""
    m = c["match"]
    win = c["win"]

    # ---- currency ----
    currency, cur_explicit = _detect_currency(c["pre"], c["post"], win)

    # ---- range (top-of-range), shared currency/period unless re-stated ----
    win30 = _window(t, m.start(), m.end(), 30)
    amount = c["mag"]
    rng = _RANGE.search(win30)
    raw_sub = raw_text[m.start():m.end()].strip()
    if rng:
        lo = _to_float(rng.group("lo"))
        hi = _to_float(rng.group("hi"))
        # `k`/`m` on one side only — apply to both (A.5 / red-team #14).
        lo_has_mult = bool(re.search(r"[kKmMкКмМ]\s*$", rng.group("lo").strip()))
        hi_has_mult = bool(re.search(r"[kKmMкКмМ]\s*$", rng.group("hi").strip()))
        if lo is not None and hi is not None:
            if hi_has_mult and not lo_has_mult and hi >= 1000:
                factor = 1000.0 if "k" in rng.group("hi").lower() else 1_000_000.0
                if lo < 1000:
                    lo *= factor
            if lo_has_mult and not hi_has_mult and lo >= 1000:
                factor = 1000.0 if "k" in rng.group("lo").lower() else 1_000_000.0
                if hi < 1000:
                    hi *= factor
            amount = max(lo, hi)
            raw_sub = raw_text[max(0, m.start()):min(len(raw_text), m.start() + rng.end())].strip()

    # ---- period detection ----
    period = "yr"
    period_inferred = False
    unhandled_unit = False
    annual_multiplier = 1.0
    if _has_cue(win30, _MONTHLY_CUES) or re.search(r"\bмес\b", win30):
        period, annual_multiplier = "mo", 12.0
    elif _has_cue(win30, _YEARLY_CUES):
        period, annual_multiplier = "yr", 1.0
    elif _has_cue(win30, _DAILY_CUES):
        # Red-team #4: day rate -> annualize via WORKING_DAYS_PER_YEAR.
        period, annual_multiplier = "yr", float(WORKING_DAYS_PER_YEAR)
    elif _has_cue(win30, _WEEKLY_CUES):
        # Weekly rate -> annualize across the working year. Marked rate_unhandled so
        # the gate keeps it soft near the floor (working-weeks vary), but the figure
        # is now correctly scaled instead of treated as a raw annual total.
        period, annual_multiplier = "yr", float(WORKING_WEEKS_PER_YEAR)
        unhandled_unit = True
    elif _has_cue(win30, _HOURLY_CUES):
        # Hourly rate -> annualize via working hours/year (WORKING_DAYS * hours/day).
        period, annual_multiplier = "yr", float(WORKING_DAYS_PER_YEAR) * float(WORKING_HOURS_PER_DAY)
        unhandled_unit = True
    else:
        # Unstated period default: a low bare figure reads as monthly; an
        # already-annual-magnitude figure stays yearly. (No currency-keyed
        # override: a disclosed annual BGN total must annualize as /yr.)
        period_inferred = True
        if amount < 25000:
            period, annual_multiplier = "mo", 12.0
        else:
            period, annual_multiplier = "yr", 1.0

    # ---- net vs gross ----
    # FIX #4: basis unstated -> ASSUME gross (do not inflate). The assumption is
    # recorded via the basis_inferred audit flag below; the field itself carries
    # the assumed "gross" per the parser contract.
    if _has_cue(win30, _NET_CUES):
        basis = "net"
        basis_inferred = False
    elif _has_cue(win30, _GROSS_CUES):
        basis = "gross"
        basis_inferred = False
    else:
        basis = "gross"
        basis_inferred = True

    # ---- annualize -> net/gross -> FX (A.7 order) ----
    annual = amount * annual_multiplier
    if basis == "net":
        annual *= NET_TO_GROSS_FACTOR
    fx = FX_RATES.get(currency, 1.0)
    top_eur = round(annual * fx)

    # Plausibility guard (A.2 magnitude bounds, post-annualization).
    if top_eur < 4000 or top_eur > 5_000_000:
        return None

    parsed = ParsedComp(
        top_eur_gross_yr=float(top_eur),
        currency=currency,
        period=period,
        basis=basis,
        raw=raw_sub or c["num_raw"].strip(),
        currency_inferred=not cur_explicit,
        period_inferred=period_inferred,
        basis_inferred=basis_inferred,
        rate_unhandled=unhandled_unit,
    )
    # Append a short human-readable audit suffix (display only — the gate reads the
    # explicit boolean flags above, never this string).
    notes = []
    if parsed.currency_inferred:
        notes.append("currency assumed EUR")
    if parsed.period_inferred:
        notes.append("period inferred")
    if parsed.rate_unhandled:
        notes.append("sub-annual rate annualized (approx)")
    if parsed.basis_inferred:
        notes.append("basis assumed gross")
    if notes:
        parsed.raw = (parsed.raw + " [" + "; ".join(notes) + "]").strip()
    return parsed


def passes_comp_gate(parsed: ParsedComp | None) -> GateResult:
    """GATE 3 verdict. FIX #3: parsed is None -> soft (manual_review), unless
    DISCLOSED_COMP_REQUIRED is True (then hard_fail). Bands on top_eur_gross_yr."""
    if parsed is None:
        if DISCLOSED_COMP_REQUIRED:
            return GateResult(
                status="hard_fail", signal=0.0,
                reasons=["ceiling: comp undisclosed and DISCLOSED_COMP_REQUIRED=True"],
            )
        return GateResult(
            status="soft", signal=0.0,
            reasons=["ceiling: comp undisclosed - manual review"],
        )

    T = parsed.top_eur_gross_yr
    audit = (
        f"ceiling: {parsed.currency} {parsed.raw} -> EUR{round(T)} "
        f"(period={parsed.period}, basis={parsed.basis})"
    )

    # Genuine parse UNCERTAINTY = the period was guessed, or a sub-annual rate was
    # approximately annualized. NOT included: basis_inferred (assumed gross) and
    # currency_inferred (assumed EUR) — those are confident defaults per the parser
    # contract, and conflating them previously leaked cleanly-disclosed below-floor
    # figures from hard_fail to soft (audit critical #2). The near-floor soft escape
    # exists for true FX/period uncertainty, not for a missing "gross"/"EUR" word.
    inferred = parsed.period_inferred or parsed.rate_unhandled
    equity_present = parsed.equity_present

    # comp_headroom (the rank signal): clamp((T-72000)/65500, 0, 1); 0.0 for soft.
    denom = float(SALARY_GOAL_EUR - SALARY_AUTO_REJECT_BELOW_EUR) or 1.0
    headroom = (T - SALARY_AUTO_REJECT_BELOW_EUR) / denom
    headroom = max(0.0, min(1.0, headroom))

    # Red-team #5: equity/OTE present -> disclosed cash is a lower bound; never
    # hard_fail on it. Surface as soft for manual review.
    if equity_present and T <= SALARY_AUTO_REJECT_BELOW_EUR:
        return GateResult(
            status="soft", signal=0.0,
            reasons=[audit, "ceiling: partial comp disclosed (base only; equity/OTE present)"],
        )

    floor = float(SALARY_AUTO_REJECT_BELOW_EUR)
    tol = floor * SALARY_FLOOR_FX_TOLERANCE
    # Red-team #6/#3/#12: a figure within +/-tolerance of the floor, OR whose
    # currency/period/basis was inferred, is too close to call -> soft, not hard.
    near_floor = (floor - tol) <= T <= (floor + tol)

    if T <= SALARY_AUTO_REJECT_BELOW_EUR:
        # Pinned contract: T <= 72000 -> hard_fail. The soft escape is reserved
        # for genuine FX/parse UNCERTAINTY (inferred currency/period/basis); a
        # CLEANLY disclosed figure at/under the floor always hard-fails, even when
        # it lands within the near-floor FX tolerance band.
        if inferred and near_floor:
            return GateResult(
                status="soft", signal=0.0,
                reasons=[audit, f"ceiling: EUR{round(T)} near/under floor with FX/parse "
                                f"uncertainty - manual review"],
            )
        return GateResult(
            status="hard_fail", signal=0.0,
            reasons=[audit, f"ceiling: disclosed top EUR{round(T)} <= 72k "
                            f"auto-reject floor"],
        )

    if T < SALARY_FLOOR_EUR:
        return GateResult(
            status="pass", signal=headroom,
            reasons=[audit, f"ceiling: EUR{round(T)} - below floor (nets < EUR10k/mo)"],
        )
    if T < SALARY_GOAL_EUR:
        return GateResult(
            status="pass", signal=headroom,
            reasons=[audit, f"ceiling: EUR{round(T)} - capable but short of "
                            f"137.5k goal"],
        )
    return GateResult(
        status="pass", signal=headroom,
        reasons=[audit, f"ceiling: EUR{round(T)} - meets 137.5k goal"],
    )


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC API — evaluate
# ──────────────────────────────────────────────────────────────────────────
def evaluate(job: dict) -> Verdict:
    """Run all three gates and compose a Verdict. NO I/O.

    verdict = 'reject'        if any gate.status == 'hard_fail'
              'manual_review' elif any gate.status == 'soft'
              'keep'          else
    reasons = gate1.reasons + gate2.reasons + gate3.reasons
    rank    = w_lane*g1.signal + w_geo*g2.signal + w_comp*g3.signal
    """
    gate1 = passes_lane_gate(job)
    gate2 = passes_geo_gate(job)

    comp_text = "\n".join(
        filter(None, [job.get("title", ""), job.get("description", ""), job.get("salary", "")])
    )
    parsed = parse_comp(comp_text)
    gate3 = passes_comp_gate(parsed)

    reasons = list(gate1.reasons) + list(gate2.reasons) + list(gate3.reasons)

    # Verdict precedence: reject if ANY hard_fail; else manual_review if ANY soft;
    # else keep. Red-team #13: only an EXPLICIT hard_fail (TITLE_HARD_DENY title /
    # LOCATION_DENY forced relocation) yields a final reject; a lone INFERRED
    # hard_fail (comp parse artifact) with two clean passes caps at manual_review.
    statuses = (gate1.status, gate2.status, gate3.status)
    hard_fails = [g for g in (gate1, gate2, gate3) if g.status == "hard_fail"]

    if hard_fails:
        explicit = _has_explicit_hard_fail(gate1, gate2, gate3)
        if explicit:
            verdict = "reject"
        elif len(hard_fails) == 1 and gate1.status == "pass" and gate2.status != "hard_fail":
            # exactly one inferred hard_fail (the comp gate) with lane passing ->
            # cap at manual_review rather than reject.
            verdict = "manual_review"
        else:
            verdict = "reject"
    elif "soft" in statuses:
        verdict = "manual_review"
    else:
        verdict = "keep"

    w = RANK_WEIGHTS
    rank = (
        w.get("w_lane", 1.0) * gate1.signal
        + w.get("w_geo", 1.0) * gate2.signal
        + w.get("w_comp", 1.0) * gate3.signal
    )

    return Verdict(
        verdict=verdict,
        gate1=gate1,
        gate2=gate2,
        gate3=gate3,
        reasons=reasons,
        rank=rank,
    )


def _has_explicit_hard_fail(g1: GateResult, g2: GateResult, g3: GateResult) -> bool:
    """An EXPLICIT hard_fail is one driven by a DISCLOSED, confident signal (operator
    title, a location deny phrase, or a cleanly-disclosed below-floor salary) rather
    than an inferred parse/FX artifact. Explicit -> final reject; a lone inferred
    hard_fail with lane+geo passing caps at manual_review (Red-team #13)."""
    if g1.status == "hard_fail" and any("operator" in r for r in g1.reasons):
        return True
    if g2.status == "hard_fail":
        # Gate 2 hard_fail is always a matched LOCATION_DENY phrase (explicit).
        return True
    if g3.status == "hard_fail" and any(
        "auto-reject floor" in r or "disclosed top" in r for r in g3.reasons
    ):
        # Gate 3 only hard_fails at all for a parsed figure; passes_comp_gate routes
        # genuinely uncertain figures (inferred currency/period/basis, near-floor FX,
        # equity-present) to SOFT instead. So a comp HARD_FAIL carrying the
        # "disclosed top <= 72k auto-reject floor" reason is a confident disclosure.
        return True
    return False
