# Deploying the chat UI

`oncallbot serve` binds `127.0.0.1:8765` and now requires a **Google sign-in
per person**. It still refuses to bind anything but loopback, because a session
cookie that authorizes Gmail access needs HTTPS in front of it.

## What has to be true before it leaves your laptop

### 1. Authentication, with identity — **done, on loopback**

Every `/api/*` route runs as one signed-in person. The cookie holds an opaque
session id, `httponly` and `SameSite=lax`; Google's refresh token stays
server-side in `.secrets/tokens/<hash>.json`, mode 600. Identity comes from
Gmail itself (`users.getProfile` on the token that was just issued), so there
is no second claim to trust and no `openid` scope.

Two things are **not** done. There is no SSO in front — the domain allowlist is
the only gate, and an `@1mg.com` colleague who is not on the support group can
sign in (they see zero threads, because Gmail returns nothing for a mailbox
they do not receive). And on anything but loopback the cookie needs TLS; the
`serve` command exits 2 rather than letting you find that out later.

### 2. An access log that records reads, not just requests

Still missing, and login makes it overdue rather than optional: there is now a
user to attribute a read to, and nothing writes it down. Log
`(user, thread_id, timestamp)` for every summary served.

### 3. A decision about who *should* see this

Right now anyone who can reach the port sees every patient's issue. "Everyone
at 1mg" is almost certainly the wrong audience for this data even with SSO;
scope it to the health-records support and engineering groups.

### 4. Mailbox identity — **done**

Each session reads as the person who consented, using their own token, with
their own summary cache in `.data/users/<hash>.sqlite3`. Nobody borrows anyone
else's mailbox access, and the admin API bearer is per session too: a
logged-in user's admin calls never fall back to the operator's
`ONCALLBOT_HRA_TOKEN`.

Migrating an existing single-user setup: `oncallbot adopt` copies the token and
summary cache from `oncallbot auth` to the per-user paths, so the first login
neither re-consents nor starts with an empty cache.

### 5. Concurrency — **partly**

One turn per session is now enforced: a second `/api/chat` while one is running
gets a 409 rather than a second model subprocess. Cross-user load is still
unsolved — five people asking for a fresh pull is five `claude` processes and
five Gmail fetches. Before real multi-user load: put summarize work on a queue
with a single worker, and let the chat endpoint report progress from the queue
rather than doing the work inline.

## Running it locally today

```bash
uv run oncallbot serve
```

Then open <http://127.0.0.1:8765>.

`--host` accepts other addresses and now **refuses** them: the session cookie
authorizes Gmail access, and over plain HTTP on a shared network it is
sniffable. Terminate TLS in front of a loopback bind, or set
`auth.enabled: false` to run the single-user way it worked before login.

## Container sketch

The pieces a real deployment needs beyond the Dockerfile:

- `.secrets/token.json` mounted as a secret, never baked into the image
- `.data/` on a volume, or moved to Postgres — SQLite does not survive a
  rescheduled pod, and `store.py` is small enough to port
- `ANTHROPIC_API_KEY` and the SDK summarizer backend instead of the `claude`
  CLI, since the CLI expects an interactive login. The `Summarizer` protocol in
  [`summarizer.py`](../src/oncallbot/summarizer.py) is the seam
- a readiness probe that fails when the Gmail token cannot refresh
