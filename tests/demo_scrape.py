import sys
import os
import argparse
import asyncio

# Ensure parent directory is in path so we can import internal modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
import profile_matcher

# 1. Override the weights to be our "John Doe" simulated engineering profile
MOCK_SKILL_WEIGHTS = {
    "python": 1.0,
    "sql": 1.0,
    "docker": 0.8,
    "kubernetes": 0.5,
    "agile": 0.5,
    "backend": 1.0,
    "software": 0.5
}
# Monkeypatch the config and profile_matcher directly
config.SKILL_WEIGHTS = dict(MOCK_SKILL_WEIGHTS)
profile_matcher.SKILL_WEIGHTS = dict(MOCK_SKILL_WEIGHTS)
config.MATCH_THRESHOLD = 30  # lowering threshold so we can see matches

import main

async def run_demo():
    print("==================================================")
    print("      DEMO SCRAPE - JOHN DOE (ENGINEER)           ")
    print("==================================================")
    print("This demo will scrape exactly 2 jobs from LinkedIn")
    print("and 2 jobs from Jobs.bg to demonstrate output tracking.")
    print(f"Simulated Profile: {list(MOCK_SKILL_WEIGHTS.keys())}")
    
    # Override search locations for a broader demo
    config.LOCATIONS = [
        {"name": "Remote", "geoId": "91000007", "location_text": "Remote", "jobsbg_location_id": "2"}
    ]

    # ─── LINKEDIN DEMO ───
    print("\n--- 🚀 RUNNING LINKEDIN DEMO ... ---")
    args_li = argparse.Namespace(
        profile="dummy_does_not_exist.pdf",  # Forces fallback to our MOCK_SKILL_WEIGHTS
        quick=False,
        keywords=["Python Engineer"],
        verbose=False,
        headless=True,
        days=7,
        max_jobs=2,
        site="linkedin",
        output="tests/demo_linkedin_leads.csv",
        log_file="tests/demo_scraper.log"
    )
    
    await main.run(args_li)
    
    # ─── JOBS.BG DEMO ───
    print("\n--- 🚀 RUNNING JOBS.BG DEMO ... ---")
    args_jb = argparse.Namespace(
        profile="dummy_does_not_exist.pdf",
        quick=False,
        keywords=["Python Engineer"],
        verbose=False,
        headless=False,  # Headed in case we hit the Jobs.bg captcha validation
        days=7,
        max_jobs=2,
        site="jobs.bg",
        output="tests/demo_jobsbg_leads.csv",
        log_file="tests/demo_scraper.log"
    )
    
    try:
        await main.run(args_jb)
    except Exception as e:
        print(f"Jobs.bg encountered an error (likely a captcha): {e}")
        
    print("\n==================================================")
    print("✅ DEMO COMPLETE")
    print("You can view the resulting files here:")
    print("-> tests/demo_linkedin_leads.csv")
    print("-> tests/demo_jobsbg_leads.csv")

if __name__ == "__main__":
    asyncio.run(run_demo())
