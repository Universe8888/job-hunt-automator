"""
LinkedIn Jobs Scraper — Profile Matcher
Extracts text from a PDF resume and scores job descriptions against weighted skills.
"""

import os
import re
import logging
from collections import defaultdict

from config import PROFILE_PDF, SKILL_WEIGHTS, MATCH_THRESHOLD

logger = logging.getLogger(__name__)


def extract_pdf_text(filepath: str) -> str:
    """Extract all text from a PDF file using PyPDF2."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(filepath)
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        full_text = "\n".join(text_parts)
        logger.info("📄 Extracted %d characters from %s (%d pages)", len(full_text), filepath, len(reader.pages))
        return full_text
    except ImportError:
        logger.warning("⚠️  PyPDF2 not installed. Skipping PDF extraction.")
        return ""
    except Exception as e:
        logger.error("❌ Failed to read PDF %s: %s", filepath, str(e))
        return ""


def extract_skills_from_profile(profile_text: str) -> dict[str, float]:
    """
    Combine hardcoded SKILL_WEIGHTS with any additional skills
    dynamically extracted from the profile text.
    """
    skills = dict(SKILL_WEIGHTS)

    # Dynamically extract additional certification/tech terms from profile
    # (This catches things the static dictionary might miss)
    cert_patterns = [
        r"(?:certified|certification)\s+(?:in\s+)?([A-Za-z0-9\s/\-]+)",
        r"(?:ISO\s*\d+)",
        r"(?:ITIL|PMP|CIPS|PRINCE2|Six Sigma|Lean)",
    ]
    profile_lower = profile_text.lower()
    for pattern in cert_patterns:
        matches = re.findall(pattern, profile_text, re.IGNORECASE)
        for match in matches:
            term = match.strip().lower()
            if len(term) > 2 and term not in skills:
                skills[term] = 0.6  # Medium weight for auto-discovered skills
                logger.debug("  🔎 Auto-discovered skill: '%s'", term)

    return skills


def load_skills(profile_pdf_path: str | None = None) -> dict[str, float]:
    """
    Load and build the complete skills dictionary from PDF + config.
    Call this ONCE at startup, then pass the result to match_jobs().

    Returns:
        dict mapping skill name -> weight
    """
    pdf_path = profile_pdf_path or PROFILE_PDF
    profile_text = ""

    if os.path.exists(pdf_path):
        logger.info("📄 Loading profile from: %s", pdf_path)
        profile_text = extract_pdf_text(pdf_path)
    else:
        logger.info("📄 No PDF found at '%s' — using hardcoded skill weights only.", pdf_path)

    if profile_text:
        skills = extract_skills_from_profile(profile_text)
        logger.info("🎯 Loaded %d skills for matching (including PDF-extracted)", len(skills))
    else:
        skills = dict(SKILL_WEIGHTS)
        logger.info("🎯 Loaded %d hardcoded skills for matching", len(skills))

    return skills


def score_job(description: str, skills: dict[str, float]) -> tuple[float, list[str]]:
    """
    Score a job description against the skill dictionary.

    Returns:
        (score, matched_skills): score is 0–100, matched_skills lists what matched.
    """
    if not description:
        return 0.0, []

    desc_lower = description.lower()
    matched = []
    total_weight = 0.0
    max_possible = sum(skills.values())

    for skill, weight in skills.items():
        # Use word-boundary matching to avoid partial matches
        # e.g. "sql" shouldn't match "result"
        pattern = r'\b' + re.escape(skill) + r'\b'
        if re.search(pattern, desc_lower):
            total_weight += weight
            matched.append(skill)

    if max_possible == 0:
        return 0.0, matched

    score = (total_weight / max_possible) * 100
    return round(score, 1), matched


def match_jobs(
    jobs: list[dict],
    profile_pdf_path: str | None = None,
    skills: dict[str, float] | None = None,
) -> list[dict]:
    """
    Run profile matching on all scraped jobs.
    Adds 'match_score', 'matched_skills', and 'match_flag' fields to each job dict.

    Args:
        jobs: list of job dicts
        profile_pdf_path: path to PDF (used only if skills is None — legacy path)
        skills: pre-built skills dict from load_skills(). Preferred.
    """
    # Use pre-built skills if provided; otherwise fall back to loading from PDF
    if skills is None:
        skills = load_skills(profile_pdf_path)

    # Score each job
    matched_count = 0
    for job in jobs:
        desc = job.get("description", "")
        title = job.get("title", "")
        # Combine title + description for matching
        combined_text = f"{title} {desc}"

        score, matched_skills = score_job(combined_text, skills)
        job["match_score"] = score
        job["matched_skills"] = matched_skills

        if score >= MATCH_THRESHOLD:
            job["match_flag"] = f"✅ Good Match ({score}%)"
            matched_count += 1
        else:
            job["match_flag"] = f"— ({score}%)"

    logger.info("🎯 Profile matching complete: %d/%d jobs flagged as good matches (threshold: %d%%)",
                matched_count, len(jobs), MATCH_THRESHOLD)

    return jobs
