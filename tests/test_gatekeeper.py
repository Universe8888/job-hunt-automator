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
MOCK_RANK_WEIGHTS = {"w_lane": 1.0, "w_geo": 1.0, "w_comp": 1.0, "w_fit": 4.0, "w_gap": 1.5}
# Over-seniority demands the candidate's record doesn't meet (de-rank only).
# Includes 'ci/cd security' (punctuation that _norm_lane rewrites) to guard the
# normalize-then-compile bugfix, and 'perform penetration testing' demand-form.
MOCK_SENIORITY_GAP_TERMS = [
    "10+ years", "build and lead a team", "incident commander",
    "perform penetration testing", "cissp", "ci/cd security",
]
# Unit-test defaults pin the LEGACY behaviour the existing E-group rank tests assume:
# fit normalized against /100, gap uncapped. New tests override these locally to
# exercise the production cap (3) and normalizer (50).
MOCK_GAP_PENALTY_CAP = 0       # uncapped (legacy)
MOCK_FIT_NORMALIZER_PCT = 100.0  # raw /100 (legacy)


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
    monkeypatch.setattr(gatekeeper, "SENIORITY_GAP_TERMS", MOCK_SENIORITY_GAP_TERMS)
    monkeypatch.setattr(gatekeeper, "GAP_PENALTY_CAP", MOCK_GAP_PENALTY_CAP)
    monkeypatch.setattr(gatekeeper, "FIT_NORMALIZER_PCT", MOCK_FIT_NORMALIZER_PCT)


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


# --- ARCHITECTURAL FIX (2026-06-30): allow-over-deny, REGION-scoped ---------------
# First-span-wins + "every geo deny is final" discarded a qualifying sibling: a role
# advertising both a denied region (US-remote) AND a clean allow (EMEA/EU/Europe)
# hard-rejected on the deny. The gate now routes a REGION-scope deny to soft when a
# CLEAN region/EOR allow sibling co-exists (human disambiguates). MODALITY denies
# (forced relocation / on-site-only) STAY hard even with an allow sibling — the spec
# pins Western relocation as a hard gate ("relocation destroys the arbitrage").

def test_geo_region_deny_with_clean_sibling_is_soft(override_config):
    """B7: a US-remote (region) deny co-occurring with a clean Europe allow routes to
    soft -> manual_review, not a final reject. The clean sibling is no longer lost."""
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act, model risk. The role is remote "
                         "across Europe; a separate US-remote track also exists."),
            location="Remote, Europe")
    v = evaluate(job)
    assert v.gate2.status == "soft", f"expected soft, got {v.gate2.status}"
    assert v.verdict == "manual_review", f"expected manual_review, got {v.verdict}"


def test_geo_pure_us_remote_no_sibling_still_rejects(override_config):
    """B8 (regression — loosening must NOT leak): a pure US-remote role with NO clean
    region sibling (only the bare 'remote' the deny phrase itself contains) still
    hard-fails -> reject. Bare 'remote' inside 'remote within US' is not a sibling."""
    job = J(title="AI Governance Lead",
            description="AI governance, EU AI Act. Remote within US only.",
            location="Remote (US)")
    v = evaluate(job)
    assert v.gate2.status == "hard_fail", f"expected hard_fail, got {v.gate2.status}"
    assert v.verdict == "reject"


def test_geo_modality_deny_not_softened_by_allow_sibling(override_config):
    """B9 (THE SPEC GUARD): a MODALITY deny (forced relocation) stays a HARD reject
    even when a clean remote allow sibling co-occurs — the spec pins Western relocation
    as a hard gate. A blanket allow-over-deny rule would wrongly soften this, silently
    weakening the geo filter; region-scoping the soften keeps it hard.

    Hardened (R2 audit): asserts against the LEAKY allow wordings that ARE in
    _GEO_EOR_OVERRIDE_HINT ('remote (emea)', 'remote (eu)', 'work from anywhere') — the
    original test passed only by accident because 'Remote across Europe' is NOT in that
    hint list, so it never exercised the pre-existing 'contrast' soft clause."""
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act. Remote across Europe, but "
                         "relocation required to the Munich hub."),
            location="Remote, Europe")
    v = evaluate(job)
    assert v.gate2.status == "hard_fail", f"expected hard_fail, got {v.gate2.status}"
    assert v.verdict == "reject"


def test_geo_modality_deny_not_softened_by_eor_hint_phrasings(override_config):
    """B9b (R2 audit, CRITICAL — the modality-deny leak): a forced-relocation / on-site
    MODALITY deny must STAY hard even when an EOR-hint allow phrase ('remote (emea)',
    'remote (eu)', 'work from anywhere') co-occurs. The pre-existing 'contrast' soft
    clause fired for ANY deny + EOR hint, softening relocation/on-site to manual_review
    — an UNSAFE false-pass (a hard-deny role escaping auto-reject). Region-scoping ALL
    softening clauses keeps modality denies hard. Each case below MUST reject."""
    for desc, loc in [
        ("AI governance. Relocation required to the Munich hub. We are a "
         "remote (EMEA) team.", "Munich"),
        ("AI governance. Relocate to London for this role. We also hire "
         "remote (eu) staff.", "London / Remote (EU)"),
        ("AI governance. On-site only at our hub, though we list work from "
         "anywhere elsewhere.", "Sofia"),
    ]:
        job = J(title="AI Governance Lead", description=desc, location=loc)
        v = evaluate(job)
        assert v.gate2.status == "hard_fail", \
            f"modality deny wrongly softened: {desc!r} -> {v.gate2.status}"
        assert v.verdict == "reject", f"{desc!r} -> {v.verdict}"


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


def test_comp_gate_belowfloor_remote_role_is_advisory_soft(override_config):
    """C13 (ADVISORY 2026-06-30): a sub-floor disclosed salary is advisory-soft, not
    hard_fail. Comp never drives a reject — the figure is surfaced for human review with
    headroom 0.0 (sorts last). Previously this hard-failed; under the advisory redesign
    no comp number, real or phantom, can flip the verdict."""
    pc = parse_comp("Remote EU. Salary: €45,000 gross per year")
    res = passes_comp_gate(pc)
    assert res.status == "soft"
    assert res.signal == 0.0


