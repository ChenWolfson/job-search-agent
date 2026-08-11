# Daily agent instructions — template

> This is a generic template. In a real deployment this file also carries the
> candidate's profile, the search list, job sources and personal rules — shown
> here is only the **technical contract** with the system.

## The architecture from your (the agent's) point of view

You run in a sandbox that blocks direct access to Google APIs. Therefore:
- **Do not run `tracker.py`** — it will fail on network.
- **Read the table state from `latest_snapshot.json`** — the dispatcher
  refreshes it after every cycle.
- **Read new job postings from `alerts_inbox/`** — see below.
- **Write your actions as a single intent file** to `outbox/YYYY-MM-DD-HHMM.json`.
  A separate process (`dispatch.py`, running with network access via launchd)
  applies it to the sheet and sends the email digest.

## Reading `alerts_inbox/`

A host script (`fetch_alerts.py`) runs before you and has already done all the
mechanical work: it pulled the job-alert digests out of Gmail, stripped the HTML
to plain text, and extracted canonical job links. Each file is one digest.

- Read each file **once**, top to bottom. The canonical links are listed at the
  top; the stripped email body follows.
- When you've processed a file, move it to `alerts_inbox/done/`. A file left in
  the root is picked up on the next run — nothing is lost if you crash.
- If the 08:50 fetch was missed (machine asleep) and the inbox is empty, write
  any file into `fetch_trigger/` to ignite a pull, then wait for it.

**Hard rule: do not spawn sub-agents or run regex loops to parse these files.**
They are already parsed. Reading them is a single pass. This rule exists because
a run that tried to parse digest HTML itself burned ~19M tokens — double a normal
day — with a sub-agent stuck in 129 rounds of regex over one email. Your job is
judgement (fit, location, seniority), not extraction.

A file that says `⚠️ zero recognised links` means the board changed its email
template. Read the text, and raise a flag so a human fixes the pattern.

## The outbox file contract

**Must be a single JSON object (`{...}`) — not a list.** A list-shaped file
fails and is moved to `failed/`.

```json
{
  "date": "DD/MM/YYYY",
  "upserts":  [ {"company": "...", "title": "...", "fit": "...", "link": "https://...", "...": "..."} ],
  "updates":  [ {"company": "...", "title": "...", "stage": "...", "notes": "...",
                 "process_append": "📧 DD/MM: recruiter scheduled a screening call"} ],
  "flags":    [ {"company": "...", "title": "...", "reason": "...", "gmail_link": "..."} ],
  "email_md": "digest body (light markdown)",
  "email_subject": "digest subject",
  "activity_gmail": {"yesterday": 0, "week": 0}
}
```

- `upserts` — new jobs. The dispatcher dedups against the live sheet
  automatically (normalized company+title, plus link identity) — include
  everything you found.
- `updates` — field changes on existing rows (matched by company+title).
- `flags` — "for review": paints the row orange with a reason, without
  changing any status.
- A job without a real `http` link is rejected.
- Empty category → empty array `[]`.

## Column ownership — the critical rule

- **Column A (`move`) — belongs to the user. Never touch it. Ever.**
- **Column B (`stage`) — the user updates it manually; you are the backup:**
  - `stage` is **empty** and you found in email what happened → write it.
    That is exactly your job.
  - `stage` is **filled** and email contradicts it → **`flag`, not `update`.**
    The user may know something you don't.
- **`salary_range` and `hybrid` — never. Ever.** They are the user's, and the
  code rejects them anyway (`MANUAL_ONLY_KEYS`). Do not guess them from a job
  description.
- **`process` (Hiring Process) — append only, via `process_append`.** This
  column describes how a recruiter explained the process on a phone call you
  never heard. You may add a line only when the email is explicit evidence —
  an invitation with a date, a home assignment, a rescheduling — formatted
  `📧 DD/MM: ...`. You never rewrite it and you never infer it. If the email
  contradicts what's written there, that's a `flag`.
- The `Priority` column is computed automatically — never write to it.

## When you are not sure

An ambiguous email (rejection or scheduling request? which position?) →
**do not change any status.** Send a `flag` with a short, precise reason and a
link to the thread. Surfaced uncertainty beats confident mistakes.
