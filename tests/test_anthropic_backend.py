"""Anthropic API backend. Verified against a stub client: no key on this machine.

The request shape is what matters here -- model id, schema, content blocks and
error mapping -- so that is what these assert.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oncallbot.anthropic_backend import (
    SUMMARY_SCHEMA,
    AnthropicSummarizer,
    complete_json,
    stream_answer,
    wrap_api_errors,
)
from oncallbot.config import AttachmentConfig, Config, SummarizerConfig
from oncallbot.models import Attachment, EmailMessage, EmailThread
from oncallbot.summarizer import SummarizerError


# --- stubs -----------------------------------------------------------------


class Block:
    def __init__(self, text: str, type_: str = "text") -> None:
        self.text = text
        self.type = type_


class Response:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [Block(text)]
        self.stop_reason = stop_reason
        self.stop_details = None


class StubMessages:
    def __init__(self, payload: dict | None = None, raises: Exception | None = None) -> None:
        self.payload = payload or {}
        self.raises = raises
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return Response(json.dumps(self.payload))


class StubClient:
    def __init__(self, **kw) -> None:
        self.messages = StubMessages(**kw)


VALID = {
    "reporter": "Asha <asha@x.com>",
    "summary": "Report missing.",
    "issue": "Lipid report not visible.",
    "category": "lab_report_not_synced",
    "severity": "p1",
    "asks": ["restore it"],
    "missing_info": ["order id"],
    "affected_entities": {
        "patient_ids": [], "order_ids": ["PO1"], "prescription_ids": [],
        "record_ids": [], "user_emails": [], "other_ids": [],
    },
    "suggested_owner": "labs",
    "confidence": 0.8,
}


def _cfg(**over) -> Config:
    c = Config()
    c.summarizer = SummarizerConfig(backend="anthropic_api", model="opus", **over)
    c.categories = ["lab_report_not_synced", "upload_failure", "other"]
    return c


def _thread(*atts: Attachment) -> EmailThread:
    m = EmailMessage(
        id="m1", thread_id="t1",
        date=datetime(2026, 9, 8, tzinfo=timezone.utc),
        sender="Asha <asha@x.com>", to="health-record-support@1mg.com", cc="",
        subject="Report missing", body_text="My lipid report is missing. Call 9876543210.",
        snippet="", attachments=list(atts),
    )
    return EmailThread(id="t1", subject=m.subject, messages=[m])


# --- model id resolution ---------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("opus", "claude-opus-5"),
        ("sonnet", "claude-sonnet-5"),
        ("haiku", "claude-haiku-4-5"),
        ("OPUS", "claude-opus-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),   # explicit id passes through
    ],
)
def test_aliases_resolve_to_real_model_ids(given, expected):
    assert SummarizerConfig(model=given).api_model == expected


# --- request shape ---------------------------------------------------------


def test_summarize_sends_the_schema_and_resolved_model():
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    s.summarize(_thread())

    call = s.client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    # The category enum is narrowed to the configured list.
    assert fmt["schema"]["properties"]["category"]["enum"] == [
        "lab_report_not_synced", "upload_failure", "other"
    ]
    assert call["output_config"]["effort"] == "medium"


def test_narrowing_categories_does_not_mutate_the_module_schema():
    before = json.dumps(SUMMARY_SCHEMA, sort_keys=True)
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    s.summarize(_thread())
    assert json.dumps(SUMMARY_SCHEMA, sort_keys=True) == before


def test_summarize_redacts_before_sending():
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    s.summarize(_thread())
    sent = json.dumps(s.client.messages.calls[0]["messages"])
    assert "9876543210" not in sent
    assert "REDACTED:PHONE" in sent


def test_summarize_can_send_raw_when_redaction_is_off():
    cfg = _cfg()
    cfg.redaction_enabled = False
    s = AnthropicSummarizer.from_config(cfg)
    s.client = StubClient(payload=VALID)
    s.summarize(_thread())
    assert "9876543210" in json.dumps(s.client.messages.calls[0]["messages"])


def test_summarize_returns_a_populated_summary():
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    out = s.summarize(_thread())
    assert out.severity == "p1"
    assert out.category == "lab_report_not_synced"
    assert out.affected.order_ids == ["PO1"]
    assert out.model == "claude-opus-5"
    assert out.permalink.endswith("t1")


# --- attachments as content blocks ------------------------------------------


class FakeGmail:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        return self.payload


def test_image_attachment_becomes_an_image_block():
    att = Attachment("shot.png", "image/png", 12, "a1", "m1")
    s = AnthropicSummarizer.from_config(_cfg(), FakeGmail(b"\x89PNG"))
    s.client = StubClient(payload=VALID)
    s.summarize(_thread(att))

    blocks = s.client.messages.calls[0]["messages"][0]["content"]
    img = next(b for b in blocks if b["type"] == "image")
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"]          # base64, not a filesystem path
    assert blocks[-1]["type"] == "text", "the prompt must come after the media"


def test_pdf_attachment_becomes_a_document_block():
    att = Attachment("lab.pdf", "application/pdf", 12, "a1", "m1")
    s = AnthropicSummarizer.from_config(_cfg(), FakeGmail(b"%PDF-1.4"))
    s.client = StubClient(payload=VALID)
    s.summarize(_thread(att))

    blocks = s.client.messages.calls[0]["messages"][0]["content"]
    doc = next(b for b in blocks if b["type"] == "document")
    assert doc["source"]["media_type"] == "application/pdf"


def test_text_attachment_is_inlined_fenced_and_redacted():
    att = Attachment("notes.txt", "text/plain", 40, "a1", "m1")
    s = AnthropicSummarizer.from_config(_cfg(), FakeGmail(b"ticket 5, phone 9812345678"))
    s.client = StubClient(payload=VALID)
    s.summarize(_thread(att))

    sent = json.dumps(s.client.messages.calls[0]["messages"])
    assert "BEGIN UNTRUSTED ATTACHMENT" in sent
    assert "9812345678" not in sent


def test_jpg_media_type_is_normalised():
    att = Attachment("a.jpg", "image/jpg", 12, "a1", "m1")
    s = AnthropicSummarizer.from_config(_cfg(), FakeGmail(b"\xff\xd8"))
    s.client = StubClient(payload=VALID)
    s.summarize(_thread(att))
    blocks = s.client.messages.calls[0]["messages"][0]["content"]
    assert next(b for b in blocks if b["type"] == "image")["source"]["media_type"] == "image/jpeg"


def test_attachments_disabled_sends_no_media_and_says_so():
    cfg = _cfg()
    cfg.attachments = AttachmentConfig(enabled=False)
    att = Attachment("lab.pdf", "application/pdf", 12, "a1", "m1")
    s = AnthropicSummarizer.from_config(cfg, FakeGmail(b"%PDF"))
    s.client = StubClient(payload=VALID)
    s.summarize(_thread(att))

    blocks = s.client.messages.calls[0]["messages"][0]["content"]
    assert all(b["type"] == "text" for b in blocks)
    assert "lab.pdf" in blocks[-1]["text"]
    assert "disabled" in blocks[-1]["text"]


# --- failures --------------------------------------------------------------


def test_refusal_is_reported_not_silently_empty():
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    s.client.messages.create = lambda **kw: Response("", stop_reason="refusal")
    with pytest.raises(SummarizerError, match="declined"):
        s.summarize(_thread())


def test_empty_content_is_reported():
    s = AnthropicSummarizer.from_config(_cfg())
    s.client = StubClient(payload=VALID)
    s.client.messages.create = lambda **kw: Response("   ")
    with pytest.raises(SummarizerError, match="no text content"):
        s.summarize(_thread())


@pytest.mark.parametrize(
    "exc_name,expected",
    [
        ("AuthenticationError", "rejected the key"),
        ("PermissionDeniedError", "lacks permission"),
        ("NotFoundError", "Unknown model id"),
        ("RateLimitError", "Rate limited"),
        ("APITimeoutError", "timed out"),
        ("APIConnectionError", "Could not reach"),
    ],
)
def test_api_errors_map_to_actionable_messages(exc_name, expected):
    exc = type(exc_name, (Exception,), {})("boom")
    assert expected in str(wrap_api_errors(exc))


def test_unknown_error_keeps_its_type_name():
    assert "ValueError" in str(wrap_api_errors(ValueError("odd")))


def test_missing_api_key_is_an_actionable_error(monkeypatch):
    from oncallbot.config import ConfigError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError) as e:
        SummarizerConfig(backend="anthropic_api").api_key()
    assert "ANTHROPIC_API_KEY" in str(e.value)
    assert ".env" in str(e.value)


def test_custom_api_key_env_is_honoured(monkeypatch):
    monkeypatch.setenv("MY_TEAM_KEY", "sk-ant-test")
    assert SummarizerConfig(api_key_env="MY_TEAM_KEY").api_key() == "sk-ant-test"


# --- answer streaming and routing ------------------------------------------


class StreamCtx:
    def __init__(self, chunks): self.text_stream = iter(chunks)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_stream_answer_yields_text_chunks():
    class C:
        class messages:
            calls: list[dict] = []

            @staticmethod
            def stream(**kw):
                C.messages.calls.append(kw)
                return StreamCtx(["Top ", "3 issues"])

    assert list(stream_answer(_cfg(), "sys", "q", client=C())) == ["Top ", "3 issues"]
    assert C.messages.calls[0]["model"] == "claude-opus-5"


def test_complete_json_returns_the_text_block():
    c = StubClient(payload={"action": "fetch", "days": 2})
    out = complete_json(_cfg(), "sys", "prompt", client=c)
    assert json.loads(out)["action"] == "fetch"
    assert c.messages.calls[0]["output_config"]["effort"] == "low"


def test_build_summarizer_selects_the_api_backend(monkeypatch):
    from oncallbot.summarizer import build_summarizer

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(build_summarizer(_cfg()), AnthropicSummarizer)


def test_build_summarizer_rejects_an_unknown_backend():
    from oncallbot.summarizer import build_summarizer

    cfg = Config()
    cfg.summarizer = SummarizerConfig(backend="gpt")
    with pytest.raises(SummarizerError, match="claude_cli, anthropic_api"):
        build_summarizer(cfg)
