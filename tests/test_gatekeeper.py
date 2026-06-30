"""Unit tests for gatekeeper.py — the 3-gate filter verdict engine.

Mirrors tests/test_profile_matcher.py style: a module-level frozen mock-config
block plus an `override_config` fixture that monkeypatches the constants the
gatekeeper module imported at module level, so edits to config.py never break
these unit tests.

Asserts against the PINNED INTERFACE CONTRACT (gatekeeper.py):
  - GateResult(status, signal, reasons)
  - ParsedComp(top_eur_gross_yr, currency, period, basis, raw)
  - Verdict(verdict, gate1, gate2, gate3, reasons, rank)
  - passes_lane_gate / passes_geo_gate / parse_comp / passes_comp_gate / evaluate
"""

import pytest

import gatekeeper
from gatekeeper import (
    passes_lane_gate,
    passes_geo_gate,
    parse_comp,
    passes_comp_gate,
    evaluate,
    GateResult,
    ParsedComp,
    Verdict,
)

# Frozen mock config so edits to config.py never break these unit tests.
# (mirrors the MOCK_SKILL_WEIGHTS pattern in test_profile_matcher.py)
MOCK_LANE_ALLOW = [
    "ai governance", "ai risk", "eu ai act", "llm eval", "mcp",
    "agentic ai", "rag", "software asset management", "sam",
    "saas governance", "grc", "soc2", "iso 27001-style controls",
    "fastapi", "playwright", "solutions engineer", "fde",
    "shadow-ai discovery", "audit evidence", "remediation tracking",
    "observability", "python automation",
]
# Broad terms also present in MOCK_LANE_ALLOW — a lone hit on these -> manual_review.
MOCK_LANE_ALLOW_WEAK = ["observability", "sam"]
MOCK_LANE_DENY = [
    "pure helpdesk", "l1 support", "desktop support", "pos support",
    "sysadmin", "procurement clerk", "logistics coordinator",
    "generic business analyst", "data entry", "stock controller",
]
MOCK_TITLE_HARD_DENY = [
    "inventory specialist", "pos support", "desktop support",
    "helpdesk", "field technician", "stock controller",
    "warehouse", "data entry", "generic business analyst",
]
MOCK_LOCATION_ALLOW = [
    "remote", "all-remote", "work from anywhere", "eor",
    "employer of record", "deel", "remote.com", "bulgaria",
    "sofia", "emea", "europe",
]
MOCK_LOCATION_DENY = [
    "relocation required", "on-site only", "onsite only",
    "office-only", "in-office", "remote within us", "us-remote",
    "must be located in the us", "namer only", "apac only",
    "relocate to london", "relocate to dublin", "relocate to zurich",
    "relocate to munich", "relocate to amsterdam",
]
MOCK_LOCATION_SOFT = ["hybrid", "inferred-eor"]
MOCK_FX_RATES = {"EUR": 1.0, "BGN": 1.0 / 1.95583, "USD": 0.92, "GBP": 1.17}
MOCK_NET_TO_GROSS = 1.146
MOCK_RANK_WEIGHTS = {"w_lane": 1.0, "w_geo": 1.0, "w_comp": 1.0}


@pytest.fixture
def override_config(monkeypatch):
    """Override the gatekeeper module's imported constants for consistent testing.

    Default assumption per the interface contract: gatekeeper imports the
    constants by name (`from config import LANE_ALLOW, ...`), so patching them on
    the `gatekeeper` module rebinds the names the gate functions actually read.
    """
    monkeypatch.setattr(gatekeeper, "LANE_ALLOW", MOCK_LANE_ALLOW)
    monkeypatch.setattr(gatekeeper, "LANE_ALLOW_WEAK", MOCK_LANE_ALLOW_WEAK)
    monkeypatch.setattr(gatekeeper, "LANE_DENY", MOCK_LANE_DENY)
    monkeypatch.setattr(gatekeeper, "TITLE_HARD_DENY", MOCK_TITLE_HARD_DENY)
    monkeypatch.setattr(gatekeeper, "LOCATION_ALLOW", MOCK_LOCATION_ALLOW)
    monkeypatch.setattr(gatekeeper, "LOCATION_DENY", MOCK_LOCATION_DENY)
    monkeypatch.setattr(gatekeeper, "LOCATION_SOFT", MOCK_LOCATION_SOFT)
    monkeypatch.setattr(gatekeeper, "SALARY_GOAL_EUR", 137500)
    monkeypatch.setattr(gatekeeper, "SALARY_FLOOR_EUR", 120000)
    monkeypatch.setattr(gatekeeper, "SALARY_AUTO_REJECT_BELOW_EUR", 72000)
    monkeypatch.setattr(gatekeeper, "DISCLOSED_COMP_REQUIRED", False)
    monkeypatch.setattr(gatekeeper, "FX_RATES", MOCK_FX_RATES)
    monkeypatch.setattr(gatekeeper, "NET_TO_GROSS_FACTOR", MOCK_NET_TO_GROSS)
    monkeypatch.setattr(gatekeeper, "RANK_WEIGHTS", MOCK_RANK_WEIGHTS)


