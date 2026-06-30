"""Tests for jobsbg_scraper.py — URL building, HTML card parsing, geo classifier."""

import pytest

from jobsbg_scraper import (
    build_jobsbg_search_url,
    parse_jobsbg_cards,
    classify_location_spans,
)


SAMPLE_LOCATION = {
    "name": "Plovdiv, Bulgaria",
    "geoId": "104642436",
    "location_text": "Plovdiv",
    "jobsbg_location_id": "2",
}


class TestBuildJobsbgSearchUrl:
    def test_basic_url(self):
        url = build_jobsbg_search_url("Python Developer", SAMPLE_LOCATION)
        assert "https://www.jobs.bg/en/front_job_search.php" in url
        assert "keywords%5B%5D=Python+Developer" in url or "keywords[]=Python+Developer" in url.replace("%5B%5D", "[]")
        assert "subm=1" in url

    def test_with_location_id(self):
        url = build_jobsbg_search_url("Python Developer", SAMPLE_LOCATION)
        assert "location_sid=2" in url

    def test_with_offset(self):
        url = build_jobsbg_search_url("Python Developer", SAMPLE_LOCATION, offset=15)
        assert "from=15" in url

    def test_without_location_id(self):
        location_no_id = {
            "name": "Remote",
            "geoId": "91000007",
            "location_text": "Remote",
            "jobsbg_location_id": "",
        }
        url = build_jobsbg_search_url("Python Developer", location_no_id)
        assert "location_sid" not in url

    def test_zero_offset_has_no_from_param(self):
        url = build_jobsbg_search_url("Python Developer", SAMPLE_LOCATION, offset=0)
        assert "from=" not in url


