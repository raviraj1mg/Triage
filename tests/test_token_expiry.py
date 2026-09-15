"""An expired admin token asks for a new one, rather than showing a 400.

The reported case: a token that had been working expired, and the order
lookup answered with the raw body --

  Nothing came back for that order. get_user_details_for_an_order:
  GET /users/order/PO10003572851-596 returned 400:
  {"error":{"message":"Invalid authorization token : ", ...

Two separate faults. The service answers 400 rather than 401 for a dead
bearer, so it was never classified as an auth failure; and an auth failure was
reported as an error anyway instead of opening the paste box.
"""

import httpx
import pytest

from oncallbot.config import HraConfig
from oncallbot.hra_client import (
    HraAuthError,
    HraBlockedError,
    HraClient,
    HraError,
    _auth_failure,
    _message_of,
)

# Verbatim from the reported failure.
BODY_400 = (
    '{"error":{"message":"Invalid authorization token : ","errors":'
    '[{"message":"Invalid authorization token : "}]},"is_success":false,'
    '"status_code":400,"sentry_raise":false}'
)


def _client(handler) -> HraClient:
    cfg = HraConfig()
    cfg.session_token = "dead-token"
    c = HraClient(cfg)
    c.client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://x/hra"
    )
    return c


# --- classification --------------------------------------------------------


def test_the_reported_body_is_recognised_as_an_auth_failure():
    assert _auth_failure(BODY_400)


def test_a_plain_bad_request_is_not_an_auth_failure():
    """Otherwise every 400 would ask for a new token."""
    assert not _auth_failure('{"error":{"message":"order not found"}}')
    assert not _auth_failure('{"error":{"message":"patient_id is required"}}')


def test_the_sentence_is_pulled_out_of_the_body():
    """The reader was shown braces, is_success and sentry_raise for what is a
    one-line problem."""
    assert _message_of(BODY_400) == "Invalid authorization token"


def test_a_non_json_body_still_yields_something_readable():
    assert _message_of("gateway timeout") == "gateway timeout"


# --- the client ------------------------------------------------------------


def test_a_400_with_an_auth_body_raises_an_auth_error():
    c = _client(lambda req: httpx.Response(400, text=BODY_400))
    with pytest.raises(HraAuthError) as e:
        c.get("/users/order/PO1-1")
    assert "expired or was rejected" in str(e.value) or "rejected the token" in str(e.value)


def test_a_400_that_is_not_about_the_token_stays_a_plain_error():
    c = _client(lambda req: httpx.Response(400, text='{"error":{"message":"no such order"}}'))
    with pytest.raises(HraError) as e:
        c.get("/users/order/PO1-1")
    assert not isinstance(e.value, HraAuthError)


def test_the_signing_post_classifies_the_same_way():
    c = _client(lambda req: httpx.Response(400, text=BODY_400))
    with pytest.raises(HraAuthError):
        c.post_read("/presigned-url", {"url": "https://x/a.pdf"})


def test_an_edge_block_is_not_reported_as_a_bad_token():
    """A Cloudflare block never reached the service, so the token was never
    checked and pasting a new one changes nothing."""
    c = _client(lambda req: httpx.Response(
        403, text="<html>Attention Required! | Cloudflare Ray ID: 1</html>"))
    with pytest.raises(HraBlockedError):
        c.get("/users/order/PO1-1")


# --- the message shown -----------------------------------------------------


def test_the_rejected_message_does_not_claim_the_token_is_absent():
    """"No admin API token" would read as though their paste had been lost."""
    msg = HraConfig().rejected_message("Invalid authorization token")
    assert "expired or was rejected" in msg
    assert "No admin API token" not in msg
    assert "accessToken" in msg
