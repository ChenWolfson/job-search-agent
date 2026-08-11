# Getting started

This repo is the **infrastructure**. It ships with none of the personal content
it needs to run — that part is yours, and this page is the list.

The fastest path: clone the repo, open it in [Claude Code](https://claude.com/claude-code),
and say:

> Read GETTING_STARTED.md and set this up for me. Interview me for everything in
> the "what you supply" table, then walk me through SETUP.md.

It will ask you the questions below and fill the files in. Or do it by hand —
nothing here is magic.

---

## What you supply

| # | What | Where | Notes |
|---|---|---|---|
| 1 | **Your profile** | `CLAUDE.md` | Copy `CLAUDE.template.md`. Background, target roles, region, years of experience, what you'd say yes to. This is what the agent scores jobs against — vague profile, useless results |
| 2 | **Your email address** | `MAIL_TO` in `tracker.py` | Hardcoded on purpose (see below) |
| 3 | **Spreadsheet ids** | `config.json` | Copy `config.example.json`. See `SETUP.md` for where the ids come from |
| 4 | **Your city / region** | `home_city` in `config.json` | Used as the sort tie-break. Leave empty to disable |
| 5 | **Local location words** | `location_tokens` in `config.json` | City names, country, etc. Stripped before comparing job titles, so "QA Engineer (Berlin)" and "QA Engineer - Hybrid" are recognised as one job |
| 6 | **Your job boards** | `ALERT_SENDERS` in `fetch_alerts.py` | The boards that matter where you live. **Read `JOB_SOURCES.md` before you trust any of them** |
| 7 | **Link patterns** | `_LINK_RULES` in `fetch_alerts.py` | One regex per board, to recover canonical job URLs from tracking redirects |
| 8 | **Target companies** | a list of your own | Companies whose careers pages get checked directly. The single best source |
| 9 | **Agent instructions** | `AGENT_INSTRUCTIONS.example.md` → your own | The technical contract is done. Add your profile, sources and personal rules |
| 10 | **Google OAuth credentials** | `.secrets/credentials.json` | `SETUP.md`, ~15 minutes, one time |

### Why the email address is hardcoded

`MAIL_TO` is a constant in the source, not a value the agent can set. The agent
writes intent files; it never writes that constant. Combined with a send-only
Gmail scope (the token cannot read your mail), the worst a compromised or
confused agent can do is send **one email to you**. Keep it that way.

---

## What you don't supply — created automatically

Don't create these by hand; don't worry when they appear.

| Path | What it is |
|---|---|
| `snapshots/` | A JSON copy of the whole table before every write. 30-day retention. Your undo |
| `latest_snapshot.json` | The current table state, for the sandboxed agent to read |
| `processed_threads.jsonl` | Ledger of pulled email digests, so nothing is pulled twice |
| `alerts_inbox/`, `outbox/` | The message queues between agent and dispatcher |
| `dispatch.log` | What the dispatcher did, and when |
| `.secrets/token.json`, `token_read.json` | OAuth tokens, written on first run |

### Directories you'll grow into

These are the working memory of a real job search — empty here on purpose:

- **`Sessions/`** — one file per run or working session. What was found, what
  changed, what's open.
- **`Companies/<name>/notes.md`** — only for companies you actually applied to.
  Interview notes, contacts, feedback.
- **`CV/`** — your master CV, plus tailored variants.

All three fill up on their own as you use the system. They're also the reason
this stays useful months in: the agent reads them for context.

---

## Order of operations

1. `SETUP.md` — Google Cloud project, OAuth, credentials. Do this first; nothing
   runs without it.
2. Create the sheet, fill `config.json`, run `python tracker.py reformat`.
3. Fill in `CLAUDE.md` and `MAIL_TO`.
4. Set up your job alerts on the boards, **wait a day**, then verify emails are
   actually arriving before adding senders to `fetch_alerts.py`.
5. Run `python fetch_alerts.py --dry-run` and confirm it sees your digests.
6. Wire up launchd (`launchd.example.plist`), or just run `run_now.sh` by hand
   until you trust it.

**Run it manually for the first week.** The point of the audit log, the
snapshots, and the "for review" flags is that you can see exactly what the
automation did and undo it. Use that period to build trust — and to find out
which of your sources are actually delivering.

---

## A note on scope

This was built for one person's job search and it shows: it assumes Gmail, Google
Sheets, macOS launchd, and a single user. None of those are load-bearing ideas —
the interesting part is the pattern, not the plumbing.

Read `ARCHITECTURE.md` for the pattern. Read `JOB_SOURCES.md` before you decide
where your jobs are going to come from.
