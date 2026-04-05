"""
Import and basic functionality tests for LinkedIn Scraper v3.0.
Converted to proper pytest format for CI pipeline integration.
Run with: pytest tests/test_imports_and_config.py
"""

import pytest

from config import (
    KEYWORDS, LOCATIONS, SKILL_WEIGHTS, OUTPUT_CSV, PROFILE_PDF,
    SIGN_IN_MODAL_MARKERS, DATE_POSTED_FILTER, EXPERIENCE_LEVEL_FILTERS,
    PROXY_URL, MAX_JOBS_PER_RUN, HEADLESS_DEFAULT,
)
from stealth_config import get_launch_options, get_context_options, get_random_user_agent
from csv_export import CSV_COLUMNS
from profile_matcher import match_jobs, load_skills, score_job
from scraper import build_api_url, is_valid_job_url, is_modal_garbage


class TestImports:
    def test_config_imports(self):
        assert KEYWORDS is not None
        assert LOCATIONS is not None
        assert SKILL_WEIGHTS is not None

    def test_stealth_config_imports(self):
        assert callable(get_launch_options)
        assert callable(get_context_options)
        assert callable(get_random_user_agent)

    def test_csv_export_imports(self):
        assert CSV_COLUMNS is not None

    def test_profile_matcher_imports(self):
        assert callable(match_jobs)
        assert callable(load_skills)
        assert callable(score_job)

    def test_scraper_imports(self):
        assert callable(build_api_url)
        assert callable(is_valid_job_url)
        assert callable(is_modal_garbage)


class TestConfig:
    def test_keywords_not_empty(self):
        assert len(KEYWORDS) > 0, "KEYWORDS list is empty"

    def test_locations_not_empty(self):
        assert len(LOCATIONS) > 0, "LOCATIONS list is empty"

    def test_skill_weights_not_empty(self):
        assert len(SKILL_WEIGHTS) > 0, "SKILL_WEIGHTS dictionary is empty"

    def test_output_csv_is_csv_file(self):
        assert OUTPUT_CSV.endswith('.csv'), "OUTPUT_CSV should be a CSV file"

    def test_modal_markers_not_empty(self):
        assert len(SIGN_IN_MODAL_MARKERS) > 0, "SIGN_IN_MODAL_MARKERS should not be empty"

    def test_location_structure(self):
        for loc in LOCATIONS:
            assert "name" in loc
            assert "geoId" in loc
            assert "location_text" in loc


class TestUrlBuilding:
    def test_basic_api_url(self):
        url = build_api_url(KEYWORDS[0], LOCATIONS[0])
        assert "linkedin.com" in url
        assert "jobs" in url

    def test_api_url_with_date_filter(self):
        url = build_api_url(KEYWORDS[0], LOCATIONS[0], date_filter="r604800")
        assert "f_TPR=r604800" in url

    def test_api_url_with_experience_filter(self):
        url = build_api_url(KEYWORDS[0], LOCATIONS[0], experience_filters=["3", "4"])
        assert "f_E=3%2C4" in url or "f_E=3,4" in url


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.linkedin.com/jobs/view/test-job-123", True),
            ("https://uk.linkedin.com/jobs/view/test-job-456", True),
            ("https://www.linkedin.com/company/deloitte", False),
            ("https://www.linkedin.com/in/john-doe", False),
            ("", False),
            ("https://google.com", False),
        ],
    )
    def test_url_validation_cases(self, url, expected):
        assert is_valid_job_url(url) == expected


class TestContentValidation:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Join LinkedIn\nEmail\nPassword", True),
            ("Agree & Join", True),
            ("We need a Python developer with 5 years experience.", False),
            ("", False),
        ],
    )
    def test_modal_garbage_detection(self, text, expected):
        assert is_modal_garbage(text) == expected


class TestStealthConfig:
    def test_headless_mode(self):
        headless_options = get_launch_options(headless=True)
        assert headless_options["headless"] is True
        assert "--disable-gpu" in headless_options["args"]

    def test_headed_mode(self):
        headed_options = get_launch_options(headless=False)
        assert headed_options["headless"] is False

    def test_user_agent_is_modern(self):
        user_agent = get_random_user_agent()
        assert isinstance(user_agent, str)
        assert len(user_agent) > 10
        assert (
            "Chrome/131" in user_agent or "Chrome/132" in user_agent or
            "Firefox/133" in user_agent or "Safari/605" in user_agent or
            "Edg/131" in user_agent
        ), f"User agent should be a 2026-era browser: {user_agent}"

    def test_context_options_has_required_keys(self):
        ua = get_random_user_agent()
        context_options = get_context_options(ua)
        assert "user_agent" in context_options
        assert "viewport" in context_options
        assert "locale" in context_options


class TestCsvHeaders:
    def test_all_expected_headers_present(self):
        expected_headers = [
            "Job Title", "Company Name", "Location", "Posting Date",
            "Salary Info", "Description", "Job URL", "Search Keyword",
            "Search Location", "Match Score", "Matched Skills", "Match Flag",
        ]
        for header in expected_headers:
            assert header in CSV_COLUMNS, f"Missing header: {header}"

    def test_header_count(self):
        assert len(CSV_COLUMNS) == 12


class TestProfileMatcherStructure:
    def test_load_skills_with_missing_pdf(self):
        skills = load_skills("nonexistent.pdf")
        assert isinstance(skills, dict)
        assert len(skills) > 0

    def test_score_job_returns_valid_range(self):
        skills = load_skills("nonexistent.pdf")
        score, matched = score_job("We need SQL and Python expertise for data analysis", skills)
        assert 0 <= score <= 100
        assert "sql" in matched
        assert "python" in matched

    def test_match_jobs_adds_required_fields(self):
        skills = load_skills("nonexistent.pdf")
        sample_jobs = [
            {
                "title": "Test Job",
                "company": "Test Company",
                "location": "Test Location",
                "description": "Test description with SQL and Python keywords",
                "url": "https://example.com",
                "search_keyword": "Test Keyword",
                "search_location": "Test Location",
            }
        ]
        matched_jobs = match_jobs(sample_jobs, skills=skills)
        assert len(matched_jobs) == 1
        assert "match_score" in matched_jobs[0]
        assert "matched_skills" in matched_jobs[0]
        assert isinstance(matched_jobs[0]["matched_skills"], list)
