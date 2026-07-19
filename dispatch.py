#!/usr/bin/env python3
"""
dispatch.py — runs in a network-enabled environment (launchd / manual) and
applies whatever the agent wrote to the outbox.

Why this exists: the AI agent runs in a sandboxed VM that blocks direct access
to googleapis.com (a known platform limitation). So the agent only *writes* an
intent file to outbox/, and this dispatcher — running in a normal environment
with network access — applies it to the sheet and sends the email digest.

Flow per outbox file: outbox/ → (claim) processing/ → apply → done/ (or failed/).
A lockfile prevents two concurrent dispatchers. One consolidated email per cycle.
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

MAIL_TO = "you@example.com"  # ← your address (display only; tracker.py holds the real one)


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_tracker(cmd, payload, extra=None):
    """Writes payload to a temp file and runs tracker.py <cmd> <file>.
    Returns the JSON the command printed."""
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
    """Saves a copy of the live table to latest_snapshot.json — the sandboxed
    agent cannot read the sheet directly, and needs this picture for its
    dedup and status checks."""
    snap = os.path.join(HERE, "latest_snapshot.json")
    res = subprocess.run([PY, TRACKER, "read"], capture_output=True, text=True, timeout=120)
    if res.returncode == 0 and res.stdout.strip():
        with open(snap, "w", encoding="utf-8") as f:
            f.write(res.stdout)
        log("  📸 latest_snapshot.json refreshed")
    else:
        log(f"  ⚠️ snapshot refresh failed: {res.stderr.strip()[-200:]}")


def apply_file(path):
    """Applies a single outbox file. Returns a dict with counters, warnings,
    and the email content."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # The agent must write an object. A list is a format mistake that would
    # otherwise crash the whole cycle.
    if not isinstance(data, dict):
        raise RuntimeError(
            f"outbox file must be a JSON object ({{...}}) with "
            f"upserts/updates/flags — got {type(data).__name__}")

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
            "subject": data.get("email_subject", "")}


def main():
    for d in (OUTBOX, PROCESSING, DONE, FAILED):
        os.makedirs(d, exist_ok=True)

    # --- lock: a single dispatcher at any moment
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
        # Files stuck in processing/ mean a previous run was interrupted
        # (sleep/shutdown). We hold the lock, so no run is in flight — it is
        # safe to re-queue them (upsert dedups anyway).
        for stale in sorted(glob.glob(os.path.join(PROCESSING, "*.json"))):
            name = os.path.basename(stale)
            os.rename(stale, os.path.join(OUTBOX, name))
            log(f"  ♻️ {name} re-queued from processing/ (previous run interrupted)")

        files = sorted(glob.glob(os.path.join(OUTBOX, "*.json")))  # excludes subdirs
        if not files:
            refresh_snapshot()  # even with no outbox — keep the agent's picture fresh
            return  # nothing to do — stay quiet (most runs are empty)

        log(f"found {len(files)} outbox file(s) to process")
        total_added = total_skipped = total_updated = total_flagged = 0
        email_parts, warnings, subject = [], [], None

        for src in files:
            name = os.path.basename(src)
            # peek: if the file is not valid JSON yet, the agent is probably
            # mid-write. Leave it in the outbox; WatchPaths will fire again
            # once the write completes.
            try:
                with open(src, encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError):
                log(f"  ⏭ {name}: partial JSON (being written?) — skipped, next run will catch it")
                continue
            claim = os.path.join(PROCESSING, name)
            os.rename(src, claim)  # atomic claim — a second run won't find it
            try:
                r = apply_file(claim)
                total_added += r["added"]
                total_skipped += r["skipped"]
                total_updated += r["updated"]
                total_flagged += r["flagged"]
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

        # --- one consolidated email per cycle. Warnings are always sent —
        #     even without email_md — otherwise a failure dies silently and
        #     nobody ever knows.
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
                # Stable Hebrew subject prefix ("job search summary") — kept
                # consistent so a Gmail filter can label these digests
                subj = subject or f"סיכום חיפוש עבודה — {datetime.now():%d/%m}"
                if warnings and not email_parts:
                    subj = f"סיכום חיפוש עבודה — ⚠️ dispatch warnings {datetime.now():%d/%m}"
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
            log("  nothing to email (files empty or failed)")

        refresh_snapshot()  # fresh picture for the next agent run
        log(f"cycle done: +{total_added} · {total_skipped} duplicates · "
            f"{total_updated} cells · {total_flagged} flags · {len(warnings)} warnings")

    finally:
        os.close(lock_fd)
        if os.path.exists(LOCK):
            os.unlink(LOCK)


if __name__ == "__main__":
    main()
