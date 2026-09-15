"""Read-only client for the unified-admin dashboard proxy.

Paths mirror what the dashboard itself calls (`unified-admin-ui/healthrecords/
src/store/actions/actions.ts`), because those are known-working through the
`/hr_admin_service` rewrite. Do not "simplify" them to the underlying
`/health-record/on-call` routes without checking the rewrite first.

Auth is the operator's own dashboard session token, so this client carries
their full admin authority -- not a narrow read-only credential. Every call is
logged locally for that reason, and only GETs live here: writes belong to
phase 3, behind an allowlist.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import HraConfig

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class HraError(RuntimeError):
    """A call failed in a way the operator may be able to act on."""


class HraAuthError(HraError):
    """The dashboard token is missing, expired or rejected.

    Kept distinct because a stale token must never be reported as an
    inconclusive diagnosis -- those look identical to the reader.

    `a_new_token_would_help` separates the two kinds. An expired bearer is
    fixed by pasting a fresh one; a 403 is the service saying this account
    lacks the role, and asking for another token from the same dashboard
    sends the reader round a loop that cannot end.
    """

    def __init__(self, message: str, *, a_new_token_would_help: bool = True,
                 detail: str = "") -> None:
        super().__init__(message)
        self.a_new_token_would_help = a_new_token_would_help
        # Short enough to sit in a sentence, unlike the whole message.
        self.detail = detail


# This service answers 400 -- not 401 -- when the bearer is expired or
# malformed:
#   {"error":{"message":"Invalid authorization token : "},"status_code":400}
# so the status code alone cannot tell an expired token from a bad request,
# and the operator was shown a raw 400 body instead of being asked for a new
# token. The body is what actually says which it is.
_AUTH_BODY_MARKERS = (
    "authorization token",
    "invalid token",
    "token expired",
    "token has expired",
    "unauthorized",
    "unauthenticated",
)


def _message_of(text: str) -> str:
    """The human sentence out of an error body, not the raw JSON.

    The reader was being shown the whole payload -- braces, `is_success`,
    `sentry_raise` and all -- for what is a one-line problem.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return (text or "").strip()[:120]
    if isinstance(data, dict):
        err = data.get("error")
        for candidate in (err.get("message") if isinstance(err, dict) else None,
                          data.get("message"),
                          err if isinstance(err, str) else None):
            if isinstance(candidate, str) and candidate.strip():
                # The service sends "Invalid authorization token : " -- the
                # trailing separator is not part of the sentence.
                return candidate.strip().rstrip(":").strip()[:120]
    return (text or "").strip()[:120]


def _auth_failure(text: str) -> bool:
    """Does this error body say the token is the problem?"""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _AUTH_BODY_MARKERS)


class HraBlockedError(HraError):
    """An edge proxy refused the request before it reached the service.

    A Cloudflare IP block also answers 403, and telling the operator to
    re-copy a perfectly good token wastes their time. The two are separated on
    the response body, not the status code.
    """