@pytest.fixture
def override_config_require_comp(override_config, monkeypatch):
    """Same as override_config but with DISCLOSED_COMP_REQUIRED flipped True.

    Used only by D2 to prove the comp gate honors the flag.
    """
    monkeypatch.setattr(gatekeeper, "DISCLOSED_COMP_REQUIRED", True)


def J(title="", description="", location="", salary=""):
    """Build a job dict (keeps tests terse)."""
    return {
        "title": title,
        "description": description,
        "location": location,
        "salary": salary,
    }


def _reasons_text(result):
    """Lower-cased concatenation of a GateResult/Verdict's reasons for substring asserts."""
    return " ".join(result.reasons).lower()


# =====================================================================
# GROUP A — GATE 1: LANE (passes_lane_gate)
# =====================================================================

def test_lane_title_hard_deny_inventory_specialist(override_config):
    """A1: TITLE_HARD_DENY short-circuits before lane scan; signal floored to 0.0."""
    job = J(title="Inventory Specialist",
            description="Manage stock levels in the warehouse")
    res = passes_lane_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    text = _reasons_text(res)
    assert "operator" in text
    assert "inventory specialist" in text


def test_lane_fix1_generic_business_analyst_title_denied(override_config):
    """A2 (FIX #1): the QUALIFIED 'generic business analyst' is in TITLE_HARD_DENY and hard-fails."""
    job = J(title="Generic Business Analyst",
            description="gather requirements, write user stories")
    res = passes_lane_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    assert "generic business analyst" in _reasons_text(res)
    # And end-to-end -> reject
    assert evaluate(job).verdict == "reject"


def test_lane_fix1_bare_business_analyst_NOT_title_denied_but_no_allow(override_config):
    """A3 (FIX #1 negative half): bare 'Business Analyst' is NEVER title-denied;
    it fails ONLY because lane_hits == 0 (distinct reason string from A2)."""
    job = J(title="Business Analyst",
            description="gather requirements and document processes")
    res = passes_lane_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    text = _reasons_text(res)
    assert "no ai-governance lane terms found" in text
    # Must NOT be the title-deny reason path from A2.
    assert "generic business analyst" not in text
    assert evaluate(job).verdict == "reject"


def test_lane_fix1_ai_gov_business_analyst_survives(override_config):
    """A4 (FIX #1 positive half): a real AI-gov BA survives on body keywords
    despite 'Business Analyst' in the title."""
    job = J(title="Business Analyst, AI Governance",
            description=("own the AI tool inventory and EU AI Act compliance, "
                         "run LLM eval"))
    res = passes_lane_gate(job)
    assert res.status == "pass"
    # matches: ai governance, eu ai act, llm eval => >= 3 distinct
    assert res.signal >= 2.0


def test_lane_hits_counts_distinct_word_boundary_case_insensitive(override_config):
    """A5: distinct-count (not occurrence-count), case-insensitive, word-boundary."""
    job = J(title="AI GOVERNANCE Lead",
            description="ai governance ai governance plus RAG and MCP")
    res = passes_lane_gate(job)
    assert res.status == "pass"
    # distinct phrases: ai governance, rag, mcp -> duplicate "ai governance" counts once
    assert res.signal == 3.0