class TestParseJobsbgCards:
    def test_parses_single_job_card(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/python-developer-123" title="Python Developer">
                    Python Developer
                </a>
                <a href="/company/techcorp-bg" title="TechCorp BG">TechCorp BG</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "Python Developer"
        assert job["url"] == "https://www.jobs.bg/en/job/python-developer-123"
        assert job["company"] == "TechCorp BG"

    def test_parses_multiple_cards(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/job-one" title="Job One">Job One</a>
                <a href="/company/co1" title="Company One">Company One</a>
            </div>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/job-two" title="Job Two">Job Two</a>
                <a href="/company/co2" title="Company Two">Company Two</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 2
        assert jobs[0]["title"] == "Job One"
        assert jobs[1]["title"] == "Job Two"

    def test_builds_full_url_from_relative_path(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/test-job" title="Test Job">Test Job</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        assert jobs[0]["url"] == "https://www.jobs.bg/en/job/test-job"

    def test_builds_full_url_from_slash_prefix(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/some/path" title="Slash Job">Slash Job</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        assert jobs[0]["url"] == "https://www.jobs.bg/some/path"

    def test_skips_cards_without_title(self):
        html = """
        <html>
            <div class="mdc-card">
                <a href="/company/nocompany" title="No Title Company">No Title Company</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 0

    def test_skips_cards_without_url(self):
        html = """
        <html>
            <div class="mdc-card">
                <span>No link here</span>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 0

    def test_empty_html_returns_empty_list(self):
        jobs = parse_jobsbg_cards("")
        assert jobs == []

    def test_html_without_cards_returns_empty_list(self):
        html = "<html><body><p>No jobs here</p></body></html>"
        jobs = parse_jobsbg_cards(html)
        assert jobs == []

    def test_fallback_company_from_secondary_text(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/fallback-job" title="Fallback Job">Fallback Job</a>
                <div class="secondary-text">Fallback Company Name</div>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Fallback Company Name"

    def test_removes_star_from_title(self):
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/starred-job" title="Senior Dev ★">Senior Dev star</a>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        assert "star" not in jobs[0]["title"].lower()

    def test_extracts_date_from_secondary_text(self):
        """Date is pulled from a secondary-text node matching dd.mm.yyyy, and the
        Ref.No suffix is stripped. Locks behavior across the bs4 text=->string= rename."""
        html = """
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/dated-job" title="Dated Job">Dated Job</a>
                <div class="secondary-text">Some Company</div>
                <div class="secondary-text">20.03.2026, Ref.No:Ps_1</div>
            </div>
        </html>
        """
        jobs = parse_jobsbg_cards(html)
        assert len(jobs) == 1
        assert jobs[0]["date"] == "20.03.2026"


class TestClassifyLocationSpans:
    """Pure location classifier (no browser). jobs.bg detail pages carry location
    only as bare <span>s identifiable by CONTENT (no class/itemprop hook):
    'Sofia', 'Fully remote work', 'Remote interview'. The classifier matches an
    allowlist of BG-town / remote phrases and skips known false-friends.

    Live DOM evidence (2026-06-29 sample, job 8514748 / 8514545 / 8514820).
    """

    def test_picks_known_bg_town(self):
        """A bare 'Sofia' span is a real work location."""
        assert classify_location_spans(["Sofia", "Remote interview"]) == "Sofia"

    def test_remote_interview_is_a_trap_not_a_location(self):
        """'Remote interview' describes the INTERVIEW, not the job — must be skipped.
        With only that span present, no usable location is found."""
        assert classify_location_spans(["Remote interview"]) == ""

    def test_fully_remote_work_is_a_real_location(self):
        """'Fully remote work' is a genuine work-location phrase."""
        assert classify_location_spans(["Fully remote work"]) == "Fully remote work"

    def test_town_wins_over_trailing_trap(self):
        """First allowlisted span wins; the 'Remote interview' trap after it is ignored."""
        assert classify_location_spans(["Plovdiv", "Remote interview", "Sofia"]) == "Plovdiv"

    def test_prose_paragraph_rejected(self):
        """A long description paragraph mentioning a town is NOT a location label."""
        prose = ("We are a Sofia-based fintech building payment rails across the EU "
                 "and we want you to join our growing engineering team today.")
        assert classify_location_spans([prose]) == ""

    def test_no_geo_span_returns_empty(self):
        """No geo-ish span -> '' so the gate falls back to its body keyword scan."""
        assert classify_location_spans(["Full time", "Permanent", "English"]) == ""

    def test_empty_input_returns_empty(self):
        assert classify_location_spans([]) == ""

    def test_company_jobs_link_is_not_a_location(self):
        """Live bug (309-run): the 'IT Jobs of <Company> EOOD' link text false-matched
        because the company's legal name contains a city/country token (Bulgaria,
        СОФИЯ, БЪЛГАРИЯ). These are company-jobs-list links, never the work location."""
        garbage = [
            "IT Jobs of Akkodis Bulgaria EOOD",
            "IT Jobs of ФЕСТО БЪЛГАРИЯ ЕООД",
            "IT Jobs of ПФК ЦСКА СОФИЯ ЕАД",
            "IT Jobs of Schwarz Digits Bulgaria EOOD",
            "IT Jobs of А1 България ЕАД",
        ]
        for g in garbage:
            assert classify_location_spans([g]) == "", f"should skip company link: {g!r}"

    def test_company_link_skipped_real_location_kept(self):
        """When the company-jobs link precedes the real location span, skip the link
        and return the genuine town (the live ordering that polluted ~17% of rows)."""
        spans = ["IT Jobs of Akkodis Bulgaria EOOD", "Sofia"]
        assert classify_location_spans(spans) == "Sofia"

    def test_office_address_span_still_kept(self):
        """A real office-address span (Sofia + street + 'view map') is a valid
        location and must survive — it is NOT a company name."""
        addr = "Sofia; ул. „Николай Хайтов“ № 3А (view map)"
        assert classify_location_spans([addr]) == addr

    def test_bare_company_entity_suffix_skipped(self):
        """A company-name span without the 'Jobs of' prefix but carrying an entity
        suffix (EOOD/ЕООД/ЕАД) is still not a location."""
        assert classify_location_spans(["Festo Bulgaria EOOD"]) == ""

    # --- Adversarial stress-test findings (60-case workflow, 2026-06-30) ---------
    # 6 false-positive classes the hand-picked tests missed. All return a non-location
    # as a location, polluting job['location']; "" is the safe outcome (body-scan
    # fallback). Each case below is a verbatim span the stress test surfaced.

    def test_entity_token_midstring_or_leading_skipped(self):
        """BUG 1/3: a legal-entity token NOT at end-of-string (mid or leading) must
        still mark the span a company, not a town. The old suffix regex only
        anchored to '$', so these leaked through on their embedded town token."""
        for span in [
            "Akkodis Bulgaria EOOD - Sofia office",  # EOOD mid-string
            "Adastra Bulgaria EOOD Sofia",           # EOOD mid-string
            "ЕАД Русе",                              # ЕАД leading
            "Ad Astra Bulgaria",                     # 'Ad' leading entity token
        ]:
            assert classify_location_spans([span]) == "", f"company not skipped: {span!r}"

    def test_gmbh_suffix_skipped(self):
        """BUG 2: 'GmbH' is a company suffix and must be skipped, not read as a
        Varna location."""
        assert classify_location_spans(["Varna Software GmbH"]) == ""

    def test_negated_remote_is_not_a_remote_location(self):
        """BUG 4: 'No remote work — strictly on-site' negates remote and states
        on-site; it must NOT be classified as a remote work location."""
        assert classify_location_spans(["No remote work — strictly on-site"]) == ""

    def test_negation_does_not_kill_a_real_town(self):
        """BUG 4 guard: the negation check blocks only the REMOTE branch — a short
        span naming a real town with a remote caveat is still a (town) location."""
        assert classify_location_spans(["Sofia (no remote)"]) == "Sofia (no remote)"

    def test_duty_and_product_prose_skipped(self):
        """BUG 5: job-duty / product-name prose where a geo token is incidental is
        not a location label ('Hybrid car parts', 'Varna Beach festival app',
        'remote support … in Sofia')."""
        for span in [
            "Hybrid car parts warehouse",
            "Опит с Varna Beach festival app",
            "Remote support for clients in Sofia",
            "Provide remote support to our Varna team",
            "You will relocate to Plovdiv",
        ]:
            assert classify_location_spans([span]) == "", f"prose not skipped: {span!r}"

    def test_town_token_matched_on_word_boundary(self):
        """BUG 6: 'ruse' must not match inside 'Ruseville' — town tokens are matched
        on word boundaries, not as bare substrings."""
        assert classify_location_spans(["Ruseville Software"]) == ""
