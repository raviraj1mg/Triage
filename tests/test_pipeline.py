"""Covers everything except the Gmail API itself."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oncallbot.config import GmailConfig, MatchConfig
from oncallbot.gmail_client import _parse_message
from oncallbot.models import EmailMessage, EmailThread, IssueSummary
from oncallbot.prompts import build_user_prompt
from oncallbot.query import build_query
from oncallbot.redact import redact
from oncallbot.render import to_markdown, to_json
from oncallbot.store import Store
from oncallbot.summarizer import SummarizerError, _parse_json_object, _unwrap_envelope


def _msg(**kw) -> EmailMessage:
    base = dict(
        id="m1",
        thread_id="t1",
        date=datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
        sender="Asha <asha@example.com>",
        to="health-record-support@1mg.com",
        cc="",
        subject="Lab report missing",
        body_text="My report for order OD12345 is not showing. Call me on 9876543210.",
        snippet="My report for order OD12345...",
    )
    base.update(kw)
    return EmailMessage(**base)


def _thread(messages=None) -> EmailThread:
    messages = messages or [_msg()]
    return EmailThread(id="t1", subject=messages[0].subject, messages=messages)


# --- query building -------------------------------------------------------


def test_query_ors_all_matchers():
    cfg = GmailConfig(
        match=MatchConfig(recipients=True, anywhere=True, labels=["hr-support"]),
        extra_query="-from:noreply@1mg.com",
        lookback_days=3,
    )
    q = build_query(cfg)
    assert "to:health-record-support@1mg.com" in q
    assert 'deliveredto:health-record-support@1mg.com' in q
    assert '"health-record-support@1mg.com"' in q
    assert "label:hr-support" in q
    assert "newer_than:3d" in q
    assert "-from:noreply@1mg.com" in q


def test_query_label_with_space_is_quoted():
    cfg = GmailConfig(match=MatchConfig(recipients=False, labels=["health records"]))
    assert 'label:"health records"' in build_query(cfg)


def test_query_requires_a_matcher():
    cfg = GmailConfig(match=MatchConfig(recipients=False, anywhere=False, labels=[]))
    with pytest.raises(ValueError, match="No matchers enabled"):
        build_query(cfg)


def test_query_omits_lookback_when_zero():
    cfg = GmailConfig(lookback_days=0)
    assert "newer_than" not in build_query(cfg)


# --- redaction ------------------------------------------------------------


def test_redact_masks_phone_keeping_last_four():
    assert redact("call 9876543210 now") == "call [REDACTED:PHONE …3210] now"


def test_redact_masks_ids_and_cards():
    out = redact("aadhaar 1234 5678 9012, pan ABCDE1234F, card 4111111111111111")
    assert "1234 5678 9012" not in out
    assert "ABCDE1234F" not in out
    assert "4111111111111111" not in out


def test_redact_keeps_triage_identifiers():
    text = "order OD12345 patient P-998 record rec_77 asha@example.com"
    assert redact(text) == text


# --- MIME parsing ---------------------------------------------------------


def _b64(s: str) -> str:
    import base64

    return base64.urlsafe_b64encode(s.encode()).decode()


def test_parse_message_prefers_plain_text_and_lists_attachments():
    raw = {
        "id": "m9",
        "threadId": "t9",
        "internalDate": "1757325600000",
        "snippet": "snip",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Mon, 8 Sep 2026 10:00:00 +0530"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain body")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html body</p>")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"size": 4096},
                },
            ],
        },
    }
    msg = _parse_message(raw)
    assert msg.body_text == "plain body"
    assert msg.subject == "Hi"
    assert msg.date is not None
    assert [(a.filename, a.size_bytes) for a in msg.attachments] == [("report.pdf", 4096)]


def test_parse_message_falls_back_to_html():
    raw = {
        "id": "m10",
        "threadId": "t9",
        "internalDate": "1757325600000",
        "snippet": "",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "From", "value": "a@b.com"}],
            "body": {"data": _b64("<div>line one</div><br><script>x</script><p>line two</p>")},
        },
    }
    msg = _parse_message(raw)
    assert "line one" in msg.body_text
    assert "line two" in msg.body_text
    assert "script" not in msg.body_text


# --- model output parsing -------------------------------------------------


def test_unwrap_envelope_extracts_result():
    payload = json.dumps({"result": '{"issue": "x"}', "is_error": False})
    assert _unwrap_envelope(payload) == '{"issue": "x"}'


def test_unwrap_envelope_raises_on_error_result():
    payload = json.dumps({"result": "boom", "is_error": True})
    with pytest.raises(SummarizerError, match="boom"):
        _unwrap_envelope(payload)


def test_parse_json_object_handles_fences_and_prose():
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_object('Sure, here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_parse_json_object_ignores_braces_inside_strings():
    assert _parse_json_object('{"issue": "a } brace"}') == {"issue": "a } brace"}


def test_parse_json_object_rejects_garbage():
    with pytest.raises(SummarizerError):
        _parse_json_object("no json here")


# --- summary construction -------------------------------------------------


def test_summary_coerces_bad_severity_and_confidence():
    s = IssueSummary.from_model_output(
        {"severity": "CRITICAL", "confidence": "9", "summary": "s", "issue": "i"},
        _thread(),
        "sonnet",
    )
    assert s.severity == "p3"
    assert s.confidence == 1.0
    assert s.permalink.endswith("t1")


def test_summary_defaults_reporter_to_first_sender():
    s = IssueSummary.from_model_output({}, _thread(), "sonnet")
    assert s.reporter == "Asha <asha@example.com>"


# --- prompt building ------------------------------------------------------


def test_prompt_fences_untrusted_content_and_lists_categories():
    p = build_user_prompt(_thread(), ["record_not_visible", "other"], 5000)
    assert "BEGIN UNTRUSTED EMAIL THREAD" in p
    assert "END UNTRUSTED EMAIL THREAD" in p
    assert "record_not_visible, other" in p


def test_prompt_respects_body_budget():
    long_msg = _msg(body_text="x" * 1000)
    p = build_user_prompt(_thread([long_msg]), ["other"], 100)
    assert "truncated" in p
    assert "x" * 200 not in p


def test_prompt_omits_bodies_once_budget_is_spent():
    msgs = [_msg(id="a", body_text="y" * 200), _msg(id="b", body_text="z" * 200)]
    p = build_user_prompt(_thread(msgs), ["other"], 150)
    assert "body omitted" in p


# --- store ----------------------------------------------------------------


def test_store_is_current_tracks_last_message(tmp_path: Path):
    with Store(tmp_path / "s.sqlite3") as store:
        summary = IssueSummary.from_model_output({"issue": "i"}, _thread(), "sonnet")
        store.upsert(summary, "m1")
        assert store.is_current("t1", "m1")
        assert not store.is_current("t1", "m2")   # new reply -> stale
        assert not store.is_current("t-unknown", "m1")


def test_store_upsert_replaces_and_round_trips(tmp_path: Path):
    with Store(tmp_path / "s.sqlite3") as store:
        th = _thread()
        store.upsert(IssueSummary.from_model_output({"issue": "first"}, th, "sonnet"), "m1")
        store.upsert(IssueSummary.from_model_output({"issue": "second"}, th, "sonnet"), "m2")
        assert len(store.recent()) == 1
        assert store.get("t1")["issue"] == "second"
        assert store.get("nope") is None


# --- rendering ------------------------------------------------------------


def test_markdown_sorts_p0_first_and_includes_ids():
    items = [
        {"severity": "p2", "subject": "later", "issue": "b", "summary": "", "affected": {}},
        {
            "severity": "p0",
            "subject": "urgent",
            "issue": "a",
            "summary": "",
            "affected": {"order_ids": ["OD1"]},
            "asks": ["restore it"],
        },
    ]
    md = to_markdown(items)
    assert md.index("urgent") < md.index("later")
    assert "order: OD1" in md
    assert "restore it" in md
    assert json.loads(to_json(items))[0]["subject"] == "urgent"


def test_markdown_handles_empty():
    assert "Nothing matched" in to_markdown([])


# --- account guard --------------------------------------------------------


class _FakeProfile:
    def __init__(self, email: str) -> None:
        self._email = email

    def users(self):
        return self

    def getProfile(self, userId):  # noqa: N802 - mirrors the Gmail API
        return self

    def execute(self):
        return {"emailAddress": self._email}


def test_assert_account_accepts_matching_mailbox():
    from oncallbot.gmail_auth import assert_account

    svc = _FakeProfile("raviraj.singh@1mg.com")
    assert assert_account(svc, "raviraj.singh@1mg.com") == "raviraj.singh@1mg.com"


def test_assert_account_is_case_insensitive():
    from oncallbot.gmail_auth import assert_account

    svc = _FakeProfile("Raviraj.Singh@1mg.com")
    assert assert_account(svc, "raviraj.singh@1mg.com")


def test_assert_account_rejects_wrong_mailbox():
    from oncallbot.gmail_auth import WrongAccountError, assert_account

    svc = _FakeProfile("someone.else@1mg.com")
    with pytest.raises(WrongAccountError, match="someone.else@1mg.com"):
        assert_account(svc, "raviraj.singh@1mg.com")


def test_assert_account_skips_check_when_unconfigured():
    from oncallbot.gmail_auth import assert_account

    svc = _FakeProfile("whoever@1mg.com")
    assert assert_account(svc, "") == "whoever@1mg.com"


# --- Gmail transient-error retry -------------------------------------------


def _stub_service(behaviour):
    """Minimal stand-in for the discovery client: users().threads().get().execute()."""

    class Exec:
        def execute(self):
            return behaviour()

    class Threads:
        def get(self, userId, id, format):  # noqa: A002, N803 - mirrors the API
            return Exec()

        def list(self, **kw):
            return Exec()

    class Users:
        def threads(self):
            return Threads()

    class Svc:
        def users(self):
            return Users()

    return Svc()


def test_get_thread_retries_a_stale_connection(monkeypatch):
    """The failure mode caused by interleaving Gmail reads with slow model calls."""
    from oncallbot import gmail_client

    monkeypatch.setattr(gmail_client.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("The read operation timed out")
        return {"messages": [{"id": "m1", "threadId": "t1", "internalDate": "1757325600000",
                              "snippet": "", "payload": {"headers": []}}]}

    client = gmail_client.GmailClient(_stub_service(flaky))
    thread = client.get_thread("t1")
    assert calls["n"] == 3
    assert thread.id == "t1"


def test_retry_gives_up_and_reraises(monkeypatch):
    from oncallbot import gmail_client

    monkeypatch.setattr(gmail_client.time, "sleep", lambda _s: None)

    def always_fails():
        raise TimeoutError("nope")

    client = gmail_client.GmailClient(_stub_service(always_fails))
    with pytest.raises(TimeoutError):
        client.get_thread("t1")


def test_retry_does_not_swallow_a_real_error(monkeypatch):
    """A 404 is not transient; retrying it would just waste time."""
    from oncallbot import gmail_client

    monkeypatch.setattr(gmail_client.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def not_found():
        calls["n"] += 1
        raise ValueError("malformed id")

    client = gmail_client.GmailClient(_stub_service(not_found))
    with pytest.raises(ValueError):
        client.get_thread("t1")
    assert calls["n"] == 1, "a non-transient error must not be retried"


def test_is_transient_classification():
    from oncallbot.gmail_client import _is_transient

    assert _is_transient(TimeoutError())
    assert _is_transient(ConnectionResetError())
    assert not _is_transient(ValueError())
    assert not _is_transient(KeyError())


# --- config: env overrides and .env, for handover setups --------------------


def test_env_var_overrides_config_file(tmp_path, monkeypatch):
    from oncallbot.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "gmail:\n  account: someone.else@1mg.com\n  lookback_days: 7\n"
        "summarizer:\n  backend: claude_cli\n  model: sonnet\n"
    )
    monkeypatch.setenv("ONCALLBOT_GMAIL_ACCOUNT", "teammate@1mg.com")
    monkeypatch.setenv("ONCALLBOT_LOOKBACK_DAYS", "3")
    monkeypatch.setenv("ONCALLBOT_BACKEND", "anthropic_api")
    monkeypatch.setenv("ONCALLBOT_MODEL", "opus")

    cfg = load_config(cfg_file)
    assert cfg.gmail.account == "teammate@1mg.com"
    assert cfg.gmail.lookback_days == 3
    assert cfg.summarizer.backend == "anthropic_api"
    assert cfg.summarizer.api_model == "claude-opus-5"


def test_env_override_coerces_booleans(tmp_path, monkeypatch):
    from oncallbot.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("attachments:\n  enabled: true\n")
    monkeypatch.setenv("ONCALLBOT_ATTACHMENTS_ENABLED", "false")
    assert load_config(cfg_file).attachments.enabled is False


def test_empty_env_var_does_not_override(tmp_path, monkeypatch):
    """An exported-but-blank variable must not wipe a real config value."""
    from oncallbot.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("gmail:\n  account: real@1mg.com\n")
    monkeypatch.setenv("ONCALLBOT_GMAIL_ACCOUNT", "")
    assert load_config(cfg_file).gmail.account == "real@1mg.com"


def test_dotenv_is_loaded_but_never_overrides_the_shell(tmp_path, monkeypatch):
    from oncallbot.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n\nANTHROPIC_API_KEY="sk-ant-from-file"\n'
        "ALREADY_SET=from-file\nMALFORMED_LINE\n"
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY_SET", "from-shell")

    loaded = load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"
    assert os.environ["ALREADY_SET"] == "from-shell", "the shell must win"
    assert "ALREADY_SET" not in loaded


def test_dotenv_missing_file_is_fine(tmp_path):
    from oncallbot.config import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == []


def test_unknown_backend_is_rejected_at_load_time(tmp_path):
    from oncallbot.config import ConfigError, load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("summarizer:\n  backend: openai\n")
    with pytest.raises(ConfigError, match="claude_cli"):
        load_config(cfg_file)


def test_missing_config_names_the_fix(tmp_path):
    from oncallbot.config import load_config

    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        load_config(tmp_path / "absent.yaml")