# --- Regression: audit-critical #1 — weekly/hourly rates must annualize ----------

def test_parse_comp_weekly_rate_annualized(override_config):
    """C14 (audit critical #1): a weekly rate is annualized (×WORKING_WEEKS_PER_YEAR),
    not treated as a raw annual total. '€5000 per week' ≈ €230k/yr, far above goal."""
    pc = parse_comp("Compensation: €5000 per week")
    assert pc is not None
    assert pc.period == "yr"
    assert pc.rate_unhandled is True
    # 5000 * 46 = 230000 — annualized far above goal.
    assert pc.top_eur_gross_yr == pytest.approx(5000 * gatekeeper.WORKING_WEEKS_PER_YEAR)
    # ADVISORY (2026-06-30): comp never drives the verdict — an at/above-goal figure is
    # soft with headroom clamped to 1.0 (it sorts to the top of manual_review).
    res = passes_comp_gate(pc)
    assert res.status == "soft"
    assert res.signal == pytest.approx(1.0)


def test_parse_comp_hourly_rate_annualized(override_config):
    """C15 (audit critical #1): an hourly rate annualizes via days×hours/day."""
    pc = parse_comp("Rate: €120 per hour")
    assert pc is not None
    assert pc.period == "yr"
    expected = 120 * gatekeeper.WORKING_DAYS_PER_YEAR * gatekeeper.WORKING_HOURS_PER_DAY
    assert pc.top_eur_gross_yr == pytest.approx(expected)


# --- Regression: audit-critical #2 — benign defaults must NOT leak below-floor ---

def test_comp_belowfloor_is_advisory_soft_with_zero_headroom(override_config):
    """C16 (ADVISORY 2026-06-30): a disclosed below-floor salary no longer HARD-fails —
    comp is advisory, so it is soft with comp_headroom 0.0 (it sorts to the BOTTOM of
    the manual_review bucket). The figure still parses cleanly; it just never drives a
    reject (which a phantom could not be distinguished from a real salary safely)."""
    pc = parse_comp("Salary: 70000 EUR per year")  # explicit currency+period, no 'gross'
    assert pc is not None
    assert pc.basis_inferred is True
    assert pc.period_inferred is False
    res = passes_comp_gate(pc)
    assert res.status == "soft"
    assert res.signal == 0.0  # below the 72k floor -> headroom clamps to 0


# --- FAIL-SAFE (structural): a fully-inferred bare integer must NEVER hard-reject ---
# The convergent fix for the phantom-comp class. A number where currency AND period
# AND basis were ALL guessed (no symbol, no /yr|/mo cue, no gross/net word) is too
# unreliable to hard-reject a real job on — it routes to manual_review. This kills
# the entire "headcount/boilerplate integer parsed as below-floor salary" family at
# the GATE, regardless of whether the upstream headcount regex caught the phrasing.

def test_comp_fully_inferred_belowfloor_is_soft_not_reject(override_config):
    """C27 (fail-safe): a below-floor figure with NO confident signal (currency,
    period, and basis ALL inferred) -> soft, never hard_fail. This is the structural
    safety net for phantom comp ('1500 full-time employees' style bare integers that
    slip past the headcount guard); a real job is never hard-rejected on a guess."""
    # A bare integer near a generic cue: currency assumed EUR, period guessed monthly,
    # basis assumed gross — all three inferred.
    pc = parse_comp("compensation 1500 full-time employees")
    if pc is not None:  # if the headcount guard drops it, that's also acceptable
        assert pc.currency_inferred and pc.period_inferred and pc.basis_inferred
        res = passes_comp_gate(pc)
        assert res.status == "soft", f"fully-inferred below-floor must be soft, got {res.status}"


def test_comp_fully_inferred_flag_routes_soft_directly(override_config):
    """C27b: prove the gate logic itself (not just the parser) fail-safes — a
    hand-built fully-inferred below-floor ParsedComp routes to soft."""
    pc = ParsedComp(top_eur_gross_yr=18000.0, currency="EUR", period="mo",
                    basis="gross", raw="1500",
                    currency_inferred=True, period_inferred=True, basis_inferred=True)
    assert passes_comp_gate(pc).status == "soft"


def test_comp_disclosed_belowfloor_is_advisory_soft_regardless_of_confidence(override_config):
    """C27c (ADVISORY 2026-06-30): every below-floor figure — cleanly disclosed or
    fully inferred — is now advisory-soft. Comp confidence no longer changes the gate
    STATUS (it never hard_fails); it only affects the rank headroom. This is the whole
    point of the advisory redesign: no comp reading can flip a verdict, so the parser's
    inability to tell a real €45k salary from a €45k phantom is harmless."""
    for txt in ["€45,000 gross per year", "Salary: 70000 EUR per year"]:
        res = passes_comp_gate(parse_comp(txt))
        assert res.status == "soft", f"{txt!r} -> {res.status}"
        assert res.signal == 0.0  # below floor -> headroom 0


def test_comp_belowfloor_period_inferred_stays_soft(override_config):
    """C17 (audit critical #2, other side): GENUINE uncertainty (period guessed) near
    the floor still gets the soft escape — we tightened the leak without removing the
    legitimate manual-review path.

    Option-B note: the figure must be a real DISCLOSURE (currency-anchored) — a bare
    'Package around 70000' with no currency and no salary label is now (correctly) not
    admitted as comp at all (-> undisclosed -> soft via passes_comp_gate(None)). Here a
    currency-anchored near-floor figure with an inferred period exercises the near-floor
    soft escape directly."""
    pc = parse_comp("around 70000 EUR")  # currency-anchored, period inferred (no /yr cue)
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


# --- Regression: live false-parse — a headcount / team-size number is NOT comp.
# Found in the 309-job re-rate (DBA @ DEVEXPERTS): "...exchange since 2015. With
# 800+ experts, we deliver..." parsed '800' as €800/mo -> €9,600/yr, which
# hard-rejects a role on a phantom salary. Company headcount must never be comp.

