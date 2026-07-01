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

# Candidate-fit rank inputs (NEW). Default empty/absent so a config that predates
# these keys keeps the original lane+geo+comp rank behaviour exactly.
#   - SENIORITY_GAP_TERMS: phrases signalling a role is above the candidate's record;
#     each distinct hit docks w_gap from rank (DE-RANK ONLY, never flips a verdict).
#   - GAP_PENALTY_CAP: max number of gap hits CHARGED (0 = uncapped). Stops a sprawling
#     senior JD from running the penalty away past the +w_fit ceiling.
#   - FIT_NORMALIZER_PCT: the match_score (%) treated as a "full" fit (-> fit_signal 1.0).
#     match_score divides by the sum of ALL skill weights, so real postings score only
#     ~5-16%; without this, w_fit is a near-no-op. 100 = legacy raw behaviour.
SENIORITY_GAP_TERMS = getattr(config, "SENIORITY_GAP_TERMS", [])
GAP_PENALTY_CAP = getattr(config, "GAP_PENALTY_CAP", 0)
FIT_NORMALIZER_PCT = getattr(config, "FIT_NORMALIZER_PCT", 100.0)

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


_RX_CACHE_NORM: dict[int, list[tuple[str, "re.Pattern"]]] = {}


def _compiled_norm(terms) -> list[tuple[str, "re.Pattern"]]:
    """Like _compiled, but each term's PATTERN is built from its _norm_lane form so
    it matches the normalized surface, while the returned LABEL is the original term.

    This is the single normalize-then-compile path (single-canonical-mapping): any
    term containing punctuation _norm_lane rewrites (e.g. 'ci/cd security') is
    compiled from its normalized form ('ci cd security') and therefore can actually
    match. Separate cache from _compiled() since the compiled pattern differs.
    A term that normalizes to empty is skipped (it could never be a real signal).
    """
    key = id(terms)
    cached = _RX_CACHE_NORM.get(key)
    if cached is None:
        built = []
        for t in terms:
            nt = _norm_lane(t)
            if not nt:
                continue
            built.append((t, re.compile(r"\b" + re.escape(nt) + r"\b")))
        cached = built
        _RX_CACHE_NORM[key] = cached
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


def title_hard_deny_hits(title: str) -> list[str]:
    """Explicit title-level operator denies after the lane-title override.

    Used by jobs.bg fast triage before opening detail pages. A title hard-deny is
    safe to reject pre-detail only when the title itself does NOT also carry a
    lane-allow term; that mirrors Stage A in _lane_core().
    """
    nt = _norm_lane(title)
    if not nt:
        return []
    title_denies = [t for t, rx in _compiled(TITLE_HARD_DENY) if rx.search(nt)]
    if not title_denies:
        return []
    title_allow_hits = [t for t, rx in _compiled(LANE_ALLOW) if rx.search(nt)]
    if title_allow_hits:
        return []
    return title_denies


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
    explicit_title_denies = title_hard_deny_hits(title)
    if explicit_title_denies:
        reasons.append(f"GATE1 hard_fail: operator title matched {explicit_title_denies}")
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