def test_lane_deny_body_term_does_not_flip_pass(override_config):
    """A6: a LANE_DENY body term alone never flips pass -> hard_fail; signal floored at 0.0."""
    job = J(title="AI Governance Engineer",
            description=("you will also support the pure helpdesk queue occasionally; "
                         "primary focus is AI governance and SOC2"))
    res = passes_lane_gate(job)
    assert res.status == "pass"
    assert res.signal >= 0.0
    # the deny hit is recorded informationally
    assert "pure helpdesk" in _reasons_text(res)


def test_lane_no_allow_terms_hard_fail(override_config):
    """A7: empty-lane operator role with a non-denied title fails via lane_hits == 0."""
    job = J(title="Network Administrator",
            description="maintain LAN/WAN, patch servers, on-call rotation")
    res = passes_lane_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    assert "no ai-governance lane terms found" in _reasons_text(res)


def test_lane_lone_weak_term_is_soft_not_pass(override_config):
    """A8 (live-data tuning): a single WEAK lane term ('observability') with no
    strong signal -> soft (manual_review), NOT a clean pass. Mirrors the live
    'Data Engineer passes on python alone' false-pass."""
    job = J(title="Data Engineer",
            description="build pipelines, add observability to our ETL jobs")
    res = passes_lane_gate(job)
    assert res.status == "soft"
    assert "only weak lane term" in _reasons_text(res)


def test_lane_two_weak_terms_pass(override_config):
    """A9: two distinct WEAK terms clear the bar (>=2 weak -> pass)."""
    job = J(title="Platform Engineer",
            description="own observability tooling; manage the sam software asset inventory")
    res = passes_lane_gate(job)
    assert res.status == "pass"


def test_lane_one_strong_term_passes(override_config):
    """A10: a single STRONG term still passes (weak rule must not over-tighten)."""
    job = J(title="Governance Analyst",
            description="run the ai governance program for the org")
    res = passes_lane_gate(job)
    assert res.status == "pass"
    assert "strong=" in _reasons_text(res)


# =====================================================================
# GROUP B — GATE 2: GEO (passes_geo_gate)
# =====================================================================

def test_geo_allow_remote_emea_passes(override_config):
    """B1: clean LOCATION_ALLOW -> pass, geo_certainty 1.0."""
    job = J(title="x", description="Fully remote across EMEA", location="Remote, EMEA")
    res = passes_geo_gate(job)
    assert res.status == "pass"
    assert res.signal == 1.0
    text = _reasons_text(res)
    assert ("remote" in text) or ("emea" in text)


def test_geo_deny_relocation_required_hard_fail(override_config):
    """B2: LOCATION_DENY -> hard_fail, geo_certainty 0.0."""
    job = J(title="x", description="Relocation required to our HQ", location="London, UK")
    res = passes_geo_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    assert "relocation required" in _reasons_text(res)


def test_geo_deny_remote_within_us_hard_fail(override_config):
    """B3: US-remote / NAMER-only style deny hard-fails (arbitrage-killer)."""
    job = J(title="x",
            description="Remote within US only; must be located in the US",
            location="USA")
    res = passes_geo_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    assert "remote within us" in _reasons_text(res)


def test_geo_fix2_hybrid_is_soft(override_config):
    """B4 (FIX #2): hybrid -> soft, NEVER blanket deny."""
    job = J(title="x", description="Hybrid working model", location="Sofia (Hybrid)")
    res = passes_geo_gate(job)
    assert res.status == "soft"
    assert res.signal == 0.5
    text = _reasons_text(res)
    assert ("hybrid" in text) or ("manual" in text)
    # end-to-end -> manual_review (lane passes on nothing? lane fails -> reject;
    # so assert at gate level only here)


def test_geo_fix2_empty_location_no_keyword_is_soft(override_config):
    """B5 (FIX #2 + decision #3): empty/unknown location, no geo keyword -> soft."""
    job = J(title="x", description="Great team, modern stack", location="")
    res = passes_geo_gate(job)
    assert res.status == "soft"
    assert res.signal == 0.5
    text = _reasons_text(res)
    assert ("undisclosed" in text) or ("unknown" in text) or ("manual" in text)


