#!/usr/bin/env python3
"""
invoke_sonnet_worker.py - Console worker: run Sonnet session, stream output, handle approvals.

Spawned by invoke_sonnet.py with CREATE_NEW_CONSOLE. Not meant to be called directly.

Wire protocol (protocol.ts):
  type="system"          → init event; send prompt on receipt
  type="message"         → content event; message_type discriminates
  type="control_request" → tool approval needed; block on input(), respond via stdin
  type="result"          → session complete
"""

import sys
import os
import json
import subprocess
import argparse

# ── Constants ─────────────────────────────────────────────────────────────────

SONNET_AGENT_ID = "agent-ed4e2792-d2d9-45c3-8646-1eb57113d35f"
DEFAULT_CWD = os.getenv("LETTA_CWD", "C:/Git")

# ── Display helpers ────────────────────────────────────────────────────────────

def format_tool_input(tool_name: str, input_dict: dict) -> str:
    """One-line summary of tool arguments for console display."""
    if tool_name == "Read":
        path = input_dict.get("file_path", "")
        offset, limit = input_dict.get("offset"), input_dict.get("limit")
        extra = f" [{offset}:{offset+limit}]" if offset and limit else (" [partial]" if offset or limit else "")
        return f"  {path}{extra}"
    elif tool_name == "Grep":
        return f"  pattern: {input_dict.get('pattern', '')!r}  path: {input_dict.get('path', '.')}"
    elif tool_name == "Glob":
        return f"  {input_dict.get('pattern', '')}  in: {input_dict.get('path', '.')}"
    elif tool_name == "Edit":
        path = input_dict.get("file_path", "")
        old = (input_dict.get("old_string") or "")[:80]
        new = (input_dict.get("new_string") or "")[:80]
        return f"  file: {path}\n  old: {old!r}\n  new: {new!r}"
    elif tool_name == "Write":
        return f"  {input_dict.get('file_path', '')}  ({len(input_dict.get('content', ''))} chars)"
    elif tool_name == "Bash":
        return f"  {(input_dict.get('command') or '')[:120]}"
    else:
        raw = json.dumps(input_dict)
        return f"  {raw[:200]}{'...' if len(raw) > 200 else ''}"


def extract_text(content) -> str:
    """Extract plain text from a Letta message content block (list of blocks or string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content) if content else ""


# ── Letta process ──────────────────────────────────────────────────────────────

def launch_letta(cwd: str) -> subprocess.Popen:
    """Start the Letta headless process and return it."""
    # letta is an npm-installed command; on Windows it resolves as letta.cmd
    cmd = [
        "letta.cmd",
        "--agent", SONNET_AGENT_ID,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--no-skills",
        "--no-system-info-reminder",
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "LETTA_CODE_AGENT_ROLE": "subagent"},
        cwd=cwd,
    )


def send_prompt(proc: subprocess.Popen, prompt: str) -> None:
    """Send the user's prompt to the Letta process via stdin."""
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
    proc.stdin.write(msg + "\n")
    proc.stdin.flush()
    print("[worker] Prompt sent — waiting...\n")


# ── Message handlers ───────────────────────────────────────────────────────────

def handle_tool_call(msg: dict) -> None:
    for tc in (msg.get("tool_calls") or []):
        tool_name = tc.get("function", {}).get("name", "?")
        raw_args = tc.get("function", {}).get("arguments", "{}")
        try:
            args_dict = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args_dict = {}
        print(f"🔧 {tool_name}")
        print(format_tool_input(tool_name, args_dict))


def handle_tool_return(msg: dict) -> None:
    tool_return = str(msg.get("tool_return", ""))
    status = msg.get("status", "")
    preview = tool_return[:150] + ("..." if len(tool_return) > 150 else "")
    print(f"  {'✅' if status == 'success' else '❌'} {preview}\n")


def flush_assistant_buffer(buffer: list) -> str | None:
    """Join buffered assistant chunks, print as one block, clear the buffer. Returns text or None."""
    if not buffer:
        return None
    text = "".join(buffer)
    buffer.clear()
    if text.strip():
        print(f"\n💬 Sonnet:\n{text}\n")
        return text
    return None


def handle_result_event(msg: dict, current_response: str | None) -> str | None:
    """Print the session summary and return the final response text."""
    subtype = msg.get("subtype", "")
    usage = msg.get("usage") or {}
    print(
        f"\n[worker] {'✅' if subtype == 'success' else '❌'} Done"
        f" | steps: {msg.get('num_turns', '?')}"
        f" | tokens: {usage.get('total_tokens', '?')}"
    )
    return current_response or msg.get("result") or ""


def dispatch_message_event(msg: dict, assistant_buffer: list) -> None:
    """
    Route a message event. assistant_message chunks are appended to assistant_buffer
    rather than printed immediately — caller flushes the buffer at transition boundaries.
    """
    message_type = msg.get("message_type", "")
    if message_type == "assistant_message":
        text = extract_text(msg.get("content", ""))
        if text:
            assistant_buffer.append(text)
    elif message_type == "tool_call_message":
        handle_tool_call(msg)
    elif message_type == "tool_return_message":
        handle_tool_return(msg)
    # reasoning_message: skip (verbose; visible in Sonnet's LC session)


