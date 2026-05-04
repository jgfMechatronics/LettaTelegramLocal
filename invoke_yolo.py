#!/usr/bin/env python3
"""
invoke_yolo.py - Truly headless agent invocation for YOLO mode in container.

LINUX ONLY - Designed for use inside the ellm-dev Docker container.

Usage:
    python3 invoke_yolo.py sonnet "your prompt here"
    python3 invoke_yolo.py sonnet --cwd /workspace/git "your prompt here"
"""

import sys
import os
import json
import subprocess
import argparse
import platform

# Lockfile mechanism: each invoke_yolo call creates /tmp/invoke_yolo_waiting_<agent_id>
# while it blocks waiting for the target to finish. Before invoking, we check if the
# target already has a lockfile — meaning they're mid-invocation and can't be re-entered.
_LOCKFILE_DIR = "/tmp"
_LOCKFILE_PREFIX = "invoke_yolo_waiting_"

# Require Linux
if platform.system() != "Linux":
    print("invoke_yolo.py is for Linux containers only. Use invoke_agent.py on Windows.", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_JSON_PATH = os.path.join(SCRIPT_DIR, "agents.json")


def load_agent_registry() -> dict:
    """Load agents.json registry."""
    try:
        with open(AGENTS_JSON_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading agents.json: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_agent(name: str, registry: dict) -> str:
    """Look up agent ID by name from registry."""
    if name not in registry:
        print(f"Unknown agent: {name}. Available: {', '.join(registry.keys())}", file=sys.stderr)
        sys.exit(1)
    
    return registry[name]["agent_id"]


def _lockfile_path(agent_id: str) -> str:
    return os.path.join(_LOCKFILE_DIR, f"{_LOCKFILE_PREFIX}{agent_id}")


def get_invoker_name(registry: dict) -> str:
    """Get the name of the invoking agent from AGENT_ID env var."""
    invoker_id = os.environ.get("AGENT_ID")
    if not invoker_id:
        return "unknown agent"
    
    # Reverse lookup: find name by agent_id
    for name, info in registry.items():
        if info.get("agent_id") == invoker_id:
            return name.capitalize()
    
    # Fallback: return truncated ID if not in registry
    return f"agent {invoker_id[:12]}..."


def main():
    parser = argparse.ArgumentParser(description="YOLO headless agent invocation")
    parser.add_argument("agent_name", help="Agent name (from agents.json)")
    parser.add_argument("prompt", help="Prompt to send")
    parser.add_argument("--cwd", default="/workspace/git", help="Working directory")
    args = parser.parse_args()

    registry = load_agent_registry()
    agent_id = resolve_agent(args.agent_name, registry)
    invoker_id = os.environ.get("AGENT_ID", "")
    invoker_name = get_invoker_name(registry)
    letta_url = os.environ.get("LETTA_BASE_URL", "http://host.docker.internal:8283")

    # Counter-invocation guard: if the target is already blocked waiting for a prior
    # invocation to complete, invoking them back creates a deadlock cycle (RAM fill,
    # server crash). Check for their lockfile before proceeding.
    target_lockfile = _lockfile_path(agent_id)
    if os.path.exists(target_lockfile):
        print(
            f"[invoke_yolo: BLOCKED] Cannot invoke '{args.agent_name}' — they are currently\n"
            f"blocked waiting for a prior invocation to complete. Invoking them creates\n"
            f"a deadlock cycle (RAM fill, server crash).\n\n"
            f"You are likely running inside a headless invocation. To communicate back:\n"
            f"write your findings as your final message and end your turn.",
            file=sys.stderr
        )
        sys.exit(1)

    # Preamble identifies invoking agent
    preamble = f"[YOLO HEADLESS INVOCATION FROM {invoker_name.upper()}]\n\n"
    full_prompt = preamble + args.prompt

    TIMEOUT_SECONDS = 480  # 8 minutes — must be less than LC Bash's 600s max

    # Claim a lockfile while blocking — lets nested invocations detect us as in-flight.
    my_lockfile = _lockfile_path(invoker_id) if invoker_id else None
    if my_lockfile:
        try:
            with open(my_lockfile, "w") as f:
                f.write(f"{args.agent_name}\n{agent_id}\n")
        except OSError:
            pass  # non-fatal

    # That's it. Just call letta with -p flag.
    try:
        result = subprocess.run(
            [
                "letta",
                "--agent", agent_id,
                "-p", full_prompt,
                "--permission-mode", "bypassPermissions",
                "--no-skills",
                "--no-system-info-reminder"
            ],
            cwd=args.cwd,
            env={**os.environ, "LETTA_BASE_URL": letta_url},
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        # Print whatever partial output arrived before the timeout
        if e.stdout:
            print(e.stdout)
        print(f"[invoke_yolo: timed out after {TIMEOUT_SECONDS}s]", file=sys.stderr)
        sys.exit(1)
    finally:
        # Always clean up our lockfile, even on error or timeout
        if my_lockfile:
            try:
                os.remove(my_lockfile)
            except OSError:
                pass

    # Output response (stdout), errors go to stderr
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
