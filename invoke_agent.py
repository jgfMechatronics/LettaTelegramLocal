#!/usr/bin/env python3
"""
invoke_agent.py - Launcher: check singleton lock, open console window, wait, return result.

Usage:
    python invoke_agent.py sonnet "your prompt here"
    python invoke_agent.py haiku "your prompt here"
    python invoke_agent.py sonnet --cwd C:/Git/LettaSource "your prompt here"

Agent names are resolved via agents.json in the same directory.

Spawns invoke_agent_worker.py in a dedicated console window so James has a live
terminal for monitoring and approvals. Blocks until the session completes, then
prints the final response to stdout for the calling process (e.g. Opus via Bash).
"""

import sys
import os
import subprocess
import tempfile

# Windows console defaults to cp1252; reconfigure stdout/stderr to UTF-8 so
# emojis in agent responses don't cause encoding errors when printing to caller.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

LOCK_FILE = os.path.join(tempfile.gettempdir(), "invoke_agent.lock")
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoke_agent_worker.py")


# ── Singleton lock ─────────────────────────────────────────────────────────────

def acquire_lock() -> bool:
    """Returns True if lock acquired, False if another session is already running."""
    if os.path.exists(LOCK_FILE):
        print(f"[invoke_agent] ⚠  Already running — Session may be hung, user intervention required")
        return False
    open(LOCK_FILE, "w").close()
    return True


def release_lock() -> None:
    try:
        os.unlink(LOCK_FILE)
    except Exception:
        pass


# ── Result file ────────────────────────────────────────────────────────────────

def make_result_file() -> str:
    """Create a temp file for the worker to write its result to. Returns the path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="invoke_agent_result_")
    os.close(fd)
    return path


def read_and_clear_result(path: str) -> str:
    """Read the result written by the worker, delete the file, return the content."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        result = f.read()
    os.unlink(path)
    return result


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    if not acquire_lock():
        return 1

    result_file = make_result_file()
    child_cmd = [sys.executable, WORKER_SCRIPT] + sys.argv[1:] + ["--result-file", result_file]

    try:
        proc = subprocess.Popen(
            child_cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        proc.wait()
    finally:
        release_lock()

    result = read_and_clear_result(result_file)
    if result.strip():
        print(result)

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
