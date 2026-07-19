#!/usr/bin/env python3
"""
Job Tracker — the bridge between the agent and Google Sheets.

Commands:
  read                      print all tracker rows as JSON
  upsert-jobs <file.json>   add new jobs (skips duplicates)
  update-status <file.json> update fields on existing rows
  flag <file.json>          mark rows for the user's review (orange) — never changes status
  notify <file.md>          email the run summary to the user (fixed recipient)
  reformat                  re-apply all sheet formatting over the full grid

This script never deletes rows and never rewrites the sheet.
"""
import argparse
import json
import os
import re
import sys
from base64 import urlsafe_b64encode
from datetime import datetime
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
#   spreadsheets    — write to the tracker sheet.
#   gmail.send      — send the run summary. Recipient is fixed below; no read access.
#   drive.readonly  — read-only; no delete/modify permissions on Drive.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

# Recipient is hardcoded. The agent cannot email anyone else.
MAIL_TO = "you@example.com"  # ← your address

TAB_JOBS = "Job Tracker"
TAB_LOG = "יומן עדכונים"  # audit-log tab

# A..S (schema v2). Headers mix English and Hebrew on purpose — this is the
# product's UI, built Hebrew-first for the Israeli job market. Same goes for
# the audit-log entries and the email subject further down.
COLUMNS = [
    "My Move", "Stage", "Company", "Job Title", "Fit", "Category", "Location",
    "Exp. Req.", "תיאור התפקיד", "Why I Fit", "למה זה מתאים לי",
    "Source", "Apply Link", "תאריך הגשה", "מעקב הבא", "איש קשר", "הערות",
    "עודכן", "Priority",
]
KEYS = [
    "move", "stage", "company", "title", "fit", "category", "location",
    "exp", "desc_he", "why_en", "why_he", "source", "link", "applied_date",
    "next_followup", "contact", "notes", "updated", "priority",
]
COL = {k: i for i, k in enumerate(KEYS)}
NCOLS = len(KEYS)

LOG_COLUMNS = ["תאריך", "פעולה", "חברה", "תפקיד", "שדה", "ערך קודם", "ערך חדש", "מקור / קישור"]

# Column A — the user's decision. Only the user touches it.
MOVE_OPTIONS = ["⭐ To Apply", "📝 Tailor CV", "🙋 Reach Out", "✅ Applied", "❌ Not Relevant"]
# Column B — pipeline stage. The user updates it manually; the agent only
# fills it when empty, and flags for review on any conflict.
STAGE_OPTIONS = ["📤 Applied", "📞 Screening", "🎤 Interview", "📝 Home Assignment",
                 "🎯 Action Required", "🎉 Offer", "❌ Rejected", "🔇 No Response"]

FIT_ORDER = {"🟢 Strong": 0, "🟡 Good Shot": 1, "🔴 Stretch": 2}

FLAG_PREFIX = "⚠️ לאישורך"  # "for your review" — prefix used in the notes column
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
                    "(OAuth client of type 'Desktop app') — see SETUP.md."
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
        sys.exit(f"Missing {CONFIG} (the spreadsheet id). "
                 "Copy config.example.json and fill it in — see SETUP.md.")
    with open(CONFIG) as f:
        return json.load(f)


# ---------------------------------------------------------------- dedup key


_LOCATION_TOKENS = {
    "tlv", "tel", "aviv", "yafo", "jaffa", "herzliya", "ramat", "gan", "petah",
    "tikva", "raanana", "netanya", "bnei", "brak", "or", "yehuda", "israel",
    "hybrid", "remote", "onsite",
}
_NOISE = {"the", "a", "an", "and", "of", "for", "il"}


def _norm(text):
    text = (text or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)          # drop parenthesized content
    text = re.sub(r"[^a-z0-9֐-׿]+", " ", text)  # strip punctuation and emoji, keep Hebrew
    words = [w for w in text.split() if w not in _LOCATION_TOKENS and w not in _NOISE]
    return " ".join(words)


def job_key(company, title):
    """Company + title. Same company with a different title = new key = new row."""
    return f"{_norm(company)}|{_norm(title)}"


# Query params that identify a specific job posting. Many boards put the job
# id in the query rather than the path (Indeed jk=, AllJobs JobID=, Greenhouse
# gh_jid=, ...) — without them, every job on such a board maps to the same URL
# path and gets wrongly skipped as a duplicate.
_JOB_ID_PARAM = re.compile(r"^jk$|job|jid|position|vacancy|req", re.I)


def link_id(link):
    """Stable identity for a link — catches exact duplicates even when the
    title was phrased differently."""
    if not link:
        return None
    parts = urlsplit(link.strip())
    if not parts.scheme.startswith("http") or not parts.netloc:
        return None
    ids = [f"{k.lower()}={v}" for k, v in sorted(parse_qsl(parts.query))
           if _JOB_ID_PARAM.search(k)]
    base = f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
    return base + (f"?{'&'.join(ids)}" if ids else "")


# ---------------------------------------------------------------- sheet io


