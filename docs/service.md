# Running oncallbot as a service

Today it runs on each person's laptop, on loopback, with a Google sign-in per
user. `serve` **refuses** any other bind address, because the session cookie
authorizes Gmail access and over plain HTTP on a shared network it is
sniffable. This is the plan for lifting that.

Read [deploy.md](deploy.md) first: it lists the five prerequisites and which
are now done. This document is the how.

## The decision that comes before any of it

**Where does the model run, and what leaves the building?**

The default backend is `claude_cli`, which shells out to the operator's own
Claude Code install. That does not containerise: there is no interactive login
inside an image. A service has to use `summarizer.backend: anthropic_api` with
a key from a secret manager — or `local`, pointed at a self-hosted model.

Those two are not the same decision:

| | What leaves the machine | What it needs |
| --- | --- | --- |
| `anthropic_api` | Redacted email bodies, and **attachment content unredacted** — lab report PDFs are raw PHI | A commercial agreement covering patient data, and `attachments.enabled: false` unless it explicitly covers attachments |
| `local` | Nothing | A GPU host, and a model good enough to be trusted with a verdict — the smaller ones tested were not |

Pick this before building anything. It is the only item on this page that a
deployment cannot work around, and [phi.md](phi.md) is the input to it.

## What breaks the moment there is more than one replica

Three pieces of state live on local disk today:

- **Sessions** — `.data/sessions.sqlite3`
- **Per-user summary caches** — `.data/users/<hash>.sqlite3`
- **Per-user Gmail refresh tokens** — `.secrets/tokens/<hash>.json`

and one lives in memory:

- **The admin API bearer**, per session, deliberately never written to disk

So a single replica with a persistent volume works unchanged. Horizontal
scaling needs, in this order:

1. **Sessions in Postgres or Redis.** Small change; `chat/auth.py` already
   isolates every read and write behind `Sessions`.
2. **Refresh tokens in a secret manager**, not a file per user. They are
   long-lived credentials for a person's mailbox.
3. **The admin bearer either sticky or shared.** In memory per process means a
   user's paste is lost the moment the load balancer moves them. Sticky
   sessions are the cheap answer; a short-TTL encrypted cache is the real one.
4. **Summary caches in Postgres** with a `user_email` column, replacing the
   file-per-user split. Until then the cache is per replica, which is
   *correct* but wasteful.

## Sequence

**Phase A — one replica, internal only.** The smallest thing that is honestly
a service.

- TLS in front (ingress or a sidecar). `Secure` on the cookie follows the
  scheme automatically; the loopback refusal in `serve` needs an explicit
  "TLS terminates in front of me" flag rather than being bypassed.
- A **Web** OAuth client with the real redirect URI registered, and the consent
  screen set to Internal for the workspace. The Desktop client works on
  loopback only.
- A read audit log: `(user_email, thread_id, action, at)` for every summary,
  thread body and diagnosis served. This is [deploy.md](deploy.md) item 2, and
  login made it overdue rather than optional — there is now a user to
  attribute a read to and nothing writes it down.
- A persistent volume for `.data` and `.secrets`, backed up and encrypted at
  rest. It holds patient-derived summaries.
- A retention policy. `.data/users/*.sqlite3` grows forever; decide how long a
  summary lives and delete on that schedule.
- Health endpoint, and structured logs that never contain a body or a token.

**Phase B — the work queue.** One `claude`/API call per thread, minutes per
turn. Five people asking for a fresh window is five concurrent runs against
one process. One turn per session is already enforced; cross-user is not.
Summarize work moves to a queue with a bounded worker pool, and the chat
endpoint reports progress from the queue rather than doing the work inline.

**Phase C — horizontal.** The four state moves above, then more than one
replica.

## What I would not deploy without

- The PHI decision made explicitly, in writing, by someone who owns that call.
- The read audit log. Without it the service cannot answer "who saw this
  patient's summary", which is the question an audit asks first.
- An audience narrower than "everyone at 1mg". The domain allowlist is not
  that: an @1mg.com colleague who is not on the support group can sign in
  today. They see zero threads, because Gmail returns nothing for a mailbox
  they do not receive — but that is Gmail protecting the data, not us.
- A dedicated mailbox decision. Reads happen as the signed-in person, which is
  right, but it means the service's reach is the union of its users' reach.

## What is already done

- Authentication with identity, per person, with per-user tokens and caches.
- The admin bearer per session, never falling back to the operator's.
- Cross-site request refusal, `httpOnly` `SameSite=lax` cookies, and no
  credential reachable from page JavaScript.
- Model calls with every built-in tool disabled, in an empty working
  directory, so an injected email cannot read the host.
- Read-only admin API access, enforced at three independent layers.
