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
import json
import os
import platform
import re
import sys

import httpx
from dotenv import load_dotenv
from prompt_toolkit import PromptSession

if platform.system() != "Linux":
    print("group_chat.py is for Linux containers only.", file=sys.stderr)
    sys.exit(1)

# Load .env from script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

AGENTS_JSON_PATH = os.path.join(SCRIPT_DIR, "agents.json")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")

DEFAULT_AGENTS = ["opus", "sonnet"]
TIMEOUT_SECONDS = 480
SEPARATOR_WIDTH = 60

GC_REMINDER = """**GC Reminders:**
- Use `<gc>` tags for your messages
- Simple file reads / web searches relevant to discussion = fine. Save proper agentic work for when James says we're back in normal LC (long agent turns interrupt conversation flow)
- Only the LAST message in a turn is captured for GC. If you get a compaction warning after trying to send a GC message: do your consolidation, then REPEAT your GC message (with tags) to end the turn."""


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


def send_message(agent_id: str, message: str) -> str:
    """Send a message to an agent via Agent Home API and return its response.
    
    Consumes the SSE stream and accumulates the text response.
    """
    url = f"{SERVER_URL}/agents/{agent_id}/messages"
    accumulated_text = ""
    in_thinking = False
    
    try:
        with httpx.stream(
            "POST",
            url,
            json={"message": message},
            timeout=TIMEOUT_SECONDS,
        ) as response:
            if response.status_code != 200:
                return f"[error: HTTP {response.status_code}]"
            
            current_event_type = None
            data_lines = []
            
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                    data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and current_event_type:
                    # End of event - process it
                    data_str = "\n".join(data_lines)
                    try:
                        data = json.loads(data_str) if data_str else {}
                    except json.JSONDecodeError:
                        data = {}
                    
                    if current_event_type == "PartStartEvent":
                        part = data.get("part", {})
                        part_kind = part.get("part_kind")
                        if part_kind == "thinking":
                            in_thinking = True
                        elif part_kind == "text":
                            in_thinking = False
                            content = part.get("content", "")
                            if content:
                                accumulated_text += content
                    elif current_event_type == "PartDeltaEvent":
                        if not in_thinking:
                            delta = data.get("delta", {})
                            content = delta.get("content_delta", "")
                            if content:
                                accumulated_text += content
                    elif current_event_type == "Error":
                        return f"[error: {data.get('message', 'Unknown error')}]"
                    
                    current_event_type = None
                    data_lines = []
                    
    except httpx.TimeoutException:
        suffix = f"\n[timed out after {TIMEOUT_SECONDS}s]"
        return (accumulated_text + suffix) if accumulated_text else suffix.strip()
    except httpx.RequestError as e:
        return f"[error: {e}]"
    
    return accumulated_text.strip()


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

    registry = load_agent_registry()
    agents = resolve_agents(args.agents, registry)
    agent_ids = {name: agent_id for name, agent_id in agents}

    participants = ["james"] + [name for name, _ in agents]
    agent_list = ", ".join(name.capitalize() for name in participants[1:])
    print(f"Group chat — {agent_list}")
    print("Type a message, 'skip' (or 's'/Enter) to let agents continue, '/quit' to exit.\n")

    thread: list[tuple[str, str]] = []
    last_seen: dict[str, int] = {name: 0 for name, _ in agents}
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
            last_seen[current] = len(thread)
            gc_content = extract_gc(raw)
            if gc_content:
                print(gc_content)
                thread.append((current, gc_content))
            else:
                print("[passed]")

        turn += 1


if __name__ == "__main__":
    if os.path.exists("/.dockerenv"):
        print("group_chat.py must be run from James's terminal, not from inside a container.")
        sys.exit(1)
    main()
