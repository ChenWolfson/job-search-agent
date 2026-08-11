#!/usr/bin/env python3
"""
Job Tracker — the bridge between the agent and Google Sheets.

Commands:
  read                      print every table row as JSON
  upsert-jobs <file.json>   add new jobs (skips duplicates)
  update-status <file.json> update fields on existing rows
  flag <file.json>          mark rows "for review" (orange) — never changes status
  notify <file.md>          email the run digest to the fixed recipient only
  reformat                  re-apply all formatting to the whole sheet (open range)
  resort                    recompute Priority and re-sort (after manual edits)
  dashboard-activity        recompute the RECENT ACTIVITY block on the dashboard

The script never deletes rows and never rewrites a sheet.
"""
import argparse
import json
import os
import re
import sys
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlsplit

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------- constants

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, ".secrets")
CREDENTIALS = os.path.join(SECRETS, "credentials.json")
TOKEN = os.path.join(SECRETS, "token.json")
CONFIG = os.path.join(HERE, "config.json")
SNAPSHOTS = os.path.join(HERE, "snapshots")

# Deliberately minimal scopes:
#   drive.readonly  — read the source file only. No delete or modify rights on Drive.
#   spreadsheets    — write to the tracker sheet.
#   gmail.send      — send the run digest. The recipient is hardcoded below.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

# ---------------------------------------------------------------------------
# SET THIS BEFORE FIRST RUN — your own address.
# Hardcoded on purpose: the agent writes intent files, never this constant, so
# it cannot be tricked into mailing anyone else. Combined with the send-only
# Gmail scope, the blast radius of a compromised agent is one email to you.
# ---------------------------------------------------------------------------
MAIL_TO = "you@example.com"

TAB_JOBS = "Job Tracker"
TAB_LOG = "Change Log"
TAB_DASH = "📊 Dashboard"

# A..U
COLUMNS = [
    "My Move", "Stage", "Company", "Job Title", "Fit", "Category", "Location",
    "Exp. Req.", "Job Description", "Why I Fit",
    "Source", "Apply Link", "Applied On", "Next Follow-up", "Contact",
    "Salary Range", "Hybrid", "Notes",
    "Hiring Process", "Updated", "Priority",
]
KEYS = [
    "move", "stage", "company", "title", "fit", "category", "location",
    "exp", "desc", "why", "source", "link", "applied_date",
    "next_followup", "contact", "salary_range", "hybrid", "notes",
    "process", "updated", "priority",
]
# salary_range, hybrid — manual only, owned by you. The agent never writes them
# (not in upsert, not in update-status). Most postings don't state either
# honestly, so guessing them costs verification effort for no real signal.
MANUAL_ONLY_KEYS = {"salary_range", "hybrid"}
COL = {k: i for i, k in enumerate(KEYS)}
NCOLS = len(KEYS)


def a1col(idx):
    """0 → A, 25 → Z, 26 → AA."""
    s = ""
    while True:
        s = chr(ord("A") + idx % 26) + s
        idx = idx // 26 - 1
        if idx < 0:
            return s


LAST_COL = a1col(NCOLS - 1)

LOG_COLUMNS = ["Date", "Action", "Company", "Job Title", "Field",
               "Old Value", "New Value", "Source / Link"]

# Column A — your decision. Only you touch it.
MOVE_OPTIONS = ["⭐ To Apply", "📝 Tailor CV", "🙋 Reach Out", "✅ Applied", "❌ Not Relevant"]
# Column B — pipeline stage. You update it manually; the agent fills it only when
# empty, and flags "for review" on any conflict.
STAGE_OPTIONS = ["📤 Applied", "📞 Screening", "🎤 Interview", "📝 Home Assignment",
                 "🎯 Action Required", "🎉 Offer", "❌ Rejected", "🔇 No Response"]

FIT_ORDER = {"🟢 Strong": 0, "🟡 Good Shot": 1, "🔴 Stretch": 2}

FLAG_PREFIX = "⚠️ For Review"
ORANGE = {"red": 1.0, "green": 0.90, "blue": 0.75}

# ---------------------------------------------------------------- auth