def read_rows(sheets, cfg):
    """Returns [(row_number, {key: value})] — row_number is 1-based in the sheet."""
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_JOBS}'!A2:S",
    ).execute()
    out = []
    for i, values in enumerate(resp.get("values", [])):
        values = list(values) + [""] * (NCOLS - len(values))
        row = {k: values[COL[k]] for k in KEYS}
        if not row["company"] and not row["title"]:
            continue  # blank row
        out.append((i + 2, row))
    return out


def _snapshot(rows, label):
    os.makedirs(SNAPSHOTS, exist_ok=True)
    path = os.path.join(SNAPSHOTS, f"{datetime.now():%Y-%m-%d-%H%M}-{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([r for _, r in rows], f, ensure_ascii=False, indent=1)
    # retention: prune snapshots older than 30 days
    cutoff = datetime.now().timestamp() - 30 * 86400
    for name in os.listdir(SNAPSHOTS):
        p = os.path.join(SNAPSHOTS, name)
        if name.endswith(".json") and os.path.getmtime(p) < cutoff:
            os.remove(p)
    return path


def _log(sheets, cfg, entries):
    """New entries are inserted at the top of the audit log."""
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


def _today():
    return datetime.now().strftime("%d/%m/%Y")


def _priority(row):
    """Priority rank as an integer (1 = highest). Computed in Python rather
    than as a sheet formula — row-referencing formulas break under sortRange."""
    move, stage, fit = row["move"], row["stage"], row["fit"]
    if any(x in stage for x in ("Screening", "Interview", "Home Assignment",
                                "Action Required", "Offer")):
        return 1
    if any(x in move for x in ("To Apply", "Tailor CV", "Reach Out")):
        return 2
    if "Applied" in stage:
        return 3
    if "Not Relevant" in move or "Rejected" in stage or "No Response" in stage:
        return 9
    if "🟢" in fit:
        return 4
    if "🟡" in fit:
        return 5
    return 6


def _resort(sheets, cfg):
    """Recomputes Priority for all rows, writes it, then sorts the table by it.
    Must run after every insert/update — otherwise new rows stay stuck at the
    bottom regardless of relevance."""
    rows = read_rows(sheets, cfg)
    if len(rows) < 2:
        return
    jid = cfg["jobs_sheet_id"]
    s_col = COL["priority"]
    # tie-break: Tel Aviv first, then company name — folded into the priority
    # value as a small fraction
    def val(r):
        p = _priority(r)
        loc = r["location"].lower()
        tlv = 0 if ("tel aviv" in loc or "תל אביב" in loc) else 1
        return p + tlv * 0.1
    values = [[val(r)] for _, r in rows]
    sheets.spreadsheets().values().update(
        spreadsheetId=cfg["spreadsheet_id"],
        range=f"'{TAB_JOBS}'!S2",
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
    """Re-applies all formatting over the *entire* grid (open-ended range, not
    just the currently filled rows): conditional colors, dropdowns, RTL
    alignment, wrap, filter. Guards against the class of bugs where formatting
    was applied to a fixed range and every row added later was left bare.
    Deletes all existing conditional-format rules and writes a clean set in
    the correct precedence order."""
    sheets, _, _ = services()
    cfg = load_config()
    jid = cfg["jobs_sheet_id"]

    meta = sheets.spreadsheets().get(spreadsheetId=cfg["spreadsheet_id"]).execute()
    tabs = [s["properties"]["title"] for s in meta["sheets"]]
    js = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == jid)
    nrows = js["properties"]["gridProperties"]["rowCount"]
    old_rules = len(js.get("conditionalFormats", []))
    print(f"tabs: {tabs} · grid rows: {nrows} · existing format rules: {old_rules}")

    body = {"sheetId": jid, "startRowIndex": 1, "endRowIndex": nrows,
            "startColumnIndex": 0, "endColumnIndex": NCOLS}

    # delete all existing rules — always index 0, since each delete shifts the rest up
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

    # RTL alignment for the Hebrew columns
    for k in ("desc_he", "why_he", "notes"):
        reqs.append({"repeatCell": {
            "range": dict(body, startColumnIndex=COL[k], endColumnIndex=COL[k] + 1),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
            "fields": "userEnteredFormat.horizontalAlignment"}})

    # conditional colors — list order is precedence: "for your review" beats everything
    rules = [
        # ⚠️ flagged for the user's review — bold orange, always on top
        (f'=REGEXMATCH($Q2&"","{FLAG_PREFIX}")', ORANGE, "bold"),
        # rejected / not relevant — gray
        ('=OR(REGEXMATCH($A2&"","Not Relevant"),REGEXMATCH($B2&"","Rejected|No Response"))',
         {"red": .93, "green": .93, "blue": .93}, "gray"),
        # 🎯 ball in the user's court — bold red
        ('=REGEXMATCH($B2&"","Action Required")', {"red": 1, "green": .80, "blue": .78}, None),
        # active process — blue
        ('=REGEXMATCH($B2&"","Screening|Interview|Home Assignment|Offer")',
         {"red": .82, "green": .89, "blue": .98}, None),
        # marked for action by the user — light orange
        ('=REGEXMATCH($A2&"","To Apply|Tailor CV|Reach Out")',
         {"red": 1, "green": .93, "blue": .80}, None),
        # default: color by Fit
        ('=REGEXMATCH($E2&"","🟢")', {"red": .85, "green": .94, "blue": .83}, None),
        ('=REGEXMATCH($E2&"","🟡")', {"red": 1.0, "green": .97, "blue": .80}, None),
        ('=REGEXMATCH($E2&"","🔴")', {"red": .99, "green": .87, "blue": .84}, None),
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

    new_rows, log, skipped, bad_link = [], [], [], []
    for job in incoming:
        company, title = job.get("company", "").strip(), job.get("title", "").strip()
        if not company or not title:
            continue

        # The link must be a real URL. Text like "🔗 Apply" is not a link —
        # and a job without a direct application link is worthless in the tracker.
        link = job.get("link", "").strip()
        if not link.startswith("http"):
            bad_link.append(f"{company} — {title} (link={link!r})")
            continue

        key, lid = job_key(company, title), link_id(link)
        if key in by_key or (lid and lid in by_link):
            skipped.append(f"{company} — {title}")
            continue
        by_key[key] = None  # prevents duplicates within the same batch
        if lid:
            by_link[lid] = None

        row = [""] * NCOLS
        for k in KEYS:
            if k in job:
                row[COL[k]] = str(job[k])
        row[COL["updated"]] = _today()
        new_rows.append(row)
        log.append([_today(), "נוספה משרה", company, title, "", "",
                    f"{job.get('fit', '')} · {job.get('source', '')}", job.get("link", "")])

    if new_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=cfg["spreadsheet_id"],
            range=f"'{TAB_JOBS}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
        _resort(sheets, cfg)   # computes Priority for the new rows + resorts the table
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
            # a new flag replaces an old flag but preserves everything else in the notes
            keep = " | ".join(s for s in old.split(" | ") if FLAG_PREFIX not in s)
            merged = f"{keep} | {note}" if keep else note
            fields = {"notes": merged}
            log.append([_today(), "⚠️ סומן לאישורך", company, title, "הערות",
                        old, merged, gmail])
        else:
            fields = {k: str(v) for k, v in item.items()
                      if k in COL and k not in ("company", "title")}
            for k, v in fields.items():
                log.append([_today(), "עודכן סטטוס", company, title, COLUMNS[COL[k]],
                            row.get(k, ""), v, item.get("gmail_link", "")])

        fields["updated"] = _today()
        for k, v in fields.items():
            c = chr(ord("A") + COL[k])
            data.append({"range": f"'{TAB_JOBS}'!{c}{n}", "values": [[v]]})

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
        _resort(sheets, cfg)   # move/stage/fit changes affect priority → resort
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
    """Converts the summary body (light markdown) to HTML. Auto-detects
    direction: Hebrew → RTL (right-aligned), English → LTR (left-aligned)."""
    import html as _html

    heb = len(re.findall(r"[֐-׿]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    rtl = heb >= lat
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

    # width:100% + text-align pin the content to the correct reading side
    # (instead of getting stuck centered or on the wrong side)
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
    msg["To"] = MAIL_TO          # fixed. Cannot be changed via input.
    msg["From"] = MAIL_TO
    # Hebrew default subject ("job search summary — <date>") — kept stable so a
    # Gmail filter can label these digests reliably
    msg["Subject"] = args.subject or f"סיכום חיפוש עבודה — {_today()}"
    msg.set_content(body)                                   # plain-text fallback
    msg.add_alternative(_md_to_html(body), subtype="html")  # primary view — RTL-aware

    sent = gmail.users().messages().send(
        userId="me",
        body={"raw": urlsafe_b64encode(msg.as_bytes()).decode()},
    ).execute()
    print(json.dumps({"sent_to": MAIL_TO, "id": sent["id"]}, ensure_ascii=False))


# ---------------------------------------------------------------- cli


def main():
    p = argparse.ArgumentParser(description="Job Tracker — bridge to Google Sheets")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("read", help="print the table as JSON").set_defaults(func=cmd_read)

    sub.add_parser("reformat",
                   help="re-apply all formatting over the full grid").set_defaults(func=cmd_reformat)

    u = sub.add_parser("upsert-jobs", help="add new jobs")
    u.add_argument("file")
    u.set_defaults(func=cmd_upsert)

    s = sub.add_parser("update-status", help="update fields on existing rows")
    s.add_argument("file")
    s.set_defaults(func=cmd_update)

    f = sub.add_parser("flag", help="mark rows for the user's review")
    f.add_argument("file")
    f.set_defaults(func=cmd_flag)

    n = sub.add_parser("notify", help="email the summary to the user")
    n.add_argument("file")
    n.add_argument("--subject")
    n.set_defaults(func=cmd_notify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