# REGION-scope deny detector (ALLOW-OVER-DENY fix). A deny phrase is REGION-scoped
# when it names a forbidden REGION (US / NAMER / APAC / LATAM) rather than a forbidden
# MODALITY (forced relocation / on-site). Only region-scope denies soften when a clean
# allow sibling co-exists; modality denies stay HARD even with a sibling (spec pins
# Western relocation as a hard gate). Matches the deny PHRASES in LOCATION_DENY:
# 'remote (us)', 'us-remote', 'namer only', 'apac only', 'must reside in the united
# states', … — and never 'relocation required' / 'on-site only' / 'based in london'.
_GEO_REGION_DENY_RX = re.compile(
    r"\b(?:us|usa|namer|apac|latam|canada)\b|\(us\)|united states|north america|americas",
    re.IGNORECASE,
)
# The bare ambiguous allow token: 'remote' alone does NOT prove a clean non-US region
# (it is also the literal substring of 'remote (us)' / 'remote within us'), so it must
# not count as the clean sibling that softens a US-region deny. A positively-scoped
# allow (emea / eu / europe / fully remote / a BG signal / EOR) does.
_GEO_AMBIGUOUS_ALLOW = ("remote",)


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

        # MODALITY vs REGION (the single invariant behind every soft escape below). A
        # deny softens to manual_review ONLY when EVERY matched deny is REGION-scoped
        # (US/NAMER/APAC/LATAM). A MODALITY deny (forced relocation / on-site-only) is
        # NEVER softened by a co-occurring remote/EOR allow — the spec pins Western
        # relocation as a HARD gate ("relocation destroys the arbitrage"). Computing
        # this once and gating ALL THREE soft clauses on it closes the R2-audit leak
        # where the EOR-override / contrast clauses softened a relocation deny just
        # because a 'remote (emea)' / 'work from anywhere' phrase appeared nearby.
        all_region_scope = all(_GEO_REGION_DENY_RX.search(d) for d in deny_hits)

        # Red-team #7: a US-auth/regional deny that co-occurs with an explicit
        # EOR / remote-EMEA / remote-global allow downgrades to soft (the role
        # plausibly hires EMEA via EOR — only a recruiter can confirm). Gated on
        # region-scope so a modality deny can never reach it (work-auth boilerplate is
        # itself region-scoped, so the legitimate boilerplate path is unaffected).
        if all_region_scope and only_boilerplate and (eor_override or has_clean_remote_allow):
            reasons = [
                "GEO manual_review (EOR override): work-auth deny "
                f"{deny_hits[:3]} co-occurs with remote/EOR allow"
            ]
            return GateResult(status="soft", signal=0.5, reasons=reasons)

        # Red-team #9 fallback: a REGION deny phrase AND a clean EMEA/EU/global remote
        # allow both present (contrast sentence) -> soft for human disambiguation.
        if all_region_scope and has_clean_remote_allow and eor_override:
            reasons = [
                "GEO manual_review (contrast): region deny "
                f"{deny_hits[:3]} co-occurs with clean remote allow {allow_hits[:3]}"
            ]
            return GateResult(status="soft", signal=0.5, reasons=reasons)

        # ALLOW-OVER-DENY (REGION-scope only). A region deny that co-occurs with a
        # GENUINE clean allow sibling routes to soft: the posting names at least one
        # qualifying region, so a human disambiguates rather than the gate discarding
        # the qualifying sibling (the live false-reject this fix targets). The clean
        # sibling must be POSITIVELY scoped (EMEA/EU/Europe/BG/EOR), NOT the ambiguous
        # bare token 'remote' — which is also the literal substring of 'remote (us)' /
        # 'remote within us', so it must not rescue a pure-US role.
        clean_sibling = [h for h in allow_hits if h not in _GEO_AMBIGUOUS_ALLOW]
        if all_region_scope and clean_sibling:
            reasons = [
                "GEO manual_review (allow-over-deny): region deny "
                f"{deny_hits[:3]} co-occurs with clean allow {clean_sibling[:3]} "
                "- needs human disambiguation"
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

# Non-comp number contexts to actively exclude (standards numbers etc.).
_NONCOMP_NEG = ("iso", "soc", "27001", "27002", "42001", "nist", "version", "v.")
# Headcount / team-size nouns: when a number is IMMEDIATELY counting one of these
# ("800+ professionals", "team of 1500 people"), it is company size, not comp.
# Live false-parse (DBA @ DEVEXPERTS): "a team of more than 800+ professionals, the
# comp..." parsed 800 as €800/mo -> €9,600/yr because 'comp' sat in the window.
#
# The discriminator is GRAMMATICAL POSITION, not currency. A count noun sits as the
# number's IMMEDIATE next token ("800 developers"); a salary's beneficiary noun is
# separated by a preposition ("50000 FOR developers", "45000 PER specialists").
# _HEADCOUNT_DIRECTLY_AFTER matches only the immediate-count form, so a cue-anchored
# currency-less salary ("Salary band 50000 for developers") is never dropped.
_HEADCOUNT_WORDS = (
    "professionals", "experts", "employees", "people", "colleagues",
    "specialists", "engineers", "developers", "members", "staff",
    "headcount", "fte", "consultants",
)
# Number directly counts a headcount noun: optional '+', optional whitespace, then
# the noun — with NO intervening preposition (for/per/to/of-the). "team of N" is
# handled by the leading 'team of' phrasing matched on the pre-number window.
_HEADCOUNT_DIRECTLY_AFTER = re.compile(
    r"^\+?\s*(?:" + "|".join(re.escape(w) for w in _HEADCOUNT_WORDS) + r")\b",
    re.IGNORECASE,
)
# "team of <N>" / "team of more than <N>": the count phrase precedes the number.
# The bridge between 'team of' and the number is restricted to explicit count
# HEDGES (more than / over / about / ~ / nearly / approximately) — NOT arbitrary
# words. A loose '\w{0,3}' bridge wrongly swallowed an unrelated salary that merely
# appeared a few words after a 'team of …' clause ("team of experts offering 80000").
_HEADCOUNT_BEFORE = re.compile(
    r"team of\s+(?:(?:more than|over|about|around|nearly|approximately|roughly)\s+)?$",
    re.IGNORECASE,
)
# How far past the number to look for the immediately-counted noun.
_HEADCOUNT_LOOKAHEAD = 18

# ── LABEL-GATED comp admission (ARCHITECTURAL FIX 2026-06-30, Option B) ──────────
# The spec floors comp on a DISCLOSED salary ONLY ("≤72k auto-reject — disclosed only";
# undisclosed -> manual_review, never reject). Four prior rounds proved that BLOCKLISTING
# non-salary numbers (budget/revenue/margin/turnover/headcount, × EN+BG, × inflections,
# × punctuation) never converges — natural language always supplies one more non-salary
# noun. So we INVERT the default: a number is admitted as comp ONLY when something
# AFFIRMATIVELY marks it as pay. The allowlist of "pay" markers is small and bounded
# (there are only so many ways to write 'salary'), so admission is decidable; a phantom
# (margin/turnover/budget) figure is never admitted regardless of phrasing.
#
# Admission rule (see _comp_admits): a money candidate is comp iff EITHER
#   (a) a HARD salary LABEL binds it — the nearest money-context noun in its CLAUSE is a
#       salary label, not a non-salary noun ("Salary: 60000", "Заплата 50000 лв"); OR
#   (b) CURRENCY is adjacent AND no non-salary noun governs it in-clause ("$150,000/yr",
#       "€120k", "60 000 лв") — a bare-currency disclosure boards emit without the word
#       'salary'.
# A bare integer with neither (currency nor a binding label) is NOT comp — this alone
# kills "gross margin reached 50000" / headcount integers, with no noun list.
#
# 'gross'/'net' are BASIS-only cues (see _GROSS_CUES/_NET_CUES below); they are NOT
# salary labels and NEVER admit a number on their own ("gross margin 50000" is not pay).
_SALARY_LABELS = (
    "salary", "compensation", "remuneration", "wage", "wages", "payroll",
    "base pay", "annual pay", "заплата", "възнаграждение", "заплащане",
)
# Non-salary money-context nouns: when one of these BINDS the number (is the nearest
# money-context noun in the clause) the figure is company spend, not pay. This list is
# now only a TIEBREAKER against a co-present currency (rule (b)); correctness no longer
# rests on it being exhaustive, because rule (a)/(b) already refuse a bare integer.
# Cyrillic stems are suffix-tolerant (BG inflects with trailing articles: бюджетът,
# оборота, приходите) — a trailing \b fails between two Cyrillic letters.
_NONSALARY_CONTEXT_WORDS = (
    "budget", "budgets", "revenue", "revenues", "turnover", "arr", "mrr",
    "funding", "grant", "grants", "valuation", "raised", "investment",
    "allowance", "stipend", "reimbursement", "discount", "savings", "spend",
    "margin", "margins", "profit", "profits", "income", "ebitda", "bookings",
)
_NONSALARY_CONTEXT_BG = ("бюджет", "приход", "оборот", "печалб", "инвестиц", "финансиран")
_SALARY_LABEL_RX = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _SALARY_LABELS) + r")"
    r"|заплат[а-я]*|възнагражд[а-я]*",
    re.IGNORECASE | re.UNICODE,
)
_NONSALARY_CONTEXT_RX = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _NONSALARY_CONTEXT_WORDS) + r")\b"
    r"|\b(?:" + "|".join(_NONSALARY_CONTEXT_BG) + r")[а-я]*",
    re.IGNORECASE | re.UNICODE,
)
# Clause boundaries scope "what binds this number": sentence enders PLUS ';' and
# newline (a budget line item after ';' must not be bound by a salary label before it).
_CLAUSE_SPLIT_RX = re.compile(r"[.!?;\n]")