def test_geo_deny_beats_allow_precedence(override_config):
    """B6: precedence deny > allow — a posting with both still hard-fails."""
    job = J(title="x",
            description="Remote-friendly culture but relocation required to Munich",
            location="Munich")
    res = passes_geo_gate(job)
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    text = _reasons_text(res)
    assert ("relocation required" in text) or ("relocate to munich" in text)


# =====================================================================
# GROUP C — parse_comp UNIT (currency / period / net-gross / range / FX)
# =====================================================================

def test_parse_comp_none_when_undisclosed(override_config):
    """C1: undisclosed -> None (the common case)."""
    assert parse_comp("Great role, competitive package, friendly team.") is None


def test_parse_comp_eur_yearly_gross(override_config):
    """C2: baseline EUR/yr/gross passthrough (FX = 1.0)."""
    pc = parse_comp("Salary: €140,000 gross per year")
    assert pc is not None
    assert pc.top_eur_gross_yr == 140000.0
    assert pc.currency == "EUR"
    assert pc.period == "yr"
    assert pc.basis == "gross"
    assert "140,000" in pc.raw


def test_parse_comp_usd_yearly_to_eur(override_config):
    """C3 (currency=USD, FX): USD->EUR; unstated basis assumed gross (not inflated)."""
    pc = parse_comp("$150,000/yr")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(138000.0)  # 150000 * 0.92
    assert pc.currency == "USD"
    assert pc.period == "yr"
    assert pc.basis == "gross"


def test_parse_comp_gbp_monthly_annualized_to_eur(override_config):
    """C4 (FIX #4: GBP + /mo): period mo->*12 THEN GBP->EUR; order-of-ops."""
    pc = parse_comp("£8,000/month")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(112320.0)  # 8000 * 12 * 1.17
    assert pc.currency == "GBP"
    assert pc.period == "mo"
    assert pc.basis == "gross"


def test_parse_comp_bgn_to_eur(override_config):
    """C5 (currency=BGN, peg): BGN pegged divide-by-1.95583."""
    pc = parse_comp("60 000 лв")  # annual BGN
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(30677.51, rel=1e-3)  # 60000 / 1.95583
    assert pc.currency == "BGN"
    assert pc.period == "yr"


def test_parse_comp_net_to_gross(override_config):
    """C6 (net basis): net -> *NET_TO_GROSS_FACTOR before threshold."""
    pc = parse_comp("€90,000 net per year")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(103140.0)  # 90000 * 1.146
    assert pc.basis == "net"
    assert pc.currency == "EUR"
    assert pc.period == "yr"


def test_parse_comp_range_takes_top(override_config):
    """C7 (range): range -> top-of-range chosen before other steps."""
    pc = parse_comp("€90k–€110k per year")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(110000.0)
    assert pc.basis == "gross"
    assert "110" in pc.raw


def test_parse_comp_k_suffix_and_assume_gross(override_config):
    """C8: 'k' suffix expansion; default period yr; default basis gross."""
    pc = parse_comp("€120k")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(120000.0)
    assert pc.basis == "gross"
    assert pc.period == "yr"


def test_parse_comp_bgn_monthly_combo(override_config):
    """C9 (FIX #4 full combo): BGN + monthly + net composed in order.

    5000*12 = 60000 BGN/yr net; *1.146 = 68760 BGN gross; /1.95583 = ~35156.5 EUR.
    """
    pc = parse_comp("5 000 лв/месечно нето")
    assert pc is not None
    assert pc.top_eur_gross_yr == pytest.approx(35156, rel=1e-3)
    assert pc.currency == "BGN"
    assert pc.period == "mo"
    assert pc.basis == "net"


# --- Regression: cue lists must match on WORD BOUNDARIES, not substrings -------
# Bug found in live-sample validation: cues were matched with `cue in text`, so
# short cues leaked into unrelated words ("ote" in "remote", "net" in "network",
# "gross" in "engrossed"). Every remote job was falsely tagged equity/OTE, and a
# "network"/"internet" mention could flip a gross salary to net (×1.146 inflation).

