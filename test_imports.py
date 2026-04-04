"""
Import and basic functionality tests for LinkedIn Scraper v2.0.
Run with: python test_imports.py
"""

import sys
from config import (
    KEYWORDS, LOCATIONS, SKILL_WEIGHTS, OUTPUT_CSV, PROFILE_PDF,
    SIGN_IN_MODAL_MARKERS, DATE_POSTED_FILTER, EXPERIENCE_LEVEL_FILTERS,
    PROXY_URL, MAX_JOBS_PER_RUN, HEADLESS_DEFAULT,
)
from stealth_config import get_launch_options, get_context_options, get_random_user_agent
from csv_export import export_to_csv, CSV_COLUMNS
from profile_matcher import match_jobs, load_skills, score_job
from scraper import scrape_jobs, build_api_url, is_valid_job_url, is_modal_garbage

def test_imports():
    """Test that all modules can be imported."""
    print("[OK] All imports OK")
    
def test_config():
    """Test configuration values."""
    print(f"[INFO] Keywords: {KEYWORDS}")
    print(f"[INFO] Locations: {[l['name'] for l in LOCATIONS]}")
    print(f"[INFO] Skills: {len(SKILL_WEIGHTS)} weighted terms")
    print(f"[INFO] Output CSV: {OUTPUT_CSV}")
    print(f"[INFO] Profile PDF: {ascii(PROFILE_PDF)}")
    print(f"[INFO] Modal markers: {len(SIGN_IN_MODAL_MARKERS)} markers")
    print(f"[INFO] Date filter: '{DATE_POSTED_FILTER}'")
    print(f"[INFO] Experience filters: {EXPERIENCE_LEVEL_FILTERS}")
    print(f"[INFO] Proxy: {'configured' if PROXY_URL else 'none'}")
    print(f"[INFO] Max jobs per run: {MAX_JOBS_PER_RUN}")
    print(f"[INFO] Headless default: {HEADLESS_DEFAULT}")
    
    # Validate config
    assert len(KEYWORDS) > 0, "KEYWORDS list is empty"
    assert len(LOCATIONS) > 0, "LOCATIONS list is empty"
    assert len(SKILL_WEIGHTS) > 0, "SKILL_WEIGHTS dictionary is empty"
    assert OUTPUT_CSV.endswith('.csv'), "OUTPUT_CSV should be a CSV file"
    assert len(SIGN_IN_MODAL_MARKERS) > 0, "SIGN_IN_MODAL_MARKERS should not be empty"
    
def test_url_building():
    """Test API URL construction."""
    url = build_api_url(KEYWORDS[0], LOCATIONS[0])
    print(f"[LINK] Sample API URL: {url[:100]}...")
    
    # Basic validation
    assert "linkedin.com" in url, "URL should contain linkedin.com"
    assert "jobs" in url, "URL should contain jobs"
    
    # Test with date filter
    url_filtered = build_api_url(KEYWORDS[0], LOCATIONS[0], date_filter="r604800")
    assert "f_TPR=r604800" in url_filtered, "Date filter should be in URL"
    print(f"[LINK] Filtered URL: {url_filtered[:100]}...")

    # Test with experience filter
    url_exp = build_api_url(KEYWORDS[0], LOCATIONS[0], experience_filters=["3", "4"])
    assert "f_E=3%2C4" in url_exp or "f_E=3,4" in url_exp, "Experience filter should be in URL"
    print(f"[LINK] Experience-filtered URL: {url_exp[:100]}...")
    
def test_url_validation():
    """Test URL validation logic."""
    # Valid job URLs
    assert is_valid_job_url("https://www.linkedin.com/jobs/view/test-job-123") == True
    assert is_valid_job_url("https://uk.linkedin.com/jobs/view/test-job-456") == True
    
    # Invalid URLs
    assert is_valid_job_url("https://www.linkedin.com/company/deloitte") == False
    assert is_valid_job_url("https://www.linkedin.com/in/john-doe") == False
    assert is_valid_job_url("") == False
    assert is_valid_job_url("https://google.com") == False
    
    print("[OK] URL validation: all cases passed")

def test_content_validation():
    """Test sign-in modal garbage detection."""
    assert is_modal_garbage("Join LinkedIn\nEmail\nPassword") == True
    assert is_modal_garbage("Agree & Join") == True
    assert is_modal_garbage("We need a Python developer with 5 years experience.") == False
    assert is_modal_garbage("") == False
    
    print("[OK] Content validation: all cases passed")