def services():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS):
                sys.exit(
                    f"Missing {CREDENTIALS}\n"
                    "Download credentials.json from Google Cloud "
                    "(OAuth Client, type: Desktop). See SETUP.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return (
        build("sheets", "v4", credentials=creds),
        build("drive", "v3", credentials=creds),
        build("gmail", "v1", credentials=creds),
    )


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit(f"Missing {CONFIG} — the spreadsheet id. "
                 "Copy config.example.json and fill it in; never create a new sheet.")
    with open(CONFIG) as f:
        cfg = json.load(f)
    # Region-specific noise words for the dedup key — city names, country, etc.
    # Set "location_tokens" in config.json for your own market.
    _LOCATION_TOKENS.update(t.lower() for t in cfg.get("location_tokens", []))
    return cfg


# ---------------------------------------------------------------- dedup key


# Words stripped before comparing company/title, so "QA Engineer (Berlin)" and
# "QA Engineer - Hybrid" collapse to the same key. Add your own city names and
# country via "location_tokens" in config.json.
_LOCATION_TOKENS = {"hybrid", "remote", "onsite", "office"}
_NOISE = {"the", "a", "an", "and", "of", "for"}


def _norm(text):
    text = (text or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)      # drop parenthesised content
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)   # punctuation and emoji out
    words = [w for w in text.split() if w not in _LOCATION_TOKENS and w not in _NOISE]
    return " ".join(words)


def job_key(company, title):
    """Company + title. Same company, different title = different key = new row."""
    return f"{_norm(company)}|{_norm(title)}"


# Job ids hiding in the link or the notes — a run of >=6 digits (req/JobID/etc).
_DIGIT_ID = re.compile(r"\d{6,}")


def _digit_ids(*fields):
    ids = set()
    for f in fields:
        ids.update(_DIGIT_ID.findall(f or ""))
    return ids


# Query params that identify a specific posting. Many boards put the job id in
# the query rather than the path (Indeed jk=, Greenhouse gh_jid=, ...) — without
# them every job on a board maps to the same path and gets blocked as a dupe.
_JOB_ID_PARAM = re.compile(r"^jk$|job|jid|position|vacancy|req", re.I)


def link_id(link):
    """Stable identity from a link — catches exact dupes even if the title differs."""
    if not link:
        return None
    parts = urlsplit(link.strip())
    if not parts.scheme.startswith("http") or not parts.netloc:
        return None
    ids = [f"{k.lower()}={v}" for k, v in sorted(parse_qsl(parts.query))
           if _JOB_ID_PARAM.search(k)]
    # There is a query but no param looks like a job id — the link is probably a
    # mailing-system redirect (e.g. /ls/click?upn=...). netloc+path is then shared
    # by every job from that source, so a shared base would block them all as
    # false dupes. Better to skip link-dedup and fall back to company+title: a
    # rare duplicate beats silently dropping a real job.
    if parts.query and not ids:
        return None
    base = f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
    return base + (f"?{'&'.join(ids)}" if ids else "")


# ---------------------------------------------------------------- sheet io


def read_rows(sheets, cfg):
    """Returns [(row_number, {key: value})] — row_number is 1-based in the sheet."""
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_JOBS}'!A2:{LAST_COL}",
    ).execute()
    out = []
    for i, values in enumerate(resp.get("values", [])):
        values = list(values) + [""] * (NCOLS - len(values))
        row = {k: values[COL[k]] for k in KEYS}
        if not row["company"] and not row["title"]:
            continue  # empty row
        out.append((i + 2, row))
    return out


def _snapshot(rows, label):
    os.makedirs(SNAPSHOTS, exist_ok=True)
    path = os.path.join(SNAPSHOTS, f"{datetime.now():%Y-%m-%d-%H%M}-{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([r for _, r in rows], f, ensure_ascii=False, indent=1)
    # retention: snapshots older than 30 days are deleted
    cutoff = datetime.now().timestamp() - 30 * 86400
    for name in os.listdir(SNAPSHOTS):
        p = os.path.join(SNAPSHOTS, name)
        if name.endswith(".json") and os.path.getmtime(p) < cutoff:
            os.remove(p)
    return path


def _log(sheets, cfg, entries):
    """New entries go in at the top of the log."""
    if not entries:
        return
    sid = cfg["log_sheet_id"]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=cfg["spreadsheet_id"],
        body={"requests": [{
            "insertDimension": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 1 + len(entries)},
                "inheritFromBefore": False,
            }
        }]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_LOG}'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": entries},
    ).execute()


def _now():
    """Date + time — for the change log and the "Updated" column."""
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def _today():
    return datetime.now().strftime("%d/%m/%Y")


