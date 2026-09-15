"""Build the Gmail search query from the configured matchers."""

from __future__ import annotations

from .config import GmailConfig


def build_query(cfg: GmailConfig) -> str:
    addr = cfg.support_address
    clauses: list[str] = []

    if cfg.match.recipients:
        clauses.append(
            " OR ".join(f"{op}:{addr}" for op in ("to", "cc", "bcc", "deliveredto"))
        )

    if cfg.match.anywhere:
        # A bare term hits headers, body, signatures and quoted replies.
        clauses.append(f'"{addr}"')

    for label in cfg.match.labels:
        clauses.append(f"label:{_quote_label(label)}")

    if not clauses:
        raise ValueError(
            "No matchers enabled. Set at least one of gmail.match.recipients, "
            "gmail.match.anywhere, or gmail.match.labels in config.yaml."
        )

    matcher = " OR ".join(f"({c})" for c in clauses)
    parts = [f"({matcher})"]

    # An explicit window wins; only fall back to a relative one when no
    # absolute bound was given, so an unparsed date can never silently become
    # "the last N days".
    if cfg.after or cfg.before:
        if cfg.after:
            parts.append(f"after:{_gmail_date(cfg.after)}")
        if cfg.before:
            parts.append(f"before:{_gmail_date(cfg.before)}")
    elif cfg.lookback_days > 0:
        parts.append(f"newer_than:{cfg.lookback_days}d")
    if cfg.extra_query.strip():
        parts.append(cfg.extra_query.strip())

    return " ".join(parts)


def _gmail_date(iso: str) -> str:
    """YYYY-MM-DD -> YYYY/MM/DD, the form Gmail search expects.

    Gmail reads a bare date in the mailbox owner's timezone, which is what a
    person means by "15th August".
    """
    return iso.replace("-", "/")


def _quote_label(label: str) -> str:
    return f'"{label}"' if " " in label else label
