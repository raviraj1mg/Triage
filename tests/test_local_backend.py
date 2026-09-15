"""Local-model backend. Verified against a mock transport: no server here."""

from __future__ import annotations

import json

import httpx
import pytest

from oncallbot.config import Config, LocalModelConfig
from oncallbot.local_backend import LocalSummarizer, complete_json, stream_answer
from oncallbot.models import Attachment, EmailMessage, EmailThread
from oncallbot.summarizer import SummarizerError, build_summarizer

VALID = {
    "reporter": "Asha <asha@x.com>", "summary": "s", "issue": "i",
    "category": "upload_failure", "severity": "p1", "asks": [], "missing_info": [],
    "affected_entities": {"patient_ids": [], "order_ids": ["PO1"],
                          "prescription_ids": [], "record_ids": [],
                          "user_emails": [], "other_ids": []},
    "suggested_owner": "", "confidence": 0.7,
}


def _cfg(**over) -> Config:
    c = Config()
    c.summarizer.backend = "local"
    c.local = LocalModelConfig(**over)
    c.categories = ["upload_failure", "other"]
    return c


def _thread(*atts) -> EmailThread:
    m = EmailMessage(
        id="m1", thread_id="t1", date=None, sender="Asha <asha@x.com>",
        to="health-record-support@1mg.com", cc="", subject="Upload fails",
        body_text="Fails at 90%. Call 9876543210.", snippet="",
        attachments=list(atts),
    )
    return EmailThread(id="t1", subject=m.subject, messages=[m])


def _mock(handler, cfg=None):
    cfg = cfg or _cfg()
    s = LocalSummarizer.from_config(cfg)
    s.client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=cfg.local.base_url
    )
    return s


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --- request shape ---------------------------------------------------------


def test_posts_openai_chat_completions_with_the_schema():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return _ok(json.dumps(VALID))

    _mock(handler).summarize(_thread())

    assert seen["path"] == "/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is False
    assert body["temperature"] == 0.0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"]["properties"]["category"]["enum"] == [
        "upload_failure", "other"
    ]


def test_steps_down_when_the_runtime_rejects_json_schema():
    """Many local runtimes only support {"type": "json_object"}."""
    tried = []

    def handler(request):
        fmt = json.loads(request.content).get("response_format")
        tried.append(fmt["type"] if fmt else None)
        if fmt and fmt["type"] == "json_schema":
            return httpx.Response(400, text="response_format.json_schema unsupported")
        return _ok(json.dumps(VALID))

    out = _mock(handler).summarize(_thread())
    assert tried == ["json_schema", "json_object"]
    assert out.severity == "p1"


def test_falls_all_the_way_back_to_no_response_format():
    tried = []

    def handler(request):
        fmt = json.loads(request.content).get("response_format")
        tried.append(fmt["type"] if fmt else None)
        if fmt:
            return httpx.Response(400, text="unsupported")
        return _ok("Here you go:\n" + json.dumps(VALID))

    out = _mock(handler).summarize(_thread())
    assert tried == ["json_schema", "json_object", None]
    assert out.category == "upload_failure", "prose-wrapped JSON is still salvaged"


def test_redacts_before_sending_to_the_local_model():
    """Local means on-machine, but the masking rule still applies uniformly."""
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return _ok(json.dumps(VALID))

    _mock(handler).summarize(_thread())
    assert "9876543210" not in seen["body"]


def test_attachments_are_named_as_unreadable_not_silently_dropped():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _ok(json.dumps(VALID))

    _mock(handler).summarize(_thread(Attachment("lab.pdf", "application/pdf", 9, "a", "m1")))
    user = seen["body"]["messages"][1]["content"]
    assert "lab.pdf" in user
    assert "cannot read attachments" in user
    assert "Do not guess at their contents" in user


def test_model_id_records_that_it_was_local():
    out = _mock(lambda r: _ok(json.dumps(VALID))).summarize(_thread())
    assert out.model == "local:llama3.1:8b"


# --- failures speak in fixable terms ---------------------------------------


def test_a_dead_server_names_the_fix():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(SummarizerError) as e:
        _mock(handler).summarize(_thread())
    assert "ollama serve" in str(e.value)
    assert "localhost:11434" in str(e.value)


def test_a_wrong_base_url_says_so():
    def handler(request):
        return httpx.Response(404, text="not found")

    with pytest.raises(SummarizerError, match="OpenAI-compatible"):
        _mock(handler).summarize(_thread())


def test_an_empty_completion_is_reported():
    with pytest.raises(SummarizerError, match="empty response"):
        _mock(lambda r: _ok("")).summarize(_thread())


def test_an_unexpected_response_shape_is_reported():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(SummarizerError, match="Unexpected response shape"):
        _mock(handler).summarize(_thread())


def test_an_api_key_is_sent_only_when_configured(monkeypatch):
    monkeypatch.setenv("LOCAL_KEY", "sk-local")
    cfg = _cfg(api_key_env="LOCAL_KEY")
    s = LocalSummarizer.from_config(cfg)
    assert s._http().headers["Authorization"] == "Bearer sk-local"

    plain = LocalSummarizer.from_config(_cfg())
    assert "Authorization" not in plain._http().headers


# --- streaming and routing -------------------------------------------------


def test_stream_answer_parses_sse_deltas(monkeypatch):
    chunks = [
        'data: {"choices":[{"delta":{"content":"Top "}}]}',
        'data: {"choices":[{"delta":{"content":"3 issues"}}]}',
        "data: [DONE]",
    ]

    def handler(request):
        return httpx.Response(200, text="\n".join(chunks))

    cfg = _cfg()
    real = LocalSummarizer.from_config

    def patched(c, gmail=None):
        s = real(c, gmail)
        s.client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url=c.local.base_url
        )
        return s

    monkeypatch.setattr(LocalSummarizer, "from_config", staticmethod(patched))
    assert list(stream_answer(cfg, "sys", "q")) == ["Top ", "3 issues"]


def test_complete_json_returns_content(monkeypatch):
    def handler(request):
        return _ok('{"action": "fetch", "days": 2}')

    cfg = _cfg()
    real = LocalSummarizer.from_config

    def patched(c, gmail=None):
        s = real(c, gmail)
        s.client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url=c.local.base_url
        )
        return s

    monkeypatch.setattr(LocalSummarizer, "from_config", staticmethod(patched))
    assert json.loads(complete_json(cfg, "sys", "prompt"))["action"] == "fetch"


def test_build_summarizer_selects_the_local_backend():
    assert isinstance(build_summarizer(_cfg()), LocalSummarizer)


def test_config_accepts_local_as_a_backend(tmp_path):
    from oncallbot.config import load_config

    f = tmp_path / "config.yaml"
    f.write_text(
        "summarizer:\n  backend: local\n"
        "local:\n  base_url: http://127.0.0.1:1234\n  model: qwen2.5:14b\n"
    )
    cfg = load_config(f)
    assert cfg.summarizer.backend == "local"
    assert cfg.local.base_url == "http://127.0.0.1:1234"
    assert cfg.local.model == "qwen2.5:14b"
