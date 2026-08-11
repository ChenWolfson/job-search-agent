#!/usr/bin/env python3
"""
fetch_alerts.py — pulls job-alert digests out of Gmail and compresses them into
small files the daily agent reads, instead of the agent reading raw HTML itself.

This is the mirror image of the outbox: the agent's sandbox can't reach Google,
so a host process brings the information to it in advance. Runs at 08:50
(launchd) — ten minutes before the 09:00 agent.

What it does — mechanical only, zero judgement:
  1. Finds new digests (known alert senders, not already pulled — see the ledger).
  2. Strips HTML to readable text and shortens job links to a canonical URL.
  3. Writes one compact file per digest into alerts_inbox/. The agent reads it,
     applies judgement (fit, location, seniority), and moves it to
     alerts_inbox/done/ when finished.

WHY THIS EXISTS: before it, the agent read raw digest HTML through the Gmail API
and a single day's run burned ~19M tokens — roughly double a normal day — with a
sub-agent stuck in 129 rounds of regex over one email. Parsing is a mechanical
job; give it to Python, not to the model. The agent is explicitly forbidden from
spawning sub-agents or regex loops to parse these files.

State, per email:
  - processed_threads.jsonl — which threads were already pulled (no double-pull).
  - the file's location (inbox root vs done/) — which digests the agent processed.
    A crashed run leaves the file in root and it is picked up next run. Nothing
    is ever lost.

OAuth: a separate read-only token (token_read.json) — it does not touch the
tracker.py token, so a scope change here can't break the automated dispatch.

Usage:
  fetch_alerts.py            pull new digests (default: 3 days back)
  fetch_alerts.py --days 7   wider window
  fetch_alerts.py --dry-run  show what would be pulled, write nothing
"""
import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime
from html.parser import HTMLParser

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------- paths
TRACKER = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(TRACKER, ".secrets")
CREDENTIALS = os.path.join(SECRETS, "credentials.json")
TOKEN_READ = os.path.join(SECRETS, "token_read.json")
INBOX = os.path.join(TRACKER, "alerts_inbox")
DONE = os.path.join(INBOX, "done")
LEDGER = os.path.join(TRACKER, "processed_threads.jsonl")
LOCK = os.path.join(TRACKER, ".fetch.lock")
TRIGGER = os.path.join(TRACKER, "fetch_trigger")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ---------------------------------------------------------------------------
# EDIT THIS FOR YOUR MARKET.
# Known alert senders only. Deliberately NOT indeedapply@ (application receipts)
# or match.indeed.com ("background match" nags) — those aren't digests.
#
# ⚠️ Verify every entry before trusting it. A wrong domain makes Gmail return
# zero results *silently, with no error*, and the pipeline looks healthy while
# pulling nothing. Check each one by hand first:
#     search Gmail for `from:<domain>` and confirm you get actual hits.
# Watch for mailbox-vs-domain mismatches — the visible sender name and the
# actual sending domain are often not the same string.
# ---------------------------------------------------------------------------
ALERT_SENDERS = [
    "jobalert.indeed.com",
    "jobalerts-noreply@linkedin.com",
    "jobs-noreply@linkedin.com",
    # Add the boards that matter in your country, e.g.:
    # "noreply@glassdoor.com",
    # "jobs@otta.com",
]

# Indeed is country-scoped. Set this to your local domain (uk.indeed.com,
# de.indeed.com, ...) or leave the global one.
INDEED_DOMAIN = "www.indeed.com"

MAX_TEXT_CHARS = 20000  # safety ceiling on the text body in the output file

# ---------------------------------------------------------------- auth


def gmail_service():
    creds = None
    if os.path.exists(TOKEN_READ):
        creds = Credentials.from_authorized_user_file(TOKEN_READ, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS):
                sys.exit(f"Missing {CREDENTIALS} — see SETUP.md")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_READ, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------- HTML → text


class _TextExtractor(HTMLParser):
    """Strips HTML to text: skips style/script, newline on block tags."""

    _SKIP = {"style", "script", "head", "title"}
    _BLOCK = {"p", "div", "tr", "br", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self):
        return "".join(self._chunks)


def strip_html(html):
    p = _TextExtractor()
    try:
        p.feed(html)
        raw = p.text()
    except Exception:
        raw = re.sub(r"<[^>]+>", " ", html)  # crude fallback if the parser chokes
    # invisible characters that bloat digests (mailing-system padding)
    raw = re.sub(r"[​‌‍͏﻿­]", "", raw)
    lines = [ln.strip() for ln in raw.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# ---------------------------------------------------------------- link harvest

# Stable URL patterns for the boards — not the email template's layout patterns.
# Add a rule per board you actually use: (name, regex capturing the job id,
# canonical URL template).
_LINK_RULES = [
    ("indeed", re.compile(r"jk(?:=|%3D)([0-9a-fA-F]{10,20})"),
     "https://" + INDEED_DOMAIN + "/viewjob?jk={}"),
    ("linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d{6,12})"),
     "https://www.linkedin.com/jobs/view/{}"),
    ("greenhouse", re.compile(r"gh_jid(?:=|%3D)(\d{4,12})"),
     "https://boards.greenhouse.io/embed/job_app?token={}"),
    ("lever", re.compile(r"jobs\.lever\.co/[^/\s\"']+/([0-9a-f-]{8,40})"),
     "https://jobs.lever.co/-/{}"),
]


