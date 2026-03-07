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


def resolve_agent(name: str) -> str:
    """Look up agent ID by name from agents.json."""
    try:
        with open(AGENTS_JSON_PATH, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading agents.json: {e}", file=sys.stderr)
        sys.exit(1)
    
    if name not in registry:
        print(f"Unknown agent: {name}. Available: {', '.join(registry.keys())}", file=sys.stderr)
        sys.exit(1)
    
    return registry[name]["agent_id"]


def main():
    parser = argparse.ArgumentParser(description="YOLO headless agent invocation")
    parser.add_argument("agent_name", help="Agent name (from agents.json)")
    parser.add_argument("prompt", help="Prompt to send")
    parser.add_argument("--cwd", default="/workspace/git", help="Working directory")
    args = parser.parse_args()

    agent_id = resolve_agent(args.agent_name)
    letta_url = os.environ.get("LETTA_BASE_URL", "http://host.docker.internal:8283")

    # Preamble tells invoked agent to be concise (reduces context bloat)
    preamble = "[YOLO HEADLESS INVOCATION FROM SIBLING AGENT]\n\n"
    full_prompt = preamble + args.prompt

    # That's it. Just call letta with -p flag.
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
    )

    # Output response (stdout), errors go to stderr
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