def _clause_bounds(t: str, start: int) -> tuple[int, int]:
    """Return [clause_start, clause_end) of the clause containing position `start`."""
    clause_start = 0
    for m in _CLAUSE_SPLIT_RX.finditer(t, 0, start):
        clause_start = m.end()
    end_m = _CLAUSE_SPLIT_RX.search(t, start)
    clause_end = end_m.start() if end_m else len(t)
    return clause_start, clause_end


# Grammatical GLUE that may sit between a number and the word that governs it without
# breaking governance: articles/prepositions/copulas/modals/hedges. A COMMA or clause
# break ends the walk. These are bounded function words, NOT a domain blocklist.
_GOVERN_CONNECTIVES = {
    "is", "are", "was", "were", "be", "been", "being", "of", "the", "a", "an",
    "per", "at", "to", "for", "this", "that", "our", "your", "their", "its",
    "with", "and", "or", "up", "around", "about", "approximately", "circa",
    "from", "between", "starting", "range", "will", "would", "shall", "can",
    "could", "may", "comes", "come", "amounts", "amount", "totals", "total",
    "equals", "reaches", "reach", "set", "sits", "stands", "roughly", "nearly",
    "over", "under", "min", "max", "minimum", "maximum",
    "е", "на", "от", "до", "са", "в", "и", "или", "е.г",
}
# Currency / period / basis unit tokens — not "content" for governance purposes.
_GOVERN_UNIT_TOKENS = {
    "eur", "usd", "gbp", "bgn", "k", "m", "лв", "лев", "лева", "year", "yr",
    "years", "month", "months", "mo", "annum", "annually", "annual", "gross",
    "net", "бруто", "нето", "годишно", "месечно", "pa", "pm",
}
# POSITIVE salary-context governors (the bounded Option-B allowlist — the ways a JD
# MARKS a number as pay). When the word governing a figure is one of these, the figure
# is comp; when it is ANY OTHER content word (size/cost/value/pool/fee/budget/margin/…)
# the figure is a non-salary phantom and is refused — with NO non-salary list. The
# multi-form salary LABELS (salary/compensation/заплата…) are also accepted via
# _SALARY_LABEL_RX so inflected/Bulgarian forms count.
_SALARY_CONTEXT_GOVERNORS = {
    "rate", "rates", "base", "band", "bands", "package", "packages", "pay",
    "comp", "compensation", "remuneration", "ote", "salary", "salaries", "wage",
    "wages", "payroll", "offer", "offers", "offering", "offered", "paying",
    "pays", "paid", "earn", "earning", "earnings", "income",
}
_WORD_CHARS = "_'"


