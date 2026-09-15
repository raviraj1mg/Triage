"""Local-model backend, for keeping patient data on the machine.

Speaks the OpenAI-compatible `/v1/chat/completions` shape, which Ollama, LM
Studio, llama.cpp's server and vLLM all expose -- one implementation covers
every runtime the team is likely to reach for. This is NOT a shim for Claude:
`backend: anthropic_api` calls Anthropic's own SDK, and this path talks to
whatever model you are hosting yourself.

The reason to pick this is not cost, it is that nothing leaves the machine --
which is the single largest concern in docs/phi.md. The tradeoff is honest:
a 7-8B local model is noticeably weaker at the two judgements that matter
here, severity and identifier extraction, and cannot read PDF attachments at
all. Measure before trusting it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from .config import AttachmentConfig, Config, LocalModelConfig
from .gmail_client import GmailClient
from .models import EmailThread, IssueSummary
from .prompts.live import prompt_for
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .streaming import Cancelled, cancelled
from .summarizer import SummarizerError, _parse_json_object, _redacted_copy


def _summary_schema(categories: list[str]) -> dict[str, Any]:
    from .anthropic_backend import SUMMARY_SCHEMA

    schema = json.loads(json.dumps(SUMMARY_SCHEMA))
    schema["properties"]["category"] = {"type": "string", "enum": list(categories)}
    return schema


@dataclass
class LocalSummarizer:
    local: LocalModelConfig
    categories: tuple[str, ...] = ("other",)
    redaction_enabled: bool = True
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    gmail: GmailClient | None = None
    client: Any = None

    @classmethod
    def from_config(cls, cfg: Config, gmail: GmailClient | None = None) -> LocalSummarizer:
        return cls(
            local=cfg.local,
            categories=tuple(cfg.categories),
            redaction_enabled=cfg.redaction_enabled,
            attachments=cfg.attachments,
            gmail=gmail,
        )

    def _http(self) -> httpx.Client:
        if self.client is None:
            headers = {"Content-Type": "application/json"}
            key = self.local.api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self.client = httpx.Client(
                base_url=self.local.base_url.rstrip("/"),
                timeout=float(self.local.timeout_seconds),
                headers=headers,
            )
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    # --- transport ---------------------------------------------------------

    def _chat(self, system: str, user: str, schema: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {
            "model": self.local.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.local.temperature,
            "max_tokens": self.local.max_tokens,
            "stream": False,
        }
        if schema is not None:
            # Runtimes disagree on structured-output support, so try the strict
            # form and step down rather than failing outright. The brace parser
            # is the final net either way.
            attempts = [
                {"type": "json_schema",
                 "json_schema": {"name": "issue_summary", "schema": schema, "strict": True}},
                {"type": "json_object"},
                None,
            ]
        else:
            attempts = [None]

        last: Exception | None = None
        for fmt in attempts:
            payload = dict(body)
            if fmt is not None:
                payload["response_format"] = fmt
            try:
                resp = self._http().post("/v1/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise SummarizerError(
                    f"Could not reach the local model at {self.local.base_url} "
                    f"({type(exc).__name__}). Is it running? "
                    f"For Ollama: `ollama serve`, then `ollama pull {self.local.model}`."
                ) from exc

            if resp.status_code == 404:
                raise SummarizerError(
                    f"{self.local.base_url} has no /v1/chat/completions endpoint. "
                    "Point local.base_url at an OpenAI-compatible server "
                    "(Ollama, LM Studio, llama.cpp --api, vLLM)."
                )
            if resp.status_code == 400 and fmt is not None:
                last = SummarizerError(resp.text[:200])
                continue  # step down to a looser response_format
            if resp.status_code >= 400:
                raise SummarizerError(
                    f"The local model returned {resp.status_code}: {resp.text.strip()[:220]}"
                )

            try:
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise SummarizerError(
                    f"Unexpected response shape from the local model: "
                    f"{resp.text.strip()[:220]}"
                ) from exc

        raise SummarizerError(
            f"The local model rejected every response_format we tried. Last error: {last}"
        )

    # --- Summarizer protocol ----------------------------------------------

    def summarize(self, thread: EmailThread) -> IssueSummary:
        prepared = _redacted_copy(thread) if self.redaction_enabled else thread

        # Local runtimes are rarely multimodal and cannot read PDFs, so name
        # the attachments rather than pretending they were considered.
        present = [a for m in thread.messages for a in m.attachments]
        skipped = [
            (a.filename, "the local model backend cannot read attachments")
            for a in present
        ]

        prompt = build_user_prompt(
            prepared,
            list(self.categories),
            self.local.max_body_chars,
            skipped_attachments=skipped,
        )
        text = self._chat(prompt_for("summarize", SYSTEM_PROMPT), prompt, _summary_schema(list(self.categories)))
        if not text.strip():
            raise SummarizerError("The local model returned an empty response.")
        return IssueSummary.from_model_output(
            _parse_json_object(text), thread, f"local:{self.local.model}"
        )


def stream_answer(cfg: Config, system_prompt: str, prompt: str) -> Iterator[str]:
    """Token stream for the chat 'answer' action, via a local model."""
    s = LocalSummarizer.from_config(cfg)
    body = {
        "model": cfg.local.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": cfg.local.temperature,
        "max_tokens": cfg.local.max_tokens,
        "stream": True,
    }
    try:
        with s._http().stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise SummarizerError(
                    f"The local model returned {resp.status_code}: {resp.text[:200]}"
                )
            for line in resp.iter_lines():
                if cancelled():
                    raise Cancelled("stopped")
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    yield delta
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise SummarizerError(
            f"Lost the local model at {cfg.local.base_url}: {exc}"
        ) from exc
    finally:
        s.close()


def complete_json(cfg: Config, system_prompt: str, prompt: str) -> str:
    """One non-streaming call that must return JSON. Used for intent routing."""
    s = LocalSummarizer.from_config(cfg)
    try:
        return s._chat(system_prompt, prompt, {"type": "object"})
    finally:
        s.close()