def test_parse_comp_headcount_not_salary(override_config):
    """C20 (live false-parse, DBA @ DEVEXPERTS, verbatim): '...with a team of more
    than 800+ professionals, the comp...' parsed '800' as €800/mo -> €9,600/yr
    because 'comp' (a salary cue) sits in the number's window. A headcount number
    must never be read as salary."""
    pc = parse_comp("We are a team of more than 800+ professionals, the company "
                    "delivers innovative software.")
    assert pc is None


def test_parse_comp_team_of_count_not_salary(override_config):
    """C21: 'a team of 1500' and similar team-size counts are headcount, not comp."""
    pc = parse_comp("Join a team of 1500 professionals. Salary discussed at interview.")
    assert pc is None


def test_parse_comp_real_salary_near_headcount_still_parses(override_config):
    """C22 (guard against over-correction): the headcount skip must drop only the
    headcount number — a real disclosed salary in the same text still parses."""
    pc = parse_comp("We are 800+ experts. Salary: €140,000 gross per year.")
    assert pc is not None
    assert pc.top_eur_gross_yr == 140000.0


def test_parse_comp_currency_salary_followed_by_role_noun_parses(override_config):
    """C23 (audit fix — the headcount guard's false-negative): a CURRENCY-anchored
    salary directly followed by a profession noun ('€60,000 for engineers') is a
    real salary, NOT headcount. The guard must apply ONLY to currency-less numbers,
    otherwise it silently drops disclosed comp."""
    for text, expected in [
        ("Salary: €60,000 for engineers", 60000.0),
        ("We offer €70,000 to senior developers", 70000.0),
        ("Compensation: €90,000 gross for specialists", 90000.0),
        ("Package: €120,000 per year for our consultants", 120000.0),
    ]:
        pc = parse_comp(text)
        assert pc is not None, f"salary wrongly dropped: {text!r}"
        assert pc.top_eur_gross_yr == expected, f"{text!r} -> {pc.top_eur_gross_yr}"


def test_parse_comp_cue_anchored_currencyless_salary_near_noun_parses(override_config):
    """C24 (re-audit critical): a CUE-anchored salary with NO currency symbol, sat
    near a profession noun, must still parse. The headcount guard must key on
    grammatical position (is the noun the IMMEDIATE next token, i.e. a count?) — not
    on 'no currency', which wrongly dropped 'Salary band 50000 for developers'.
    The noun here is a beneficiary ('for developers'), not a count ('800 developers')."""
    for text, expected in [
        ("Salary band 50000 for developers", 50000.0),
        ("Compensation 45000 per specialists", 45000.0),
        ("Salary: 60000 for engineers", 60000.0),
    ]:
        pc = parse_comp(text)
        assert pc is not None, f"cue-anchored salary wrongly dropped: {text!r}"
        assert pc.top_eur_gross_yr == expected, f"{text!r} -> {pc.top_eur_gross_yr}"


def test_parse_comp_team_of_clause_does_not_swallow_unrelated_salary(override_config):
    """C25 (round-2 false-neg): the 'team of …' headcount prefix must NOT swallow an
    unrelated salary that merely appears a few words later. The 'team of' guard should
    only fire on a genuine 'team of <hedge> <N>' COUNT ('team of more than 800'), not
    on any number within a few words of a 'team of …' clause."""
    for text, expected in [
        ("team of experts offering 80000 eur", 80000.0),
        ("join the team of the new salary 60000 eur", 60000.0),
        ("our team of professionals; salary 70000 eur gross", 70000.0),
    ]:
        pc = parse_comp(text)
        assert pc is not None, f"team-of clause swallowed a real salary: {text!r}"
        assert pc.top_eur_gross_yr == expected, f"{text!r} -> {pc.top_eur_gross_yr}"


def test_parse_comp_genuine_team_of_count_still_dropped(override_config):
    """C26 (guard for C25's fix): a genuine 'team of <N>' / 'team of more than <N>'
    count must STILL be dropped — the tightened bridge must not over-relax."""
    for text in [
        "Join a team of 1500 professionals",
        "team of more than 800 people",
        "a team of over 200 engineers",
    ]:
        assert parse_comp(text) is None, f"genuine headcount not dropped: {text!r}"


# --- ARCHITECTURAL FIX (2026-06-30): non-salary-context suppression. -------------
# The confirmed false-reject class: free-text numbers that are NOT salary (budget /
# perk / revenue / allowance) carry an explicit currency+period, so the parser read
# them as disclosed below-floor pay and HARD-REJECTED a real role. A salary number is
# only "disclosed comp" (spec: "≤72k auto-reject — disclosed ONLY") when it is bound
# to a salary disclosure, not merely governed by a budget/revenue noun. parse_comp
# now suppresses a money candidate physically governed by a non-salary noun UNLESS a
# salary LABEL sits CLOSER to the number than the non-salary noun (directional rescue).

def test_parse_comp_budget_line_not_salary(override_config):
    """C-budget-1 (confirmed defect): a tooling-budget line with explicit currency+
    period is NOT salary -> None. Previously parsed €30k and hard-rejected the role."""
    assert parse_comp("Budget: 30000 EUR per year for tooling") is None


