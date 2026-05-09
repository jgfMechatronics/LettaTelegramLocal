#!/usr/bin/env python3
"""
group_chat.py - Bidirectional multi-agent group chat console.

Round-robin conversation: James and agents take turns. Agents receive only
NEW messages since their last turn (delta, not full thread — their Letta
context already has the history). Broadcast responses using <gc>...</gc> tags.
No tags = pass.

Usage:
    python3 group_chat.py                          # default: opus + sonnet
    python3 group_chat.py --agents opus sonnet haiku
    python3 group_chat.py --max-skips 3            # allow more consecutive skips

Commands:
    skip, pass, s, or empty  - Let agents continue without adding a message
    quit, exit               - End the chat

TODO: load_agent_registry and agent resolution duplicate invoke_yolo.py.
      Extract to a shared agents_registry.py module when convenient.
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys

from prompt_toolkit import PromptSession

if platform.system() != "Linux":
    print("group_chat.py is for Linux containers only.", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_JSON_PATH = os.path.join(SCRIPT_DIR, "agents.json")

DEFAULT_AGENTS = ["opus", "sonnet"]
TIMEOUT_SECONDS = 480
SEPARATOR_WIDTH = 60


def load_agent_registry() -> dict:
    try:
        with open(AGENTS_JSON_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading agents.json: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_agents(names: list[str], registry: dict) -> list[tuple[str, str]]:
    """Resolve agent names to (name, agent_id) pairs. Exits on unknown names."""
    result = []
    for name in names:
        if name not in registry:
            print(f"Unknown agent: {name!r}. Available: {', '.join(registry.keys())}", file=sys.stderr)
            sys.exit(1)
        result.append((name, registry[name]["agent_id"]))
    return result


def send_message(agent_id: str, message: str, letta_url: str) -> str:
    """Send a message to an agent via the letta CLI and return its response."""
    try:
        result = subprocess.run(
            [
                "letta",
                "--agent", agent_id,
                "-p", message,
                "--permission-mode", "bypassPermissions",
                "--no-skills",
                "--no-system-info-reminder",
            ],
            cwd="/workspace/git",
            env={**os.environ, "LETTA_BASE_URL": letta_url},
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "").strip()
        suffix = f"\n[timed out after {TIMEOUT_SECONDS}s]"
        return (partial + suffix) if partial else suffix.strip()

    if result.returncode != 0 and result.stderr:
        return f"[error: {result.stderr.strip()}]"
    return result.stdout.strip()


def print_separator(label: str) -> None:
    """Print a named horizontal rule: ── Label ──────────"""
    label_str = f"  {label.capitalize()}  "
    dashes = SEPARATOR_WIDTH - len(label_str)
    left = dashes // 2
    print(f"\n{'─' * left}{label_str}{'─' * (dashes - left)}\n")


def extract_gc(response: str) -> str | None:
    """Extract content from <gc>...</gc> tags. Returns None if no tags found."""
    match = re.search(r"<gc>(.*?)</gc>", response, re.DOTALL)
    return match.group(1).strip() if match else None


def format_thread_for_agent(thread: list[tuple[str, str]]) -> str:
    """Format conversation thread for sending to an agent.
    
    Each entry is (speaker_name, message). Output format:
    [GROUP CHAT]
    [James]: Hello everyone
    [Opus]: Hey! What's up?
    [Sonnet]: Hi there!
    """
    if not thread:
        return "[GROUP CHAT]\n(no messages yet)"
    
    lines = ["[GROUP CHAT]"]
    for speaker, message in thread:
        lines.append(f"[{speaker.capitalize()}]: {message}")
    return "\n".join(lines)


def _make_prompt_session() -> PromptSession:
    """Create a PromptSession with arrow key and history support.

    Note: Shift+Enter newline insertion is not supported in prompt_toolkit 3.0.36
    (ShiftEnter absent from Keys enum). Deferred for a future upgrade or workaround.
    """
    return PromptSession()


def main():
    parser = argparse.ArgumentParser(description="Group chat with multiple Letta agents")
    parser.add_argument(
        "--agents", nargs="+", default=DEFAULT_AGENTS,
        metavar="AGENT",
        help=f"Agents to include (default: {' '.join(DEFAULT_AGENTS)})",
    )
    parser.add_argument(
        "--max-skips", type=int, default=2,
        metavar="N",
        help="Consecutive James skips before requiring real input (default: 2)",
    )
    args = parser.parse_args()

    registry = load_agent_registry()
    agents = resolve_agents(args.agents, registry)
    agent_ids = {name: agent_id for name, agent_id in agents}
    letta_url = os.environ.get("LETTA_BASE_URL", "http://host.docker.internal:8283")

    participants = ["james"] + [name for name, _ in agents]
    agent_list = ", ".join(name.capitalize() for name in participants[1:])
    print(f"Group chat — {agent_list}")
    print("Type a message, 'skip' (or 's'/Enter) to let agents continue, 'quit' to exit.\n")

    thread: list[tuple[str, str]] = []
    last_seen: dict[str, int] = {name: 0 for name, _ in agents}  # track where each agent last saw
    turn = 0
    consecutive_skips = 0
    prompt_session = _make_prompt_session()

    while True:
        current = participants[turn % len(participants)]

        if current == "james":
            print_separator("james")
            prompt = "> " if consecutive_skips == 0 else f"(skipped {consecutive_skips}/{args.max_skips}) > "
            try:
                # Replace Shift+Enter CSI u sequence (escape stripped by terminal) with newline
                user_input = prompt_session.prompt(prompt).replace("[13;2u", "\n").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                break

            if user_input.lower() in ("quit", "exit"):
                print("Exiting.")
                break
            elif user_input.lower() in ("skip", "pass", "s", ""):
                consecutive_skips += 1
                if consecutive_skips >= args.max_skips:
                    print(f"[{args.max_skips} consecutive skips — type a message to continue]")
                    consecutive_skips = 0
                    continue  # re-prompt James without advancing turn
            else:
                consecutive_skips = 0
                thread.append(("james", user_input))

        else:
            agent_id = agent_ids[current]
            print_separator(current)
            # Only send messages since this agent's last turn (delta, not full thread)
            new_messages = thread[last_seen[current]:]
            raw = send_message(agent_id, format_thread_for_agent(new_messages), letta_url)
            last_seen[current] = len(thread)  # update before appending response
            gc_content = extract_gc(raw)
            if gc_content:
                print(gc_content)
                thread.append((current, gc_content))
            else:
                print("[passed]")

        turn += 1


if __name__ == "__main__":
    if os.environ.get("AGENT_ID"):
        print("group_chat.py must be run from James's terminal, not from a Letta Code session.")
        print("Launch it from Windows/host: python3 /workspace/git/LettaTelegramLocal/group_chat.py")
        sys.exit(1)
    main()
