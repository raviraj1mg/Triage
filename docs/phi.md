# PHI handling

This inbox is health-record support. Assume every thread contains patient data.

## What the code does today

`redact.py` masks, before any body leaves the machine:

- card numbers (13–19 digits)
- Aadhaar (12 digits, grouped or not)
- PAN
- Indian mobile numbers — replaced with `[REDACTED:PHONE …1234]`, keeping the
  last four so a responder can still match the ticket to a user

It deliberately keeps order ids, patient ids, record ids and the reporter's email
address: triage is useless without them, and phase 2 diagnostics need them.

Only summaries are persisted to `.data/oncallbot.sqlite3`. Raw bodies are held
in memory for the duration of one run and are never written to disk by this code.

## Attachments

`attachments.enabled` downloads image, PDF and text attachments so the model can
read the screenshot or lab report instead of only seeing a filename. This is the
single largest expansion of what leaves the machine, and it deserves a decision
rather than a default.

**Attachment content cannot be redacted.** `redact.py` masks a phone number in a
body because a body is text this code can rewrite. A lab report PDF is a whole
patient record — name, age, every measured value, the referring doctor. There is
no mask to apply. When this is on, those files are uploaded whole.

What is controlled instead:

- **Type allow-list.** PNG, JPEG, GIF, WebP, PDF, plain text, CSV. Anything else
  is named to the model but never downloaded.
- **Size and count caps.** 5 files per thread, 5MB each, 15MB per thread by
  default. Byte length is re-checked after download, because the size declared
  in the payload can lie.
- **Filenames are never trusted.** Names come from email, so each is reduced to
  a safe basename before touching the filesystem; the declared MIME type decides
  the extension, not the claimed one.
- **Nothing persists.** Files are written to a `TemporaryDirectory` and deleted
  when the thread finishes, whether it succeeded or failed. Only the summary is
  stored.
- **The model is confined to those files.** The summarizer subprocess runs with
  its working directory set to that temp directory, so `--restricted` limits its
  file tools to the staged attachments — it cannot reach the project, and in
  particular cannot reach `.secrets/token.json`. This was verified: an attempt
  to read the token from an isolated cwd is refused by the harness, not merely
  declined by the model.
- **Dictation sends audio to the browser's vendor.** The mic in the composer
  uses the Web Speech API: Chrome relays the audio to Google, Safari to Apple.
  Nothing of ours receives it, and nothing is stored — but a patient name or
  an order id spoken into it has left the machine, which typing one does not.
  This is not covered by any decision recorded here. A local Whisper behind
  our own endpoint is the alternative if it should not leave.
- **Only the summarizer gets a file tool, and only `Read`.** Every other model
  call — routing, the diagnosis reason and findings, the further check, the
  grouping, the chat answers — runs with `--tools ""`, which disables all
  built-in tools, in a fresh empty directory. `--restricted` on its own keeps
  `Read`/`Grep`/`Glob` inside the working directory, which for the server is
  the project: `.secrets/tokens/`, `.env` and `.data/` are all in there. Those
  calls have every fact they need in the prompt, so a tool call is never the
  answer — it is the model wandering, and one steered by an injected email
  would be wandering somewhere it should not.

Turning it off (`enabled: false`) is a real option. The model is then told each
attachment exists and is unavailable, and asks for what it needs in
`missing_info` instead of guessing.

## What it does not do

- It is a regex pass, not a classifier. Free-text PHI — a diagnosis, a
  medication, a doctor's note quoted in the body — is not detected and is sent
  to the model as-is.
- Attachment *contents* are not redacted at all, by design — see above. Filenames
  frequently carry patient names (`Ujjwal.pdf`, `Sarbani.pdf` are real examples
  from this inbox) and are not masked either.
- Attachment types outside the allow-list (xlsx, zip, docx) are never read, so
  an issue whose only evidence is a spreadsheet stays opaque.
- `redaction.enabled: false` disables all of it.

## Before pointing this at the real inbox

- Confirm with whoever owns data governance at 1mg that sending redacted support
  bodies to the Claude API is within policy. `claude_cli` sends content to
  Anthropic's API under your Claude Code account.
- Decide whether `.data/` and any `--out` digest are acceptable places for
  summaries derived from patient data, and where those files live.
- If the answer to either is no, the summarizer interface in `summarizer.py` is
  the seam — a self-hosted backend drops in without touching anything else.
