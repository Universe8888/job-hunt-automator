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
