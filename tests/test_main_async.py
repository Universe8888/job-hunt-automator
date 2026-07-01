"""Async smoke tests for main.py BrowserSession and orchestrator helpers.

These tests use pytest-asyncio and mock Playwright to exercise the
BrowserSession lifecycle and the run() entry point without launching
a real browser.
"""

import asyncio
import csv
import os
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
