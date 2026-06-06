"""
Sprint 10 smoke: cookie persistence + CAPTCHA notification.

Tests:
  - session_cookies: save/load/clear round-trip (file-based, no real browser)
  - captcha_notifier: email send queues manifest; completion reply detected
  - captcha_notifier: non-matching reply is not treated as completion
  - captcha_notifier: write_captcha_reply + _is_completion_reply integration
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── session_cookies ──────────────────────────────────────────────────────────

class TestSessionCookies:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        from src.browser import session_cookies as sc
        monkeypatch.setattr(sc, "_COOKIES_DIR", tmp_path / "cookies")

        # Create a mock page/context with fake cookies
        fake_cookies = [
            {"name": "li_at", "value": "tok123", "domain": ".linkedin.com", "path": "/"},
            {"name": "other", "value": "x",     "domain": ".google.com",   "path": "/"},
        ]
        mock_context = MagicMock()
        mock_context.cookies.return_value = fake_cookies
        mock_page = MagicMock()
        mock_page.context = mock_context

        out = sc.save_cookies_from_page(mock_page, "linkedin")
        assert out.exists()

        loaded = json.loads(out.read_text())
        assert len(loaded) == 1
        assert loaded[0]["name"] == "li_at"

    def test_load_into_context(self, tmp_path, monkeypatch):
        from src.browser import session_cookies as sc
        monkeypatch.setattr(sc, "_COOKIES_DIR", tmp_path / "cookies")
        (tmp_path / "cookies").mkdir()

        # Write fake cookie file
        cookie_data = [{"name": "sess", "value": "abc", "domain": ".indeed.com", "path": "/"}]
        (tmp_path / "cookies" / "indeed.json").write_text(json.dumps(cookie_data))

        mock_ctx = MagicMock()
        n = sc.load_cookies_into_context(mock_ctx, "indeed")
        assert n == 1
        mock_ctx.add_cookies.assert_called_once_with(cookie_data)

    def test_load_missing_file_returns_zero(self, tmp_path, monkeypatch):
        from src.browser import session_cookies as sc
        monkeypatch.setattr(sc, "_COOKIES_DIR", tmp_path / "cookies")
        (tmp_path / "cookies").mkdir()

        mock_ctx = MagicMock()
        n = sc.load_cookies_into_context(mock_ctx, "linkedin")
        assert n == 0
        mock_ctx.add_cookies.assert_not_called()

    def test_cookies_exist(self, tmp_path, monkeypatch):
        from src.browser import session_cookies as sc
        monkeypatch.setattr(sc, "_COOKIES_DIR", tmp_path / "cookies")
        (tmp_path / "cookies").mkdir()

        assert not sc.cookies_exist("linkedin")
        (tmp_path / "cookies" / "linkedin.json").write_text("[]")
        assert sc.cookies_exist("linkedin")

    def test_clear_cookies(self, tmp_path, monkeypatch):
        from src.browser import session_cookies as sc
        monkeypatch.setattr(sc, "_COOKIES_DIR", tmp_path / "cookies")
        (tmp_path / "cookies").mkdir()
        f = tmp_path / "cookies" / "linkedin.json"
        f.write_text("[]")
        assert f.exists()
        sc.clear_cookies("linkedin")
        assert not f.exists()


# ── captcha_notifier ─────────────────────────────────────────────────────────

class TestCaptchaNotifier:
    def test_send_notification_queues_manifest(self, tmp_path, monkeypatch):
        from src.browser import captcha_notifier as cn

        monkeypatch.setenv("YOUR_EMAIL_ADDRESS", "user@example.com")

        # Mock gmail_client.send_email
        mock_client = MagicMock()
        mock_client.send_email.return_value = "action_123"

        action_id = cn.send_captcha_notification(
            mock_client,
            app_id=42,
            company="Acme Corp",
            title="Senior Engineer",
            portal_url="https://jobs.acme.com/apply/123",
            challenge_type="CAPTCHA",
        )

        assert action_id == "action_123"
        mock_client.send_email.assert_called_once()
        call_kwargs = mock_client.send_email.call_args
        subject = call_kwargs.kwargs.get("subject") or call_kwargs.args[1]
        assert "CAPTCHA" in subject
        assert "APP-42" in subject
        assert "Acme Corp" in subject

    def test_send_notification_no_email_env_returns_empty(self, monkeypatch):
        from src.browser import captcha_notifier as cn

        monkeypatch.delenv("YOUR_EMAIL_ADDRESS", raising=False)
        mock_client = MagicMock()
        result = cn.send_captcha_notification(
            mock_client, app_id=1, company="X", title="Y", portal_url="http://x.com"
        )
        assert result == ""
        mock_client.send_email.assert_not_called()

    def test_is_completion_reply_matches_patterns(self):
        from src.browser.captcha_notifier import _is_completion_reply

        assert _is_completion_reply("captcha completed go ahead")
        assert _is_completion_reply("CAPTCHA DONE")
        assert _is_completion_reply("go ahead")
        assert _is_completion_reply("all good")
        assert _is_completion_reply("mfa done")
        assert _is_completion_reply("continue")

    def test_is_completion_reply_rejects_irrelevant(self):
        from src.browser.captcha_notifier import _is_completion_reply

        assert not _is_completion_reply("Hi, I'm interested in the job")
        assert not _is_completion_reply("please wait")
        assert not _is_completion_reply("")
        assert not _is_completion_reply("I visited the link")

    def test_write_captcha_reply_creates_file(self, tmp_path, monkeypatch):
        from src.browser import captcha_notifier as cn
        monkeypatch.setattr(cn, "_RESULTS_DIR", tmp_path)

        cn.write_captcha_reply(99, "captcha completed go ahead")

        result_path = tmp_path / "captcha_reply_99.json"
        assert result_path.exists()
        data = json.loads(result_path.read_text())
        assert data["app_id"] == 99
        assert "captcha" in data["reply_text"].lower()

    def test_poll_detects_reply_in_result_file(self, tmp_path, monkeypatch):
        """poll_for_captcha_reply returns True when result file contains completion text."""
        from src.browser import captcha_notifier as cn

        # Patch _RESULTS_DIR so it looks in tmp_path
        monkeypatch.setattr(cn, "_RESULTS_DIR", tmp_path)

        # Pre-write result file (simulates Claude Code having already executed MCP)
        result_file = tmp_path / "captcha_reply_7.json"
        result_file.write_text(json.dumps({"app_id": 7, "reply_text": "go ahead"}))

        mock_client = MagicMock()
        # Patch _queue_captcha_reply_search to avoid touching the real pending dir
        with patch.object(cn, "_queue_captcha_reply_search"):
            with patch("time.sleep"):  # instant poll
                found = cn.poll_for_captcha_reply(
                    7, mock_client, poll_interval=1, max_wait=5
                )

        assert found is True
        # File should be cleaned up
        assert not result_file.exists()

    def test_poll_times_out_without_reply(self, tmp_path, monkeypatch):
        from src.browser import captcha_notifier as cn

        monkeypatch.setattr(cn, "_RESULTS_DIR", tmp_path)
        mock_client = MagicMock()

        with patch.object(cn, "_queue_captcha_reply_search"):
            with patch("time.sleep"):
                found = cn.poll_for_captcha_reply(
                    8, mock_client, poll_interval=5, max_wait=4
                )

        assert found is False