def _priority(row):
    """Priority as an integer (1 = highest). Computed in Python, not as a sheet
    formula — a formula that references its own row number breaks under sortRange."""
    move, stage, fit = row["move"], row["stage"], row["fit"]
    # ❌ Not Relevant — your decision, and it beats *everything*, including an
    # active process. Must come first: a job you walked away from mid-process
    # still carries an active Stage (Action Required / Interview) that cannot be
    # cleared, and without this it would stay pinned to the top of the table.
    # (Conditional formatting already behaves this way — grey beats red.)
    if "Not Relevant" in move:
        return 9
    if any(x in stage for x in ("Screening", "Interview", "Home Assignment",
                                "Action Required", "Offer")):
        return 1
    if any(x in move for x in ("To Apply", "Tailor CV", "Reach Out")):
        return 2
    # Rejected / silent — to the bottom. Must come *before* the Applied check: a
    # rejected row still carries Move=✅ Applied (you applied, then got rejected),
    # and without this it would climb back up to 3.
    if "Rejected" in stage or "No Response" in stage:
        return 9
    # Applied and waiting — by Stage (evidence-based) *or* by Move=✅ Applied
    # (your own declaration). Marking Move alone is enough; you don't have to
    # fill Stage as well for the row to rise.
    if "Applied" in stage or "Applied" in move:
        return 3
    if "🟢" in fit:
        return 4
    if "🟡" in fit:
        return 5
    return 6