def test_parse_comp_budget_line_routes_manual_review_end_to_end(override_config):
    """C-budget-2 (end-to-end): an in-lane remote role whose only number is a tooling
    budget must NOT reject — comp is undisclosed -> soft -> manual_review."""
    job = J(title="AI Governance Lead",
            description=("Own AI governance and EU AI Act compliance. Fully remote "
                         "across EMEA. Budget: 30000 EUR per year for tooling."),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.gate3.status == "soft"
    assert v.verdict == "manual_review", f"expected manual_review, got {v.verdict}"


def test_parse_comp_revenue_arr_not_salary(override_config):
    """C-revenue: a revenue/ARR figure with currency+period is company metrics, not pay."""
    assert parse_comp("Our ARR reached 90000 EUR this year") is None
    assert parse_comp("Annual revenue of 250000 EUR per year") is None


def test_parse_comp_allowance_not_salary(override_config):
    """C-allowance: a perk/allowance figure is not the salary -> None."""
    assert parse_comp("Wellness allowance 5000 EUR per year") is None


def test_parse_comp_bg_budget_not_salary(override_config):
    """C-bg-budget (bilingual gap): a Bulgarian budget noun ('бюджет') suppresses too."""
    assert parse_comp("Бюджет 30000 EUR на година за инструменти") is None


def test_parse_comp_salary_near_budget_keeps_the_salary(override_config):
    """C-overcorrect (directional rescue — the bug the design judge caught): a real
    disclosed salary that sits NEAR a larger budget figure must still parse to the
    SALARY, not the budget. A naive 'salary word anywhere in window' rescue would keep
    the €500k budget (its window also catches 'gross') and select it as the top figure;
    the directional rule keeps €130k (label adjacent) and suppresses €500k (budget
    adjacent)."""
    pc = parse_comp("Salary: €130,000 gross. You also manage a 500000 EUR budget.")
    assert pc is not None
    assert pc.top_eur_gross_yr == 130000.0, f"expected 130000, got {pc.top_eur_gross_yr}"


def test_parse_comp_period_unit_alone_does_not_anchor(override_config):
    """C-perk-no-currency (anchor narrowing, defense in depth): a bare period unit
    ('per year') with NO currency and NO salary label can no longer anchor a number as
    comp — closes the perk/revenue case the noun list alone would miss."""
    assert parse_comp("30000 per year for tooling") is None


def test_parse_comp_suppression_does_not_break_real_disclosures(override_config):
    """C-guard: the suppression must not over-fire — disclosures with no non-salary
    noun nearby still parse exactly as before (regression net for C2/C17/C24)."""
    assert parse_comp("Salary: €140,000 gross per year").top_eur_gross_yr == 140000.0
    # Option B: a currency-anchored figure still parses; the salary-LABEL path admits a
    # currency-less figure ("Salary band 50000") because the label binds it.
    assert parse_comp("around 70000 EUR").period_inferred is True
    assert parse_comp("Salary band 50000 for developers").top_eur_gross_yr == 50000.0
    # ADVISORY: a disclosed below-floor salary parses and is advisory-soft (headroom 0).
    res = passes_comp_gate(parse_comp("Salary: 70000 EUR per year"))
    assert res.status == "soft" and res.signal == 0.0


def test_parse_comp_leading_salary_label_governs_despite_interposed_perk(override_config):
    """C-rescue-clause (R2 audit, safe-direction false-reject): a disclosed below-floor
    salary introduced by a leading 'Salary' label must STILL hard-reject even when a
    perk/budget noun sits between the label and the number (a subordinate clause like
    'after the relocation allowance'). The directional rescue compares to the NEAREST
    salary label by RAW char distance, so an interposed perk noun wrongly won and the
    figure was suppressed -> manual_review. A salary label that PRECEDES the number in
    the same sentence governs it, so these must parse and hard-reject."""
    for text in [
        "Your salary, after the relocation allowance, will be 60000 EUR per year.",
        "Salary, after reimbursement adjustments, is 55000 EUR per year.",
        "Salary: 60000 EUR per year. Stipend separate.",
    ]:
        pc = parse_comp(text)
        assert pc is not None, f"disclosed salary wrongly suppressed: {text!r}"
        # ADVISORY: below-floor disclosed salary -> soft with headroom 0 (was hard_fail).
        res = passes_comp_gate(pc)
        assert res.status == "soft" and res.signal == 0.0, \
            f"below-floor disclosed salary must be advisory-soft: {text!r}"
    # CONTROL — must NOT over-correct: a number that is genuinely the BUDGET (no salary
    # label before it in the clause) is still suppressed.
    assert parse_comp("Budget: 30000 EUR per year for tooling") is None
    assert parse_comp("You also manage a 500000 EUR budget") is None
    # ACCEPTED SAFE-DIRECTION (Option B): a contrived clause where a generic content
    # noun ('issues') grammatically governs the number is refused -> undisclosed ->
    # manual_review. Never a false keep/reject; realistic 'Salary: N' / 'Salary range:
    # 55000-60000' forms above still parse. Pinned so the tradeoff is explicit.
    assert parse_comp("Salary range with no stipend issues: 60000 EUR. Stipend separate.") is None
    assert parse_comp("Salary range: 55000-60000 EUR per year.").top_eur_gross_yr == 60000.0


# --- LABEL-GATED comp (Option B, 2026-06-30): structural admission. A number is comp
# ONLY when a salary LABEL binds it OR currency is adjacent with no non-salary noun
# governing it. Kills the whole phantom class (financial jargon, BG inflections, ';'
# line items) the R3 audit surfaced — by construction, not by enumerating nouns.

def test_comp_business_jargon_integer_not_salary(override_config):
    """C-jargon (R3 F6/F7, was critical both directions): a bare integer near business
    jargon 'gross margin' / 'gross profit' / 'net new' / 'net revenue' is NOT pay.
    'gross'/'net' are BASIS cues, never salary LABELS, so they cannot admit a bare
    integer. These route to undisclosed -> manual_review, never reject and never keep."""
    for d in [
        "Gross margin reached 50000 last year.",
        "We hit 12000 in gross margin.",
        "We added 10000 net new seats.",
        "Net revenue of 90000 last quarter.",
        "Delivered 5000 in gross profit per client.",
    ]:
        assert parse_comp(d) is None, f"business jargon wrongly parsed as comp: {d!r}"
        job = J(title="AI Governance Engineer",
                description=d + " ai governance, grc, soc2.", location="Remote (EMEA)")
        v = evaluate(job)
        assert v.verdict == "manual_review", f"{d!r} -> {v.verdict}"


def test_comp_bg_inflected_nonsalary_nouns_suppressed(override_config):
    """C-bg-inflect (R3 F3/F4, was critical): Bulgarian non-salary nouns inflect with a
    trailing definite article (бюджетът, оборота, приходите) — stem-tolerant matching
    must still suppress them. A BG turnover line must not mask a disclosed salary, and a
    BG budget line must not be read as a disclosed salary."""
    # F4: a pure BG budget line -> undisclosed (suppressed), NOT a disclosed €50k reject
    assert parse_comp("Бюджетът за този проект е 50000 EUR годишно.") is None
    assert parse_comp("Приходите на компанията са 250000 EUR.") is None
    # F3: a disclosed below-floor salary is KEPT despite a huge co-present turnover line
    pc = parse_comp("Брутна заплата 60000 EUR годишно. Оборотът на компанията "
                    "надхвърля 5000000 EUR.")
    assert pc is not None and pc.top_eur_gross_yr == 60000.0
    # ADVISORY: the disclosed 60k (not the 5M turnover) is selected, soft + headroom 0.
    res = passes_comp_gate(pc)
    assert res.status == "soft" and res.signal == 0.0


def test_comp_currency_adjacent_business_figure_not_admitted(override_config):
    """C-rule-b (R4, was 8x blocking): a currency-adjacent number GOVERNED by a
    non-salary content noun (deal size / contract value / project cost / prize pool /
    quota / pipeline / fee) is NOT comp — even though the noun is not on any blocklist.
    Admission is structural (governing head must mark PAY), so the phantom class is
    closed regardless of which non-salary noun is used. Below-floor phantoms must NOT
    reject; above-floor phantoms must NOT keep — both route to manual_review."""
    for t in [
        "Average deal size is 60,000 EUR per region.",
        "Contract value: 250,000 EUR over three years.",
        "Our project cost 200000 EUR.",
        "The prize pool is 500,000 EUR for winners.",
        "Your quota is 1,200,000 EUR.",
        "Sales pipeline of 3,000,000 EUR.",
        "Our annual tooling fee is 30000 EUR.",
        "We have 1500 EUR per employee in perks.",
    ]:
        assert parse_comp(t) is None, f"non-salary money figure wrongly admitted: {t!r}"
    # end-to-end: an in-lane remote role whose only number is a business figure ->
    # manual_review (undisclosed), never reject (below-floor) or keep (above-floor).
    for desc in [
        "Fully remote EMEA. Average deal size is 60,000 EUR per region.",      # below floor
        "Remote EMEA. Our project cost 200000 EUR. Comp discussed at interview.",  # above
        "Remote EMEA. Our annual tooling fee is 30000 EUR. Comp competitive.",     # below
    ]:
        job = J(title="AI Governance Lead",
                description=desc + " AI tool inventory, shadow-AI discovery, EU AI Act.",
                location="Remote (EMEA)")
        v = evaluate(job)
        assert v.verdict == "manual_review", f"{desc!r} -> {v.verdict}"


def test_comp_currency_adjacent_real_disclosures_still_parse(override_config):
    """C-rule-b guard: the structural admission must NOT over-refuse genuine bare-currency
    and pay-marked disclosures — these are the legitimate forms boards emit."""
    for t, expected in [
        ("$150,000/yr", 138000.0),
        ("€120k", 120000.0),
        ("60 000 лв", 30678.0),
        ("We offer €70,000 to senior developers", 70000.0),
        ("Salary band 50000 for developers", 50000.0),
        ("Rate: €120 per hour", 211200.0),
        ("Base €120,000 + equity. Per year.", 120000.0),
    ]:
        pc = parse_comp(t)
        assert pc is not None, f"real disclosure wrongly refused: {t!r}"
        assert round(pc.top_eur_gross_yr) == round(expected), f"{t!r} -> {pc.top_eur_gross_yr}"


def test_comp_cross_clause_basis_period_cue_does_not_leak(override_config):
    """C-clause-cue (R4, was critical): net/gross + period cue detection is CLAUSE-scoped,
    so a 'net margin'/'net income'/'monthly ...' idiom in a NEIGHBORING clause cannot flip
    a disclosed salary's basis or period. Previously '65000 gross. Net margin...' grossed
    up x1.146 to clear the floor (false keep); '70000/yr; monthly spend' flipped to /mo."""
    # cross-clause 'net' must NOT flip a below-floor gross salary into a grossed-up one:
    # the magnitude stays 65000 (basis gross), so its rank headroom is 0 (below floor).
    pc = parse_comp("Salary 65000 EUR per year. Net margin grew 5000000.")
    assert pc is not None and pc.basis == "gross" and pc.top_eur_gross_yr == 65000.0
    assert passes_comp_gate(pc).signal == 0.0  # advisory: below-floor -> headroom 0
    # cross-clause 'net income' (';'-joined) must not gross-up the salary
    pc2 = parse_comp("Salary 130000 EUR; net income 5000000 EUR")
    assert pc2 is not None and pc2.basis == "gross" and pc2.top_eur_gross_yr == 130000.0
    # cross-clause 'monthly' must not annualize x12
    pc3 = parse_comp("Salary 70000 EUR per year; monthly spend is tracked.")
    assert pc3 is not None and pc3.period == "yr" and pc3.top_eur_gross_yr == 70000.0
    # IN-clause basis/period still work (no over-correction)
    assert parse_comp("Salary 90000 EUR net per year").basis == "net"
    assert parse_comp("Salary: 6000 EUR per month").period == "mo"


def test_comp_label_does_not_reach_across_clause_to_budget(override_config):
    """C-clause (R3 F2/F5, was unsafe): a salary LABEL must not 'rescue' a budget figure
    in a SEPARATE clause (';'/'.'/newline) or one that a non-salary noun GOVERNS. So a
    'salary philosophy ... budget is 130000' parses nothing (budget governs), and a
    ';'-joined 'Salary: 68000; budget: 200000' keeps the 68000 salary, not the budget."""
    assert parse_comp("Our salary philosophy is transparent and our annual "
                      "marketing budget is 130000 EUR.") is None
    assert parse_comp("Competitive salary offered; separately, the project budget "
                      "is 150000 EUR per year.") is None
    # ';'-joined salary + budget line items: the salary wins, budget is not rescued.
    pc = parse_comp("Salary: 68000 EUR; budget: 200000 EUR for the team.")
    assert pc is not None and pc.top_eur_gross_yr == 68000.0


# --- ADVISORY comp (2026-06-30): every comp reading is verdict-HARMLESS ----------
# The structural guarantee the advisory redesign buys: NO comp figure — genuine salary
# OR phantom (budget/deal-size/turnover/margin, any phrasing/language/punctuation) —
# can ever flip evaluate() to keep or reject. The R5 audit's 8 blocking phantoms (which
# defeated every parser-side rule) are pinned here as harmless: each yields
# manual_review on an in-lane/in-geo job, never reject (false drop) and never keep
# (false lead). This is what closed the comp arms race — verdict invariance, not a
# perfect parser.

def test_comp_advisory_phantoms_are_verdict_harmless(override_config):
    """The R5 phantom set — comma-appositive, clause-leading, right-governed,
    multi-figure — can no longer corrupt a verdict. Each in-lane/in-geo job lands in
    manual_review regardless of whether the phantom is below- or above-floor."""
    phantoms = [
        "Salary varies, cost center budget, 60000 EUR, is managed by you.",   # R5 #1
        "Revenue, 5000000 EUR, grew last year.",                              # R5 #2/#3
        "50000 EUR budget for security tooling this year.",                   # R5 #4 (clause-leading)
        "Budget, allocated this year, was 200000 EUR.",                       # R5 #5 (comma)
        "Our run rate is 3000000 EUR and ARR hit 5000000 EUR.",               # novel nouns
        "The fund is 200000 EUR; the prize pool is 80000 EUR.",               # multi-figure
    ]
    for body in phantoms:
        job = J(title="AI Governance Lead",
                description=("AI tool inventory, shadow-AI discovery, GRC automation, "
                             "EU AI Act, AI risk. Fully remote EMEA. " + body),
                location="Remote (EMEA)")
        v = evaluate(job)
        assert v.verdict == "manual_review", \
            f"phantom corrupted verdict: {body!r} -> {v.verdict}"
        assert v.gate3.status == "soft"


def test_comp_advisory_disclosed_salary_also_manual_review(override_config):
    """Symmetry check: a genuinely disclosed at-goal salary on an in-lane/in-geo role is
    ALSO manual_review (comp never 'passes'). keep is unreachable via comp by design;
    the disclosed role is distinguished from undisclosed by its higher RANK, not verdict."""
    job = J(title="AI Governance Engineer",
            description=("AI governance, EU AI Act, LLM eval. Remote EMEA. "
                         "Salary: €140,000 gross per year."),
            location="Remote (EMEA)")
    v = evaluate(job)
    assert v.verdict == "manual_review"
    assert v.gate3.status == "soft"
    assert v.gate3.signal == 1.0             # at goal -> top of the manual_review bucket


def test_comp_advisory_disclosed_comp_required_optin_still_hard_fails(override_config_require_comp):
    """The disclosure-PRESENCE gate is orthogonal and preserved: with the explicit
    DISCLOSED_COMP_REQUIRED opt-in, an UNDISCLOSED role still hard_fails -> reject. The
    advisory redesign removed the FIGURE-magnitude reject, not this presence opt-in."""
    job = J(title="AI Governance Engineer",
            description="AI governance, EU AI Act, LLM eval. Remote EMEA. No pay listed.",
            location="Remote (EMEA)")
    v = evaluate(job)
    assert v.gate3.status == "hard_fail"
    assert v.verdict == "reject"
    # but a DISCLOSED below-floor figure under the same flag is still advisory-soft
    # (the opt-in gates DISCLOSURE PRESENCE, not the figure's magnitude):
    res = passes_comp_gate(parse_comp("Salary: 45000 EUR per year"))
    assert res.status == "soft"


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


# ADVISORY redesign (2026-06-30): a PARSED figure never drives the verdict — Gate 3 is
# always 'soft' for any figure (never hard_fail, never the 'pass' that completes a keep).
# The EUR72k/floor/goal bands survive ONLY as comp_headroom = clamp((T-72000)/65500,0,1),
# a pure RANK signal that sorts survivors inside the manual_review bucket. D3-D9 below
# assert the SIGNAL (the band math, unchanged) while the STATUS is uniformly soft.

def test_comp_at_72000_advisory_soft_zero_headroom(override_config):
    """D3 (boundary <=): 72000 is at/under the floor -> headroom 0; advisory soft."""
    res = passes_comp_gate(_comp(72000))
    assert res.status == "soft"
    assert res.signal == 0.0


def test_comp_just_above_72000_headroom_positive(override_config):
    """D4 (boundary): 72001 -> headroom just above 0 (strict >); advisory soft."""
    res = passes_comp_gate(_comp(72001))
    assert res.status == "soft"
    assert "below floor" in _reasons_text(res)
    assert res.signal == pytest.approx(1.0 / 65500, abs=1e-7)  # (72001-72000)/65500
    assert res.signal > 0.0


def test_comp_below_floor_band_headroom(override_config):
    """D5: 72k < T < 120k -> below-floor band label + headroom math (rank signal)."""
    res = passes_comp_gate(_comp(110000))
    assert res.status == "soft"
    assert "below floor" in _reasons_text(res)
    assert res.signal == pytest.approx(0.5802, abs=1e-3)  # (110000-72000)/65500


def test_comp_at_120000_capable_but_short_headroom(override_config):
    """D6 (boundary): inclusive 120000 -> capable-but-short band; advisory soft."""
    res = passes_comp_gate(_comp(120000))
    assert res.status == "soft"
    assert "capable but short" in _reasons_text(res)
    assert res.signal == pytest.approx(0.7328, abs=1e-3)  # (120000-72000)/65500


def test_comp_capable_but_short_just_under_goal(override_config):
    """D7 (boundary): 137499 still capable-but-short (strict < goal); advisory soft."""
    res = passes_comp_gate(_comp(137499))
    assert res.status == "soft"
    assert "capable but short" in _reasons_text(res)


def test_comp_at_goal_headroom_one(override_config):
    """D8 (boundary): inclusive 137500 -> meets-goal label; headroom clamps to 1.0."""
    res = passes_comp_gate(_comp(137500))
    assert res.status == "soft"
    assert "137.5k goal" in _reasons_text(res)
    assert res.signal == 1.0


def test_comp_above_goal_headroom_clamped(override_config):
    """D9: headroom clamp ceiling at 1.0; advisory soft."""
    res = passes_comp_gate(_comp(200000))
    assert res.status == "soft"
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
    # ADVISORY (2026-06-30): comp is always soft, so an in-lane/in-geo role with a
    # parsed figure is manual_review (not keep). Rank is UNCHANGED — comp_headroom (1.0
    # at goal) still feeds w_comp. keep is now reachable only when comp is... never:
    # comp never 'passes', so the top funnel bucket is manual_review. Both keep and
    # manual_review go to the leads CSV, so the funnel is unaffected.
    assert v.verdict == "manual_review"
    assert v.gate1.status == "pass"
    assert v.gate2.status == "pass"
    assert v.gate3.status == "soft"
    assert v.gate1.signal == pytest.approx(3.0)   # ai governance, llm eval, eu ai act
    assert v.gate2.signal == 1.0
    assert v.gate3.signal == 1.0                   # 140000 >= goal -> headroom clamp 1.0
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


def test_evaluate_disclosed_belowfloor_comp_is_manual_review(override_config):
    """E3b (ADVISORY POLICY 2026-06-30 — deliberate change): a cleanly DISCLOSED
    below-floor salary on an in-lane/in-geo role is now manual_review, NOT reject.

    History: this previously hard-rejected (the EUR72k auto-reject floor). Five audit
    rounds proved the parser cannot reliably tell a real €45k salary from a €45k phantom
    (deal size / budget / turnover), so comp was made ADVISORY: it never drives a reject.
    The €72k floor survives as a RANK signal (headroom 0 -> sorts last in manual_review),
    so a below-floor role is still de-prioritised, just not auto-dropped. User-approved
    tradeoff; see .local/filter-spec.md comp-advisory note.
    """
    job = J(title="Junior AI Compliance Analyst",
            description=("AI governance, GRC automation, ISO 27001-style controls. "
                         "Remote EU. Salary: €45,000 gross per year."),
            location="Remote EU")
    v = evaluate(job)
    assert v.gate1.status == "pass"
    assert v.gate2.status == "pass"
    assert v.gate3.status == "soft"
    assert v.gate3.signal == 0.0             # below floor -> headroom 0 (ranks last)
    assert v.verdict == "manual_review"


def test_evaluate_undisclosed_comp_is_manual_review(override_config):
    """E3c: an undisclosed-comp in-lane/in-geo role is manual_review (comp soft). Pairs
    with E3b — disclosed-below-floor and undisclosed now land in the SAME bucket, which
    is the advisory design's point: comp never splits keep/reject, a human confirms pay."""
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act, model risk, fully remote across "
                         "EMEA. Competitive package."),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.gate3.status == "soft"          # undisclosed -> soft
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
    assert v.verdict == "manual_review"      # advisory comp -> soft (rank unchanged)
    assert v.rank == pytest.approx(6.0)


