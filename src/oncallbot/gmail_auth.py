"""OAuth desktop flow. One browser consent, then a cached refresh token."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read-only for now. Phase 2 (labelling, replying, auto-fix) will need
# gmail.modify -- adding a scope invalidates the cached token, so the user
# re-consents at that point.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class WrongAccountError(RuntimeError):
    pass


def get_credentials(
    credentials_file: Path,
    token_file: Path,
    *,
    interactive: bool = True,
    login_hint: str = "",
) -> Credentials:
    creds: Credentials | None = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, token_file)
        return creds

    if not interactive:
        raise RuntimeError(
            f"No usable token at {token_file}. Run `oncallbot auth` first."
        )

    if not credentials_file.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {credentials_file}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console "
            "(APIs & Services > Credentials), download the JSON, and save it there. "
            "See README.md for the exact steps."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    # login_hint pre-selects the account on the consent screen. It is a hint,
    # not a constraint -- the user can still pick another account, which is why
    # authorized_email() is checked afterwards.
    extra = {"login_hint": login_hint} if login_hint else {}
    creds = flow.run_local_server(port=0, prompt="consent", **extra)
    _save(creds, token_file)
    return creds


def build_service(
    credentials_file: Path,
    token_file: Path,
    *,
    interactive: bool = False,
    login_hint: str = "",
):
    creds = get_credentials(
        credentials_file, token_file, interactive=interactive, login_hint=login_hint
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def authorized_email(service) -> str:
    """Which mailbox this token actually reads."""
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")


def assert_account(service, expected: str) -> str:
    """Fail loudly rather than silently reading the wrong mailbox."""
    actual = authorized_email(service)
    if expected and actual.lower() != expected.lower():
        raise WrongAccountError(
            f"Token authorizes {actual}, but gmail.account is {expected}.\n"
            "You consented with the wrong Google account. Delete the cached token "
            "and re-run `oncallbot auth`, picking the right account on the consent "
            "screen."
        )
    return actual


def _save(creds: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    token_file.chmod(0o600)