def harvest_links(*bodies):
    """Returns (canonical de-duplicated links, count of unrecognised domains)."""
    blob = "\n".join(b for b in bodies if b)
    canonical, seen = [], set()
    for source, rx, tmpl in _LINK_RULES:
        for m in rx.finditer(blob):
            key = (source, m.group(1))
            if key not in seen:
                seen.add(key)
                canonical.append((source, m.group(1), tmpl.format(m.group(1))))
    # domains of links we didn't recognise — the signal that a board changed its
    # template and a rule above needs updating
    other = {}
    for href in re.findall(r'href=["\']?(https?://[^"\'\s<>]+)', blob):
        dom = re.sub(r"^https?://([^/]+).*", r"\1", href)
        if not any(rx.search(href) for _, rx, _ in _LINK_RULES):
            other[dom] = other.get(dom, 0) + 1
    return canonical, other


# ---------------------------------------------------------------- gmail helpers


def _walk_parts(payload):
    """Returns (plain, html) from every part of the message."""
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain), "\n".join(html)


def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _source_of(sender):
    s = sender.lower()
    for dom, tag in [("indeed", "indeed"), ("linkedin", "linkedin"),
                     ("glassdoor", "glassdoor"), ("greenhouse", "greenhouse"),
                     ("lever", "lever")]:
        if dom in s:
            return tag
    return "other"


# ---------------------------------------------------------------- ledger


def ledger_ids():
    ids = set()
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["thread_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return ids


def ledger_append(entry):
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(DONE, exist_ok=True)

    # --- lock: one fetcher at a time (same pattern as dispatch.py).
    # Prevents a double pull when launchd catches up on a missed run at the exact
    # moment you also trigger one by hand.
    try:
        lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if (datetime.now().timestamp() - os.path.getmtime(LOCK)) < 1800:
            print("another fetch is already running — exiting.")
            return
        os.remove(LOCK)  # stale lock (>30 min) — release it and continue
        lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _run(args)
    finally:
        os.close(lock_fd)
        os.remove(LOCK)


def _run(args):
    # Clear trigger files (the agent writes one there to ignite us via launchd
    # WatchPaths, in case the scheduled 08:50 run was missed — e.g. machine asleep)
    for f in os.listdir(TRIGGER) if os.path.isdir(TRIGGER) else []:
        try:
            os.remove(os.path.join(TRIGGER, f))
        except OSError:
            pass
    svc = gmail_service()

    senders = " ".join(f"from:{s}" for s in ALERT_SENDERS)
    query = f"{{{senders}}} newer_than:{args.days}d"
    resp = svc.users().threads().list(userId="me", q=query, maxResults=50).execute()
    threads = resp.get("threads", [])
    known = ledger_ids()
    new = [t for t in threads if t["id"] not in known]
    print(f"found {len(threads)} threads in window, {len(new)} new")

    for t in new:
        tid = t["id"]
        full = svc.users().threads().get(userId="me", id=tid, format="full").execute()
        msg = full["messages"][0]  # digests are a single message
        subject = _header(msg, "Subject") or "(no subject)"
        sender = _header(msg, "From")
        date = _header(msg, "Date")
        source = _source_of(sender)

        if args.dry_run:
            print(f"  [dry] {source:<12} {subject[:70]}")
            continue

        plain, html = _walk_parts(msg["payload"])
        links, other_domains = harvest_links(html, plain)
        # text: stripped HTML (what a human sees) beats plain, which is often missing
        text = strip_html(html) if html.strip() else plain.strip()
        text = text[:MAX_TEXT_CHARS]

        stamp = datetime.now().strftime("%Y%m%d")
        fname = f"{stamp}-{source}-{tid}.txt"
        lines = [
            f"Subject: {subject}",
            f"From:    {sender}",
            f"Date:    {date}",
            f"Gmail:   https://mail.google.com/mail/u/0/#all/{tid}",
            "",
            f"=== canonical job links ({len(links)}) ===",
        ]
        lines += [f"{s:<12} {jid:<18} {url}" for s, jid, url in links]
        if not links:
            lines.append("⚠️ zero recognised links — the board may have changed its "
                         "template. Read the text below and raise a flag.")
        if other_domains:
            top = sorted(other_domains.items(), key=lambda kv: -kv[1])[:5]
            lines.append("(unrecognised: " + ", ".join(f"{d}×{n}" for d, n in top) + ")")
        lines += ["", "=== email body (stripped text) ===", text, ""]

        # Atomic publish: write to a hidden temp file then os.replace — the agent
        # (which may wake up concurrently after the machine resumes) never sees a
        # half-written file.
        path = os.path.join(INBOX, fname)
        tmp = os.path.join(INBOX, f".tmp-{fname}")
        with open(tmp, "w") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
        ledger_append({
            "thread_id": tid,
            "date_fetched": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "subject": subject,
            "file": fname,
        })
        print(f"  ✓ {source:<12} {len(links):>2} links  {subject[:60]}")

    if not new:
        print("no new digests.")


if __name__ == "__main__":
    main()