def _resort(sheets, cfg):
    """Recomputes Priority for every row (as an integer), writes it, and sorts by it.
    Must run after every add/update — otherwise new rows stay stuck at the bottom."""
    rows = read_rows(sheets, cfg)
    if len(rows) < 2:
        return
    jid = cfg["jobs_sheet_id"]
    s_col = COL["priority"]
    # tie-break: your preferred location first, then company name — folded into
    # the column value as a small fraction. "home_city" comes from config.json;
    # leave it empty and the tie-break simply does nothing.
    home = (cfg.get("home_city") or "").strip().lower()

    def val(r):
        p = _priority(r)
        near = 0 if (home and home in r["location"].lower()) else 1
        return p + near * 0.1

    values = [[val(r)] for _, r in rows]
    sheets.spreadsheets().values().update(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_JOBS}'!{a1col(s_col)}2",
        valueInputOption="USER_ENTERED",
        body={"values": values}).execute()
    sheets.spreadsheets().batchUpdate(spreadsheetId=cfg["spreadsheet_id"], body={"requests": [
        {"repeatCell": {
            "range": {"sheetId": jid, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                      "startColumnIndex": s_col, "endColumnIndex": s_col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"sortRange": {
            "range": {"sheetId": jid, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                      "startColumnIndex": 0, "endColumnIndex": NCOLS},
            "sortSpecs": [{"dimensionIndex": s_col, "sortOrder": "ASCENDING"}]}},
    ]}).execute()


# ---------------------------------------------------------------- reformat


def cmd_reformat(args):
    """Re-applies all formatting to the *whole* grid (open range, not just the rows
    that exist today): conditional colours, dropdowns, text direction, wrap, filter.
    Fixes the class of bug where formatting was applied once over a fixed range and
    every row added later stayed unformatted.
    Deletes all existing colour rules and writes a clean set in the right order."""
    sheets, _, _ = services()
    cfg = load_config()
    jid = cfg["jobs_sheet_id"]

    meta = sheets.spreadsheets().get(spreadsheetId=cfg["spreadsheet_id"]).execute()
    tabs = [s["properties"]["title"] for s in meta["sheets"]]
    js = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == jid)
    nrows = js["properties"]["gridProperties"]["rowCount"]
    old_rules = len(js.get("conditionalFormats", []))
    print(f"tabs: {tabs} · grid rows: {nrows} · existing colour rules: {old_rules}")

    body = {"sheetId": jid, "startRowIndex": 1, "endRowIndex": nrows,
            "startColumnIndex": 0, "endColumnIndex": NCOLS}

    # Delete every existing rule — always index 0, since each delete shifts the rest up
    reqs = [{"deleteConditionalFormatRule": {"sheetId": jid, "index": 0}}] * old_rules

    reqs += [
        # table body: wrap text, top-align
        {"repeatCell": {
            "range": dict(body),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                           "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        # autofilter over the whole grid
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": jid, "startRowIndex": 0, "endRowIndex": nrows,
            "startColumnIndex": 0, "endColumnIndex": NCOLS}}}},
        # dropdowns: My Move + Stage
        {"setDataValidation": {
            "range": dict(body, startColumnIndex=COL["move"], endColumnIndex=COL["move"] + 1),
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": o} for o in MOVE_OPTIONS]},
                     "showCustomUi": True, "strict": False}}},
        {"setDataValidation": {
            "range": dict(body, startColumnIndex=COL["stage"], endColumnIndex=COL["stage"] + 1),
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": o} for o in STAGE_OPTIONS]},
                     "showCustomUi": True, "strict": False}}},
    ]

    # Right-align columns whose content is in a right-to-left language.
    # Set "rtl_columns" in config.json (e.g. ["desc", "notes"]); empty by default.
    for k in cfg.get("rtl_columns", []):
        if k not in COL:
            continue
        reqs.append({"repeatCell": {
            "range": dict(body, startColumnIndex=COL[k], endColumnIndex=COL[k] + 1),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
            "fields": "userEnteredFormat.horizontalAlignment"}})

    # Conditional colours — order *is* precedence: "for review" beats everything.
    # Column letters are derived from COL so adding or moving a column can't
    # silently break the formulas.
    c_move, c_stage, c_fit, c_notes = (a1col(COL[k]) for k in ("move", "stage", "fit", "notes"))
    rules = [
        # ⚠️ For Review — bold orange, always on top
        (f'=REGEXMATCH(${c_notes}2&"","{FLAG_PREFIX}")', ORANGE, "bold"),
        # rejected / not relevant — grey
        (f'=OR(REGEXMATCH(${c_move}2&"","Not Relevant"),REGEXMATCH(${c_stage}2&"","Rejected|No Response"))',
         {"red": .93, "green": .93, "blue": .93}, "gray"),
        # 🎯 ball is in your court — strong red
        (f'=REGEXMATCH(${c_stage}2&"","Action Required")', {"red": 1, "green": .80, "blue": .78}, None),
        # active process — blue
        (f'=REGEXMATCH(${c_stage}2&"","Screening|Interview|Home Assignment|Offer")',
         {"red": .82, "green": .89, "blue": .98}, None),
        # what you marked for action — light orange
        (f'=REGEXMATCH(${c_move}2&"","To Apply|Tailor CV|Reach Out")',
         {"red": 1, "green": .93, "blue": .80}, None),
        # default by Fit
        (f'=REGEXMATCH(${c_fit}2&"","🟢")', {"red": .85, "green": .94, "blue": .83}, None),
        (f'=REGEXMATCH(${c_fit}2&"","🟡")', {"red": 1.0, "green": .97, "blue": .80}, None),
        (f'=REGEXMATCH(${c_fit}2&"","🔴")', {"red": .99, "green": .87, "blue": .84}, None),
    ]
    for idx, (formula, color, style) in enumerate(rules):
        fmt = {"backgroundColor": color}
        if style == "bold":
            fmt["textFormat"] = {"bold": True}
        elif style == "gray":
            fmt["textFormat"] = {"foregroundColor": {"red": .5, "green": .5, "blue": .5}}
        reqs.append({"addConditionalFormatRule": {"index": idx, "rule": {
            "ranges": [dict(body)],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": formula}]},
                            "format": fmt}}}})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=cfg["spreadsheet_id"], body={"requests": reqs}).execute()
    print(json.dumps({"rules_deleted": old_rules, "rules_added": len(rules),
                      "formatted_rows": nrows - 1, "tabs": tabs}, ensure_ascii=False))


# ---------------------------------------------------------------- read


def cmd_resort(args):
    """Recomputes every Priority number and sorts the table — without changing data.
    Useful after manually editing Move/Stage, or after changing _priority."""
    sheets, _, _ = services()
    cfg = load_config()
    _resort(sheets, cfg)
    print("resorted ✓")


