"""Tests for csv_export.py — deduplication, truncation, BOM encoding."""

import csv
import os
import tempfile

import pytest

from csv_export import export_to_csv, load_existing_urls, _truncate_description, CSV_COLUMNS


@pytest.fixture
def temp_csv_path():
    """Provide a temporary CSV file path and clean it up after the test."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


class TestTruncateDescription:
    def test_short_description_unchanged(self):
        text = "A short description."
        assert _truncate_description(text) == text

    def test_empty_description(self):
        assert _truncate_description("") == ""

    def test_long_description_truncated(self):
        text = "Sentence. " * 600
        result = _truncate_description(text)
        assert len(result) <= 5100
        assert "[truncated]" in result

    def test_truncation_at_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence. " + "More. " * 2000
        result = _truncate_description(text)
        assert "[truncated]" in result


class TestLoadExistingUrls:
    def test_nonexistent_file(self):
        urls = load_existing_urls("nonexistent_file.csv")
        assert urls == set()

    def test_loads_urls_from_csv(self, temp_csv_path):
        with open(temp_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerow({
                "Job Title": "Test",
                "Company Name": "TestCo",
                "Location": "Sofia",
                "Posting Date": "2026-01-01",
                "Salary Info": "",
                "Description": "Test desc",
                "Job URL": "https://www.linkedin.com/jobs/view/123",
                "Search Keyword": "Python",
                "Search Location": "Bulgaria",
                "Match Score": "50",
                "Matched Skills": "python",
                "Match Flag": "Good Match",
            })

        urls = load_existing_urls(temp_csv_path)
        assert "https://www.linkedin.com/jobs/view/123" in urls

    def test_skips_empty_urls(self, temp_csv_path):
        with open(temp_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerow({
                "Job Title": "Test",
                "Company Name": "TestCo",
                "Location": "Sofia",
                "Posting Date": "2026-01-01",
                "Salary Info": "",
                "Description": "Test desc",
                "Job URL": "",
                "Search Keyword": "Python",
                "Search Location": "Bulgaria",
                "Match Score": "50",
                "Matched Skills": "python",
                "Match Flag": "Good Match",
            })

        urls = load_existing_urls(temp_csv_path)
        assert len(urls) == 0

    def test_corrupt_csv_returns_empty_set(self, temp_csv_path):
        with open(temp_csv_path, "w", encoding="utf-8") as f:
            f.write("this is not valid csv content\x00\x01\x02")

        urls = load_existing_urls(temp_csv_path)
        assert urls == set()


class TestExportToCsv:
    def test_writes_new_file_with_header(self, temp_csv_path):
        jobs = [
            {
                "title": "Software Engineer",
                "company": "TestCorp",
                "location": "Sofia",
                "date": "2026-01-01",
                "salary": "Competitive",
                "description": "We need a Python dev.",
                "url": "https://www.linkedin.com/jobs/view/001",
                "search_keyword": "Python",
                "search_location": "Bulgaria",
                "match_score": 75.0,
                "matched_skills": ["python", "sql"],
                "match_flag": "Good Match (75.0%)",
            }
        ]

        count = export_to_csv(jobs, temp_csv_path)
        assert count == 1
        assert os.path.exists(temp_csv_path)

        with open(temp_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 1
            assert rows[0]["Job Title"] == "Software Engineer"
            assert rows[0]["Job URL"] == "https://www.linkedin.com/jobs/view/001"

    def test_deduplicates_by_url(self, temp_csv_path):
        job = {
            "title": "Software Engineer",
            "company": "TestCorp",
            "location": "Sofia",
            "date": "2026-01-01",
            "salary": "",
            "description": "We need a Python dev.",
            "url": "https://www.linkedin.com/jobs/view/001",
            "search_keyword": "Python",
            "search_location": "Bulgaria",
            "match_score": 75.0,
            "matched_skills": ["python"],
            "match_flag": "Good Match",
        }

        export_to_csv([job], temp_csv_path)
        count = export_to_csv([job], temp_csv_path)
        assert count == 0

    def test_appends_to_existing_file(self, temp_csv_path):
        job1 = {
            "title": "Job One",
            "company": "Co1",
            "location": "Sofia",
            "date": "2026-01-01",
            "salary": "",
            "description": "Desc 1",
            "url": "https://www.linkedin.com/jobs/view/001",
            "search_keyword": "Python",
            "search_location": "Bulgaria",
            "match_score": 50.0,
            "matched_skills": [],
            "match_flag": "— (50.0%)",
        }
        job2 = {
            "title": "Job Two",
            "company": "Co2",
            "location": "Plovdiv",
            "date": "2026-01-02",
            "salary": "",
            "description": "Desc 2",
            "url": "https://www.linkedin.com/jobs/view/002",
            "search_keyword": "Python",
            "search_location": "Bulgaria",
            "match_score": 60.0,
            "matched_skills": ["python"],
            "match_flag": "Good Match",
        }

        export_to_csv([job1], temp_csv_path)
        count = export_to_csv([job2], temp_csv_path)
        assert count == 1

        with open(temp_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 2

    def test_writes_no_jobs_returns_zero(self, temp_csv_path):
        count = export_to_csv([], temp_csv_path)
        assert count == 0

    def test_utf8_bom_encoding(self, temp_csv_path):
        job = {
            "title": "Инженер",
            "company": "Тест ООД",
            "location": "София",
            "date": "2026-01-01",
            "salary": "",
            "description": "Търсим разработчик.",
            "url": "https://www.linkedin.com/jobs/view/003",
            "search_keyword": "Python",
            "search_location": "България",
            "match_score": 50.0,
            "matched_skills": [],
            "match_flag": "— (50.0%)",
        }

        export_to_csv([job], temp_csv_path)

        with open(temp_csv_path, "rb") as f:
            raw = f.read()
            assert raw.startswith(b"\xef\xbb\xbf")

    def test_truncates_long_descriptions(self, temp_csv_path):
        long_desc = "Word. " * 2000
        job = {
            "title": "Long Desc Job",
            "company": "Co",
            "location": "Sofia",
            "date": "2026-01-01",
            "salary": "",
            "description": long_desc,
            "url": "https://www.linkedin.com/jobs/view/004",
            "search_keyword": "Python",
            "search_location": "Bulgaria",
            "match_score": 50.0,
            "matched_skills": [],
            "match_flag": "— (50.0%)",
        }

        export_to_csv([job], temp_csv_path)

        with open(temp_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            row = list(reader)[0]
            assert len(row["Description"]) <= 5100

    def test_matched_skills_formatted_as_string(self, temp_csv_path):
        job = {
            "title": "Skills Job",
            "company": "Co",
            "location": "Sofia",
            "date": "2026-01-01",
            "salary": "",
            "description": "Desc",
            "url": "https://www.linkedin.com/jobs/view/005",
            "search_keyword": "Python",
            "search_location": "Bulgaria",
            "match_score": 70.0,
            "matched_skills": ["python", "sql", "docker"],
            "match_flag": "Good Match",
        }

        export_to_csv([job], temp_csv_path)

        with open(temp_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            row = list(reader)[0]
            assert row["Matched Skills"] == "python, sql, docker"


class TestCsvColumns:
    def test_all_expected_columns_present(self):
        expected = [
            "Job Title", "Company Name", "Location", "Posting Date",
            "Salary Info", "Description", "Job URL", "Search Keyword",
            "Search Location", "Match Score", "Matched Skills", "Match Flag",
        ]
        for col in expected:
            assert col in CSV_COLUMNS

    def test_column_count(self):
        assert len(CSV_COLUMNS) == 12