@dataclass
class HraClient:
    cfg: HraConfig
    client: Any = None

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.cfg.base_url + self.cfg.path_prefix,
                timeout=float(self.cfg.timeout_seconds),
                headers={
                    # Accept a token pasted either bare or already carrying the
                    # scheme — copying from devtools yields both forms.
                    "Authorization": _bearer(self.cfg.token()),
                    "x-access-key": self.cfg.access_key,
                    "Accept": "application/json",
                },
                follow_redirects=True,
            )
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    # --- transport ---------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        last: Exception | None = None

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            logger.info("hra GET %s params=%s attempt=%d", path, params, attempt)
            try:
                resp = self._http().get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt == _RETRY_ATTEMPTS:
                    raise HraError(
                        f"Could not reach the admin API ({type(exc).__name__}). "
                        "Are you on the network/VPN that serves "
                        f"{self.cfg.base_url}?"
                    ) from exc
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue

            blocked = _edge_block(resp)
            if blocked:
                raise HraBlockedError(
                    f"Blocked at the edge before reaching the service: {blocked}. "
                    "Your token was never checked. This is usually a network "
                    "problem — connect to the office VPN, or ask whoever owns "
                    f"the {self.cfg.base_url} Cloudflare rules to allow your IP."
                )
            if resp.status_code == 401:
                raise HraAuthError(
                    "The admin API rejected the token (401). Dashboard tokens "
                    "expire — open the dashboard, copy a fresh `accessToken` "
                    f"from localStorage, and update ${self.cfg.token_env} in .env.",
                    detail="401: the token was rejected",
                )
            if resp.status_code == 403:
                raise HraAuthError(
                    "The admin API accepted the request but refused it (403). "
                    "The token is reaching the service and is not expired — it "
                    "lacks permission for this endpoint, so check the role on "
                    "the account you copied it from.",
                    a_new_token_would_help=False,
                )
            if resp.status_code in _TRANSIENT_STATUSES and attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            if resp.status_code >= 400 and _auth_failure(resp.text):
                raise HraAuthError(
                    f"The admin API rejected the token "
                    f"({resp.status_code}: {_message_of(resp.text)}). Dashboard "
                    "tokens expire after about ten hours.",
                    detail=f"{resp.status_code}: {_message_of(resp.text)}",
                )
            if resp.status_code >= 400:
                raise HraError(
                    f"GET {path} returned {resp.status_code}: "
                    f"{resp.text.strip()[:220]}"
                )

            try:
                body = resp.json()
            except json.JSONDecodeError as exc:
                raise HraError(
                    f"GET {path} returned {resp.status_code} but not JSON: "
                    f"{resp.text.strip()[:180]}"
                ) from exc
            return _unwrap(body)

        raise HraError(f"GET {path} failed: {last}")

    def post_read(self, path: str, body: dict[str, Any]) -> Any:
        """POST for the one endpoint that reads rather than mutates.

        Deliberately narrow: there is no general `post`, so a write endpoint
        cannot be reached through this client at all.
        """
        if path.rstrip("/") != "/presigned-url":
            raise HraError(
                f"post_read refuses {path}. Only /presigned-url is a POST that "
                "does not mutate; writes belong to the resolve phase."
            )
        try:
            resp = self._http().post(path, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HraError(f"Could not reach the admin API: {exc}") from exc
        blocked = _edge_block(resp)
        if blocked:
            raise HraBlockedError(f"Blocked at the edge: {blocked}.")
        if resp.status_code == 401:
            raise HraAuthError(
                "The admin API rejected the token (401). Re-copy `accessToken` "
                f"and update ${self.cfg.token_env} in .env."
            )
        if resp.status_code >= 400 and _auth_failure(resp.text):
            raise HraAuthError(
                f"The admin API rejected the token "
                f"({resp.status_code}: {_message_of(resp.text)}). Dashboard "
                "tokens expire after about ten hours.",
                detail=f"{resp.status_code}: {_message_of(resp.text)}",
            )
        if resp.status_code >= 400:
            raise HraError(f"POST {path} returned {resp.status_code}: {resp.text[:200]}")
        return _unwrap(resp.json())

    # --- the five reads phase 2 needs -------------------------------------

    def user_details(self, order_group_id: str) -> dict[str, Any]:
        """order_group_id -> {user_id, patient_details[]}."""
        return self.get(f"/users/order/{order_group_id}")

    def orders(self, user_id: str, order_group_id: str | None = None) -> Any:
        """Bookings for a user, narrowed to one order group. Carries booking_id
        and the booking's own patient_id."""
        return self.get(f"/user/{user_id}/orders", {"order_group_id": order_group_id})

    def json_report_url(self, booking_id: str, order_group_id: str) -> dict[str, Any]:
        """-> {json_url: presigned}."""
        return self.get(
            f"/booking/{booking_id}/json-report", {"order_group_id": order_group_id}
        )

    def patient_versions(self, patient_id: str) -> dict[str, Any]:
        """Change history for a patient; `object` / `object_changes` are parsed."""
        return self.get(f"/patient/{patient_id}/versions")

    def diagnostic_data(
        self, order_group_id: str, patient_id: str, booking_id: str | None = None
    ) -> dict[str, Any]:
        return self.get(
            f"/order/{order_group_id}/bookings/diagnostic",
            {"patient_id": patient_id, "booking_id": booking_id},
        )

    # --- the report itself -------------------------------------------------

    def fetch_json_report(self, presigned_url: str) -> Any:
        """Fetch a presigned report. Deliberately a bare client: the presigned
        URL carries its own auth and must not receive our bearer token."""
        try:
            with httpx.Client(timeout=float(self.cfg.timeout_seconds)) as bare:
                resp = bare.get(presigned_url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HraError(f"Could not fetch the JSON report: {exc}") from exc
        if resp.status_code >= 400:
            raise HraError(
                f"The JSON report URL returned {resp.status_code}. Presigned "
                "URLs expire — re-run the diagnosis to get a fresh one."
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise HraError("The report URL did not return JSON.") from exc


def _edge_block(resp: httpx.Response) -> str:
    """Name the edge that refused us, or "" when the service itself answered."""
    if resp.status_code not in (403, 503):
        return ""
    server = (resp.headers.get("server") or "").lower()
    body = resp.text[:600].lower()
    if "cloudflare" in server or "cloudflare" in body:
        if "error 1106" in body or "blocked your ip" in body:
            return "Cloudflare 1106, IP not allowed"
        return f"Cloudflare {resp.status_code}"
    if "akamai" in server or "x-akamai-request-id" in resp.headers:
        return f"Akamai {resp.status_code}"
    # An HTML body from a 403 is an edge, not a JSON API.
    if "text/html" in (resp.headers.get("content-type") or ""):
        return f"an HTML {resp.status_code} from {server or 'an intermediary'}"
    return ""


def _bearer(token: str) -> str:
    token = token.strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def _unwrap(body: Any) -> Any:
    """Torpedo wraps successful responses in {"data": ...}."""
    if isinstance(body, dict) and "data" in body and len(body) <= 3:
        return body["data"]
    return body