def _word_left_of(clause: str, pos: int) -> tuple[str, int] | None:
    """The word token ending at-or-before `pos` (skipping currency/glue punctuation),
    as (lowercased_word, word_start_index). None if a comma/clause break or string
    start intervenes first (governance broken — the number is its own clause subject)."""
    i = pos - 1
    while i >= 0 and clause[i] in " \t:=-–—()+€$£":
        i -= 1
    if i < 0 or clause[i] in ",.;":
        return None
    j = i
    while j >= 0 and (clause[j].isalnum() or clause[j] in _WORD_CHARS):
        j -= 1
    word = clause[j + 1:i + 1]
    if not word:
        return None
    return word.lower(), j + 1


def _governing_head(clause: str, rel_start: int, rel_end: int) -> str | None:
    """Return the CONTENT word that grammatically governs the number at `rel_start`, or
    None if the number is ungoverned (a bare amount, or its own clause subject).

    Walks left over connective/unit/currency/numeric tokens; the first CONTENT word is
    the head. 'deal size is 60000 eur' -> 'size'; 'project cost 200000' -> 'cost';
    '$150,000/yr' / 'eur 120k' -> None (only currency/units precede); 'we offer €70000'
    -> 'offer'; 'your salary, after the allowance, will be 60000' -> None (the walk
    skips 'will be' as glue and hits the comma after 'allowance' -> ungoverned, so the
    in-clause salary label admits it)."""
    pos = rel_start
    while pos > 0:
        res = _word_left_of(clause, pos)
        if res is None:
            return None
        w, w_start = res
        # Glue: function words, currency/period units, and bare numbers / number+unit
        # tokens (a range lo like '90k' left of the hi '110k', or '5 000' grouping).
        if (w in _GOVERN_CONNECTIVES or w in _GOVERN_UNIT_TOKENS
                or re.fullmatch(r"\d[\d.,]*[km]?", w)):
            pos = w_start
            continue
        return w
    return None


