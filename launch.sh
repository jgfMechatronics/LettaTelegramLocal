#!/bin/bash
# Launch the telegram bridge with a systemd sleep inhibitor.
#
# systemd-inhibit holds a logind lock for the lifetime of the child process.
# The lock is tied to the process's fd — if the bridge dies for ANY reason
# (clean exit, SIGKILL, crash), logind releases the lock automatically.
# No lock files, no cleanup code, no stuck inhibitors.
#
# Replaces manual `caffeine` use — the PC can't idle-sleep while the bridge runs,
# and explicit suspend attempts are blocked until the bridge stops.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec systemd-inhibit \
    --what=sleep:idle \
    --who="Agent Home Telegram Bridge" \
    --why="Bridge running — agents need message access" \
    uv run python LettaTelegramLocal.py "$@"