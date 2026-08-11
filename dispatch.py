#!/usr/bin/env python3
"""
dispatch.py — runs in an environment that has network access (launchd / manual),
and applies whatever the agent wrote to outbox/.

Why it exists: the agent runs in an isolated VM that blocks direct access to
googleapis.com (a platform constraint, not a bug). So the agent only *writes* an
intent file to outbox/, and this dispatcher — running normally, with network —
applies it to the sheet and sends the email.

Flow per outbox file: outbox/ → (claim) processing/ → applied → done/ (or failed/).
A lockfile prevents two dispatchers running at once. One consolidated email per cycle.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
TRACKER = os.path.join(HERE, "tracker.py")
OUTBOX = os.path.join(HERE, "outbox")
PROCESSING = os.path.join(OUTBOX, "processing")
DONE = os.path.join(OUTBOX, "done")
FAILED = os.path.join(OUTBOX, "failed")
LOCK = os.path.join(HERE, ".dispatch.lock")
LOG = os.path.join(HERE, "dispatch.log")

# Shown in log lines only. The address that actually receives mail is MAIL_TO in
# tracker.py — that is the one you must set.
MAIL_TO = "you@example.com"

# Every digest subject starts with this, so you can write one Gmail filter for
# the whole system. Change it if you like, but keep it stable.
SUBJECT_PREFIX = "Job search digest"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_tracker(cmd, payload, extra=None):
    """Writes payload to a temp file and runs tracker.py <cmd> <file>.
    Returns the JSON it printed."""
    if not payload:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        json.dump(payload, tf, ensure_ascii=False)
        path = tf.name
    try:
        args = [PY, TRACKER, cmd, path] + (extra or [])
        res = subprocess.run(args, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"{cmd} failed: {res.stderr.strip()[-400:]}")
        # tracker.py prints google-auth warnings to stderr and JSON to stdout
        out = res.stdout.strip()
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"_raw": out}
    finally:
        os.unlink(path)


def refresh_snapshot():
    """Saves a copy of the live table to latest_snapshot.json — the sandboxed agent
    cannot read the sheet directly, and it needs a picture of the current state for
    dedup and status checks."""
    snap = os.path.join(HERE, "latest_snapshot.json")
    res = subprocess.run([PY, TRACKER, "read"], capture_output=True, text=True, timeout=120)
    if res.returncode == 0 and res.stdout.strip():
        with open(snap, "w", encoding="utf-8") as f:
            f.write(res.stdout)
        log("  📸 latest_snapshot.json refreshed")
    else:
        log(f"  ⚠️ snapshot refresh failed: {res.stderr.strip()[-200:]}")


def update_dashboard(gmail_counts=None):
    """Runs tracker.py dashboard-activity — updates the RECENT ACTIVITY block.
    The whole comparison is a snapshot diff inside tracker.py (Python, zero tokens).
    gmail_counts (optional) — {'yesterday':N,'week':N} from the agent."""
    args = [PY, TRACKER, "dashboard-activity"]
    tmp = None
    if gmail_counts:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tf:
            json.dump(gmail_counts, tf, ensure_ascii=False)
            tmp = tf.name
        args.append(tmp)
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            log("  📊 dashboard — RECENT ACTIVITY updated")
        else:
            log(f"  ⚠️ dashboard update failed: {res.stderr.strip()[-200:]}")
    finally:
        if tmp:
            os.unlink(tmp)


def apply_file(path):
    """Applies a single outbox file. Returns counters, warnings, and the email body."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # The agent must write an object. A list is the format mistake that once broke
    # a whole dispatch cycle — so it fails loudly and lands in failed/.
    if not isinstance(data, dict):
        raise RuntimeError(
            f"An outbox file must be a JSON object ({{...}}) with upserts/updates/flags — "
            f"got {type(data).__name__}")

    warnings = []

    up = run_tracker("upsert-jobs", data.get("upserts", []))
    if up.get("rejected_no_valid_link"):
        warnings.append("Rejected (no valid link): " + " · ".join(up["rejected_no_valid_link"]))

    upd = run_tracker("update-status", data.get("updates", []))
    if upd.get("not_found"):
        warnings.append("Status update — job not found in sheet (check spelling): "
                        + " · ".join(upd["not_found"]))

    fl = run_tracker("flag", data.get("flags", []))
    if fl.get("not_found"):
        warnings.append("Flag — job not found in sheet: " + " · ".join(fl["not_found"]))

    return {"added": up.get("added", 0),
            "skipped": up.get("skipped_duplicates", 0),
            "updated": upd.get("cells_updated", 0),
            "flagged": fl.get("flagged", 0),
            "warnings": warnings,
            "email_md": data.get("email_md", ""),
            "subject": data.get("email_subject", ""),
            "gmail": data.get("activity_gmail")}