# ── Approval handling ──────────────────────────────────────────────────────────

def _prompt_approval_choice() -> dict:
    """Block on James's input and return a behavior dict for the control_response."""
    while True:
        try:
            choice = input("  [a]llow / [d]eny: ").strip().lower()
        except EOFError:
            return {"behavior": "deny", "message": "Denied (no TTY)"}

        if choice in ("a", "allow", "y", "yes"):
            return {"behavior": "allow"}
        elif choice in ("d", "deny", "n", "no"):
            try:
                reason = input("  Reason (optional, Enter to skip): ").strip()
            except EOFError:
                reason = ""
            return {"behavior": "deny", "message": reason or "Denied by James"}
        else:
            print("  Please enter 'a' to allow or 'd' to deny.")


def handle_approval(proc: subprocess.Popen, request_id: str, tool_name: str, tool_input: dict) -> None:
    """Display the approval prompt, get James's decision, send control_response to Letta."""
    print("\n" + "=" * 60)
    print(f"  ⚠  APPROVAL REQUEST — {tool_name}")
    print(format_tool_input(tool_name, tool_input))
    print("=" * 60)

    behavior = _prompt_approval_choice()

    print()
    response = {"subtype": "success", "request_id": request_id, "response": behavior}
    proc.stdin.write(json.dumps({"type": "control_response", "response": response}) + "\n")
    proc.stdin.flush()


def handle_control_request(proc: subprocess.Popen, msg: dict) -> None:
    """Route a control_request event, sending tool approvals to handle_approval."""
    req = msg.get("request", {})
    if req.get("subtype") == "can_use_tool":
        handle_approval(
            proc,
            request_id=msg.get("request_id", ""),
            tool_name=req.get("tool_name", "?"),
            tool_input=req.get("input", {}),
        )


# ── Event loop ─────────────────────────────────────────────────────────────────

def process_events(proc: subprocess.Popen, prompt: str) -> str | None:
    """
    Read and dispatch events from the Letta process until the session completes.
    Returns the final assistant response, or None if the session ended without one.

    LC streams assistant responses as multiple consecutive assistant_message chunks.
    We buffer them and flush as a single block at every non-assistant event boundary.
    """
    final_response = None
    prompt_sent = False
    assistant_buffer: list = []

    for raw_line in proc.stdout:
        raw_line = raw_line.rstrip("\n")
        if not raw_line.strip():
            continue

        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            print(f"[raw] {raw_line}")
            continue

        msg_type = msg.get("type", "")

        if msg_type == "system" and msg.get("subtype") == "init":
            if not prompt_sent:
                send_prompt(proc, prompt)
                prompt_sent = True

        elif msg_type == "message":
            if msg.get("message_type") != "assistant_message":
                flushed = flush_assistant_buffer(assistant_buffer)
                if flushed:
                    final_response = flushed
            dispatch_message_event(msg, assistant_buffer)

        elif msg_type == "control_request":
            flushed = flush_assistant_buffer(assistant_buffer)
            if flushed:
                final_response = flushed
            handle_control_request(proc, msg)

        elif msg_type == "result":
            flushed = flush_assistant_buffer(assistant_buffer)
            if flushed:
                final_response = flushed
            final_response = handle_result_event(msg, final_response)
            break

        elif msg_type == "error":
            flush_assistant_buffer(assistant_buffer)
            print(f"\n[worker] ❌ Error: {msg.get('message', 'unknown')}")
            break

    return final_response


# ── Session orchestration ──────────────────────────────────────────────────────

def print_session_header(cwd: str, prompt: str) -> None:
    print(f"\n{'='*60}")
    print(f"  invoke_sonnet_worker.py")
    print(f"  cwd: {cwd}")
    print(f"  prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
    print(f"{'='*60}\n")


def run_session(cwd: str, prompt: str) -> tuple[int, str | None]:
    """Launch Sonnet, process the event stream, return (exit_code, final_response)."""
    print_session_header(cwd, prompt)
    proc = launch_letta(cwd)
    final_response = None

    try:
        final_response = process_events(proc, prompt)
    except KeyboardInterrupt:
        print("\n[worker] Interrupted")
        proc.terminate()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()

    if final_response:
        print(f"\n{'='*60}\nFINAL RESPONSE:\n{'='*60}\n{final_response}\n{'='*60}\n")

    return proc.returncode, final_response


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Sonnet session worker (spawned by invoke_sonnet.py)")
    parser.add_argument("--cwd", default=DEFAULT_CWD, help="Working directory for Sonnet")
    parser.add_argument("--result-file", dest="result_file", default=None,
                        help="Path to write final response for the launcher to capture")
    parser.add_argument("prompt", help="The prompt to send to Sonnet")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exit_code, final_response = run_session(args.cwd, args.prompt)

    if args.result_file and final_response:
        try:
            with open(args.result_file, "w", encoding="utf-8") as f:
                f.write(final_response)
        except Exception as e:
            print(f"[worker] Warning: could not write result file: {e}")

    try:
        input("\n  Press Enter to close this window...")
    except EOFError:
        pass

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
