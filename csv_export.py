"""
LinkedIn Jobs Scraper — CSV Export (v3.0)
Writes scraped jobs to a CSV with deduplication and UTF-8 BOM for Excel.
Now includes Matched Skills and Salary Info columns.
"""

import csv
import os
import logging

from config import OUTPUT_CSV

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "Job Title",
    "Company Name",
    "Location",
    "Posting Date",
    "Salary Info",
    "Description",
    "Job URL",
    "Search Keyword",
    "Search Location",
    "Match Score",
    "Matched Skills",
    "Match Flag",
]


def load_existing_urls(filepath: str) -> set[str]:
    """Load existing job URLs from CSV for deduplication."""
    urls = set()
    if not os.path.exists(filepath):
        return urls
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = row.get("Job URL", "").strip()
                if url:
                    urls.add(url)
    except Exception as e:
        logger.warning("⚠️  Failed to load existing URLs from %s: %s", filepath, str(e))
    return urls


def export_to_csv(jobs: list[dict], filepath: str | None = None):
    """
    Write job data to CSV.
    - Appends to existing file (deduplicates by URL).
    - Uses UTF-8 with BOM for Excel compatibility.
    """
    filepath = filepath or OUTPUT_CSV
    existing_urls = load_existing_urls(filepath)
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0

    new_jobs = []
    for job in jobs:
        url = job.get("url", "").strip()
        if url and url not in existing_urls:
            new_jobs.append(job)
            existing_urls.add(url)

    if not new_jobs:
        logger.info("📝 No new jobs to write (all duplicates).")
        return 0

    mode = "a" if file_exists else "w"
    with open(filepath, mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")

        if not file_exists:
            writer.writeheader()

        for job in new_jobs:
            # Format matched skills as a readable list
            matched_skills = job.get("matched_skills", [])
            skills_str = ", ".join(matched_skills) if isinstance(matched_skills, list) else str(matched_skills)

            row = {
                "Job Title": job.get("title", ""),
                "Company Name": job.get("company", ""),
                "Location": job.get("location", ""),
                "Posting Date": job.get("date", ""),
                "Salary Info": job.get("salary", ""),
                "Description": _truncate_description(job.get("description", "")),
                "Job URL": job.get("url", ""),
                "Search Keyword": job.get("search_keyword", ""),
                "Search Location": job.get("search_location", ""),
                "Match Score": job.get("match_score", ""),
                "Matched Skills": skills_str,
                "Match Flag": job.get("match_flag", ""),
            }
            writer.writerow(row)

    logger.info("📝 Wrote %d new jobs to %s (total tracked: %d)", len(new_jobs), filepath, len(existing_urls))
    return len(new_jobs)


def _truncate_description(text: str, max_len: int = 5000) -> str:
    """Truncate long descriptions at a sentence boundary for readability."""
    if not text or len(text) <= max_len:
        return text

    # Try to cut at a sentence boundary near max_len
    truncated = text[:max_len]
    # Look for the last period followed by a space or end of string
    last_period = truncated.rfind(". ")
    if last_period > max_len * 0.7:  # Only if we don't lose too much
        return truncated[:last_period + 1] + " …[truncated]"

    return truncated + "…[truncated]"
