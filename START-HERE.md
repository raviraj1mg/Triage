# oncallbot — start here

Triage for the `health-record-support@1mg.com` inbox: it lists the oncall
threads, summarizes them, groups them into categories, and diagnoses the order
behind a ticket against the admin APIs.

Everything runs **on your own machine**, reading Gmail **as you**. Nothing is
shared with a server, and nobody else's mailbox is involved.

## Four commands

```bash
# 1. Install (once). Gets Python and every dependency.
curl -LsSf https://astral.sh/uv/install.sh | sh     # skip if you have uv
uv sync

# 2. Your config. No edits needed to get started.
cp config.example.yaml config.yaml

# 3. Check the setup. Nothing red means you are good.
uv run oncallbot doctor

# 4. Run it.
uv run oncallbot serve
```

Open <http://localhost:8765>, click **Sign in with Google**, and consent with
your **@1mg.com** account. That is the whole setup.

To stop it: `uv run oncallbot stop`. To pick up code changes:
`uv run oncallbot restart`.

## What you need before step 3

**One of these**, for the model that writes the summaries:

- **Claude Code** installed **and signed in** — this is the default and needs
  no config. Installed is not enough: run `claude` once on its own and finish
  the login before step 3. A CLI that is on your PATH but has never been
  signed in is the one setup failure that used to get all the way through to
  a broken first question; or
- an **Anthropic API key**: `cp .env.example .env` and put the key in it as
  `ANTHROPIC_API_KEY=sk-ant-...`. That alone is enough — the `claude` CLI
  picks the variable up too, so you do not have to change `config.yaml`
  unless you want to skip the CLI entirely (`summarizer.backend:
  anthropic_api`); or
- a **local model** through Ollama, if you would rather nothing left the
  machine: `ollama serve && ollama pull gemma3`. Pick it from the dropdown in
  the chat box; no key and no config change needed.

`doctor` tells you which one it found. It now makes one real call through the
CLI rather than only looking for it on your PATH, so **claude CLI answers**
failing is the check to read — it means signed-in, not installed.

The dropdown beside the input switches model per session, and says at the
point of choosing what each one was measured doing — `gemma3` handled
routing, identifiers and diagnosis bullets correctly here; `phi4-mini` routed
well but filled the identifier fields with rubbish. See
[README.md](README.md) for the numbers.

## First time in the app

- **Ask in plain English.** "oncalls from today", "this week's oncalls",
  "divide this week's oncalls into categories".
- Listing is free — it does not call the model. **Summarize** and **Diagnose**
  on a card are per-thread, on demand.
- **Anything that reads the admin APIs asks for a token the first time** —
  Diagnose on a card, or a question about a specific order. Click **How to get token — see this video**
  in that prompt for a recording of where to find it: unified-admin
  → DevTools → Application → Local Storage → `accessToken`. It lasts about 10 hours, is kept in memory
  only, and is never written to disk — so expect to paste it again after a
  restart or the next day.

## If sign-in fails

| What you see | What it means |
| --- | --- |
| **Access blocked** on Google's screen | The OAuth client is in Testing mode and your account is not a test user. Ask Raviraj to add you. |
| `not allowed to use oncallbot` | You signed in with a non-@1mg.com account. Use your work account. |
| `redirect_uri_mismatch` | Only happens if you changed the port or host. The page tells you the exact URI to register. |
| Signs in fine but **0 threads** | You are probably not a member of `health-record-support@1mg.com`. Google cannot tell us that — the mailbox search just comes back empty. |

## Two things to know

- **This reads real patient data.** Summaries derive from patient records, and
  attachment reading is on by default, which sends lab reports and screenshots
  to the model. Read [docs/phi.md](docs/phi.md) before pointing it at anything.
- **Keep it on localhost.** `serve` refuses any other bind address, because the
  session cookie authorizes Gmail access. See [docs/deploy.md](docs/deploy.md).

Full detail — how matching works, what each command does, how diagnosis
decides a verdict — is in [README.md](README.md).
