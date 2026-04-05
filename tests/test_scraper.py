"""Tests for scraper.py — URL building, HTML parsing, content sanitization."""

import pytest

from scraper import (
    build_api_url,
    build_search_url,
    is_valid_job_url,
    is_modal_garbage,
    parse_job_cards_from_html,
)


SAMPLE_LOCATION = {
    "name": "Plovdiv, Bulgaria",
    "geoId": "104642436",
    "location_text": "Plovdiv",
}


class TestBuildApiUrl:
    def test_basic_url(self):
        url = build_api_url("Python Developer", SAMPLE_LOCATION)
        assert "linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search" in url
        assert "keywords=Python+Developer" in url
        assert "geoId=104642436" in url
        assert "location=Plovdiv" in url

    def test_start_parameter(self):
        url = build_api_url("Python Developer", SAMPLE_LOCATION, start=25)
        assert "start=25" in url

    def test_date_filter(self):
        url = build_api_url("Python Developer", SAMPLE_LOCATION, date_filter="r604800")
        assert "f_TPR=r604800" in url

    def test_experience_filter(self):
        url = build_api_url("Python Developer", SAMPLE_LOCATION, experience_filters=["3", "4"])
        assert "f_E=3%2C4" in url or "f_E=3,4" in url

    def test_work_type_filter_present(self):
        url = build_api_url("Python Developer", SAMPLE_LOCATION)
        assert "f_WT=" in url


class TestBuildSearchUrl:
    def test_basic_url(self):
        url = build_search_url("Python Developer", SAMPLE_LOCATION)
        assert "linkedin.com/jobs/search/" in url
        assert "keywords=Python+Developer" in url
        assert "geoId=104642436" in url

    def test_date_filter(self):
        url = build_search_url("Python Developer", SAMPLE_LOCATION, date_filter="r86400")
        assert "f_TPR=r86400" in url

    def test_experience_filter(self):
        url = build_search_url("Python Developer", SAMPLE_LOCATION, experience_filters=["3"])
        assert "f_E=3" in url


class TestIsValidJobUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.linkedin.com/jobs/view/procurement-manager-at-asos-4378190960", True),
            ("https://uk.linkedin.com/jobs/view/senior-lead-at-vermelo-4384072189", True),
            ("https://fr.linkedin.com/jobs/view/test-job-123", True),
            ("https://www.linkedin.com/company/deloitte", False),
            ("https://www.linkedin.com/in/john-doe", False),
            ("https://fr.linkedin.com/company/sia-partners", False),
            ("", False),
            ("https://google.com", False),
            (None, False),
        ],
    )
    def test_url_validation(self, url, expected):
        assert is_valid_job_url(url) == expected


class TestIsModalGarbage:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Join LinkedIn\nEmail\nPassword (6+ characters)", True),
            ("Agree & Join", True),
            ("Already on Linkedin? Sign in to view full description.", True),
            ("Get it in the Microsoft Store", True),
            ("LinkedIn is better on the app", True),
            ("We are looking for a Senior Developer with Python skills.", False),
            ("", False),
            (None, False),
        ],
    )
    def test_modal_garbage_detection(self, text, expected):
        assert is_modal_garbage(text) == expected


class TestParseJobCardsFromHtml:
    def test_parses_linkedin_job_cards(self):
        html = """
        <html>
            <div class="base-card base-search-card">
                <h3 class="base-search-card__title">Software Engineer</h3>
                <h4 class="base-search-card__subtitle">
                    <a class="hidden-nested-link" href="/company/techcorp">TechCorp</a>
                </h4>
                <span class="job-search-card__location">Sofia, Bulgaria</span>
                <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/123456">Apply</a>
                <time datetime="2026-01-15">2 days ago</time>
                <span class="job-search-card__salary-info">$80k - $120k</span>
            </div>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "Software Engineer"
        assert job["company"] == "TechCorp"
        assert job["location"] == "Sofia, Bulgaria"
        assert job["url"] == "https://www.linkedin.com/jobs/view/123456"
        assert job["date"] == "2026-01-15"
        assert job["salary"] == "$80k - $120k"

    def test_parses_multiple_cards(self):
        html = """
        <html>
            <div class="base-card">
                <h3 class="base-search-card__title">Job One</h3>
                <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1">Link</a>
            </div>
            <div class="base-card">
                <h3 class="base-search-card__title">Job Two</h3>
                <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/2">Link</a>
            </div>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 2
        assert jobs[0]["title"] == "Job One"
        assert jobs[1]["title"] == "Job Two"

    def test_skips_cards_without_title(self):
        html = """
        <html>
            <div class="base-card">
                <span class="job-search-card__location">Sofia</span>
            </div>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 0

    def test_skips_non_job_urls(self):
        html = """
        <html>
            <div class="base-card">
                <h3 class="base-search-card__title">Some Job</h3>
                <a class="base-card__full-link" href="https://www.linkedin.com/company/foobar">Company</a>
            </div>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 1
        assert "url" not in jobs[0]

    def test_cleans_tracking_params_from_url(self):
        html = """
        <html>
            <div class="base-card">
                <h3 class="base-search-card__title">Clean URL Job</h3>
                <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/999?refId=abc123&trk=xyz">Link</a>
            </div>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 1
        assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/999"

    def test_empty_html_returns_empty_list(self):
        jobs = parse_job_cards_from_html("")
        assert jobs == []

    def test_html_without_cards_returns_empty_list(self):
        html = "<html><body><p>No jobs here</p></body></html>"
        jobs = parse_job_cards_from_html(html)
        assert jobs == []

    def test_fallback_to_li_elements(self):
        html = """
        <html>
            <li>
                <h3 class="base-search-card__title">Fallback Job</h3>
                <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/777">Link</a>
            </li>
        </html>
        """
        jobs = parse_job_cards_from_html(html)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Fallback Job"
