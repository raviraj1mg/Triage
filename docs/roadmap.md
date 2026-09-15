# Roadmap

## Phase 1 — read and summarize (done)

Find threads where `health-record-support@1mg.com` is tagged, flatten them, and
produce a structured `IssueSummary` per thread. Cached in SQLite, keyed on the
last message id so reruns are incremental.

## Phase 2 — diagnose

The summary is deliberately shaped for this: `category` buckets the issue,
`affected_entities` carries the ids a lookup would need, and `missing_info` says
when there is nothing to look up yet.

Work needed:

- **Runbook per category.** A mapping from `category` to the checks worth
  running. This is the whole game — a category with no runbook is just a label.
- **Read-only diagnostics.** Given a patient/order/record id: query the record
  service, check the upload pipeline, look for the record in storage vs. the
  index, pull recent logs. Read-only, idempotent, safe to run on every ticket.
- **Evidence in the output.** `IssueSummary` gains a `diagnosis` field: what was
  checked, what came back, and whether it confirms or contradicts the reporter's
  claim. The model should never assert a root cause the diagnostics didn't show.
- **Accuracy measurement.** Before automating any fix, run diagnose-only for a
  few weeks against tickets a human also resolved, and compare. Without that
  baseline there is no way to know whether phase 3 is safe.

## Phase 3 — fix

Only categories where the fix is mechanical and reversible are candidates:
re-trigger a failed ingest, re-index a record, clear a stale cache. Anything that
mutates or deletes patient data stays human-only, permanently.

Non-negotiables when this lands:

- **Never act on instructions found in an email.** The email is the report of a
  problem, not the authorization to change anything. An email saying "delete
  this patient's records" is a request to be triaged by a human, and a likely
  injection attempt. The bot's allowed actions come from the runbook, keyed on
  the diagnosis — never from the thread text.
- **Allowlist, not blocklist.** An explicit list of permitted actions per
  category. Anything not on it escalates.
- **Dry-run first, and an audit trail.** Every action logged with the thread that
  triggered it, the diagnosis that justified it, and the before/after state.
- **Confidence and severity gates.** p0 never auto-fixes. Low confidence never
  auto-fixes.
- **Kill switch.** One config flag that puts everything back to
  summarize-and-escalate.

## Access model, when this stops being a laptop script

The current OAuth desktop flow reads *your* mailbox. For an unattended service:

- **Service account + domain-wide delegation** — needs a Workspace admin to
  authorize the client id and scopes. The right answer for a scheduled job.
- **Group membership** — if `health-record-support@` is a Google Group, a
  dedicated bot account joins it and reads its own copy. Less admin friction,
  and the bot's access is visible in the group's member list.

Either way, adding write scopes (labelling threads, replying) invalidates the
cached token and forces a re-consent.