def cmd_read(args):
    sheets, _, _ = services()
    cfg = load_config()
    rows = read_rows(sheets, cfg)
    print(json.dumps(
        [dict(r, _row=n, _key=job_key(r["company"], r["title"])) for n, r in rows],
        ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- upsert


def cmd_upsert(args):
    with open(args.file, encoding="utf-8") as f:
        incoming = json.load(f)

    sheets, _, _ = services()
    cfg = load_config()
    existing = read_rows(sheets, cfg)
    _snapshot(existing, "before-upsert")

    by_key = {job_key(r["company"], r["title"]): (n, r) for n, r in existing}
    by_link = {}
    for n, r in existing:
        lid = link_id(r["link"])
        if lid:
            by_link[lid] = (n, r)

    # A third key: the same job arrives once from a board under the legal entity
    # name ("ACME EMEA SARL") and once from the ATS under the brand ("Acme") —
    # company+title misses, and the links differ. So: identical normalised title
    # + a shared job id (>=6 digits in the link or notes) = same job, skip it.
    by_title = []
    for n, r in existing:
        ids = _digit_ids(r.get("link"), r.get("notes"))
        if ids:
            by_title.append((_norm(r["title"]), ids))

    new_rows, log, skipped, bad_link = [], [], [], []
    for job in incoming:
        company, title = job.get("company", "").strip(), job.get("title", "").strip()
        if not company or not title:
            continue

        # The link must be a real URL. Text like "🔗 Apply" is not a link — and a
        # job with no direct link is worthless when you come back to apply.
        link = job.get("link", "").strip()
        if not link.startswith("http"):
            bad_link.append(f"{company} — {title} (link={link!r})")
            continue

        key, lid = job_key(company, title), link_id(link)
        if key in by_key or (lid and lid in by_link):
            skipped.append(f"{company} — {title}")
            continue
        ids, nt = _digit_ids(link, job.get("notes")), _norm(title)
        if any(nt == t and ids & xids for t, xids in by_title):
            skipped.append(f"{company} — {title} (same title + shared job id)")
            continue
        by_key[key] = None  # prevents duplicates within the same run
        if lid:
            by_link[lid] = None
        if ids:
            by_title.append((nt, ids))

        row = [""] * NCOLS
        for k in KEYS:
            if k in job and k not in MANUAL_ONLY_KEYS:
                row[COL[k]] = str(job[k])
        row[COL["updated"]] = _now()
        new_rows.append(row)
        log.append([_now(), "job added", company, title, "", "",
                    f"{job.get('fit', '')} · {job.get('source', '')}", job.get("link", "")])

    if new_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=cfg["spreadsheet_id"],
            range=f"'{TAB_JOBS}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
        _resort(sheets, cfg)   # computes Priority for the new rows + sorts the table
        _log(sheets, cfg, log)

    print(json.dumps({"added": len(new_rows),
                      "skipped_duplicates": len(skipped),
                      "skipped": skipped,
                      "rejected_no_valid_link": bad_link}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- update / flag


def _find(existing, company, title):
    key = job_key(company, title)
    for n, r in existing:
        if job_key(r["company"], r["title"]) == key:
            return n, r
    return None, None


def _apply_updates(sheets, cfg, existing, items, flag_mode):
    data, log, missing = [], [], []
    for item in items:
        company, title = item.get("company", ""), item.get("title", "")
        n, row = _find(existing, company, title)
        if not n:
            missing.append(f"{company} — {title}")
            continue

        if flag_mode:
            reason = item.get("reason", "")
            gmail = item.get("gmail_link", "")
            note = f"{FLAG_PREFIX} — {reason}".strip()
            old = row.get("notes", "")
            # a new flag replaces an old flag but keeps everything else in the notes
            keep = " | ".join(s for s in old.split(" | ") if FLAG_PREFIX not in s)
            merged = f"{keep} | {note}" if keep else note
            fields = {"notes": merged}
            log.append([_now(), "⚠️ flagged for review", company, title, "Notes",
                        old, merged, gmail])
        else:
            fields = {k: str(v) for k, v in item.items()
                      if k in COL and k not in ("company", "title") and k not in MANUAL_ONLY_KEYS}
            # "Hiring Process" is yours. The main text comes from phone calls the
            # agent never sees, so a direct write is allowed only into an empty
            # cell; otherwise it is turned into an appended line.
            # `process_append` is the correct channel for the agent.
            add = str(item.get("process_append", "") or "").strip()
            direct = fields.pop("process", None)
            if direct is not None:
                if row.get("process", "").strip():
                    add = f"{add}\n{direct}".strip() if add else direct
                else:
                    fields["process"] = direct
            if add:
                old_p = row.get("process", "").rstrip()
                if add in old_p:      # idempotent — a re-run doesn't duplicate the line
                    add = ""
                else:
                    fields["process"] = f"{old_p}\n\n{add}" if old_p else add
            for k, v in fields.items():
                log.append([_now(), "status updated", company, title, COLUMNS[COL[k]],
                            row.get(k, ""), v, item.get("gmail_link", "")])

        fields["updated"] = _now()
        for k, v in fields.items():
            data.append({"range": f"'{TAB_JOBS}'!{a1col(COL[k])}{n}", "values": [[v]]})

    if data:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=cfg["spreadsheet_id"],
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
        _log(sheets, cfg, log)
    return len(data), missing


def cmd_update(args):
    with open(args.file, encoding="utf-8") as f:
        items = json.load(f)
    sheets, _, _ = services()
    cfg = load_config()
    existing = read_rows(sheets, cfg)
    _snapshot(existing, "before-update")
    n, missing = _apply_updates(sheets, cfg, existing, items, flag_mode=False)
    if n:
        _resort(sheets, cfg)   # move/stage/fit changed → priority changed → re-sort
    print(json.dumps({"cells_updated": n, "not_found": missing},
                     ensure_ascii=False, indent=1))


def cmd_flag(args):
    with open(args.file, encoding="utf-8") as f:
        items = json.load(f)
    sheets, _, _ = services()
    cfg = load_config()
    existing = read_rows(sheets, cfg)
    _snapshot(existing, "before-flag")
    n, missing = _apply_updates(sheets, cfg, existing, items, flag_mode=True)
    print(json.dumps({"flagged": n, "not_found": missing},
                     ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- notify


def _md_to_html(text):
    """Renders the digest body (light markdown) as HTML. Detects text direction
    automatically, so the digest also reads correctly in a right-to-left language."""
    import html as _html

    rtl_chars = len(re.findall(r"[֐-ࣿ]", text))   # RTL script ranges
    latin = len(re.findall(r"[A-Za-z]", text))
    rtl = rtl_chars >= latin
    direction = "rtl" if rtl else "ltr"
    align = "right" if rtl else "left"
    pad = "padding-right" if rtl else "padding-left"

    def linkify(s):
        return re.sub(r'(https?://[^\s]+)',
                      r'<a href="\1" style="color:#2a6fdb">\1</a>', s)

    out = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out.append('<div style="height:8px"></div>')
            continue
        if re.match(r"^[=─—_]{5,}$", line.strip()):
            out.append('<hr style="border:none;border-top:1px solid #ddd;margin:12px 0">')
            continue
        esc = _html.escape(line)
        if line.startswith("## "):
            out.append(f'<div style="font-weight:700;font-size:15px;margin:14px 0 4px">'
                       f'{linkify(esc[3:])}</div>')
        elif line.lstrip().startswith(("•", "-", "*")):
            body = linkify(esc.lstrip()[1:].strip())
            out.append(f'<div style="margin:2px 0;{pad}:14px">• {body}</div>')
        else:
            out.append(f'<div style="margin:2px 0">{linkify(esc)}</div>')

    # width:100% + text-align keep the content pinned to the correct reading edge
    return (
        f'<div dir="{direction}" style="direction:{direction};text-align:{align};'
        f'width:100%;font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        f'line-height:1.6;color:#222">'
        + "\n".join(out) + "</div>"
    )


def cmd_notify(args):
    with open(args.file, encoding="utf-8") as f:
        body = f.read()

    _, _, gmail = services()
    msg = EmailMessage()
    msg["To"] = MAIL_TO          # fixed. Cannot be changed through the input file.
    msg["From"] = MAIL_TO
    msg["Subject"] = args.subject or f"Job search digest — {_today()}"
    msg.set_content(body)                                   # plain-text fallback
    msg.add_alternative(_md_to_html(body), subtype="html")  # main rendering

    sent = gmail.users().messages().send(
        userId="me",
        body={"raw": urlsafe_b64encode(msg.as_bytes()).decode()},
    ).execute()
    print(json.dumps({"sent_to": MAIL_TO, "id": sent["id"]}, ensure_ascii=False))


# ---------------------------------------------------------------- dashboard activity
#
# The "RECENT ACTIVITY" block — a measure of momentum. Compares the current state
# against a baseline snapshot (yesterday · 7 days) and counts state transitions.
# The whole comparison is plain Python — zero LLM tokens. It also catches your own
# manual edits, because it diffs *states*, not *events* in the log.

_ACT_LABELS = [
    "CVs submitted",
    "Marked not relevant",
    "New jobs added to pool",
    "Processes advanced (screening+)",
    "Rejections received",
    "New recruiter / ATS emails",   # supplied by the agent (Gmail), not by snapshot diff
]
_ACT_KEYS = ["cv", "nr", "new", "adv", "rej"]
_PIPELINE_ACTIVE = ("Screening", "Interview", "Home Assignment", "Offer", "Action Required")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _is_applied(r):
    return "✅ Applied" in r.get("move", "") or "📤 Applied" in r.get("stage", "")


def _in_pipeline(r):
    return any(x in r.get("stage", "") for x in _PIPELINE_ACTIVE)


def _activity_metrics(cur_rows, base_rows):
    """Counts state transitions between base_rows and cur_rows (both lists of dicts)."""
    base = {job_key(r.get("company", ""), r.get("title", "")): r for r in base_rows}
    m = {k: 0 for k in _ACT_KEYS}
    for row in cur_rows:
        b = base.get(job_key(row.get("company", ""), row.get("title", "")))
        if b is None:
            m["new"] += 1
        if _is_applied(row) and not (b and _is_applied(b)):
            m["cv"] += 1
        if "❌ Not Relevant" in row.get("move", "") \
                and not (b and "❌ Not Relevant" in b.get("move", "")):
            m["nr"] += 1
        if _in_pipeline(row) and not (b and _in_pipeline(b)):
            m["adv"] += 1
        if "Rejected" in row.get("stage", "") \
                and not (b and "Rejected" in b.get("stage", "")):
            m["rej"] += 1
    return m


def _snap_dt(name):
    """YYYY-MM-DD-HHMM-label.json → datetime (or None if unrecognised)."""
    try:
        p = name.split("-")
        return datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3][:2]), int(p[3][2:4]))
    except (ValueError, IndexError):
        return None


def _all_snapshots():
    out = []
    if os.path.isdir(SNAPSHOTS):
        for name in os.listdir(SNAPSHOTS):
            if name.endswith(".json"):
                dt = _snap_dt(name)
                if dt:
                    out.append((dt, os.path.join(SNAPSHOTS, name)))
    out.sort()
    return out


def _baseline_before(snaps, cutoff):
    """The most recent snapshot taken before cutoff (or None)."""
    chosen = None
    for dt, path in snaps:
        if dt < cutoff:
            chosen = (dt, path)
    return chosen


def _week_label(base, now):
    if not base:
        return "—"
    return "Last 7 days" if 6 <= (now - base[0]).days <= 8 else f"Since {base[0]:%d/%m}"


def _day_label(end, now):
    """The "yesterday" window = the previous calendar day, represented by the
    snapshot taken at the end of yesterday."""
    if not end:
        return "—"
    return "Yesterday" if end[0].date() == (now.date() - timedelta(days=1)) \
        else f"Up to {end[0]:%d/%m}"


def _sheet_id(sheets, cfg, title):
    meta = sheets.spreadsheets().get(
        spreadsheetId=cfg["spreadsheet_id"],
        fields="sheets.properties(sheetId,title)").execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    return None


def _write_activity(sheets, cfg, rec_label, wk_label, m_rec, m_wk, g_rec, g_wk):
    """Plants/updates the RECENT ACTIVITY block right after ACTION NEEDED (before
    PIPELINE). Idempotent — finds the anchor by its text and rewrites in place;
    only inserts rows if the block is missing."""
    sid = _sheet_id(sheets, cfg, TAB_DASH)
    colA = sheets.spreadsheets().values().get(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_DASH}'!A1:A80").execute().get("values", [])
    labels = [(r[0].strip() if r else "") for r in colA]

    def find(text):
        for i, v in enumerate(labels):
            if v == text:
                return i + 1
        return None

    anchor = find("RECENT ACTIVITY")
    if not anchor:
        pipeline = find("PIPELINE")
        if not pipeline:
            raise RuntimeError("No PIPELINE block found on the dashboard — "
                               "nowhere to plant RECENT ACTIVITY")
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=cfg["spreadsheet_id"],
            body={"requests": [{"insertDimension": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": pipeline - 1, "endIndex": pipeline - 1 + 8},
                "inheritFromBefore": False}}]}).execute()
        anchor = pipeline

    def num(m, key):
        return m[key] if m else "—"

    # The email row comes from the agent (Gmail), not from a snapshot diff. If this
    # run didn't supply a number (a plain dispatch with no agent scan), keep the
    # existing value instead of zeroing it.
    prev = sheets.spreadsheets().values().get(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_DASH}'!B{anchor + 6}:C{anchor + 6}").execute().get("values", [[]])
    prev_row = (prev[0] if prev else []) + ["", ""]
    email_b = g_rec if g_rec is not None else (prev_row[0] or "—")
    email_c = g_wk if g_wk is not None else (prev_row[1] or "—")

    vals = [["RECENT ACTIVITY", rec_label, wk_label]]
    for i, key in enumerate(_ACT_KEYS):
        vals.append([_ACT_LABELS[i], num(m_rec, key), num(m_wk, key)])
    vals.append([_ACT_LABELS[5], email_b, email_c])

    sheets.spreadsheets().values().update(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_DASH}'!A{anchor}:C{anchor + 6}",
        valueInputOption="USER_ENTERED",
        body={"values": vals}).execute()
    # the block heading is bold, like the other headings
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=cfg["spreadsheet_id"],
        body={"requests": [{"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": anchor - 1, "endRowIndex": anchor,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}}]}).execute()
    return anchor