def test_parse_comp_remote_not_flagged_as_equity(override_config):
    """C10 (regression): 'remote' must NOT trip the equity/OTE cue ('ote' substring)."""
    pc = parse_comp("Fully remote across EMEA. Salary: €45,000 gross per year")
    assert pc is not None
    assert pc.basis == "gross"
    assert "equity/OTE markers present" not in pc.raw


def test_parse_comp_network_not_treated_as_net(override_config):
    """C11 (regression): 'network'/'internet' must NOT trip the net cue ('net' substring)."""
    pc = parse_comp("Network engineer for an internet company. €90,000 gross per year")
    assert pc is not None
    # Must stay gross — a substring 'net' hit would flip basis and inflate ×1.146.
    assert pc.basis == "gross"
    assert pc.top_eur_gross_yr == pytest.approx(90000.0)


def test_parse_comp_real_equity_still_detected(override_config):
    """C12: a genuine equity/OTE mention is still flagged (no over-correction)."""
    pc = parse_comp("Base €120,000 + equity, OTE higher. Per year.")
    assert pc is not None
    assert "equity/OTE markers present" in pc.raw


def test_comp_gate_belowfloor_remote_role_hard_fails(override_config):
    """C13 (the live-sample mismatch): a sub-floor disclosed REMOTE in-lane role must
    hard_fail on ceiling — the spurious equity tag previously downgraded it to soft."""
    pc = parse_comp("Remote EU. Salary: €45,000 gross per year")
    res = passes_comp_gate(pc)
    assert res.status == "hard_fail"


# --- Regression: audit-critical #1 — weekly/hourly rates must annualize ----------

def test_parse_comp_weekly_rate_annualized(override_config):
    """C14 (audit critical #1): a weekly rate is annualized (×WORKING_WEEKS_PER_YEAR),
    not treated as a raw annual total. '€5000 per week' ≈ €230k/yr, far above goal."""
    pc = parse_comp("Compensation: €5000 per week")
    assert pc is not None
    assert pc.period == "yr"
    assert pc.rate_unhandled is True
    # 5000 * 46 = 230000 — must clear the goal, not hard-fail as €5000/yr.
    assert pc.top_eur_gross_yr == pytest.approx(5000 * gatekeeper.WORKING_WEEKS_PER_YEAR)
    assert passes_comp_gate(pc).status == "pass"


def test_parse_comp_hourly_rate_annualized(override_config):
    """C15 (audit critical #1): an hourly rate annualizes via days×hours/day."""
    pc = parse_comp("Rate: €120 per hour")
    assert pc is not None
    assert pc.period == "yr"
    expected = 120 * gatekeeper.WORKING_DAYS_PER_YEAR * gatekeeper.WORKING_HOURS_PER_DAY
    assert pc.top_eur_gross_yr == pytest.approx(expected)


# --- Regression: audit-critical #2 — benign defaults must NOT leak below-floor ---

def test_comp_belowfloor_basis_unstated_still_hard_fails(override_config):
    """C16 (audit critical #2): a near-floor figure missing only the word 'gross'
    (basis assumed) must STILL hard_fail — an assumed-gross default is not the same
    as genuine FX/period uncertainty and must not trigger the near-floor soft escape."""
    pc = parse_comp("Salary: 70000 EUR per year")  # explicit currency+period, no 'gross'
    assert pc is not None
    assert pc.basis_inferred is True
    assert pc.period_inferred is False
    res = passes_comp_gate(pc)
    assert res.status == "hard_fail"


def test_comp_belowfloor_period_inferred_stays_soft(override_config):
    """C17 (audit critical #2, other side): GENUINE uncertainty (period guessed) near
    the floor still gets the soft escape — we tightened the leak without removing the
    legitimate manual-review path."""
    # A bare near-floor annual-magnitude number: period is inferred (no /yr cue).
    pc = parse_comp("Package around 70000")
    assert pc is not None
    assert pc.period_inferred is True
    res = passes_comp_gate(pc)
    assert res.status == "soft"


# --- Regression: live false-parse — a number glued to a degree/percent sign is
# NOT compensation. Found in the 389-job IT-category sample: the SDR posting
# "ALEX & GROSS offers genuine 360° sales solutions" parsed '360' as €360/mo
# (the company name 'GROSS' supplied the salary cue), inventing a €4,320/yr
# figure that would hard-reject any role whose lane gate passed.