def test_stealth_config():
    """Test stealth configuration functions."""
    launch_options = get_launch_options()
    print(f"[SHIELD] Launch options: {list(launch_options.keys())}")
    
    # Test headless mode
    headless_options = get_launch_options(headless=True)
    assert headless_options["headless"] == True, "Headless should be True"
    assert "--disable-gpu" in headless_options["args"], "Should have --disable-gpu in headless"
    
    headed_options = get_launch_options(headless=False)
    assert headed_options["headless"] == False, "Headless should be False"
    
    user_agent = get_random_user_agent()
    context_options = get_context_options(user_agent)
    print(f"[SHIELD] Context options: {len(context_options)} settings")
    
    assert isinstance(user_agent, str), "User agent should be a string"
    assert len(user_agent) > 10, "User agent should be a meaningful string"
    assert "Chrome/131" in user_agent or "Chrome/132" in user_agent or \
           "Firefox/133" in user_agent or "Safari/605" in user_agent or \
           "Edg/131" in user_agent, \
           f"User agent should be a 2026-era browser: {user_agent}"
    print(f"[OK] User-Agent is modern: {user_agent[:50]}...")
    
def test_csv_headers():
    """Test CSV export structure."""
    print(f"[CSV] CSV Headers: {CSV_COLUMNS}")
    
    expected_headers = [
        "Job Title", "Company Name", "Location", "Posting Date",
        "Salary Info", "Description", "Job URL", "Search Keyword",
        "Search Location", "Match Score", "Matched Skills", "Match Flag",
    ]
    
    for header in expected_headers:
        assert header in CSV_COLUMNS, f"Missing header: {header}"
    
    print(f"[OK] All {len(expected_headers)} expected headers present")
    
def test_profile_matcher_structure():
    """Test profile matcher — load_skills and match_jobs."""
    # Test load_skills with missing PDF (should use hardcoded skills)
    skills = load_skills("nonexistent.pdf")
    assert isinstance(skills, dict), "load_skills should return a dict"
    assert len(skills) > 0, "Should have some skills"
    print(f"[OK] load_skills returned {len(skills)} skills")
    
    # Test scoring
    score, matched = score_job("We need SQL and Python expertise for data analysis", skills)
    assert 0 <= score <= 100, f"Score should be 0-100, got {score}"
    assert "sql" in matched, "Should match 'sql'"
    assert "python" in matched, "Should match 'python'"
    print(f"[OK] score_job: {score}% — matched {matched}")

    # Test match_jobs with pre-built skills
    sample_jobs = [
        {
            "title": "Test Job",
            "company": "Test Company",
            "location": "Test Location",
            "description": "Test description with SQL and Python keywords",
            "url": "https://example.com",
            "search_keyword": "Test Keyword",
            "search_location": "Test Location"
        }
    ]
    
    matched_jobs = match_jobs(sample_jobs, skills=skills)
    assert len(matched_jobs) == 1, "Should return 1 job"
    assert "match_score" in matched_jobs[0], "match_score should be added"
    assert "matched_skills" in matched_jobs[0], "matched_skills should be added"
    assert isinstance(matched_jobs[0]["matched_skills"], list), "matched_skills should be a list"
    print(f"[OK] match_jobs with pre-built skills: score={matched_jobs[0]['match_score']}%")

def run_all_tests():
    """Run all tests."""
    tests = [
        ("Imports", test_imports),
        ("Configuration", test_config),
        ("URL Building", test_url_building),
        ("URL Validation", test_url_validation),
        ("Content Validation", test_content_validation),
        ("Stealth Config", test_stealth_config),
        ("CSV Headers", test_csv_headers),
        ("Profile Matcher", test_profile_matcher_structure),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        try:
            print(f"\n{'='*60}")
            print(f"Running: {test_name}")
            print(f"{'='*60}")
            test_func()
            passed += 1
        except Exception as e:
            print(f"[FAIL] Test failed: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"TEST SUMMARY: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    
    if failed == 0:
        print("[PASS] All tests passed! The scraper should work correctly.")
    else:
        print(f"[WARN] {failed} test(s) failed. Check the errors above.")
    
    return failed == 0

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
