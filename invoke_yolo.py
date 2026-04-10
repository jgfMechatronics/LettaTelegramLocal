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
    invoker_name = get_invoker_name(registry)
    letta_url = os.environ.get("LETTA_BASE_URL", "http://host.docker.internal:8283")

    # Preamble identifies invoking agent
    preamble = f"[YOLO HEADLESS INVOCATION FROM {invoker_name.upper()}]\n\n"
    full_prompt = preamble + args.prompt

    TIMEOUT_SECONDS = 480  # 8 minutes — must be less than LC Bash's 600s max

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

    # Output response (stdout), errors go to stderr
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