def test_parse_comp_degree_symbol_not_salary(override_config):
    """C18 (live false-parse): '360°' must NOT be read as salary even when a
    salary cue word ('gross', here from a company name) sits in the window."""
    pc = parse_comp("GROSS offers genuine 360° sales solutions to clients")
    assert pc is None


def test_parse_comp_percent_glued_number_not_salary(override_config):
    """C19: a number glued to '%' is never comp. Uses a percent value that would
    SURVIVE the magnitude floor if mis-parsed ('9000%' near the cue 'gross' would
    otherwise read as €9000/mo -> €108k/yr, an in-band false-pass), so this test
    genuinely exercises the percent guard rather than the plausibility floor."""
    pc = parse_comp("Our gross revenue grew 9000% year over year")
    assert pc is None


# =====================================================================
# GROUP D — passes_comp_gate BANDS + BOUNDARIES (FIX #3)
# =====================================================================

def _comp(T):
    """Build a disclosed ParsedComp at top_eur_gross_yr == T (EUR/yr gross)."""
    return ParsedComp(top_eur_gross_yr=float(T), currency="EUR",
                      period="yr", basis="gross", raw=f"€{T}")


def test_comp_undisclosed_none_is_soft(override_config):
    """D1 (FIX #3): undisclosed NEVER hard-rejects when DISCLOSED_COMP_REQUIRED is False."""
    res = passes_comp_gate(None)
    assert res.status == "soft"
    assert res.signal == 0.0
    assert "comp undisclosed" in _reasons_text(res)


def test_comp_undisclosed_hard_fail_when_required(override_config_require_comp):
    """D2 (FIX #3 flag): gate honors DISCLOSED_COMP_REQUIRED=True."""
    res = passes_comp_gate(None)
    assert res.status == "hard_fail"
    assert res.signal == 0.0


def test_comp_at_72000_hard_fail(override_config):
    """D3 (boundary <=): inclusive lower bound — 72000 hard-fails."""
    res = passes_comp_gate(_comp(72000))
    assert res.status == "hard_fail"
    assert res.signal == 0.0
    assert "72k" in _reasons_text(res)


def test_comp_just_above_72000_below_floor(override_config):
    """D4 (boundary): 72001 flips to pass (strict >); below-floor label."""
    res = passes_comp_gate(_comp(72001))
    assert res.status == "pass"
    assert "below floor" in _reasons_text(res)
    assert res.signal == pytest.approx(1.0 / 65500, abs=1e-7)  # (72001-72000)/65500
    assert res.signal > 0.0


def test_comp_below_floor_band_label(override_config):
    """D5: 72k < T < 120k band + headroom math."""
    res = passes_comp_gate(_comp(110000))
    assert res.status == "pass"
    assert "below floor" in _reasons_text(res)
    assert res.signal == pytest.approx(0.5802, abs=1e-3)  # (110000-72000)/65500


def test_comp_at_120000_capable_but_short(override_config):
    """D6 (boundary): inclusive 120000 enters capable-but-short band (>=)."""
    res = passes_comp_gate(_comp(120000))
    assert res.status == "pass"
    assert "capable but short" in _reasons_text(res)
    assert res.signal == pytest.approx(0.7328, abs=1e-3)  # (120000-72000)/65500


def test_comp_capable_but_short_just_under_goal(override_config):
    """D7 (boundary): 137499 still capable-but-short (strict < goal)."""
    res = passes_comp_gate(_comp(137499))
    assert res.status == "pass"
    assert "capable but short" in _reasons_text(res)


def test_comp_at_goal_clean_pass(override_config):
    """D8 (boundary): inclusive 137500 = clean pass; headroom clamps to 1.0."""
    res = passes_comp_gate(_comp(137500))
    assert res.status == "pass"
    assert "137.5k goal" in _reasons_text(res)
    assert res.signal == 1.0


def test_comp_above_goal_headroom_clamped(override_config):
    """D9: headroom clamp ceiling at 1.0."""
    res = passes_comp_gate(_comp(200000))
    assert res.status == "pass"
    assert "goal" in _reasons_text(res)
    assert res.signal == 1.0


