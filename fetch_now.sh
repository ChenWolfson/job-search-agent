#!/bin/bash
# Pulls job-alert digests from Gmail into alerts_inbox/ — right now.
# Runs automatically at 08:50 (launchd), before the 09:00 agent. This script is the manual path.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/fetch_alerts.py" "$@"
