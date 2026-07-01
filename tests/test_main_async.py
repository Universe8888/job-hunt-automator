"""Async smoke tests for main.py BrowserSession and orchestrator helpers.

These tests use pytest-asyncio and mock Playwright to exercise the
BrowserSession lifecycle and the run() entry point without launching
a real browser.
"""

import csv
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
from main import BrowserSession, DAYS_TO_FILTER


class TestDaysToFilter:
    def test_one_day(self):
        assert DAYS_TO_FILTER[1] == "r86400"

    def test_seven_days(self):
        assert DAYS_TO_FILTER[7] == "r604800"

    def test_thirty_days(self):
        assert DAYS_TO_FILTER[30] == "r2592000"

    def test_unsupported_day_value(self):
        assert DAYS_TO_FILTER.get(14, "") == ""


class TestBrowserSession:
    """Smoke tests for BrowserSession lifecycle with mocked Playwright."""

    def _make_mock_page(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)
        page.goto = AsyncMock()
        page.close = AsyncMock()
        return page

    def _make_mock_context(self, page):
        context = AsyncMock()
        context.pages = [page]
        context.close = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        return context

    def _make_mock_playwright(self, context):
        pw = AsyncMock()
        pw.chromium = AsyncMock()
        pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
        return pw

    @pytest.mark.asyncio
    async def test_start_creates_context_and_page(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        pw.chromium.launch_persistent_context.assert_awaited_once()
        assert session.page is page

    @pytest.mark.asyncio
    async def test_ensure_alive_returns_false_when_page_is_ok(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        result = await session.ensure_alive()
        assert result is False
        page.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensure_alive_raises_systemExit_when_page_is_dead(self):
        page = self._make_mock_page()
        page.evaluate = AsyncMock(side_effect=Exception("Target closed"))
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        with pytest.raises(SystemExit):
            await session.ensure_alive()

    @pytest.mark.asyncio
    async def test_close_shuts_down_context(self):
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        pw = self._make_mock_playwright(context)

        with patch("main.get_launch_options", return_value={"headless": True, "args": []}):
            with patch("main.get_context_options", return_value={"user_agent": "test"}):
                with patch.object(os, "makedirs"):
                    session = await BrowserSession(pw, headless=True).start()

        await session.close()
        context.close.assert_awaited_once()


class TestFastTriage:
    def test_disabled_fast_triage_never_skips(self, monkeypatch):
        called = False

        def fake_hits(_title):
            nonlocal called
            called = True
            return ["inventory specialist"]

        monkeypatch.setattr(main.gatekeeper, "title_hard_deny_hits", fake_hits)

        assert main._fast_triage_reject_reason(
            {"title": "Inventory Specialist"}, enabled=False, is_jobsbg=True
        ) is None
        assert called is False

    def test_non_jobsbg_fast_triage_never_skips(self, monkeypatch):
        called = False

        def fake_hits(_title):
            nonlocal called
            called = True
            return ["inventory specialist"]

        monkeypatch.setattr(main.gatekeeper, "title_hard_deny_hits", fake_hits)

        assert main._fast_triage_reject_reason(
            {"title": "Inventory Specialist"}, enabled=True, is_jobsbg=False
        ) is None
        assert called is False

    def test_title_operator_hard_deny_skips_detail_fetch(self, monkeypatch):
        monkeypatch.setattr(main.gatekeeper, "title_hard_deny_hits", lambda _title: ["inventory specialist"])

        reason = main._fast_triage_reject_reason(
            {"title": "Inventory Specialist"}, enabled=True, is_jobsbg=True
        )

        assert reason == main.FAST_TRIAGE_AUDIT_REASON

    def test_no_lane_terms_hard_fail_does_not_skip_detail_fetch(self, monkeypatch):
        monkeypatch.setattr(main.gatekeeper, "title_hard_deny_hits", lambda _title: [])

        assert main._fast_triage_reject_reason(
            {"title": "Business Analyst"}, enabled=True, is_jobsbg=True
        ) is None

    def test_soft_or_pass_title_does_not_skip_detail_fetch(self, monkeypatch):
        monkeypatch.setattr(main.gatekeeper, "title_hard_deny_hits", lambda _title: [])

        assert main._fast_triage_reject_reason(
            {"title": "Data Engineer"}, enabled=True, is_jobsbg=True
        ) is None

    def test_fast_triage_reject_routes_to_rejects_csv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main.gatekeeper, "TITLE_HARD_DENY", ["inventory specialist"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW", ["ai governance"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW_WEAK", [])
        monkeypatch.setattr(main.gatekeeper, "LANE_DENY", [])
        monkeypatch.setattr(main.gatekeeper, "DISCLOSED_COMP_REQUIRED", False)

        leads_csv = tmp_path / "leads.csv"
        rejects_csv = tmp_path / "rejects.csv"
        job = {
            "title": "Inventory Specialist",
            "company": "Example Co",
            "url": "https://www.jobs.bg/en/job/triaged",
        }

        verdict, count = main._route_fast_triage_reject(
            job,
            skills={},
            leads_csv=str(leads_csv),
            rejects_csv=str(rejects_csv),
            fast_triage_reason=main.FAST_TRIAGE_AUDIT_REASON,
        )

        assert verdict == "reject"
        assert count == 1
        assert not leads_csv.exists()
        with open(rejects_csv, newline="", encoding="utf-8-sig") as f:
            row = next(csv.DictReader(f))
        assert row["Failing Gates"] == "lane"
        assert main.FAST_TRIAGE_AUDIT_REASON in row["Reject Reasons"]
        assert row["Description"] == ""


class TestTimingSummary:
    def test_logs_new_runtime_counters(self):
        logger = MagicMock()

        main._log_timing_summary(
            logger,
            total_jobs=12,
            elapsed=600.0,
            search_time=60.0,
            desc_time=120.0,
            cdp_pace_time=300.0,
            non_cdp_pace_time=0.0,
            gate_route_time=30.0,
            sort_time=6.0,
            duplicate_skips=4,
            fast_triage_skips=9,
            blocked_resource_counts={"font": 2, "image": 7},
        )

        messages = [call.args[0] for call in logger.info.call_args_list]
        assert any("Jobs routed this run" in msg for msg in messages)
        assert any("CDP pacing sleep" in msg for msg in messages)
        assert any("Final leads sort" in msg for msg in messages)
        assert any("Duplicate skips before detail fetch" in msg for msg in messages)
        assert any("Fast-triage detail skips" in msg for msg in messages)
        assert any("Fast-triage blocked requests" in msg for msg in messages)


class TestCrossCsvDedupe:
    def test_detail_skip_state_skips_leads_but_rechecks_rejects(self, tmp_path):
        leads_csv = tmp_path / "leads.csv"
        rejects_csv = tmp_path / "rejects.csv"
        with open(leads_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["Verdict", "Job URL"])
            writer.writeheader()
            writer.writerow({"Verdict": "manual_review", "Job URL": "https://example.com/lead"})
        with open(rejects_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["Failing Gates", "Job URL"])
            writer.writeheader()
            writer.writerow({"Failing Gates": "lane", "Job URL": "https://example.com/reject"})

        seen_urls, stale_reject_urls = main._load_detail_skip_state(
            str(leads_csv),
            str(rejects_csv),
            MagicMock(),
        )

        assert seen_urls == {"https://example.com/lead"}
        assert stale_reject_urls == {"https://example.com/reject"}

    def test_promoted_reject_is_removed_from_rejects_csv(self, tmp_path):
        url = "https://example.com/promoted"
        leads_csv = tmp_path / "leads.csv"
        rejects_csv = tmp_path / "rejects.csv"
        with open(rejects_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["Failing Gates", "Reject Reasons", "Job URL"])
            writer.writeheader()
            writer.writerow({"Failing Gates": "lane", "Reject Reasons": "old", "Job URL": url})

        job = {
            "verdict": "manual_review",
            "rank": 1.25,
            "title": "AI Governance Analyst",
            "company": "Example Co",
            "url": url,
            "description": "In-lane role.",
        }
        stale_reject_urls = {url}

        verdict, count = main._route_job(
            job,
            str(leads_csv),
            str(rejects_csv),
            stale_reject_urls=stale_reject_urls,
        )

        assert verdict == "manual_review"
        assert count == 1
        assert stale_reject_urls == set()
        with open(leads_csv, newline="", encoding="utf-8-sig") as f:
            assert next(csv.DictReader(f))["Job URL"] == url
        with open(rejects_csv, newline="", encoding="utf-8-sig") as f:
            assert list(csv.DictReader(f)) == []


class TestFastResourceBlocking:
    @pytest.mark.asyncio
    async def test_not_installed_on_reused_human_cdp_tab(self):
        page = AsyncMock()
        page.route = AsyncMock()
        session = MagicMock()
        session.owns_page = False
        logger = MagicMock()

        result = await main._maybe_enable_fast_resource_blocking(
            page,
            session,
            fast_triage=True,
            is_jobsbg=True,
            use_cdp=True,
            blocked_page_ids=set(),
            blocked_resource_counts={},
            logger=logger,
        )

        assert result is False
        page.route.assert_not_awaited()


class TestJobsbgContinuationSources:
    def test_builds_single_url_job_from_jobsbg_path(self):
        job = main._build_jobsbg_single_url_job("/en/job/ai-governance-analyst-12345", source="single-url")

        assert job["url"] == "https://www.jobs.bg/en/job/ai-governance-analyst-12345"
        assert job["title"] == "AI Governance Analyst"
        assert job["search_keyword"] == "single-url"
        assert job["search_location"] == "direct URL"
        assert job[main.JOBSBG_TITLE_SOURCE_FIELD] == main.JOBSBG_TITLE_SOURCE_URL_SLUG

    def test_single_url_rejects_non_detail_url(self):
        with pytest.raises(ValueError, match="job detail URL"):
            main._build_jobsbg_single_url_job("https://www.jobs.bg/en/front_job_search.php?subm=1", source="single-url")

    def test_jobsbg_url_rejects_lookalike_domain(self):
        with pytest.raises(ValueError, match="jobs.bg"):
            main._normalize_jobsbg_job_url("https://eviljobs.bg/en/job/fake")

    def test_jobsbg_url_rejects_subdomain_lookalike(self):
        with pytest.raises(ValueError, match="jobs.bg"):
            main._normalize_jobsbg_job_url("https://evil.jobs.bg/en/job/fake")

    def test_jobsbg_url_canonicalizes_host_scheme_and_strips_tracking(self):
        assert (
            main._normalize_jobsbg_job_url("http://jobs.bg/en/job/ai-governance-analyst-123?utm=1#details")
            == "https://www.jobs.bg/en/job/ai-governance-analyst-123"
        )

    @pytest.mark.asyncio
    async def test_current_tab_detail_url_becomes_single_job(self):
        page = AsyncMock()
        page.url = "https://www.jobs.bg/en/job/ai-governance-analyst-999"
        page.content = AsyncMock()

        jobs = await main._jobsbg_jobs_from_current_tab(page, MagicMock())

        assert len(jobs) == 1
        assert jobs[0]["url"] == page.url
        assert jobs[0]["title"] == "AI Governance Analyst"
        assert jobs[0]["search_keyword"] == "current-tab"
        assert jobs[0][main.JOBSBG_TITLE_SOURCE_FIELD] == main.JOBSBG_TITLE_SOURCE_URL_SLUG
        page.content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_current_tab_result_cards_are_parsed(self):
        page = AsyncMock()
        page.url = "https://www.jobs.bg/en/front_job_search.php?subm=1"
        page.content = AsyncMock(return_value="""
        <html>
            <div class="mdc-card">
                <a class="black-link-b" href="/en/job/ai-risk-analyst" title="AI Risk Analyst">AI Risk Analyst</a>
                <a href="/company/example" title="Example Co">Example Co</a>
            </div>
        </html>
        """)

        jobs = await main._jobsbg_jobs_from_current_tab(page, MagicMock())

        assert len(jobs) == 1
        assert jobs[0]["title"] == "AI Risk Analyst"
        assert jobs[0]["url"] == "https://www.jobs.bg/en/job/ai-risk-analyst"
        assert jobs[0]["search_keyword"] == "current-tab"
        assert jobs[0]["search_location"] == "current tab"
        assert jobs[0][main.JOBSBG_TITLE_SOURCE_FIELD] == main.JOBSBG_TITLE_SOURCE_CARD

    @pytest.mark.asyncio
    async def test_current_tab_rejects_non_jobsbg_tab(self):
        page = AsyncMock()
        page.url = "https://example.com/not-jobs"

        with pytest.raises(RuntimeError, match="jobs.bg"):
            await main._jobsbg_jobs_from_current_tab(page, MagicMock())

    @pytest.mark.asyncio
    async def test_current_tab_rejects_jobsbg_homepage(self):
        page = AsyncMock()
        page.url = "https://www.jobs.bg/en/"
        page.content = AsyncMock()

        with pytest.raises(RuntimeError, match="search results page or job detail page"):
            await main._jobsbg_jobs_from_current_tab(page, MagicMock())
        page.content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_current_tab_rejects_search_page_without_cards(self):
        page = AsyncMock()
        page.url = "https://www.jobs.bg/en/front_job_search.php?subm=1"
        page.content = AsyncMock(return_value="<html></html>")

        with pytest.raises(RuntimeError, match="no jobs.bg job cards"):
            await main._jobsbg_jobs_from_current_tab(page, MagicMock())


class TestJobsbgContinuationRun:
    def _args(self, tmp_path, **overrides):
        defaults = {
            "profile": None,
            "quick": False,
            "headless": False,
            "max_jobs": 2,
            "fast_triage": False,
            "cdp": True,
            "cdp_endpoint": main.CDP_DEFAULT_ENDPOINT,
            "current_tab": False,
            "job_url": "",
            "site": None,
            "output": str(tmp_path / "leads.csv"),
            "keywords": None,
            "days": None,
            "cookies": None,
            "login": False,
            "verbose": False,
            "log_file": str(tmp_path / "scraper.log"),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _install_fake_playwright(self, monkeypatch):
        class FakePlaywrightContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeStealth:
            def use_async(self, _playwright):
                return FakePlaywrightContext()

        playwright_pkg = ModuleType("playwright")
        async_api_module = ModuleType("playwright.async_api")
        async_api_module.async_playwright = lambda: object()
        stealth_module = ModuleType("playwright_stealth")
        stealth_module.Stealth = FakeStealth

        monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
        monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)
        monkeypatch.setitem(sys.modules, "playwright_stealth", stealth_module)

    def _install_common_run_fakes(self, monkeypatch, fake_session):
        class FakeCDPSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def start(self):
                return fake_session

        monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)
        monkeypatch.setattr(main, "load_cookies", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(main, "load_skills", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(main, "_load_detail_skip_state", lambda *_args, **_kwargs: (set(), set()))
        monkeypatch.setattr(main, "_maybe_enable_fast_resource_blocking", AsyncMock(return_value=False))
        monkeypatch.setattr(main, "sort_leads_csv", MagicMock())
        monkeypatch.setattr(main, "_log_timing_summary", MagicMock())
        monkeypatch.setattr(main, "CDPSession", FakeCDPSession)
        monkeypatch.setattr(main.jobsbg_scraper, "scrape_jobs", AsyncMock(return_value=[]))

    def _fake_session(self):
        page = AsyncMock()
        page.url = "https://www.jobs.bg/en/front_job_search.php?subm=1"
        session = MagicMock()
        session.page = page
        session.owns_page = False
        session.ensure_alive = AsyncMock(return_value=False)
        session.close = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_run_current_tab_requires_cdp_before_browser_start(self, tmp_path):
        args = self._args(tmp_path, cdp=False, current_tab=True)

        with pytest.raises(SystemExit, match="--current-tab requires --cdp"):
            await main.run(args)

    @pytest.mark.asyncio
    async def test_run_job_url_requires_cdp_before_browser_start(self, tmp_path):
        args = self._args(tmp_path, cdp=False, job_url="https://www.jobs.bg/en/job/ai-1")

        with pytest.raises(SystemExit, match="--job-url requires --cdp"):
            await main.run(args)

    @pytest.mark.asyncio
    async def test_run_rejects_conflicting_continuation_sources(self, tmp_path):
        args = self._args(
            tmp_path,
            current_tab=True,
            job_url="https://www.jobs.bg/en/job/ai-1",
        )

        with pytest.raises(SystemExit, match="Use only one continuation source"):
            await main.run(args)

    @pytest.mark.asyncio
    async def test_run_rejects_linkedin_site_with_jobsbg_continuation(self, tmp_path):
        args = self._args(
            tmp_path,
            site="linkedin",
            job_url="https://www.jobs.bg/en/job/ai-1",
        )

        with pytest.raises(SystemExit, match="cannot be used with --site linkedin"):
            await main.run(args)

    @pytest.mark.asyncio
    async def test_run_job_url_skips_keyword_scrape_and_closes_session(self, tmp_path, monkeypatch):
        self._install_fake_playwright(monkeypatch)
        fake_session = self._fake_session()
        self._install_common_run_fakes(monkeypatch, fake_session)
        processed = {}

        async def fake_process_job_batch(basic_jobs, page, session, ctx):
            processed["jobs"] = basic_jobs
            processed["page"] = page
            processed["session"] = session
            processed["max_jobs"] = ctx.max_jobs
            ctx.routing.all_jobs.extend(basic_jobs)
            return True

        monkeypatch.setattr(main, "_process_job_batch", fake_process_job_batch)
        args = self._args(
            tmp_path,
            max_jobs=1,
            job_url="http://jobs.bg/en/job/ai-governance-analyst-123?utm=1#tracked",
        )

        await main.run(args)

        assert processed["jobs"][0]["url"] == "https://www.jobs.bg/en/job/ai-governance-analyst-123"
        assert processed["page"] is fake_session.page
        assert processed["session"] is fake_session
        assert processed["max_jobs"] == 1
        main.jobsbg_scraper.scrape_jobs.assert_not_awaited()
        fake_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_current_tab_error_closes_session(self, tmp_path, monkeypatch):
        self._install_fake_playwright(monkeypatch)
        fake_session = self._fake_session()
        self._install_common_run_fakes(monkeypatch, fake_session)
        monkeypatch.setattr(
            main,
            "_jobsbg_jobs_from_current_tab",
            AsyncMock(side_effect=RuntimeError("bad current tab")),
        )

        args = self._args(tmp_path, current_tab=True)

        with pytest.raises(RuntimeError, match="bad current tab"):
            await main.run(args)
        fake_session.close.assert_awaited_once()


class TestContinuationBatchProcessing:
    def _session(self, page):
        session = MagicMock()
        session.page = page
        session.ensure_alive = AsyncMock(return_value=False)
        return session

    def _context(
        self,
        tmp_path,
        scraper,
        stats,
        *,
        all_jobs=None,
        seen_urls=None,
        stale_reject_urls=None,
        use_cdp=True,
        fast_triage=True,
        max_jobs=0,
    ):
        return main.BatchContext(
            active_scraper=scraper,
            is_jobsbg=True,
            fetch_descriptions=True,
            fast_triage=fast_triage,
            use_cdp=use_cdp,
            skills={},
            routing=main.RoutingState(
                leads_csv=str(tmp_path / "leads.csv"),
                rejects_csv=str(tmp_path / "rejects.csv"),
                seen_urls=seen_urls if seen_urls is not None else set(),
                stale_reject_urls=stale_reject_urls if stale_reject_urls is not None else set(),
                all_jobs=all_jobs if all_jobs is not None else [],
            ),
            stats=stats,
            max_jobs=max_jobs,
            resources=main.ResourceBlockState(page_ids=set(), counts={}),
            logger=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_fast_triage_continuation_skips_detail_fetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "HAS_TQDM", False)
        monkeypatch.setattr(main.gatekeeper, "TITLE_HARD_DENY", ["inventory specialist"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW", ["ai governance"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW_WEAK", [])
        monkeypatch.setattr(main.gatekeeper, "LANE_DENY", [])
        monkeypatch.setattr(main.gatekeeper, "DISCLOSED_COMP_REQUIRED", False)

        page = AsyncMock()
        session = self._session(page)
        scraper = MagicMock()
        scraper.fetch_job_description = AsyncMock(return_value="should not be fetched")
        stats = main.RuntimeStats()
        all_jobs = []
        job = {
            "title": "Inventory Specialist",
            "company": "Example Co",
            "url": "https://www.jobs.bg/en/job/triaged",
        }
        ctx = self._context(tmp_path, scraper, stats, all_jobs=all_jobs)

        hit_max = await main._process_job_batch([job], page, session, ctx)

        assert hit_max is False
        scraper.fetch_job_description.assert_not_awaited()
        assert stats.fast_triage_skips == 1
        assert all_jobs == [job]
        assert not (tmp_path / "leads.csv").exists()
        with open(tmp_path / "rejects.csv", newline="", encoding="utf-8-sig") as f:
            row = next(csv.DictReader(f))
        assert row["Job URL"] == job["url"]
        assert main.FAST_TRIAGE_AUDIT_REASON in row["Reject Reasons"]

    @pytest.mark.asyncio
    async def test_url_slug_title_does_not_fast_triage_or_sleep_after_single_job(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "HAS_TQDM", False)
        monkeypatch.setattr(main, "cdp_human_pace", AsyncMock())
        monkeypatch.setattr(main, "match_jobs", lambda jobs, skills: jobs)

        def attach_reject(job):
            job["verdict"] = "reject"
            job["rank"] = 0.0
            job["gate1_status"] = "hard_fail"
            job["gate2_status"] = "soft"
            job["gate3_status"] = "soft"
            job["gate_reasons"] = "test reject"
            job["parsed_comp_eur"] = ""
            return "reject"

        monkeypatch.setattr(main, "_attach_verdict", attach_reject)

        page = AsyncMock()
        session = self._session(page)
        scraper = MagicMock()
        scraper.fetch_job_description = AsyncMock(return_value="Real detail text.")
        stats = main.RuntimeStats()
        job = main._build_jobsbg_single_url_job(
            "https://www.jobs.bg/en/job/inventory-specialist-12345",
            source="single-url",
        )
        ctx = self._context(tmp_path, scraper, stats)

        hit_max = await main._process_job_batch([job], page, session, ctx)

        assert hit_max is False
        scraper.fetch_job_description.assert_awaited_once_with(page, job["url"], job)
        main.cdp_human_pace.assert_not_awaited()
        assert stats.fast_triage_skips == 0

    @pytest.mark.asyncio
    async def test_continuation_rechecks_and_promotes_stale_reject(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "HAS_TQDM", False)
        monkeypatch.setattr(main, "human_delay", AsyncMock())
        monkeypatch.setattr(main, "match_jobs", lambda jobs, skills: jobs)

        def attach_manual_review(job):
            job["verdict"] = "manual_review"
            job["rank"] = 1.25
            job["gate1_status"] = "pass"
            job["gate2_status"] = "pass"
            job["gate3_status"] = "soft"
            job["gate_reasons"] = "test promotion"
            job["parsed_comp_eur"] = ""
            return "manual_review"

        monkeypatch.setattr(main, "_attach_verdict", attach_manual_review)

        url = "https://www.jobs.bg/en/job/old-reject"
        leads_csv = tmp_path / "leads.csv"
        rejects_csv = tmp_path / "rejects.csv"
        with open(rejects_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["Failing Gates", "Reject Reasons", "Job URL"])
            writer.writeheader()
            writer.writerow({"Failing Gates": "lane", "Reject Reasons": "old", "Job URL": url})

        page = AsyncMock()
        session = self._session(page)
        scraper = MagicMock()
        scraper.fetch_job_description = AsyncMock(return_value="AI governance role based in Sofia.")
        stats = main.RuntimeStats()
        stale_reject_urls = {url}
        job = {
            "title": "AI Governance Analyst",
            "company": "Example Co",
            "url": url,
            "location": "Sofia",
        }
        ctx = self._context(
            tmp_path,
            scraper,
            stats,
            stale_reject_urls=stale_reject_urls,
            use_cdp=False,
            fast_triage=False,
        )

        hit_max = await main._process_job_batch([job], page, session, ctx)

        assert hit_max is False
        scraper.fetch_job_description.assert_awaited_once_with(page, url, job)
        assert stats.duplicate_skips == 0
        assert stale_reject_urls == set()
        with open(leads_csv, newline="", encoding="utf-8-sig") as f:
            assert next(csv.DictReader(f))["Job URL"] == url
        with open(rejects_csv, newline="", encoding="utf-8-sig") as f:
            assert list(csv.DictReader(f)) == []

    @pytest.mark.asyncio
    async def test_no_pace_when_remaining_jobs_do_not_fetch_details(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "HAS_TQDM", False)
        monkeypatch.setattr(main, "cdp_human_pace", AsyncMock())
        monkeypatch.setattr(main, "match_jobs", lambda jobs, skills: jobs)
        monkeypatch.setattr(main.gatekeeper, "TITLE_HARD_DENY", ["inventory specialist"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW", ["ai governance"])
        monkeypatch.setattr(main.gatekeeper, "LANE_ALLOW_WEAK", [])
        monkeypatch.setattr(main.gatekeeper, "LANE_DENY", [])
        monkeypatch.setattr(main.gatekeeper, "DISCLOSED_COMP_REQUIRED", False)

        def attach_reject(job):
            job["verdict"] = "reject"
            job["rank"] = 0.0
            job["gate1_status"] = "hard_fail"
            job["gate2_status"] = "soft"
            job["gate3_status"] = "soft"
            job["gate_reasons"] = "test reject"
            job["parsed_comp_eur"] = ""
            return "reject"

        monkeypatch.setattr(main, "_attach_verdict", attach_reject)

        page = AsyncMock()
        session = self._session(page)
        scraper = MagicMock()
        scraper.fetch_job_description = AsyncMock(return_value="Real detail text.")
        stats = main.RuntimeStats()
        first_url = "https://www.jobs.bg/en/job/ai-governance-analyst-1"
        jobs = [
            {"title": "AI Governance Analyst", "url": first_url},
            {"title": "Inventory Specialist", "url": "https://www.jobs.bg/en/job/inventory-1"},
            {"title": "AI Governance Analyst", "url": first_url},
        ]
        ctx = self._context(tmp_path, scraper, stats)

        hit_max = await main._process_job_batch(jobs, page, session, ctx)

        assert hit_max is False
        scraper.fetch_job_description.assert_awaited_once_with(page, first_url, jobs[0])
        main.cdp_human_pace.assert_not_awaited()
        assert stats.fast_triage_skips == 1
        assert stats.duplicate_skips == 1

    @pytest.mark.asyncio
    async def test_no_pace_when_max_jobs_stops_before_next_fetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "HAS_TQDM", False)
        monkeypatch.setattr(main, "cdp_human_pace", AsyncMock())
        monkeypatch.setattr(main, "match_jobs", lambda jobs, skills: jobs)

        def attach_reject(job):
            job["verdict"] = "reject"
            job["rank"] = 0.0
            job["gate1_status"] = "hard_fail"
            job["gate2_status"] = "soft"
            job["gate3_status"] = "soft"
            job["gate_reasons"] = "test reject"
            job["parsed_comp_eur"] = ""
            return "reject"

        monkeypatch.setattr(main, "_attach_verdict", attach_reject)

        page = AsyncMock()
        session = self._session(page)
        scraper = MagicMock()
        scraper.fetch_job_description = AsyncMock(return_value="Real detail text.")
        stats = main.RuntimeStats()
        jobs = [
            {"title": "AI Governance Analyst", "url": "https://www.jobs.bg/en/job/ai-1"},
            {"title": "AI Risk Analyst", "url": "https://www.jobs.bg/en/job/ai-2"},
        ]
        ctx = self._context(tmp_path, scraper, stats, fast_triage=False, max_jobs=1)

        hit_max = await main._process_job_batch(jobs, page, session, ctx)

        assert hit_max is True
        scraper.fetch_job_description.assert_awaited_once_with(page, jobs[0]["url"], jobs[0])
        main.cdp_human_pace.assert_not_awaited()