# =====================================================================
# GROUP E — evaluate() PRECEDENCE + RANK
# =====================================================================

def test_evaluate_all_pass_is_keep(override_config):
    """E1: all-pass -> keep; rank = w_lane*lane_hits + w_geo*geo + w_comp*headroom."""
    job = J(title="AI Governance Engineer",
            description=("own AI governance, run LLM eval, EU AI Act, fully remote "
                         "across EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.verdict == "keep"
    assert v.gate1.status == "pass"
    assert v.gate2.status == "pass"
    assert v.gate3.status == "pass"
    assert v.gate1.signal == pytest.approx(3.0)   # ai governance, llm eval, eu ai act
    assert v.gate2.signal == 1.0
    assert v.gate3.signal == 1.0                   # 140000 >= goal -> clamp 1.0
    assert v.rank == pytest.approx(5.0)            # 1.0*3.0 + 1.0*1.0 + 1.0*1.0


def test_evaluate_any_soft_no_hard_is_manual_review(override_config):
    """E2: soft present, no hard -> manual_review; rank includes soft signals."""
    job = J(title="AI Governance Engineer",
            description=("AI governance, EU AI Act, hybrid model, competitive package"),
            location="Sofia (Hybrid)")
    v = evaluate(job)
    assert v.verdict == "manual_review"
    assert v.gate1.status == "pass"
    assert v.gate2.status == "soft"
    assert v.gate3.status == "soft"
    assert v.gate1.signal == pytest.approx(2.0)   # ai governance, eu ai act
    assert v.gate2.signal == 0.5
    assert v.gate3.signal == 0.0                   # undisclosed
    assert v.rank == pytest.approx(2.5)            # 2.0 + 0.5 + 0.0


def test_evaluate_any_hard_fail_is_reject(override_config):
    """E3: a single hard_fail (geo) -> reject, overriding soft/pass."""
    job = J(title="AI Governance Lead",
            description=("AI governance role, relocation required to Munich, "
                         "€140,000 gross"),
            location="Munich")
    v = evaluate(job)
    assert v.verdict == "reject"
    assert v.gate2.status == "hard_fail"
    assert v.gate2.signal == 0.0
    # reason concat shows the geo deny
    text = " ".join(v.reasons).lower()
    assert ("relocation required" in text) or ("relocate to munich" in text)


def test_evaluate_disclosed_belowfloor_comp_is_reject(override_config):
    """E3b (policy): a CLEANLY DISCLOSED below-floor salary (lane+geo pass) -> reject.

    Found in live-sample validation: a confidently-parsed '€45,000 gross/year' on an
    in-lane remote role was capping at manual_review because the code treated ALL
    comp hard_fails as 'inferred'. A disclosed figure clearly <= the 72k floor is the
    one case Gate 3's auto-reject band exists for — it must reject.
    """
    job = J(title="Junior AI Compliance Analyst",
            description=("AI governance, GRC automation, ISO 27001-style controls. "
                         "Remote EU. Salary: €45,000 gross per year."),
            location="Remote EU")
    v = evaluate(job)
    assert v.gate1.status == "pass"
    assert v.gate2.status == "pass"
    assert v.gate3.status == "hard_fail"
    assert v.verdict == "reject"


def test_evaluate_inferred_comp_hardfail_still_caps_manual(override_config):
    """E3c (Red-team #13 preserved): a comp hard_fail that is genuinely INFERRED
    (undisclosed) must NOT reject when lane+geo pass — it caps at manual_review.
    This guards against the E3b fix over-correcting into false-rejects."""
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act, model risk, fully remote across "
                         "EMEA. Competitive package."),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.gate3.status == "soft"          # undisclosed -> soft, not hard_fail
    assert v.verdict == "manual_review"


def test_evaluate_reasons_concatenated_order(override_config):
    """E4: Verdict.reasons is the ordered concatenation gate1 + gate2 + gate3."""
    job = J(title="AI Governance Engineer",
            description=("AI governance, EU AI Act, hybrid model, competitive package"),
            location="Sofia (Hybrid)")
    v = evaluate(job)
    assert v.verdict == "manual_review"
    expected = list(v.gate1.reasons) + list(v.gate2.reasons) + list(v.gate3.reasons)
    assert v.reasons == expected


def test_evaluate_rank_reads_weights_from_config(override_config, monkeypatch):
    """E1b (recommended extra): prove rank weights are read from config, not hardcoded.

    Patch w_geo=2.0 -> rank = 1.0*3.0 + 2.0*1.0 + 1.0*1.0 = 6.0.
    """
    monkeypatch.setattr(gatekeeper, "RANK_WEIGHTS",
                        {"w_lane": 1.0, "w_geo": 2.0, "w_comp": 1.0})
    job = J(title="AI Governance Engineer",
            description=("own AI governance, run LLM eval, EU AI Act, fully remote "
                         "across EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.verdict == "keep"
    assert v.rank == pytest.approx(6.0)


# =====================================================================
# GROUP F — GOLDEN CASES (worked rejects + clean keep + undisclosed keep-path)
# =====================================================================

def test_golden_lidl_it_business_solutions_specialist_rejects(override_config):
    """F1 (WORKED REJECT 1 — 0/3): Lidl 'IT Business Solutions Specialist'.

    Gate 1 hard_fail (lane_hits == 0; POS/sysadmin operator body). Verdict reject.
    """
    job = J(title="IT Business Solutions Specialist",
            description=("Support branch/store systems, POS support, sysadmin tasks "
                         "across retail sites, travel to North Macedonia, on-site only "
                         "in Sofia"),
            location="Sofia, Bulgaria")
    v = evaluate(job)
    assert v.verdict == "reject"
    assert v.gate1.status == "hard_fail"
    assert v.gate1.signal == 0.0
    assert "no ai-governance lane terms found" in _reasons_text(v.gate1)


def test_golden_merkle_it_business_analyst_sofia_rejects(override_config):
    """F2 (WORKED REJECT 2 — 0/3): Merkle 'IT Business Analyst, Sofia'.

    FIX #1 interaction: bare 'IT Business Analyst' survives title-deny but fails
    lane on lane_hits == 0 (NOT title-denied); net reject (one hard fail).
    Gate 2 soft (hybrid), Gate 3 soft (undisclosed) for audit context.
    """
    job = J(title="IT Business Analyst",
            description=("Gather requirements, stakeholder management, document "
                         "business processes; hybrid Sofia office; agile delivery"),
            location="Sofia (Hybrid)")
    v = evaluate(job)
    assert v.verdict == "reject"
    # Gate 1 hard-fails on lane_hits==0, NOT on the title-deny path:
    assert v.gate1.status == "hard_fail"
    g1_text = _reasons_text(v.gate1)
    assert "no ai-governance lane terms found" in g1_text
    assert "generic business analyst" not in g1_text
    # Audit context: geo soft (hybrid), comp soft (undisclosed)
    assert v.gate2.status == "soft"
    assert v.gate3.status == "soft"


def test_golden_inlane_emea_remote_disclosed_keep_outranks_undisclosed(override_config):
    """F3 (clean keep + FIX #3 manual_review rank): disclosed-in-band keep ranks
    ABOVE an undisclosed manual_review (comp_headroom 1.0 vs 0.0)."""
    job_a = J(title="AI Governance Engineer",
              description=("AI governance, MCP, RAG, fully remote EMEA, €140,000 gross/yr"),
              location="Remote EMEA")
    job_b = J(title="AI Governance Engineer",
              description=("AI governance, MCP, fully remote EMEA, competitive package"),
              location="Remote EMEA")
    va = evaluate(job_a)
    vb = evaluate(job_b)

    # (a) clean keep — all three pass
    assert va.verdict == "keep"
    assert va.gate1.status == "pass"
    assert va.gate2.status == "pass"
    assert va.gate3.status == "pass"

    # (b) undisclosed-good-gates -> manual_review NOT reject (FIX #3)
    assert vb.verdict == "manual_review"
    assert vb.gate3.status == "soft"
    assert vb.gate3.signal == 0.0

    # disclosed-in-band keep ranks ABOVE undisclosed
    assert va.rank > vb.rank