def main():
    for d in (OUTBOX, PROCESSING, DONE, FAILED):
        os.makedirs(d, exist_ok=True)

    # --- lock: one dispatcher at a time
    try:
        lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # stale lock (>30 min) — release it; otherwise exit quietly
        if os.path.exists(LOCK) and (datetime.now().timestamp() - os.path.getmtime(LOCK)) < 1800:
            log("another dispatcher is already running — exiting")
            return
        try:
            os.unlink(LOCK)
            lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, FileNotFoundError):
            log("another dispatcher grabbed the lock at the same moment — exiting")
            return

    try:
        # Files stuck in processing/ mean a previous run was cut off mid-cycle
        # (sleep/shutdown). We hold the lock, so nothing else is running — safe to
        # requeue them (upsert dedups anyway).
        for stale in sorted(glob.glob(os.path.join(PROCESSING, "*.json"))):
            name = os.path.basename(stale)
            os.rename(stale, os.path.join(OUTBOX, name))
            log(f"  ♻️ {name} returned from processing/ (previous run was interrupted)")

        files = sorted(glob.glob(os.path.join(OUTBOX, "*.json")))  # excludes subdirectories
        if not files:
            refresh_snapshot()  # even with no outbox — refresh the picture for the next agent
            update_dashboard()  # and update the activity metric (catches manual edits too)
            return  # nothing to do — quiet (most runs are empty)

        log(f"found {len(files)} outbox file(s) to process")
        total_added = total_skipped = total_updated = total_flagged = 0
        email_parts, warnings, subject = [], [], None
        gmail_counts = None

        for src in files:
            name = os.path.basename(src)
            # peek: if the file isn't valid JSON yet, the agent is probably still
            # writing it. Leave it in outbox/; WatchPaths fires again when the
            # write completes.
            try:
                with open(src, encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError):
                log(f"  ⏭ {name}: partial JSON (being written?) — skipped, caught next run")
                continue
            claim = os.path.join(PROCESSING, name)
            os.rename(src, claim)  # atomic claim — a second run won't find it
            try:
                r = apply_file(claim)
                total_added += r["added"]
                total_skipped += r["skipped"]
                total_updated += r["updated"]
                total_flagged += r["flagged"]
                gmail_counts = r["gmail"] or gmail_counts
                warnings += r["warnings"]
                if r["email_md"]:
                    email_parts.append(r["email_md"])
                subject = r["subject"] or subject
                os.rename(claim, os.path.join(DONE, name))
                log(f"  ✓ {name}: +{r['added']} jobs, {r['skipped']} duplicates, "
                    f"{r['updated']} cells updated, {r['flagged']} flags")
                for w in r["warnings"]:
                    log(f"  ⚠️ {name}: {w}")
            except Exception as e:
                os.rename(claim, os.path.join(FAILED, name))
                log(f"  ✗ {name} failed — moved to failed/: {e}")
                warnings.append(f"File FAILED (moved to outbox/failed/, nothing applied): "
                                f"{name} — {e}")

        # --- One consolidated email per cycle. Warnings are always sent, even with
        #     no email_md — otherwise a failure is swallowed silently and nobody
        #     ever finds out.
        if email_parts or warnings:
            runs = f"{len(email_parts)} runs" if len(email_parts) > 1 else "1 run"
            header = (f"## 🔧 Dispatch Summary\n"
                      f"{runs} processed · {total_added} jobs added · "
                      f"{total_skipped} skipped as duplicate · "
                      f"{total_updated} cells updated · {total_flagged} to approve\n")
            if warnings:
                header += ("\n## ⚠️ Dispatch warnings — needs a look\n"
                           + "\n".join(f"• {w}" for w in warnings) + "\n")
            body = header + "\n" + ("\n\n─────\n\n".join(email_parts))
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as tf:
                tf.write(body)
                mail_path = tf.name
            try:
                # The subject must start with SUBJECT_PREFIX so one Gmail filter
                # catches every message this system sends.
                subj = subject or f"{SUBJECT_PREFIX} — {datetime.now():%d/%m}"
                if warnings and not email_parts:
                    subj = f"{SUBJECT_PREFIX} — ⚠️ dispatch warnings {datetime.now():%d/%m}"
                res = subprocess.run(
                    [PY, TRACKER, "notify", mail_path, "--subject", subj],
                    capture_output=True, text=True, timeout=120)
                if res.returncode == 0:
                    log(f"  ✉️ email sent to {MAIL_TO}")
                else:
                    log(f"  ✗ sending email failed: {res.stderr.strip()[-300:]}")
            finally:
                os.unlink(mail_path)
        else:
            log("  no email content (files were empty or failed)")

        refresh_snapshot()  # fresh picture for the next agent run (it can't read the sheet)
        update_dashboard(gmail_counts)  # the activity metric on the dashboard
        log(f"cycle done: +{total_added} · {total_skipped} duplicates · "
            f"{total_updated} cells · {total_flagged} flags · {len(warnings)} warnings")

    finally:
        os.close(lock_fd)
        if os.path.exists(LOCK):
            os.unlink(LOCK)


if __name__ == "__main__":
    main()