def test_rank_fit_term_lifts_matching_role(override_config):
    """E5 (NEW — fit): a job carrying match_score gains w_fit*(score/100).
    Base rank 5.0 (3 lane + 1 geo + 1 comp) + 4.0*0.75 = 8.0."""
    job = J(title="AI Governance Engineer",
            description=("own AI governance, run LLM eval, EU AI Act, fully remote "
                         "across EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    job["match_score"] = 75.0           # attached upstream by profile_matcher
    v = evaluate(job)
    assert v.verdict == "manual_review"     # advisory comp -> soft (rank unchanged)
    assert v.rank == pytest.approx(8.0)     # 5.0 + 4.0*0.75
    assert any("fit" in r.lower() for r in v.reasons)


def test_rank_fit_absent_is_backcompat_zero(override_config):
    """E5b (NEW — back-compat): no match_score -> fit term is 0, rank unchanged."""
    job = J(title="AI Governance Engineer",
            description=("AI governance, LLM eval, EU AI Act, fully remote EMEA, "
                         "€140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    assert v.rank == pytest.approx(5.0)     # identical to E1 — no fit, no gap


def test_rank_over_seniority_penalty_docks_sprawling_role(override_config):
    """E6 (NEW — gap, THE 14.0 FIX): a keyword-dense senior JD demanding tenure +
    people-leadership + SecOps the candidate lacks is docked w_gap per distinct hit.
    Lane=3, geo=1, comp=1 (base 5.0); 3 gap hits (10+ years, build and lead a team,
    incident commander) -> -1.5*3 = -4.5 -> rank 0.5."""
    job = J(title="AI Governance, Risk & Compliance Manager",
            description=("own AI governance, EU AI Act, LLM eval. Requires 10+ years "
                         "in security, build and lead a team, act as incident commander. "
                         "Fully remote EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    # de-rank only — NEVER flips the verdict to reject. (Verdict is manual_review under
    # advisory comp; the gap penalty only lowers rank, it never rejects.)
    assert v.verdict == "manual_review"
    assert v.rank == pytest.approx(0.5)      # 5.0 - 1.5*3
    assert any("over-seniority" in r.lower() for r in v.reasons)


def test_rank_tight_fit_role_outranks_sprawling_monster(override_config):
    """E7 (NEW — the whole point): a tight, in-altitude AI-Gov role the candidate
    fits OUT-RANKS the sprawling over-senior JD, reversing the keyword-only order."""
    monster = J(title="AI Governance, Risk & Compliance Manager",
                description=("AI governance, EU AI Act, LLM eval, MCP, RAG, SAM. "
                             "10+ years, build and lead a team, incident commander, "
                             "penetration testing, CISSP. Remote EMEA, €140,000 gross/yr"),
                location="Remote EMEA")
    monster["match_score"] = 25.0            # candidate matches little of it
    tight = J(title="AI Governance Analyst",
              description=("AI governance, EU AI Act, fully remote EMEA, "
                           "€130,000 gross/yr"),
              location="Remote EMEA")
    tight["match_score"] = 80.0              # strong personal fit
    vm = evaluate(monster)
    vt = evaluate(tight)
    # Keyword-only, the monster would win on lane_hits; with fit+gap the tight role wins.
    assert vt.rank > vm.rank


def test_gap_term_with_punctuation_matches_after_normalize(override_config):
    """E8 (NEW — BUGFIX: ci/cd dead-rule). A gap term whose _norm_lane form diverges
    from its raw form ('ci/cd security' -> 'ci cd security') must still fire. Before
    the normalize-then-compile fix this matched NOTHING and silently escaped the
    penalty. The hit label is reported as the ORIGINAL term for readable audit."""
    job = J(title="Security Engineer",
            description=("own AI governance, EU AI Act, LLM eval. You will own CI/CD "
                         "security and pipeline hardening. Remote EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    hits = gatekeeper._seniority_gap_hits(job)
    assert "ci/cd security" in hits          # original-term label, matched via norm form
    # base 5.0 (3 lane + geo + comp_headroom) - 1.5*1 gap = 3.5
    assert v.rank == pytest.approx(3.5)
    assert v.verdict == "manual_review"      # de-rank only (advisory comp -> soft)


def test_gap_penalty_cap_limits_charged_hits(monkeypatch, override_config):
    """E9 (NEW — Fix #4: cap). With GAP_PENALTY_CAP=3, a JD hitting 4 distinct gap
    terms is charged only 3 (-4.5), not 4 (-6.0). Reason string notes the cap."""
    monkeypatch.setattr(gatekeeper, "GAP_PENALTY_CAP", 3)
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act, LLM eval. Requires 10+ years, "
                         "build and lead a team, act as incident commander, and you "
                         "will perform penetration testing. Remote EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    hits = gatekeeper._seniority_gap_hits(job)
    assert len(hits) == 4                     # all four still detected...
    assert v.rank == pytest.approx(0.5)       # ...but only 3 charged: 5.0 - 1.5*3
    assert any("capped at 3 of 4" in r for r in v.reasons)


def test_gap_penalty_uncapped_when_cap_zero(override_config):
    """E9b (NEW — cap off): GAP_PENALTY_CAP=0 (the fixture default) charges every hit,
    preserving the original uncapped behaviour."""
    job = J(title="AI Governance Lead",
            description=("AI governance, EU AI Act, LLM eval. 10+ years, build and lead "
                         "a team, act as incident commander, perform penetration testing. "
                         "Remote EMEA, €140,000 gross/yr"),
            location="Remote EMEA")
    v = evaluate(job)
    assert len(gatekeeper._seniority_gap_hits(job)) == 4
    assert v.rank == pytest.approx(-1.0)      # 5.0 - 1.5*4, uncapped


def test_fit_normalizer_rescales_realistic_scores(monkeypatch, override_config):
    """E10 (NEW — Fix #6: fit normalizer). With FIT_NORMALIZER_PCT=50, a realistic
    16% match maps to fit_signal 0.32 -> +1.28 (not +0.64 under raw /100), making fit
    a real lever. Still clamped to 1.0 for an unusually high score."""
    monkeypatch.setattr(gatekeeper, "FIT_NORMALIZER_PCT", 50.0)
    job = J(title="AI Governance Analyst",
            description=("AI governance, EU AI Act, LLM eval, fully remote EMEA, "
                         "€140,000 gross/yr"),
            location="Remote EMEA")
    job["match_score"] = 16.0
    v = evaluate(job)
    # base 5.0 + w_fit(4.0)*min(16/50,1)=0.32 -> +1.28 = 6.28
    assert v.rank == pytest.approx(6.28)
    # clamp: a 60% score over a 50 normalizer is still capped at fit_signal 1.0
    job2 = J(title="AI Governance Analyst",
             description="AI governance, EU AI Act, LLM eval, remote EMEA, €140,000 gross/yr",
             location="Remote EMEA")
    job2["match_score"] = 60.0
    assert gatekeeper._fit_signal(job2) == pytest.approx(1.0)


def test_governance_adjacent_term_does_not_false_fire(override_config):
    """E11 (NEW — Fix #2/#3: false-penalty). A compliance role that merely LISTS a
    security concept as oversight scope must NOT be docked; only demand-form fires.
    'knowledge of vulnerability management' -> no hit (not in mock list as bare term);
    'at scale' is gone entirely so benign 'automation at scale' never fires."""
    job = J(title="AI Governance Analyst",
            description=("map AI governance controls including knowledge of vulnerability "
                         "management evidence; build automation at scale; EU AI Act; "
                         "remote EMEA, €130,000 gross/yr"),
            location="Remote EMEA")
    hits = gatekeeper._seniority_gap_hits(job)
    assert hits == []                         # no false gap on governance-adjacent language
    v = evaluate(job)
    assert not any("over-seniority" in r.lower() for r in v.reasons)


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


def test_golden_inlane_emea_remote_disclosed_outranks_undisclosed(override_config):
    """F3 (ADVISORY rank): a disclosed at-goal salary RANKS ABOVE an undisclosed role
    (comp_headroom 1.0 vs 0.0), even though both now land in manual_review (comp is
    advisory). The rank ordering — the thing that actually surfaces the better lead to
    the top of the leads CSV — is preserved; only the verdict label changed."""
    job_a = J(title="AI Governance Engineer",
              description=("AI governance, MCP, RAG, fully remote EMEA, €140,000 gross/yr"),
              location="Remote EMEA")
    job_b = J(title="AI Governance Engineer",
              description=("AI governance, MCP, fully remote EMEA, competitive package"),
              location="Remote EMEA")
    va = evaluate(job_a)
    vb = evaluate(job_b)

    # (a) disclosed at-goal: lane+geo pass, comp advisory-soft with headroom 1.0
    assert va.verdict == "manual_review"
    assert va.gate1.status == "pass"
    assert va.gate2.status == "pass"
    assert va.gate3.status == "soft"
    assert va.gate3.signal == 1.0            # 140k >= goal -> headroom clamp 1.0

    # (b) undisclosed: comp soft, headroom 0.0
    assert vb.verdict == "manual_review"
    assert vb.gate3.status == "soft"
    assert vb.gate3.signal == 0.0

    # disclosed-at-goal RANKS ABOVE undisclosed (the surviving discriminator)
    assert va.rank > vb.rank