def _comp_admits(t: str, start: int, end: int, has_currency: bool) -> bool:
    """True if the money span [start,end) is admissible as DISCLOSED comp.

    LABEL-GATED (Option B, structural). Decided within the number's CLAUSE
    (';'/newline/sentence-scoped). Let H = the content word that grammatically GOVERNS
    the number (_governing_head — walks left over only function/currency/unit words):
      (a) ADMIT if H marks PAY — a salary LABEL or a salary-context governor
          (rate/base/band/package/offer/paying/…). "Salary: 60000", "We offer €70,000",
          "Rate: €120/hour", "Salary band 50000".
      (b) else ADMIT if the number is UNGOVERNED (H is None) AND (currency is adjacent OR
          a salary label is elsewhere in-clause). Covers bare disclosures ("$150,000/yr",
          "€120k", "60 000 лв") and the salary-subject clause ("Your salary, after the
          allowance, will be 60000").
      (c) else REFUSE — H is a NON-salary content noun ("deal size", "project cost",
          "prize pool", "turnover", "fee", BG "разход") or a bare integer with no
          currency. Refuses EVERY non-salary money figure structurally, with NO
          enumerated non-salary list (closing the rule-(b) blocklist whack-a-mole).
    """
    clause_start, clause_end = _clause_bounds(t, start)
    clause = t[clause_start:clause_end]
    rel_start, rel_end = start - clause_start, end - clause_start

    head = _governing_head(clause, rel_start, rel_end)
    if head is not None:
        if head in _SALARY_CONTEXT_GOVERNORS or _SALARY_LABEL_RX.fullmatch(head):
            return True                   # a pay marker governs the number
        return False                      # a non-salary content noun governs it -> phantom
    # head is None: the number is ungoverned within its clause.
    if _SALARY_LABEL_RX.search(clause) is not None:
        return True                       # salary clause subject ("salary, …, 60000")
    return bool(has_currency)             # bare currency amount, no governing noun

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

        # LABEL-GATED ADMISSION (Option B). A number is comp ONLY when a salary label
        # binds it OR currency is adjacent with no non-salary noun governing it in its
        # clause. A bare integer near business jargon ("gross margin 50000") or a
        # company-spend figure ("turnover 5,000,000 EUR", "Бюджетът … 50000 EUR") is
        # NOT admitted — so a phantom can never reach the gate, in any phrasing/language.
        if not _comp_admits(t, m.start(), m.end(), has_currency):
            continue

        # Exclude standards/version numbers even if a cue is incidentally nearby.
        tight = _window(t, m.start(), m.end(), 6)
        if any(neg in tight for neg in _NONCOMP_NEG):
            continue

        # Headcount guard (POSITION-based): drop a number DIRECTLY counting a headcount
        # noun ("800+ professionals") or following "team of ...". Retained as a cheap
        # early-out; label-gating already refuses most headcount integers (no currency,
        # no binding salary label), but a currency-bearing "€800 per professional" style
        # phrasing is still caught here.
        after = t[m.end():m.end() + _HEADCOUNT_LOOKAHEAD]
        before = t[max(0, m.start() - 30):m.start()]
        if _HEADCOUNT_DIRECTLY_AFTER.search(after) or _HEADCOUNT_BEFORE.search(before):
            continue

        # A number glued to a degree or percent sign is not money: "360° sales",
        # "9000% growth", "20% bonus". Salary figures never carry these units. Look at
        # the char immediately following the matched run, skipping a single space.
        after = t[m.end():m.end() + 2].lstrip()
        if after[:1] in ("°", "%"):
            continue

        mag = _to_float(num_raw)
        if mag is None or mag < 1.0:
            continue

        # label_bound: this number is governed by a salary LABEL, or it is ungoverned
        # inside a salary clause. Used in selection so a genuine disclosed salary always
        # beats a co-present bare-currency figure (e.g. keep the 60000 salary over a
        # 5,000,000 turnover line that named a currency). Mirrors _comp_admits rule (a).
        cs, ce = _clause_bounds(t, m.start())
        clause = t[cs:ce]
        rel_s, rel_e = m.start() - cs, m.end() - cs
        head = _governing_head(clause, rel_s, rel_e)
        if head is not None:
            label_bound = bool(
                head in _SALARY_CONTEXT_GOVERNORS or _SALARY_LABEL_RX.fullmatch(head)
            )
        else:
            label_bound = _SALARY_LABEL_RX.search(clause) is not None

        candidates.append({
            "match": m,
            "pre": pre,
            "post": post,
            "win": win,
            "mag": mag,
            "num_raw": num_raw,
            "has_currency": has_currency,
            "label_bound": label_bound,
        })

    if not candidates:
        return None

    # ---- candidate selection (A.8) ----------------------------------------
    # Precedence: a salary-LABEL-bound figure beats any merely currency-anchored one
    # (so a disclosed 60000 salary wins over a co-present 5,000,000 turnover line that
    # named a currency); then explicit-currency over cue-only; then OTE/on-target when
    # equity markers are present; tie-break on highest EUR.
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
            int(c["label_bound"]),
            int(c["has_currency"]),
            _ote_proximity(c),
            eur,
        )
        cur_best = (
            int(best["label_bound"]) if best else -1,
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
    # Clause-scoped window for PERIOD and NET/GROSS cue detection (R4 fix): a fixed
    # ±30-char window crosses '.'/';'/newline boundaries, so a 'net margin' / 'per
    # month budget' idiom in a NEIGHBORING clause flipped this salary's basis/period
    # (65000 gross -> net*1.146 cleared the floor; 130000/yr -> /mo *12). Cue scanning
    # must stay inside the number's own clause, intersected with the proximity window.
    _cs, _ce = _clause_bounds(t, m.start())
    cwin = t[max(_cs, m.start() - 30):min(_ce, m.end() + 30)]
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
    if _has_cue(cwin, _MONTHLY_CUES) or re.search(r"\bмес\b", cwin):
        period, annual_multiplier = "mo", 12.0
    elif _has_cue(cwin, _YEARLY_CUES):
        period, annual_multiplier = "yr", 1.0
    elif _has_cue(cwin, _DAILY_CUES):
        # Red-team #4: day rate -> annualize via WORKING_DAYS_PER_YEAR.
        period, annual_multiplier = "yr", float(WORKING_DAYS_PER_YEAR)
    elif _has_cue(cwin, _WEEKLY_CUES):
        # Weekly rate -> annualize across the working year. Marked rate_unhandled so
        # the gate keeps it soft near the floor (working-weeks vary), but the figure
        # is now correctly scaled instead of treated as a raw annual total.
        period, annual_multiplier = "yr", float(WORKING_WEEKS_PER_YEAR)
        unhandled_unit = True
    elif _has_cue(cwin, _HOURLY_CUES):
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
    if _has_cue(cwin, _NET_CUES):
        basis = "net"
        basis_inferred = False
    elif _has_cue(cwin, _GROSS_CUES):
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
    """GATE 3 verdict — ADVISORY (2026-06-30 redesign).

    Free-text comp extraction cannot reliably separate a real salary from a phantom
    (budget / revenue / deal-size / turnover / margin numbers carry the same currency
    and period tokens as pay). Five adversarial audit rounds proved that no parser-side
    rule converges: every phrasing fixed surfaced another. So Gate 3 no longer lets a
    PARSED FIGURE drive the verdict at all — it is advisory:

        * parsed is None        -> soft (manual_review); hard_fail only if the explicit
                                   DISCLOSED_COMP_REQUIRED opt-in is set (disclosure-
                                   presence gate, orthogonal to the figure's magnitude).
        * parsed is a figure    -> ALWAYS soft. NEVER hard_fail (so a phantom can't
                                   false-REJECT a real role) and NEVER 'pass' (so a
                                   phantom can't false-KEEP an undisclosed one). The
                                   EUR72k/goal bands survive ONLY as comp_headroom, a
                                   pure RANK signal that sorts survivors within the
                                   manual_review bucket — not as a verdict gate.

    Consequence (intended, user-approved): a verdict of 'keep' is no longer reached via
    comp; an in-lane, in-geo role with any comp reading lands in manual_review and a
    human confirms the pay. This trades the auto-keep/auto-reject the parser could never
    do safely for a guarantee that NO comp number — genuine or phantom — ever corrupts a
    verdict. Salary is disclosed <5% of the time and untrustworthy when parsed, so this
    is the safe design. See .local/filter-spec.md (comp-advisory note).
    """
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

    # comp_headroom — the RANK signal only. clamp((T-72000)/65500, 0, 1): a below-floor
    # figure scores 0 (ranks like undisclosed), an at-goal figure scores 1. This is the
    # ONLY thing the EUR72k floor now drives — sorting, never the verdict.
    denom = float(SALARY_GOAL_EUR - SALARY_AUTO_REJECT_BELOW_EUR) or 1.0
    headroom = max(0.0, min(1.0, (T - SALARY_AUTO_REJECT_BELOW_EUR) / denom))

    # Advisory band label (manual_review context for the human; not a verdict).
    if T <= SALARY_AUTO_REJECT_BELOW_EUR:
        band = (f"ceiling (advisory): EUR{round(T)} <= 72k floor — likely below target; "
                f"verify with recruiter (manual review)")
    elif T < SALARY_FLOOR_EUR:
        band = f"ceiling (advisory): EUR{round(T)} - below floor (nets < EUR10k/mo)"
    elif T < SALARY_GOAL_EUR:
        band = f"ceiling (advisory): EUR{round(T)} - capable but short of 137.5k goal"
    else:
        band = f"ceiling (advisory): EUR{round(T)} - meets 137.5k goal"

    return GateResult(status="soft", signal=headroom, reasons=[audit, band])


# ──────────────────────────────────────────────────────────────────────────
# RANK FIT INPUTS (NEW) — candidate-aware rank terms. Pure, no I/O.
# ──────────────────────────────────────────────────────────────────────────
def _fit_signal(job: dict) -> float:
    """0..1 fraction of the JD the candidate can deliver.

    Reuses the INFO-only skill overlap already attached upstream by
    profile_matcher.match_jobs (job['match_score'], a 0..100 percent). Returns 0.0
    when absent (e.g. unit tests that call evaluate() directly), so the fit term
    vanishes and rank reduces to the original lane+geo+comp formula.

    Normalized against FIT_NORMALIZER_PCT (validation finding): match_score divides
    the matched skill weight by the sum of ALL ~45 skills, so a strong real posting
    only scores ~10-16%. Dividing by a flat 100 made w_fit a near-no-op (+0.2..+0.6)
    vs a multi-point gap penalty. Treating FIT_NORMALIZER_PCT as "full fit" rescales
    the realistic band to ~0..1 so the fit lever actually competes. Still clamped to
    1.0, so an unusually high score can't over-reward. 100.0 = legacy raw behaviour.
    """
    raw = job.get("match_score")
    if raw is None:
        return 0.0
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return 0.0
    denom = FIT_NORMALIZER_PCT if FIT_NORMALIZER_PCT else 100.0
    return max(0.0, min(1.0, pct / denom))


def _seniority_gap_hits(job: dict) -> list[str]:
    """Distinct SENIORITY_GAP_TERMS present in title+body (word-boundary, normalized).

    Each hit marks a demand the role makes that the candidate's record does not
    meet (tenure bar, people-leadership, SecOps/IR spine, deep cloud-at-scale,
    heavyweight cert). Used ONLY to de-rank — never to change the verdict.

    BUGFIX (validation finding): the matcher MUST compile each term through the
    SAME normalizer applied to the surface (_norm_lane). Previously the surface was
    normalized (e.g. 'ci/cd security' -> 'ci cd security') but the regex was built
    from the RAW term (\\bci/cd security\\b), so any term whose normalization
    diverges from its raw form (the only one was 'ci/cd security') was a DEAD RULE
    that never matched. _compiled_norm() closes the whole class, not just that term.
    The returned hit label is the ORIGINAL term (for readable audit reasons).
    """
    if not SENIORITY_GAP_TERMS:
        return []
    surface = _norm_lane((job.get("title", "") + " " + job.get("description", "")))
    return [orig for orig, rx in _compiled_norm(SENIORITY_GAP_TERMS) if rx.search(surface)]


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
    # Base posting↔lane rank (unchanged).
    rank = (
        w.get("w_lane", 1.0) * gate1.signal
        + w.get("w_geo", 1.0) * gate2.signal
        + w.get("w_comp", 1.0) * gate3.signal
    )

    # Candidate-aware adjustments (NEW). Both default to 0 when their inputs are
    # absent (no match_score / empty SENIORITY_GAP_TERMS), so rank is identical to
    # the original formula unless the candidate profile is wired in.
    fit = _fit_signal(job)                       # 0..1
    gap_hits = _seniority_gap_hits(job)          # distinct over-seniority demands

    # Gap penalty is CAPPED (validation finding): an uncapped count let a sprawling
    # senior JD reach -19.5, dwarfing the +w_fit ceiling so the fit term became a
    # no-op. Capping the *charged* hits keeps both levers comparable while still
    # sinking over-senior roles. GAP_PENALTY_CAP=0 disables the cap (uncapped).
    charged_gap = len(gap_hits)
    if GAP_PENALTY_CAP and charged_gap > GAP_PENALTY_CAP:
        charged_gap = GAP_PENALTY_CAP

    rank += w.get("w_fit", 0.0) * fit
    rank -= w.get("w_gap", 0.0) * float(charged_gap)

    if fit:
        reasons.append(f"RANK fit: +{w.get('w_fit', 0.0) * fit:.2f} "
                       f"({fit * 100:.0f}% skill overlap)")
    if gap_hits:
        capped_note = (f"; capped at {GAP_PENALTY_CAP} of {len(gap_hits)}"
                       if GAP_PENALTY_CAP and len(gap_hits) > GAP_PENALTY_CAP else "")
        reasons.append(f"RANK over-seniority penalty: -{w.get('w_gap', 0.0) * charged_gap:.2f} "
                       f"via {gap_hits[:6]}{capped_note}")

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
    title or a location deny phrase) rather than an inferred artifact. Explicit -> final
    reject (Red-team #13).

    NOTE (comp-advisory redesign): Gate 3 no longer hard_fails on any PARSED FIGURE — a
    comp number can never drive a reject. Gate 3 hard_fails only when comp is UNDISCLOSED
    and the explicit DISCLOSED_COMP_REQUIRED opt-in is set; that is a confident
    disclosure-presence decision the operator asked for, so it counts as explicit."""
    if g1.status == "hard_fail" and any("operator" in r for r in g1.reasons):
        return True
    if g2.status == "hard_fail":
        # Gate 2 hard_fail is always a matched LOCATION_DENY phrase (explicit).
        return True
    if g3.status == "hard_fail" and any(
        "DISCLOSED_COMP_REQUIRED" in r for r in g3.reasons
    ):
        # The only remaining Gate-3 hard_fail: comp undisclosed + opt-in flag set.
        return True
    return False
