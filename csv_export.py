"""
Job Hunt Automator — CSV Export (3-gate filter)
Writes evaluated jobs to two CSVs with deduplication and a UTF-8 BOM for Excel:
  • leads CSV   — verdict in {keep, manual_review}  (export_to_csv)
  • rejects CSV — verdict == reject                 (export_rejects_csv)
Nothing is silently dropped: every job lands in exactly one of the two files.
The legacy Match Score / Matched Skills / Match Flag columns are retained as
info-only fields (the 3 gates decide the verdict, not the skill score).
"""

import csv
import os
import logging

from config import OUTPUT_CSV

# LEADS_CSV / REJECTS_CSV are added to config by the 3-gate rebuild. Import them
# defensively so this module keeps working against an older config that only
# defines OUTPUT_CSV (rejects path is then derived from the leads path by suffix).
try:
    from config import LEADS_CSV
except ImportError:
    LEADS_CSV = OUTPUT_CSV

try:
    from config import REJECTS_CSV
except ImportError:
    _root, _ext = os.path.splitext(LEADS_CSV)
    REJECTS_CSV = _root + "-rejects" + (_ext or ".csv")

logger = logging.getLogger(__name__)

# Legacy column set (v3.0). Kept for backward compatibility with callers/tests
# that still reference it; the live exporters use LEADS_COLUMNS / REJECTS_COLUMNS.
CSV_COLUMNS = [
    "Job Title",
    "Company Name",
    "Location",
    "Posting Date",
    "Salary Info",
    "Seniority",
    "Employment Type",
    "Applicants",
    "Description",
    "Job URL",
    "Search Keyword",
    "Search Location",
    "Match Score",
    "Matched Skills",
    "Match Flag",
]

# Leads CSV (keep + manual_review) — spec §5.1.
LEADS_COLUMNS = [
    "Verdict",
    "Rank",
    "Job Title",
    "Company Name",
    "Location",
    "Posting Date",
    "Salary Info",
    "Parsed Comp (EUR/yr gross)",
    "Gate1 Lane",
    "Gate2 Geo",
    "Gate3 Ceiling",
    "Gate Reasons",
    "Seniority",
    "Employment Type",
    "Applicants",
    "Description",
    "Job URL",
    "Search Keyword",
    "Search Location",
    "Match Score",
    "Matched Skills",
    "Match Flag",
]

