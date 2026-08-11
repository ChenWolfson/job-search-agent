# Job sources — what works, what doesn't

This is the part you can't get from reading the code, and the part that decides
whether the system finds real jobs or quietly finds nothing.

All of it was learned by running this pipeline daily against a real job search.
The board names in your country will differ; **the failure modes won't.**

---

## The short version

| Source | Verdict |
|---|---|
| Company career pages | 🥇 **The best source.** Jobs appear here that are on no board at all |
| Job alert emails | ✅ **The stable channel.** Works without a browser, without login, every day |
| Curated / niche boards | ✅ Worth it — but they serve expired jobs too. Always verify a job is live |
| Big boards, via a real logged-in browser | ⚠️ Works, but only when the browser happens to be open |
| Scraping big boards directly | ❌ Blocked. Don't build on it |
| Web search for jobs | ❌ Returns aggregator pages and the wrong country |

---

## ✅ Job alert emails — build on this one

Every major board will email you a digest of new postings matching a saved
search. This is the channel that survives everything else: no scraping, no
login, no browser, no bot detection. It lands in Gmail on its own schedule and
`fetch_alerts.py` picks it up.

**Set up one alert per role family, not one big alert.** A single broad alert
returns mush. Separate searches — one per job title cluster you'd actually
accept — produce digests you can triage.

### ⚠️ Alerts die silently. Verify them.

The two failures below both cost days of a pipeline that looked perfectly
healthy while delivering nothing. Both are invisible unless you go looking.

**1. The board stops sending and never tells you.**
Four alerts, configured on a major board, produced **zero emails in fourteen
days** — nothing in inbox, spam, or trash. The saved searches still existed in
the account. They just stopped firing.

> **Check:** a week after setup, search your mail for each board and confirm
> messages are actually arriving. Put a recurring reminder on it. An alert that
> produced nothing for a week is broken, not quiet.

**2. A wrong sender address returns zero results — with no error.**
`ALERT_SENDERS` in `fetch_alerts.py` is a list of Gmail `from:` filters. If one
is wrong, Gmail returns an empty result set **successfully**. No exception, no
warning, no zero-results message. The run reports "0 new digests" and looks fine.

One board in the original deployment sent from a domain that differed from its
website address by a single letter — the mailbox name was plural, the domain was
singular. The typo survived three days and had pulled *zero* digests from that
board, ever.

> **Check:** before adding any sender, search Gmail for `from:<domain>` by hand
> and confirm you get hits. Do it once per entry. It takes ten seconds and it is
> the single highest-value verification in this whole setup.

### Seeding the ledger on first run
When you fix a broken sender, the next run will suddenly see weeks of backlog and
try to process all of it at once. Before enabling a newly-fixed source, write the
old thread ids into `processed_threads.jsonl` so only fresh digests come through.

---

## 🥇 Company career pages — the highest-value source

Keep a list of companies you actually want to work at, and have the agent check
their careers pages directly. Several of the best matches found by this system
appeared on **no job board at all** — only on the company's own site.

It's also the source with no bot detection, no expired listings, and no
middleman. Slower to set up, worth it.

---

## ⚠️ The big boards — read this before trusting them

**Scraping them directly does not work.** Expect: a majority of results being
sponsored/irrelevant, link extraction blocked, and bot-detection challenges —
even while logged in. This isn't a technique problem to solve with a better
selector; it's the product working as designed.

**A real, logged-in browser does work** — a browser-automation extension driving
your actual Chrome session can open a job page and read the full description.
But it only works *when that browser is open*, which at 09:00 is not guaranteed.
Treat it as a bonus path, never the backbone. Have the agent check whether a
browser is connected and skip cleanly when it isn't.

**Search in your local language too.** An English-only search misses an entire
axis of postings in any non-English market. Run the same search in the local
language and you'll find roles the English query never returns.

### Two lies to watch for

**Location tags lie.** A posting tagged "multiple locations" in a digest turned
out, on opening the actual page, to be a site three hours away. If location
matters, verify it on the job page — not from the alert email.

**Digests hide the employer.** Many alert emails list a job title and nothing
else — no company, sometimes no direct link. The canonical URL patterns in
`fetch_alerts.py` exist to recover the real link from tracking redirects. When a
board changes its email template, those patterns stop matching; the output file
says `⚠️ zero recognised links` precisely so you notice.

---

## ✅ Curated and niche boards

Smaller, human-curated boards punch above their weight — less noise, better
matches. Two caveats: their search results often include **expired postings**
(the live ones tend to be on the front page), and their alert links are usually
mailing-system redirects (`/ls/click?upn=...`) rather than real job URLs.

The dedup logic already handles that second one: a link with a query string but
no recognisable job id returns `None` instead of a shared base URL, so dedup
falls back to company+title. Without that, *every* job from such a board collapses
into one and gets silently dropped as a duplicate.

---

## ❌ Web search

Searching the open web for jobs returns aggregator landing pages instead of
postings, and skews heavily toward the largest English-speaking market
regardless of the location in the query. It is not a job source.

---

## The rule underneath all of it

**Every source needs a liveness check, because every one of these failures is
silent.** No exception is raised when a board stops emailing you, when a filter
matches nothing, when a template changes, or when a scraper starts getting served
an empty page. The pipeline keeps reporting success.

Build the check into your routine: if a source contributed zero jobs for a week,
assume it's broken and go verify it by hand.
