#!/usr/bin/env python3
"""
invoke_agent_worker.py - Console worker: run agent session, stream output, handle approvals.

Spawned by invoke_agent.py with CREATE_NEW_CONSOLE. Not meant to be called directly.

Usage:
    python invoke_agent_worker.py <agent_name> [--cwd PATH] [--result-file PATH] "prompt"

Agent names are resolved via agents.json in the same directory.

Wire protocol (protocol.ts):
  type="system"          -> init event; send prompt on receipt
  type="message"         -> content event; message_type discriminates
  type="control_request" -> tool approval needed; block on input(), respond via stdin
  type="result"          -> session complete
"""

import sys
import os
import json
import subprocess
import argparse
import time
import platform

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_CWD = os.getenv("LETTA_CWD", "/workspace/git")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOT_ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
AGENTS_JSON_PATH = os.path.join(SCRIPT_DIR, "agents.json")

VALID_PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}


# ── Agent registry ─────────────────────────────────────────────────────────────

def load_agent_registry() -> dict:
    """Load agents.json from the script directory. Returns the registry dict."""
    try:
        with open(AGENTS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[worker] ❌ agents.json not found at {AGENTS_JSON_PATH}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[worker] ❌ agents.json is invalid JSON: {e}")
        sys.exit(1)


def resolve_agent(name: str) -> dict:
    """Look up agent by name. Returns {port, agent_id}. Exits with clear error if not found."""
    registry = load_agent_registry()
    if name not in registry:
        available = ", ".join(sorted(registry.keys()))
        print(f"[worker] ❌ Unknown agent {name!r}. Available agents: {available}")
        print(f"[worker]    Check {AGENTS_JSON_PATH} to add new agents.")
        sys.exit(1)
    return registry[name]


# ── Permission mode ────────────────────────────────────────────────────────────

def read_permission_mode() -> str:
    """Read PERMISSION_MODE from .env in the same directory as this script."""
    try:
        with open(DOT_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "PERMISSION_MODE":
                    mode = value.strip().strip('"').strip("'")
                    if mode in VALID_PERMISSION_MODES:
                        return mode
                    print(
                        f"[worker] ⚠  .env: invalid PERMISSION_MODE={mode!r}. "
                        f"Valid: {', '.join(sorted(VALID_PERMISSION_MODES))}. "
                        f"Falling back to 'default'."
                    )
                    return "default"
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[worker] ⚠  Could not read {DOT_ENV_PATH}: {e}. Using 'default'.")
    return "default"


# ── Display helpers ────────────────────────────────────────────────────────────

def format_tool_input(tool_name: str, input_dict: dict) -> str:
    """One-line summary of tool arguments for console display."""
    match tool_name:
        case "Read":
            path = input_dict.get("file_path", "")
            offset, limit = input_dict.get("offset"), input_dict.get("limit")
            extra = f" [{offset}:{offset+limit}]" if offset and limit else (" [partial]" if offset or limit else "")
            return f"  {path}{extra}"
        case "Grep":
            return f"  pattern: {input_dict.get('pattern', '')!r}  path: {input_dict.get('path', '.')}"
        case "Glob":
            return f"  {input_dict.get('pattern', '')}  in: {input_dict.get('path', '.')}"
        case "Edit":
            path = input_dict.get("file_path", "")
            old = (input_dict.get("old_string") or "")[:80]
            new = (input_dict.get("new_string") or "")[:80]
            return f"  file: {path}\n  old: {old!r}\n  new: {new!r}"
        case "Write":
            return f"  {input_dict.get('file_path', '')}  ({len(input_dict.get('content', ''))} chars)"
        case "Bash":
            return f"  {(input_dict.get('command') or '')[:120]}"
        case _:
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

def launch_letta(cwd: str, agent_id: str, letta_url: str) -> subprocess.Popen:
    """Start the Letta headless process and return it."""
    permission_mode = read_permission_mode()
    print(f"[worker] Permission mode: {permission_mode}")
    # letta.cmd on Windows, letta on Linux/Mac
    letta_cmd = "letta.cmd" if platform.system() == "Windows" else "letta"
    cmd = [
        letta_cmd,
        "--agent", agent_id,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--no-skills",
        "--no-system-info-reminder",
        "--permission-mode", permission_mode,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "LETTA_CODE_AGENT_ROLE": "subagent", "LETTA_BASE_URL": letta_url},
        cwd=cwd,
    )


def send_prompt(proc: subprocess.Popen, prompt: str) -> None:
    """Send the user's prompt to the Letta process via stdin."""
    preamble = "[LC HEADLESS MODE INVOCATION FROM OTHER AGENT]\n\n"
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": preamble + prompt}})
    proc.stdin.write(msg + "\n")
    proc.stdin.flush()
    print("[worker] Prompt sent — waiting...\n")


# ── Message handlers ───────────────────────────────────────────────────────────

def handle_tool_call(msg: dict) -> None:
    tc = msg.get("tool_call") or {}
    tool_name = tc.get("name", "?")
    raw_args = tc.get("arguments", "")
    try:
        args_dict = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return  # partial streaming chunk — skip until arguments are complete
    print(f"🔧 {tool_name}")
    print(format_tool_input(tool_name, args_dict))


def handle_tool_return(msg: dict) -> None:
    tool_return = str(msg.get("tool_return", ""))
    status = msg.get("status", "")
    preview = tool_return[:150] + ("..." if len(tool_return) > 150 else "")
    print(f"  {'✅' if status == 'success' else '❌'} {preview}\n")


def flush_assistant_buffer(buffer: list, agent_name: str) -> str | None:
    """Join buffered assistant chunks, print as one block, clear the buffer. Returns text or None."""
    if not buffer:
        return None
    text = "".join(buffer)
    buffer.clear()
    if text.strip():
        print(f"\n💬 {agent_name.capitalize()}:\n{text}\n")
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
    # reasoning_message: skip (verbose; visible in agent's LC session)


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

def process_events(proc: subprocess.Popen, prompt: str, agent_name: str) -> str | None:
    """
    Read and dispatch events from the Letta process until the session completes.
    Returns the final assistant response, or None if the session ended without one.
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

        match msg_type:
            case "system" if msg.get("subtype") == "init":
                if not prompt_sent:
                    send_prompt(proc, prompt)
                    prompt_sent = True

            case "message":
                if msg.get("message_type") != "assistant_message":
                    flushed = flush_assistant_buffer(assistant_buffer, agent_name)
                    if flushed:
                        final_response = flushed
                dispatch_message_event(msg, assistant_buffer)

            case "control_request":
                flushed = flush_assistant_buffer(assistant_buffer, agent_name)
                if flushed:
                    final_response = flushed
                handle_control_request(proc, msg)

            case "result":
                flushed = flush_assistant_buffer(assistant_buffer, agent_name)
                if flushed:
                    final_response = flushed
                final_response = handle_result_event(msg, final_response)
                break

            case "error":
                flush_assistant_buffer(assistant_buffer, agent_name)
                print(f"\n[worker] ❌ Error: {msg.get('message', 'unknown')}")
                break

    return final_response


# ── Session orchestration ──────────────────────────────────────────────────────

def print_session_header(agent_name: str, cwd: str, prompt: str) -> None:
    print(f"\n{'='*60}")
    print(f"  invoke_agent_worker.py  [{agent_name}]")
    print(f"  cwd: {cwd}")
    print(f"  prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
    print(f"{'='*60}\n")


def run_session(agent_name: str, agent_id: str, letta_url: str, cwd: str, prompt: str) -> tuple[int, str | None]:
    """Launch agent, process the event stream, return (exit_code, final_response)."""
    print_session_header(agent_name, cwd, prompt)
    proc = launch_letta(cwd, agent_id, letta_url)
    final_response = None

    terminated = False
    try:
        final_response = process_events(proc, prompt, agent_name)
    except KeyboardInterrupt:
        print("\n[worker] Interrupted")
        proc.terminate()
        terminated = True
    except Exception as e:
        print(f"\n[worker] ❌ Unexpected error: {e}")
        proc.terminate()
        terminated = True
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        if terminated:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            proc.wait()

    if final_response:
        print(f"\n{'='*60}\nFINAL RESPONSE:\n{'='*60}\n{final_response}\n{'='*60}\n")

    return proc.returncode, final_response


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Agent session worker (spawned by invoke_agent.py)")
    parser.add_argument("agent_name", help="Name of the agent to invoke (must be in agents.json)")
    parser.add_argument("--cwd", default=DEFAULT_CWD, help="Working directory for the agent")
    parser.add_argument("--result-file", dest="result_file", default=None,
                        help="Path to write final response for the launcher to capture")
    parser.add_argument("prompt", help="The prompt to send to the agent")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agent = resolve_agent(args.agent_name)
    # Use LETTA_BASE_URL env var if set (e.g., in Docker), else construct from localhost
    letta_url = os.environ.get("LETTA_BASE_URL") or f"http://localhost:{agent['port']}"

    exit_code, final_response = run_session(
        agent_name=args.agent_name,
        agent_id=agent["agent_id"],
        letta_url=letta_url,
        cwd=args.cwd,
        prompt=args.prompt,
    )

    if args.result_file and final_response:
        try:
            with open(args.result_file, "w", encoding="utf-8") as f:
                f.write(final_response)
        except Exception as e:
            print(f"[worker] Warning: could not write result file: {e}")

    print("Closing in 5s")
    time.sleep(5)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())