# Rejects CSV (verdict == reject) — spec §5.2.
REJECTS_COLUMNS = [
    "Failing Gates",
    "Reject Reasons",
    "Rank",
    "Job Title",
    "Company Name",
    "Location",
    "Posting Date",
    "Salary Info",
    "Parsed Comp (EUR/yr gross)",
    "Gate1 Lane",
    "Gate2 Geo",
    "Gate3 Ceiling",
    "Description",
    "Job URL",
    "Search Keyword",
    "Search Location",
    "Match Score",
    "Matched Skills",
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


def _has_valid_header(filepath: str, expected_first_col: str) -> bool:
    """Check if the CSV file starts with the expected header column."""
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            first_line = f.readline().strip()
        return first_line.startswith(expected_first_col)
    except Exception:
        return False


def _format_skills(job: dict) -> str:
    """Format matched skills (list or str) as a readable comma-joined string."""
    matched_skills = job.get("matched_skills", [])
    if isinstance(matched_skills, list):
        return ", ".join(matched_skills)
    return str(matched_skills)


def _failing_gates(job: dict) -> str:
    """Comma-joined names of gates whose status == 'hard_fail' (spec §5.2)."""
    gate_names = (
        ("lane", job.get("gate1_status", "")),
        ("geo", job.get("gate2_status", "")),
        ("ceiling", job.get("gate3_status", "")),
    )
    return ", ".join(name for name, status in gate_names if status == "hard_fail")


def _new_jobs_by_url(jobs: list[dict], existing_urls: set[str]) -> list[dict]:
    """Filter to jobs with a URL not already seen, updating the seen set in place."""
    new_jobs = []
    for job in jobs:
        url = job.get("url", "").strip()
        if url and url not in existing_urls:
            new_jobs.append(job)
            existing_urls.add(url)
    return new_jobs


def _append_rows(
    filepath: str,
    columns: list[str],
    new_jobs: list[dict],
    build_row,
) -> int:
    """
    Shared writer: append rows for ``new_jobs`` to ``filepath`` using ``columns``.
    - Uses UTF-8 with BOM for Excel compatibility.
    - Writes the header only when starting a fresh file.
    - ``extrasaction="ignore"`` tolerates extra keys on the row dicts.
    Returns the number of rows written.
    """
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    mode = "a" if file_exists else "w"
    with open(filepath, mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for job in new_jobs:
            writer.writerow(build_row(job))
    return len(new_jobs)


def export_to_csv(jobs: list[dict], filepath: str | None = None,
                  sort_after: bool = True) -> int:
    """
    Write LEADS (verdict in {keep, manual_review}) to the leads CSV.
    - Appends to existing file (deduplicates by Job URL).
    - Uses UTF-8 with BOM for Excel compatibility.
    - Validates header integrity before appending (regenerates a stale header).
    - When ``sort_after`` is True, sorts the file by Rank descending as a final pass.

    Pass ``sort_after=False`` for incremental per-row saving in a loop (the sort is
    O(file) read+rewrite, so sorting on every row is O(n²) over a run); call
    ``sort_leads_csv()`` ONCE after the loop instead.
    Returns the count of newly written rows.
    """
    filepath = filepath or LEADS_CSV

    # If file exists but has no valid header, start fresh.
    if (
        os.path.exists(filepath)
        and os.path.getsize(filepath) > 0
        and not _has_valid_header(filepath, LEADS_COLUMNS[0])
    ):
        logger.warning("⚠️  %s has no valid header — starting fresh.", filepath)
        os.remove(filepath)

    existing_urls = load_existing_urls(filepath)
    new_jobs = _new_jobs_by_url(jobs, existing_urls)

    if not new_jobs:
        logger.info("📝 No new leads to write (all duplicates).")
        return 0

    def build_row(job: dict) -> dict:
        return {
            "Verdict": job.get("verdict", ""),
            "Rank": job.get("rank", ""),
            "Job Title": job.get("title", ""),
            "Company Name": job.get("company", ""),
            "Location": job.get("location", ""),
            "Posting Date": job.get("date", ""),
            "Salary Info": job.get("salary", ""),
            "Parsed Comp (EUR/yr gross)": job.get("parsed_comp_eur", ""),
            "Gate1 Lane": job.get("gate1_status", ""),
            "Gate2 Geo": job.get("gate2_status", ""),
            "Gate3 Ceiling": job.get("gate3_status", ""),
            "Gate Reasons": job.get("gate_reasons", ""),
            "Seniority": job.get("seniority", ""),
            "Employment Type": job.get("employment_type", ""),
            "Applicants": job.get("applicants_count", ""),
            "Description": _truncate_description(job.get("description", "")),
            "Job URL": job.get("url", ""),
            "Search Keyword": job.get("search_keyword", ""),
            "Search Location": job.get("search_location", ""),
            "Match Score": job.get("match_score", ""),
            "Matched Skills": _format_skills(job),
            "Match Flag": job.get("match_flag", ""),
        }

    count = _append_rows(filepath, LEADS_COLUMNS, new_jobs, build_row)
    logger.info(
        "📝 Wrote %d new leads to %s (total tracked: %d)",
        count,
        filepath,
        len(existing_urls),
    )

    # Final pass: keep apply-first roles on top (skip during incremental saving).
    if sort_after:
        _sort_csv_by_rank_desc(filepath, LEADS_COLUMNS)

    return count


def sort_leads_csv(filepath: str | None = None) -> None:
    """Sort the leads CSV by Rank descending. Call ONCE after incremental saving
    (when export_to_csv was used with sort_after=False)."""
    _sort_csv_by_rank_desc(filepath or LEADS_CSV, LEADS_COLUMNS)


def export_rejects_csv(jobs: list[dict], filepath: str | None = None) -> int:
    """
    Write REJECTS (verdict == reject) to the rejects audit CSV.
    Same dedup-by-URL + UTF-8 BOM + header-validation contract as export_to_csv.
    Records the failing gate(s) and their reasons so a false-reject can be spotted
    on review. Returns the count of newly written rows.
    """
    filepath = filepath or REJECTS_CSV

    # If file exists but has no valid header, start fresh.
    if (
        os.path.exists(filepath)
        and os.path.getsize(filepath) > 0
        and not _has_valid_header(filepath, REJECTS_COLUMNS[0])
    ):
        logger.warning("⚠️  %s has no valid header — starting fresh.", filepath)
        os.remove(filepath)

    existing_urls = load_existing_urls(filepath)
    new_jobs = _new_jobs_by_url(jobs, existing_urls)

    if not new_jobs:
        logger.info("📝 No new rejects to write (all duplicates).")
        return 0

    def build_row(job: dict) -> dict:
        return {
            "Failing Gates": _failing_gates(job),
            "Reject Reasons": job.get("gate_reasons", ""),
            "Rank": job.get("rank", ""),
            "Job Title": job.get("title", ""),
            "Company Name": job.get("company", ""),
            "Location": job.get("location", ""),
            "Posting Date": job.get("date", ""),
            "Salary Info": job.get("salary", ""),
            "Parsed Comp (EUR/yr gross)": job.get("parsed_comp_eur", ""),
            "Gate1 Lane": job.get("gate1_status", ""),
            "Gate2 Geo": job.get("gate2_status", ""),
            "Gate3 Ceiling": job.get("gate3_status", ""),
            "Description": _truncate_description(job.get("description", "")),
            "Job URL": job.get("url", ""),
            "Search Keyword": job.get("search_keyword", ""),
            "Search Location": job.get("search_location", ""),
            "Match Score": job.get("match_score", ""),
            "Matched Skills": _format_skills(job),
        }

    count = _append_rows(filepath, REJECTS_COLUMNS, new_jobs, build_row)
    logger.info(
        "📝 Wrote %d new rejects to %s (total tracked: %d)",
        count,
        filepath,
        len(existing_urls),
    )
    return count


def _sort_csv_by_rank_desc(filepath: str, columns: list[str]) -> None:
    """
    Final pass: rewrite ``filepath`` with rows sorted by the Rank column,
    descending, so apply-first leads sit on top. Header order is preserved.
    Rows with a missing/non-numeric Rank sort last. Best-effort: a failure here
    never loses already-written data (the unsorted file is left intact).
    """
    if "Rank" not in columns or not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        def rank_key(row: dict) -> float:
            try:
                return float(row.get("Rank", "") or "")
            except (TypeError, ValueError):
                return float("-inf")

        rows.sort(key=rank_key, reverse=True)

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    except Exception as e:
        logger.warning("⚠️  Failed to sort %s by Rank: %s", filepath, str(e))


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
