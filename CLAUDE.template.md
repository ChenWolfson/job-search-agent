# Job search — <YOUR NAME>

> Copy this file to `CLAUDE.md` and fill it in. The agent reads it on every run;
> it is the entire basis for how jobs get scored. Be specific — "looking for
> something in tech" produces exactly the results you'd expect.

## Who I am

- **Background:** <e.g. 2 years as a QA engineer at a B2B SaaS company>
- **What I actually did:** <the honest version — which parts were yours, which
  were the team's. This matters: it's what makes "why I fit" credible>
- **Current situation:** <between jobs / employed and looking quietly / relocating>

## What I'm looking for

- **Target roles:** <list the job titles you'd genuinely accept — 5 to 10 of
  them. Include adjacent ones you're willing to stretch into>
- **Location:** <city / region · on-site, hybrid, or remote>
- **Experience bracket:** <e.g. 0-3 years — used to filter out postings that
  will never call you back>
- **Deal-breakers:** <shift work, relocation, agencies, specific industries>

## What I bring

<Three or four concrete strengths, with evidence. Certifications, tools you're
fluent in, domains you know, languages you speak. The agent uses this to write
the "Why I Fit" column, and it can only work with what you give it here.>

## Role vocabulary

<The same job shows up under different titles in different companies. List the
variants so searches don't miss them.>

Example:
- "QA Engineer" · "QA Analyst" · "Test Engineer" · "SDET" · "Quality Specialist"
- Local-language variants: <...>

## Target companies

<Companies whose careers pages should be checked directly, every run. This is
the highest-value source in the system — see JOB_SOURCES.md. Start with 10-20.>

## Fit scoring

How the agent should rate a job:

- 🟢 **Strong** — matches a target role, right experience bracket, right location
- 🟡 **Good Shot** — adjacent role or slight stretch on experience
- 🔴 **Stretch** — worth knowing about, probably a long shot

## Pipeline

<Live processes, so the agent doesn't treat a company you're already talking to
as a new discovery. Update as you go.>

| Company | Role | Status |
|---|---|---|
| | | |

## Instructions for the agent

- Never touch the `My Move` column — that's my decision, not yours.
- Fill `Stage` only when it's empty. If it's filled and an email contradicts it,
  **flag it**, don't overwrite it.
- Never guess `Salary Range` or `Hybrid` from a job description.
- When an email is ambiguous — rejection or scheduling request? which role? —
  don't change any status. Flag it with a reason and a link to the thread.
- Surfaced uncertainty beats a confident mistake. Always.