def cmd_dashboard_activity(args):
    sheets, _, _ = services()
    cfg = load_config()
    rows = read_rows(sheets, cfg)
    current = [r for _, r in rows]
    now = datetime.now()
    snaps = _all_snapshots()
    midnight = datetime(now.year, now.month, now.day)

    # "Yesterday" = the previous calendar day only: end-of-yesterday state against
    # start-of-yesterday state. Not against now — otherwise yesterday's changes get
    # swallowed by the baseline and today leaks in.
    end_yst = _baseline_before(snaps, midnight)
    start_yst = _baseline_before(snaps, midnight - timedelta(days=1))
    # "7 days" = a rolling window up to now.
    wk = _baseline_before(snaps, now - timedelta(days=7)) or (snaps[0] if snaps else None)

    m_rec = (_activity_metrics(_load_json(end_yst[1]), _load_json(start_yst[1]))
             if end_yst and start_yst and end_yst[1] != start_yst[1] else None)
    m_wk = _activity_metrics(current, _load_json(wk[1])) if wk else None

    g_rec = g_wk = None
    if getattr(args, "file", None) and os.path.exists(args.file):
        g = _load_json(args.file)
        g_rec, g_wk = g.get("yesterday"), g.get("week")

    _write_activity(sheets, cfg,
                    _day_label(end_yst, now), _week_label(wk, now),
                    m_rec, m_wk, g_rec, g_wk)
    _snapshot(rows, "baseline")   # the baseline for the next run
    print(json.dumps({"yesterday": m_rec, "week": m_wk, "gmail": [g_rec, g_wk]},
                     ensure_ascii=False))


