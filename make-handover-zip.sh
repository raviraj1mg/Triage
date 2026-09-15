#!/usr/bin/env bash
# Build a zip for teammates. Excludes every credential and all patient data.
#
# What is deliberately left out:
#   .secrets/token.json  your personal Gmail token (CLI)
#   .secrets/tokens/     one Gmail refresh token per person who signed in
#   .env                 API keys
#   .data/               summaries derived from patient records
#   out/                 generated digests
#
# .secrets/oauth_client.json IS included by default: a Desktop-app OAuth client
# is not a confidential secret (desktop apps cannot keep one), and sharing it
# saves every teammate a Google Cloud setup. Each person still consents as
# themselves and gets their own token. Pass --no-oauth-client to leave it out.
set -euo pipefail

cd "$(dirname "$0")"
NAME="oncallbot-handover-$(date +%Y%m%d).zip"
INCLUDE_CLIENT=1
INCLUDE_VIDEO=1
for arg in "$@"; do
  case "$arg" in
    --no-oauth-client) INCLUDE_CLIENT=0 ;;
    # The how-to recording is ~9MB of the zip. Worth it for a teammate setting
    # up for the first time, but drop it if you are sharing over something
    # size-limited and can send the video separately.
    --no-video)        INCLUDE_VIDEO=0 ;;
  esac
done

# .env.example ships with the zip, so a value left in it would be handed to
# the whole team. Refuse rather than warn.
if [[ -f .env.example ]] && grep -qE '^[A-Za-z_][A-Za-z0-9_]*=.+' .env.example; then
  echo "REFUSING: .env.example contains a non-empty value:" >&2
  grep -nE '^[A-Za-z_][A-Za-z0-9_]*=.+' .env.example | sed 's/=.*/=<redacted>/' >&2
  echo "Move it to .env (gitignored) and leave .env.example blank." >&2
  exit 1
fi

rm -f "$NAME"

EXCLUDES=(
  -x '.git/*' -x '.venv/*' -x '__pycache__/*' -x '*/__pycache__/*' -x '*.pyc'
  -x '.pytest_cache/*' -x '.ruff_cache/*' -x '.DS_Store' -x '*/.DS_Store'
  -x '.data/*' -x 'out/*' -x '.env'
  -x '.secrets/token.json'
  -x '.secrets/tokens/*'           # one refresh token per signed-in user
  -x 'config.yaml'                 # personal: mailbox address
  -x 'oncallbot-handover-*.zip'    # any previous build, not just today's
)
[[ $INCLUDE_CLIENT -eq 0 ]] && EXCLUDES+=( -x '.secrets/oauth_client.json' )
[[ $INCLUDE_VIDEO -eq 0 ]] && EXCLUDES+=( -x 'src/oncallbot/chat/static/help/*' )

zip -r -q "$NAME" . "${EXCLUDES[@]}"

echo "Built $NAME"

# The listing is captured ONCE, then matched against in memory. Never
# `unzip -l | grep -q`: grep exits on the first match, unzip takes SIGPIPE, and
# under `set -o pipefail` the pipeline reports failure -- which would make a
# file that IS in the zip read as absent. That is the wrong way for a leak
# check to fail.
LISTING="$(unzip -Z1 "$NAME")"

has() { printf '%s\n' "$LISTING" | grep -qx -- "$1"; }
has_under() { printf '%s\n' "$LISTING" | grep -q -- "^$1"; }

fail=0

if ! has "START-HERE.md"; then
  echo "  !! START-HERE.md is missing -- the recipient's first file" >&2
  fail=1
fi

echo
echo "Contents check -- these must all be absent:"
for f in .secrets/token.json .env config.yaml; do
  if has "$f"; then
    echo "  !! LEAKED: $f"; fail=1
  else
    echo "  ok, excluded: $f"
  fi
done
for d in .data/ .secrets/tokens/; do
  if has_under "$d"; then
    echo "  !! LEAKED: $d"; fail=1
  else
    echo "  ok, excluded: $d"
  fi
done

# Prove the matcher itself works. A guard that cannot see a file it should see
# is worse than no guard, and this is the check that catches that.
if ! has "README.md"; then
  echo "  !! self-test failed: the matcher cannot see README.md, so the" >&2
  echo "     exclusion checks above prove nothing. Do not send this zip." >&2
  fail=1
else
  echo "  ok, matcher works: README.md found as expected"
fi

if [[ $fail -ne 0 ]]; then
  echo
  echo "REFUSING: $NAME is not safe to send." >&2
  rm -f "$NAME"
  exit 1
fi

if [[ $INCLUDE_CLIENT -eq 1 ]] && has ".secrets/oauth_client.json"; then
  echo "  included:    .secrets/oauth_client.json (shared desktop OAuth client)"
fi
if has "src/oncallbot/chat/static/help/admin-token.mp4"; then
  echo "  included:    admin-token.mp4 (how-to video; --no-video to drop it)"
fi

echo
echo "Size: $(du -h "$NAME" | cut -f1)"
