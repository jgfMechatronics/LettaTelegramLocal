#!/usr/bin/env python3
"""
group_chat.py - Bidirectional multi-agent group chat console.

Round-robin conversation: James and agents take turns. Agents receive only
NEW messages since their last turn (delta, not full thread — their Agent Home
context already has the history). Broadcast responses using <gc>...</gc> tags.
No tags = pass.

Usage:
    python3 group_chat.py                          # default: opus + sonnet
    python3 group_chat.py --agents opus sonnet haiku
    python3 group_chat.py --max-skips 3            # allow more consecutive skips

Commands:
    skip, pass, s, or empty  - Let agents continue without adding a message
    /quit, /exit             - End the chat
"""

import argparse
import os
import platform
import re
import sys

from dotenv import load_dotenv
from prompt_toolkit import PromptSession

from agent_home_client import AgentStreamError, resolve_agent_id_by_name_sync, send_message_sync

if platform.system() != "Linux":
    print("group_chat.py is for Linux containers only.", file=sys.stderr)
    sys.exit(1)

# Load .env from script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")

DEFAULT_AGENTS = ["opus", "sonnet"]
SEPARATOR_WIDTH = 60

GC_REMINDER = """**GC Reminders:**
- Use `<gc>` tags for your messages
- Simple file reads / web searches relevant to discussion = fine. Save proper agentic work for when James says we're back in a normal TUI conversation (long agent turns interrupt conversation flow)
- Only the LAST message in a turn is captured for GC. If you get a compaction warning after trying to send a GC message: do your consolidation, then REPEAT your GC message (with tags) to end the turn."""


def send_message(agent_id: str, message: str) -> str:
    """Send a message to an agent, returning response or error text.

    Thin wrapper over the shared client — GC treats failures as printable
    strings rather than exceptions (the chat loop keeps running).
    """
    try:
        return send_message_sync(SERVER_URL, agent_id, message)
    except AgentStreamError as e:
        return f"[error: {e}]"


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


GC_HEADER = "[GROUP CHAT. Use <gc>your-reply</gc> on the **last message/action of your turn** to respond. Leave wrapper off to pass. Avoid agentic action in response to this message]"


def format_thread_for_agent(thread: list[tuple[str, str]]) -> str:
    """Format conversation thread for sending to an agent.
    
    Each entry is (speaker_name, message). Output format:
    [GROUP CHAT. Use <gc>your-reply</gc> to respond. ...]
    [James]: Hello everyone
    [Opus]: Hey! What's up?
    [Sonnet]: Hi there!
    """
    if not thread:
        return f"{GC_HEADER}\n(no messages yet)"
    
    lines = [GC_HEADER]
    for speaker, message in thread:
        lines.append(f"[{speaker.capitalize()}]: {message}")
    return "\n".join(lines)


def _make_prompt_session() -> PromptSession:
    """Create a PromptSession with arrow key and history support."""
    return PromptSession()


def main():
    parser = argparse.ArgumentParser(description="Group chat with multiple agents")
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

    # Resolve agent names to IDs via the Agent Home registry
    try:
        agent_ids = {name: resolve_agent_id_by_name_sync(SERVER_URL, name) for name in args.agents}
    except AgentStreamError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    participants = ["james"] + list(args.agents)
    agent_list = ", ".join(name.capitalize() for name in participants[1:])
    print(f"Group chat — {agent_list}")
    print("Type a message, 'skip' (or 's'/Enter) to let agents continue, '/quit' to exit.\n")

    thread: list[tuple[str, str]] = []
    last_seen: dict[str, int] = {name: 0 for name in args.agents}
    turn = 0
    consecutive_skips = 0
    reminder_injected = False
    prompt_session = _make_prompt_session()

    while True:
        current = participants[turn % len(participants)]

        if current == "james":
            print_separator("james")
            prompt = "> " if consecutive_skips == 0 else f"(skipped {consecutive_skips}/{args.max_skips}) > "
            try:
                user_input = prompt_session.prompt(prompt).strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                break

            if user_input.lower() in ("/quit", "/exit"):
                print("Exiting.")
                break
            elif user_input.lower() in ("skip", "pass", "s", ""):
                consecutive_skips += 1
                if consecutive_skips >= args.max_skips:
                    print(f"[{args.max_skips} consecutive skips — type a message to continue]")
                    consecutive_skips = 0
                    continue
            else:
                consecutive_skips = 0
                if not reminder_injected:
                    thread.append(("System", GC_REMINDER))
                    reminder_injected = True
                thread.append(("james", user_input))

        else:
            agent_id = agent_ids[current]
            print_separator(current)
            new_messages = thread[last_seen[current]:]
            raw = send_message(agent_id, format_thread_for_agent(new_messages))
            gc_content = extract_gc(raw)
            if gc_content:
                print(gc_content)
                thread.append((current, gc_content))
            else:
                print("[passed]")
            last_seen[current] = len(thread)

        turn += 1


if __name__ == "__main__":
    if os.path.exists("/.dockerenv"):
        print("group_chat.py must be run from James's terminal, not from inside a container.")
        sys.exit(1)
    main()
