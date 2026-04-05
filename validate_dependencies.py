#!/usr/bin/env python3
"""
Dependency validation script for LinkedIn Scraper v3.0.
Checks if all required packages are installed and Playwright is properly set up.
"""

import sys
import subprocess
import importlib.util
import importlib.metadata

REQUIRED_PACKAGES = [
    ("playwright", ">=1.40.0"),
    ("playwright_stealth", ">=1.0.6"),
    ("beautifulsoup4", ">=4.12.0"),
    ("lxml", ">=5.0.0"),
    ("PyPDF2", ">=3.0.0"),
    ("python-dotenv", ">=1.0.0"),
    ("tqdm", ">=4.66.0"),
]

def check_python_version():
    """Check if Python version is sufficient."""
    print("[CHECK] Checking Python version...")
    if sys.version_info < (3, 10):
        print(f"[FAIL] Python 3.10+ required (for type hints), found {sys.version}")
        return False
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True

def check_package(package_name, version_constraint):
    """Check if a package is installed and meets version requirements."""
    try:
        # Normalize package name for metadata lookup
        lookup_name = package_name.replace("_", "-")
        version = importlib.metadata.version(lookup_name)
        
        # Simple version check (basic)
        required_version = version_constraint.replace(">=", "")
        installed_parts = [int(x) for x in version.split(".")[:3]]
        required_parts = [int(x) for x in required_version.split(".")[:3]]
        
        # Pad to 3 parts
        while len(installed_parts) < 3:
            installed_parts.append(0)
        while len(required_parts) < 3:
            required_parts.append(0)
        
        if tuple(installed_parts) < tuple(required_parts):
            print(f"[FAIL] {package_name}: installed v{version}, requires {version_constraint}")
            return False
        
        print(f"[OK] {package_name}: v{version}")
        return True
    except importlib.metadata.PackageNotFoundError:
        print(f"[FAIL] {package_name}: Not installed")
        return False
    except Exception as e:
        print(f"[WARN] {package_name}: Error checking version - {e}")
        return False

def check_playwright_browsers():
    """Check if Playwright browsers are installed."""
    print("\n🔍 Checking Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if "chromium" in result.stdout.lower():
            print("✅ Playwright browsers are installed")
            return True
        else:
            print("⚠️  Playwright browsers may not be fully installed")
            print("   Run: playwright install chromium")
            return False
    except subprocess.TimeoutExpired:
        print("⚠️  Playwright check timed out")
        return False
    except FileNotFoundError:
        print("❌ Playwright command not found")
        return False
    except Exception as e:
        print(f"⚠️  Error checking Playwright: {e}")
        return False

def check_project_imports():
    """Check if project modules can be imported."""
    print("\n🔍 Checking project imports...")
    modules_to_check = [
        ("config", "KEYWORDS"),
        ("config", "SIGN_IN_MODAL_MARKERS"),
        ("config", "DATE_POSTED_FILTER"),
        ("scraper", "scrape_jobs"),
        ("scraper", "is_valid_job_url"),
        ("profile_matcher", "match_jobs"),
        ("profile_matcher", "load_skills"),
        ("csv_export", "export_to_csv"),
        ("stealth_config", "get_launch_options"),
    ]
    
    all_ok = True
    for module_name, attribute_name in modules_to_check:
        try:
            spec = importlib.util.find_spec(module_name)
            if spec is None:
                print(f"❌ {module_name}: Module not found")
                all_ok = False
            else:
                module = importlib.import_module(module_name)
                if hasattr(module, attribute_name):
                    print(f"✅ {module_name}.{attribute_name}: OK")
                else:
                    print(f"⚠️  {module_name}: Missing {attribute_name}")
                    all_ok = False
        except Exception as e:
            print(f"❌ {module_name}: Import error - {e}")
            all_ok = False
    
    return all_ok

def check_url_validation():
    """Check that URL validation works correctly."""
    print("\n🔍 Checking URL validation...")
    from scraper import is_valid_job_url
    
    test_cases = [
        ("https://www.linkedin.com/jobs/view/procurement-manager-at-asos-4378190960", True),
        ("https://uk.linkedin.com/jobs/view/senior-lead-at-vermelo-4384072189", True),
        ("https://www.linkedin.com/company/deloitte", False),
        ("https://fr.linkedin.com/company/sia-partners", False),
        ("https://www.linkedin.com/in/some-profile", False),
        ("", False),
    ]
    
    all_ok = True
    for url, expected in test_cases:
        result = is_valid_job_url(url)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_ok = False
        label = url[:60] if url else "(empty)"
        print(f"  {status} {label} → {'valid' if result else 'invalid'}")
    
    return all_ok

def check_content_validation():
    """Check that modal garbage detection works."""
    print("\n🔍 Checking content sanitization...")
    from scraper import is_modal_garbage
    
    test_cases = [
        ("Join LinkedIn\nEmail\nPassword (6+ characters)", True),
        ("We are looking for a Senior Developer with Python skills.", False),
        ("Already on Linkedin? Sign in to view full description.", True),
        ("", False),
    ]
    
    all_ok = True
    for text, expected in test_cases:
        result = is_modal_garbage(text)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_ok = False
        label = text[:50] if text else "(empty)"
        print(f"  {status} \"{label}\" → {'garbage' if result else 'clean'}")
    
    return all_ok

def main():
    print("=" * 60)
    print("LinkedIn Scraper v3.0 — Dependency Validation")
    print("=" * 60)
    
    checks_passed = []
    
    # Run checks
    checks_passed.append(("Python version", check_python_version()))
    
    print("\n🔍 Checking required packages...")
    packages_ok = True
    for package, version in REQUIRED_PACKAGES:
        if not check_package(package, version):
            packages_ok = False
    checks_passed.append(("Packages", packages_ok))
    
    checks_passed.append(("Playwright browsers", check_playwright_browsers()))
    checks_passed.append(("Project imports", check_project_imports()))
    checks_passed.append(("URL validation", check_url_validation()))
    checks_passed.append(("Content sanitization", check_content_validation()))
    
    # Summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    
    for name, passed in checks_passed:
        status = "✅" if passed else "❌"
        print(f"  {status} {name}")
    
    passed_count = sum(1 for _, p in checks_passed if p)
    total_count = len(checks_passed)
    
    print()
    if all(p for _, p in checks_passed):
        print(f"✅ All {total_count} checks passed!")
        print("\n🎉 Your environment is ready to run the scraper.")
        print("   Run: python main.py")
        return 0
    else:
        print(f"❌ {total_count - passed_count}/{total_count} checks failed")
        print("\n⚠️  Some dependencies are missing or outdated.")
        print("\nTo fix:")
        print("1. Install missing packages:")
        print("   pip install -r requirements.txt")
        print("2. Install Playwright browsers:")
        print("   playwright install chromium")
        print("3. Verify your Python environment is activated")
        return 1

if __name__ == "__main__":
    sys.exit(main())