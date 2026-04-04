import pytest
from profile_matcher import extract_skills_from_profile, score_job, match_jobs
import config

# We need to temporarily force config.SKILL_WEIGHTS and config.MATCH_THRESHOLD
# to a known state for unit testing so that changes in config.py don't break tests.
MOCK_SKILL_WEIGHTS = {
    "python": 1.0,
    "sql": 1.0,
    "docker": 0.8,
    "kubernetes": 0.5,
    "agile": 0.5
}
# Total max possible score = 3.8

@pytest.fixture
def override_config(monkeypatch):
    """Override the global config variables to ensure consistent testing."""
    import profile_matcher
    monkeypatch.setattr(profile_matcher, "SKILL_WEIGHTS", MOCK_SKILL_WEIGHTS)
    monkeypatch.setattr(profile_matcher, "MATCH_THRESHOLD", 50)


def test_extract_skills_from_profile_adds_certifications(override_config):
    """Test that dynamic extraction pulls out certifications from text like a CV."""
    cv_text = "John Doe is a Software Engineer certified in ITIL and ISO 27001. Experienced with standard methodologies."
    
    # Run extraction
    skills = extract_skills_from_profile(cv_text)
    
    # Check that base skills are retained
    assert "python" in skills
    assert skills["python"] == 1.0
    
    # Check that new dynamic skills were added (from regexes)
    # The regex catches 'ITIL' and 'ISO 27001'
    assert "itil" in skills
    assert "iso 27001" in skills
    assert skills["itil"] == 0.6
    assert skills["iso 27001"] == 0.6


def test_score_job_perfect_match(override_config):
    """Test that a job description with all skills yields a 100% score."""
    job_desc = "We need an engineer who knows Python, SQL, Docker, Kubernetes, and Agile methodology."
    
    score, matched = score_job(job_desc, MOCK_SKILL_WEIGHTS)
    
    assert score == 100.0
    assert len(matched) == 5
    assert set(matched) == set(["python", "sql", "docker", "kubernetes", "agile"])


def test_score_job_partial_match(override_config):
    """Test that a job description with some skills yields a proportional score."""
    # Only "python" (1.0) and "sql" (1.0) are present out of 3.8 max.
    job_desc = "Looking for a backend dev with Python and basic SQL."
    
    score, matched = score_job(job_desc, MOCK_SKILL_WEIGHTS)
    
    expected_score = round((2.0 / 3.8) * 100, 1)
    assert score == expected_score
    assert set(matched) == set(["python", "sql"])


def test_score_job_no_match(override_config):
    """Test that an irrelevant job description yields 0%."""
    job_desc = "Looking for a marketing specialist fluent in SEO and content writing."
    
    score, matched = score_job(job_desc, MOCK_SKILL_WEIGHTS)
    
    assert score == 0.0
    assert len(matched) == 0


def test_match_jobs_threshold(override_config, monkeypatch):
    """Test that match_jobs correctly flags jobs passing the threshold."""
    # Mock load_skills to just return our mock weights so we don't try to parse an actual PDF
    monkeypatch.setattr("profile_matcher.load_skills", lambda *args, **kwargs: MOCK_SKILL_WEIGHTS)

    jobs = [
        {"id": 1, "title": "Backend Eng", "description": "Needs python and sql and docker to build APIs"}, # 2.8 / 3.8 = 73.7%
        {"id": 2, "title": "Scrum Master", "description": "Lead agile teams doing sprint planning"},       # 0.5 / 3.8 = 13.2%
    ]
    
    processed_jobs = match_jobs(jobs)
    
    # First job should be a good match (> 50%)
    assert processed_jobs[0]["match_score"] > 50
    assert processed_jobs[0]["match_flag"].startswith("✅ Good Match")
    assert "python" in processed_jobs[0]["matched_skills"]
    
    # Second job should not be a good match (< 50%)
    assert processed_jobs[1]["match_score"] < 50
    assert processed_jobs[1]["match_flag"].startswith("—")

