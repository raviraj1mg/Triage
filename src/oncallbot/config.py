"""Config loading. YAML on disk, dataclasses in memory."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")

# Any setting can be overridden without editing the YAML, which is what a
# teammate running from a shared zip wants: one file to touch, or nothing at
# all if they prefer environment variables.
ENV_PREFIX = "ONCALLBOT_"
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "ONCALLBOT_GMAIL_ACCOUNT": ("gmail", "account"),
    "ONCALLBOT_SUPPORT_ADDRESS": ("gmail", "support_address"),
    "ONCALLBOT_LOOKBACK_DAYS": ("gmail", "lookback_days"),
    "ONCALLBOT_MAX_THREADS": ("gmail", "max_threads"),
    "ONCALLBOT_CREDENTIALS_FILE": ("gmail", "credentials_file"),
    "ONCALLBOT_TOKEN_FILE": ("gmail", "token_file"),
    "ONCALLBOT_BACKEND": ("summarizer", "backend"),
    "ONCALLBOT_MODEL": ("summarizer", "model"),
    "ONCALLBOT_EFFORT": ("summarizer", "effort"),
    "ONCALLBOT_API_KEY_ENV": ("summarizer", "api_key_env"),
    "ONCALLBOT_ATTACHMENTS_ENABLED": ("attachments", "enabled"),
    "ONCALLBOT_REDACTION_ENABLED": ("redaction", "enabled"),
    "ONCALLBOT_STORE_PATH": ("store", "path"),
    "ONCALLBOT_LOCAL_BASE_URL": ("local", "base_url"),
    "ONCALLBOT_LOCAL_MODEL": ("local", "model"),
    "ONCALLBOT_HRA_BASE_URL": ("hra", "base_url"),
    "ONCALLBOT_HRA_TOKEN_ENV": ("hra", "token_env"),
}


class ConfigError(RuntimeError):
    """A configuration problem the operator can fix, phrased so they can."""


def load_dotenv(path: Path | None = None) -> list[str]:
    """Read KEY=VALUE lines into the environment. Existing values win.

    Deliberately tiny and dependency-free: a shared zip should work after
    editing one file, without a pip install anyone has to remember.
    """
    path = path or DEFAULT_ENV_PATH
    if not path.exists():
        return []
    loaded: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _apply_env_overrides(raw: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    for env_name, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        raw.setdefault(section, {})
        if not isinstance(raw[section], dict):
            raw[section] = {}
        raw[section][key] = _coerce(value)
        applied.append(env_name)
    return applied


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "yes", "1", "on"):
        return True
    if low in ("false", "no", "0", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        return value


@dataclass
class MatchConfig:
    recipients: bool = True
    anywhere: bool = False
    labels: list[str] = field(default_factory=list)


@dataclass
class GmailConfig:
    support_address: str = "health-record-support@1mg.com"
    # The mailbox whose mail we read. Used as the OAuth login_hint, and checked
    # against the account that actually consented.
    account: str = ""
    match: MatchConfig = field(default_factory=MatchConfig)
    extra_query: str = ""
    lookback_days: int = 7
    # Absolute window, as YYYY-MM-DD. When set, these win over lookback_days.
    after: str = ""
    before: str = ""
    max_threads: int = 50
    include_spam_trash: bool = False
    credentials_file: Path = Path(".secrets/oauth_client.json")
    token_file: Path = Path(".secrets/token.json")


@dataclass
class HraConfig:
    """The unified-admin dashboard proxy.

    oncallbot mirrors the dashboard's own URLs, so whatever /hr_admin_service
    rewrites to internally stays irrelevant -- those paths are known-working.
    """

    base_url: str = "https://unifiedadmin.1mg.com/hra"
    path_prefix: str = "/hr_admin_service/v1/health-record/admin"
    token_env: str = "ONCALLBOT_HRA_TOKEN"
    access_key: str = "1mg_client_access_key"
    timeout_seconds: int = 30

    # Set per session when a logged-in user pastes their own bearer.
    session_token: str = ""
    # True once this config belongs to a logged-in person: the operator's
    # environment token is then ignored, so an admin API call is always made
    # with the bearer of whoever is asking. Without this, every user would
    # silently borrow whoever started the server.
    session_only: bool = False

    def token_value(self, required: bool = True) -> str:
        """The bearer to use, or "" when there is none and none is demanded."""
        tok = self.session_token.strip()
        if not tok and not self.session_only:
            tok = os.environ.get(self.token_env, "").strip()
        if not tok and required:
            raise ConfigError(self.missing_message())
        return tok

    def missing_message(self) -> str:
        return (
            "No admin API token, so diagnosis cannot reach the admin APIs.\n"
            "Open the unified-admin dashboard, copy `accessToken` from "
            "localStorage (DevTools > Application > Local Storage), and paste "
            "it into oncallbot."
        )

    def rejected_message(self, detail: str = "") -> str:
        """Asked for again because the one we had was refused.

        Distinct from missing_message: the reader pasted a token and it
        worked, so "no admin API token" would read as though their paste was
        lost. The dashboard mints these for about ten hours.
        """
        why = f" ({detail})" if detail else ""
        return (
            f"The admin API token has expired or was rejected{why}.\n"
            "Open the unified-admin dashboard, copy a fresh `accessToken` from "
            "localStorage (DevTools > Application > Local Storage), and paste "
            "it in to carry on."
        )

    def token(self) -> str:
        tok = self.token_value(required=False)
        if not tok:
            raise ConfigError(
                f"${self.token_env} is not set, so diagnosis cannot reach the "
                "admin APIs.\n"
                "Open the unified-admin dashboard, copy `accessToken` from "
                "localStorage (DevTools > Application > Local Storage), and put "
                f"it in .env as {self.token_env}=<token>."
            )
        return tok


@dataclass
class LocalModelConfig:
    """A self-hosted model over the OpenAI-compatible chat-completions shape.

    Ollama, LM Studio, llama.cpp --api and vLLM all speak it. The point is
    that nothing leaves the machine; see docs/phi.md.
    """

    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    # Optional. Most local servers need no key; vLLM behind a gateway might.
    api_key_env: str = ""
    temperature: float = 0.0
    max_tokens: int = 4096
    max_body_chars: int = 8000
    timeout_seconds: int = 300

    def api_key(self) -> str:
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "").strip()


@dataclass
class AttachmentConfig:
    """Attachment content cannot be redacted, so every limit here is a control."""

    enabled: bool = True
    max_per_thread: int = 5
    max_bytes_each: int = 5 * 1024 * 1024
    max_total_bytes: int = 15 * 1024 * 1024


# Friendly aliases so config.yaml can say "opus" rather than a versioned id.
# Anything containing "claude-" is passed through untouched.
API_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}

BACKENDS = ("claude_cli", "anthropic_api", "local")

# The port `serve` binds and `doctor` quotes in the redirect URI. One constant,
# so the two cannot drift apart -- a redirect URI that names the wrong port is
# a sign-in failure with a confusing message.
DEFAULT_PORT = 8765


@dataclass
class SummarizerConfig:
    backend: str = "claude_cli"
    model: str = "opus"
    timeout_seconds: int = 180
    max_body_chars: int = 12000
    # anthropic_api only. The key is read from this environment variable and is
    # never stored in config.yaml.
    api_key_env: str = "ANTHROPIC_API_KEY"
    effort: str = "medium"

    @property
    def api_model(self) -> str:
        """The concrete model id to send to the API."""
        if "claude-" in self.model:
            return self.model
        return API_MODEL_ALIASES.get(self.model.lower(), self.model)

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ConfigError(
                f"summarizer.backend is 'anthropic_api' but ${self.api_key_env} is "
                f"not set.\nPut it in a .env file beside config.yaml as "
                f"{self.api_key_env}=sk-ant-... , or export it in your shell.\n"
                f"Alternatively set summarizer.backend: claude_cli to use the "
                f"Claude Code CLI instead of an API key."
            )
        return key


@dataclass
class AuthConfig:
    """Browser login. Every session reads Gmail as the person who consented.

    `allowed_domain` is the only membership check available without Directory
    API scopes: an @1mg.com colleague who is not on the support group signs in
    fine and sees zero threads, because Google cannot tell us they are not a
    member -- the mailbox search simply returns nothing.
    """

    enabled: bool = True
    allowed_domain: str = "1mg.com"
    # Optional tighter allowlist. Empty means "anyone in allowed_domain".
    allowed_emails: list[str] = field(default_factory=list)
    # Where Google sends the browser back. The port is substituted at serve
    # time; a path other than the one registered on the OAuth client is what
    # redirect_uri_mismatch means.
    redirect_path: str = "/auth/callback"
    # Idle and absolute session lifetimes.
    idle_hours: int = 12
    max_hours: int = 168
    # Per-user state, derived from the address that consented.
    tokens_dir: Path = Path(".secrets/tokens")
    stores_dir: Path = Path(".data/users")

    def permits(self, email: str) -> bool:
        addr = (email or "").strip().lower()
        if not addr or "@" not in addr:
            return False
        if self.allowed_emails:
            return addr in {e.strip().lower() for e in self.allowed_emails}
        return addr.endswith("@" + self.allowed_domain.strip().lower().lstrip("@"))


@dataclass
class Config:
    gmail: GmailConfig = field(default_factory=GmailConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    categories: list[str] = field(default_factory=lambda: ["other"])
    redaction_enabled: bool = True
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    hra: HraConfig = field(default_factory=HraConfig)
    local: LocalModelConfig = field(default_factory=LocalModelConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    store_path: Path = Path(".data/oncallbot.sqlite3")

    def for_user(self, email: str, group_email: str = "") -> Config:
        """This config as it applies to one logged-in person.

        Their own token, their own summary cache, and the group mailbox they
        asked for. Copied rather than mutated: one Config per request, so two
        users in flight cannot read each other's settings.
        """
        import hashlib
        from copy import deepcopy

        c = deepcopy(self)
        addr = (email or "").strip().lower()
        if addr:
            slug = hashlib.sha256(addr.encode()).hexdigest()[:16]
            c.gmail.account = addr
            c.gmail.token_file = self.auth.tokens_dir / f"{slug}.json"
            # A separate file, not a shared table: isolation you can verify
            # with ls, and no migration of the existing store.
            c.store_path = self.auth.stores_dir / f"{slug}.sqlite3"
        if group_email.strip():
            c.gmail.support_address = group_email.strip()
        # Whoever is logged in uses their own admin bearer, or none.
        c.hra.session_only = True
        c.hra.session_token = ""
        return c


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    load_dotenv(path.parent / ".env" if path.parent != Path("") else DEFAULT_ENV_PATH)

    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to {path} and edit it "
            "(see README.md → Setup)."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping.")
    _apply_env_overrides(raw)

    g = raw.get("gmail", {})
    m = g.get("match", {})
    gmail = GmailConfig(
        support_address=g.get("support_address", GmailConfig.support_address),
        account=g.get("account", ""),
        match=MatchConfig(
            recipients=m.get("recipients", True),
            anywhere=m.get("anywhere", False),
            labels=list(m.get("labels") or []),
        ),
        extra_query=g.get("extra_query", ""),
        lookback_days=int(g.get("lookback_days", 7)),
        max_threads=int(g.get("max_threads", 50)),
        include_spam_trash=bool(g.get("include_spam_trash", False)),
        credentials_file=Path(g.get("credentials_file", ".secrets/oauth_client.json")),
        token_file=Path(g.get("token_file", ".secrets/token.json")),
    )

    s = raw.get("summarizer", {})
    backend = str(s.get("backend", "claude_cli"))
    if backend not in BACKENDS:
        raise ConfigError(
            f"summarizer.backend is {backend!r}; expected one of {', '.join(BACKENDS)}."
        )
    summarizer = SummarizerConfig(
        backend=backend,
        model=str(s.get("model", "opus")),
        timeout_seconds=int(s.get("timeout_seconds", 180)),
        max_body_chars=int(s.get("max_body_chars", 12000)),
        api_key_env=str(s.get("api_key_env", "ANTHROPIC_API_KEY")),
        effort=str(s.get("effort", "medium")),
    )

    a = raw.get("attachments", {})
    attachments = AttachmentConfig(
        enabled=bool(a.get("enabled", True)),
        max_per_thread=int(a.get("max_per_thread", 5)),
        max_bytes_each=int(a.get("max_bytes_each", 5 * 1024 * 1024)),
        max_total_bytes=int(a.get("max_total_bytes", 15 * 1024 * 1024)),
    )

    lo = raw.get("local", {})
    local = LocalModelConfig(
        base_url=str(lo.get("base_url", LocalModelConfig.base_url)),
        model=str(lo.get("model", LocalModelConfig.model)),
        api_key_env=str(lo.get("api_key_env", "")),
        temperature=float(lo.get("temperature", 0.0)),
        max_tokens=int(lo.get("max_tokens", 4096)),
        max_body_chars=int(lo.get("max_body_chars", 8000)),
        timeout_seconds=int(lo.get("timeout_seconds", 300)),
    )

    h = raw.get("hra", {})
    hra = HraConfig(
        base_url=str(h.get("base_url", HraConfig.base_url)).rstrip("/"),
        path_prefix=str(h.get("path_prefix", HraConfig.path_prefix)),
        token_env=str(h.get("token_env", "ONCALLBOT_HRA_TOKEN")),
        access_key=str(h.get("access_key", "1mg_client_access_key")),
        timeout_seconds=int(h.get("timeout_seconds", 30)),
    )

    au = raw.get("auth") or {}
    auth = AuthConfig(
        enabled=bool(au.get("enabled", True)),
        allowed_domain=str(au.get("allowed_domain", "1mg.com")),
        allowed_emails=[str(e) for e in (au.get("allowed_emails") or [])],
        redirect_path=str(au.get("redirect_path", "/auth/callback")),
        idle_hours=int(au.get("idle_hours", 12)),
        max_hours=int(au.get("max_hours", 168)),
        tokens_dir=Path(au.get("tokens_dir", ".secrets/tokens")),
        stores_dir=Path(au.get("stores_dir", ".data/users")),
    )

    return Config(
        gmail=gmail,
        summarizer=summarizer,
        attachments=attachments,
        hra=hra,
        local=local,
        auth=auth,
        categories=list(raw.get("categories") or ["other"]),
        redaction_enabled=bool((raw.get("redaction") or {}).get("enabled", True)),
        store_path=Path((raw.get("store") or {}).get("path", ".data/oncallbot.sqlite3")),
    )
