#!/bin/bash
# Runs the dispatcher now — applies everything the agent wrote to outbox/ and sends the digest.
# Three ways to trigger: this script in a terminal · launchd (automatic) · asking your AI assistant.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/dispatch.py" "$@"
