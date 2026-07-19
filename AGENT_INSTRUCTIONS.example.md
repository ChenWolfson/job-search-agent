# Daily agent instructions — template

> This is a generic template. In a real deployment this file also carries the
> candidate's profile, the search list, job sources and personal rules — shown
> here is only the **technical contract** with the system.

## The architecture from your (the agent's) point of view

You run in a sandbox that blocks direct access to Google APIs. Therefore:
- **Do not run `tracker.py`** — it will fail on network.
- **Read the table state from `latest_snapshot.json`** — the dispatcher
  refreshes it after every cycle.
- **Write your actions as a single intent file** to `outbox/YYYY-MM-DD-HHMM.json`.
  A separate process (`dispatch.py`, running with network access via launchd)
  applies it to the sheet and sends the email digest.

## The outbox file contract

**Must be a single JSON object (`{...}`) — not a list.** A list-shaped file
fails and is moved to `failed/`.

```json
{
  "date": "DD/MM/YYYY",
  "upserts":  [ {"company": "...", "title": "...", "fit": "...", "link": "https://...", "...": "..."} ],
  "updates":  [ {"company": "...", "title": "...", "stage": "...", "notes": "..."} ],
  "flags":    [ {"company": "...", "title": "...", "reason": "...", "gmail_link": "..."} ],
  "email_md": "digest body (light markdown)",
  "email_subject": "digest subject"
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
- The `Priority` column is computed automatically — never write to it.

## When you are not sure

An ambiguous email (rejection or scheduling request? which position?) →
**do not change any status.** Send a `flag` with a short, precise reason and a
link to the thread. Surfaced uncertainty beats confident mistakes.