# ---------------------------------------------------------------- cli


def main():
    p = argparse.ArgumentParser(description="Job Tracker — bridge to Google Sheets")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("read", help="print the table as JSON").set_defaults(func=cmd_read)

    sub.add_parser("reformat",
                   help="re-apply all formatting to the whole sheet").set_defaults(func=cmd_reformat)

    sub.add_parser("resort",
                   help="re-sort by Priority (after manual edits)").set_defaults(func=cmd_resort)

    u = sub.add_parser("upsert-jobs", help="add new jobs")
    u.add_argument("file")
    u.set_defaults(func=cmd_upsert)

    s = sub.add_parser("update-status", help="update fields on existing rows")
    s.add_argument("file")
    s.set_defaults(func=cmd_update)

    f = sub.add_parser("flag", help='mark rows "for review"')
    f.add_argument("file")
    f.set_defaults(func=cmd_flag)

    n = sub.add_parser("notify", help="email the digest to the fixed recipient")
    n.add_argument("file")
    n.add_argument("--subject")
    n.set_defaults(func=cmd_notify)

    da = sub.add_parser("dashboard-activity",
                        help="recompute the RECENT ACTIVITY block on the dashboard")
    da.add_argument("file", nargs="?",
                    help='optional — json with email counts {"yesterday":N,"week":N}')
    da.set_defaults(func=cmd_dashboard_activity)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
