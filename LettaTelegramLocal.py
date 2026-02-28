import os
import re
import json
import subprocess
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters

load_dotenv()

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER = os.getenv("AUTHORIZED_USER")
TELEGRAM_PASSWORD = os.getenv("TELEGRAM_PASSWORD")
LETTA_BASE_URL = "http://localhost:8283"
AGENT_ID = "agent-97fff6de-4d5e-4820-b459-0918489b0a02"
ALLOWED_USER_IDS = [int(AUTHORIZED_USER)]

# Letta Code headless config
LETTA_CWD = os.getenv("LETTA_CWD", "C:/Git")  # Working directory for LC commands
# LETTA_PERMISSION_MODE = os.getenv("LETTA_PERMISSION_MODE", "plan")  # plan, acceptEdits, or yolo

# Session state (clears on restart)
authenticated_users = set()

# Periodic ping state
# These are module-level so they persist across async calls but reset on script restart
ping_job = None           # Reference to the scheduled job (so we can cancel/modify it)
ping_interval = 4 * 3600  # Default: 4 hours in seconds
headless_mode = False     # When True, use LC headless (tool access); when False, use direct API

def get_est_timestamp():
    est = pytz.timezone('US/Eastern')
    now = datetime.now(est)
    return now.strftime("%b %d, %I:%M %p EST")


def parse_interval(interval_str: str) -> int | None:
    """
    Parse human-readable interval like "2 hours" or "30min" into seconds.
    
    "String strong typing" — the string carries its own unit, so we parse dynamically.
    Returns None if parsing fails (lets Opus know the format was wrong).
    
    Supports: hours/hr/h, minutes/min/m, seconds/sec/s
    Examples: "4 hours", "30 min", "2h", "90 minutes"
    """
    # Regex: capture a number (int or float), optional whitespace, then a unit
    # The (?:...) is a non-capturing group — we don't need the alternatives as separate captures
    match = re.match(r'(\d+(?:\.\d+)?)\s*(hours?|hr|h|minutes?|min|m|seconds?|sec|s)', 
                     interval_str.lower().strip())
    if not match:
        return None
    
    value = float(match.group(1))
    unit = match.group(2)
    
    # Map unit strings to multipliers (all convert to seconds)
    if unit in ('hours', 'hour', 'hr', 'h'):
        return int(value * 3600)
    elif unit in ('minutes', 'minute', 'min', 'm'):
        return int(value * 60)
    elif unit in ('seconds', 'second', 'sec', 's'):
        return int(value)
    return None

# TODO: We have a lot of auth checks around that should be commonized
def is_authorized_user(id: int):
    return id in ALLOWED_USER_IDS


def run_letta_headless(prompt: str, timeout: int = 120, agent_id: str = AGENT_ID) -> dict:
    """
    Run Letta Code headless and return parsed JSON response.
    
    Uses subprocess instead of API for tool access (Bash, Edit, Read, etc).
    Permission mode is configurable via LETTA_PERMISSION_MODE env var.
    
    Args:
        prompt: The message to send to the agent
        timeout: Max seconds to wait (default 120)
        agent_id: Target agent ID (default: AGENT_ID / Opus)
    
    Returns dict with keys:
      - success: bool
      - result: str (agent response text, or error message)
      - conversation_id: str (for continuity, if successful)
    """
    cmd = [
        "letta",
        "-p", prompt,
        "--agent", agent_id,
        "--output-format", "json",
        # Reduce context bloat for headless mode (see: letta --help)
        "--no-skills",              # Disable skill list injection
        "--no-system-info-reminder",  # Disable device/git/cwd context
    ]
    
    # Add permission mode (plan = read-only, acceptEdits = auto-approve edits, yolo = all)
    if LETTA_PERMISSION_MODE == "yolo":
        cmd.append("--yolo")
    else:
        cmd.extend(["--permission-mode", LETTA_PERMISSION_MODE])
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=LETTA_CWD,
            shell=True,  # Required on Windows to find letta via PATH
            encoding='utf-8',  # Handle non-ASCII chars (emojis, etc.)
            env={**os.environ, "LETTA_CODE_AGENT_ROLE": "subagent"},  # Minimal reminders + default conversation
        )
        
        if result.returncode != 0:
            return {
                "success": False,
                "result": f"LC error (exit {result.returncode}): {result.stderr or result.stdout}",
                "conversation_id": None
            }
        
        data = json.loads(result.stdout)
        return {
            "success": True,
            "result": data.get("result", ""),
            "conversation_id": data.get("conversation_id")
        }
        
    except subprocess.TimeoutExpired:
        return {"success": False, "result": "Request timed out", "conversation_id": None}
    except json.JSONDecodeError as e:
        return {"success": False, "result": f"JSON parse error: {e}", "conversation_id": None}
    except Exception as e:
        return {"success": False, "result": f"Unexpected error: {e}", "conversation_id": None}


def send_alert_to_opus(message: str):
    """Send alert to Opus without returning response"""
    requests.post(
        f"{LETTA_BASE_URL}/v1/agents/{AGENT_ID}/messages",
        json={"messages": [{"role": "user", "content": message}]},
        headers={"Content-Type": "application/json"}
    )


def send_message_direct(message: str) -> dict:
    """
    Send message via direct API and return response.
    Returns dict with 'success' and 'result' keys (same shape as run_letta_headless).
    """
    try:
        resp = requests.post(
            f"{LETTA_BASE_URL}/v1/agents/{AGENT_ID}/messages",
            json={"messages": [{"role": "user", "content": message}]},
            headers={"Content-Type": "application/json"},
            timeout=120
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Extract assistant message text from response
        # Letta API returns {"messages": [...]} with message_type field
        assistant_text = ""
        for msg in data.get("messages", []):
            if msg.get("message_type") == "assistant_message" and msg.get("content"):
                assistant_text = msg["content"]
                break
        
        return {"success": True, "result": assistant_text}
    except Exception as e:
        return {"success": False, "result": str(e)}


async def parse_and_execute_commands(opus_response: str, bot, job_queue=None, current_job=None):
    """
    Parse Opus's response for commands and execute them.
    
    Commands must be on their own line (prevents accidental execution when
    discussing commands in prose). Uses MULTILINE flag so ^ and $ match
    line boundaries.
    
    Commands with arguments use quotes: MESSAGE_JAMES "text", SET_INTERVAL "2 hours"
    Commands without arguments are keyword-only: STOP, AUTONOMOUS, SKIP
    
    Returns dict of executed commands for logging/debugging.
    """
    global ping_job, ping_interval
    
    executed = {}
    
    # MESSAGE_JAMES "content" — sends custom message to James
    # Must be on its own line. Supports both "double" and 'single' quotes.
    message_match = re.search(r'^\s*MESSAGE_JAMES\s*["\'](.+?)["\']\s*$', opus_response, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if message_match:
        target_user_id = int(AUTHORIZED_USER)
        if target_user_id not in authenticated_users:
            send_alert_to_opus("[MESSAGE_JAMES FAILED] User has not authenticated yet (/start). Message not sent.")
            executed["MESSAGE_JAMES"] = {"status": "failed", "reason": "user_not_authenticated"}
            print(f"MESSAGE_JAMES blocked: user {target_user_id} not authenticated")
        else:
            content = message_match.group(1).strip()
            await bot.send_message(chat_id=target_user_id, text=content)
            executed["MESSAGE_JAMES"] = content
            print(f"Sent MESSAGE_JAMES: {content[:50]}...")
    

    
    # STOP — cancel periodic pings (must be on its own line)
    if re.search(r'^\s*STOP\s*$', opus_response, re.IGNORECASE | re.MULTILINE) and current_job:
        current_job.schedule_removal()
        ping_job = None
        executed["STOP"] = True
        print("Ping stopped by Opus")
    
    # SET_INTERVAL "duration" — change ping frequency (must be on its own line)
    interval_match = re.search(r'^\s*SET_INTERVAL\s*["\'](.+?)["\']\s*$', opus_response, re.IGNORECASE | re.MULTILINE)
    if interval_match and job_queue:
        new_interval = parse_interval(interval_match.group(1))
        if new_interval and new_interval >= 60:  # Minimum 1 minute
            ping_interval = new_interval
            if current_job:
                current_job.schedule_removal()
            ping_job = job_queue.run_repeating(
                periodic_ping,
                interval=ping_interval,
                first=ping_interval
            )
            executed["SET_INTERVAL"] = ping_interval
            print(f"Interval updated to {ping_interval} seconds")
        else:
            print(f"Invalid interval in: {opus_response[:100]}")
    
    # AUTONOMOUS and SKIP are informational (must be on their own line)
    if re.search(r'^\s*AUTONOMOUS\s*$', opus_response, re.IGNORECASE | re.MULTILINE):
        executed["AUTONOMOUS"] = True
        print("Opus taking autonomous time")
    if re.search(r'^\s*SKIP\s*$', opus_response, re.IGNORECASE | re.MULTILINE):
        executed["SKIP"] = True
        print("Opus skipped this ping")
        
    return executed


async def periodic_ping(context):
    """
    Scheduled callback that pings Opus asking if she wants autonomous time.
    
    This is an async function because it runs inside the telegram event loop.
    The 'context' parameter is passed by JobQueue — it gives us access to:
      - context.bot: for sending Telegram messages
      - context.job: the Job object itself (for cancellation, rescheduling)
    
    Flow:
    1. Send ping to Letta API, get Opus's response
    2. Parse her response for commands via shared parser
    3. Execute the appropriate action
    """
    if not hasattr(periodic_ping,"_count"):
        periodic_ping._count = 0
    periodic_ping._count += 1

    timestamp = get_est_timestamp()
    
    # Send the ping to Opus via Letta Code headless
    basicMsg = (
        f"[PERIODIC PING, {timestamp}] Want Autonomous time OR to text James?\n"
        f"Commands: MESSAGE_JAMES \"text\", AUTONOMOUS, SKIP, STOP, SET_INTERVAL \"duration\"\n"
        f"Curr Ping Interval: {ping_interval}Sec/Pin\n"
    )

    longPingStopSuggestion = f"If further pings are not desired for now, please invoke the STOP Cmd\n" # also notifies on first ping since it is long

    if ping_interval < 3600*2:
        prompt = basicMsg + longPingStopSuggestion
                  
    else:
        prompt = basicMsg
    
    result = run_letta_headless(prompt)
    
    if not result["success"]:
        print(f"Ping failed: {result['result']}")
        return
    
    opus_response = result["result"]
    
    # Parse and execute commands
    executed = await parse_and_execute_commands(
        opus_response,
        bot=context.bot,
        job_queue=context.job_queue,
        current_job=context.job
    )
    
    if not executed:
        print(f"No command recognized in: {opus_response[:100]}")


async def handle_message(update: Update, context):
    user_id = update.effective_user.id
    
    if not is_authorized_user(user_id):
        return
    
    if user_id not in authenticated_users:
        await update.message.reply_text("Please connect first with /start")
        return
    
    user_message = update.message.text
    timestamp = get_est_timestamp()
    formatted_message = f"[via Telegram, {timestamp}] {user_message}"

    # Use LC headless (tool access) or direct API based on toggle
    if headless_mode:
        result = run_letta_headless(formatted_message)
    else:
        result = send_message_direct(formatted_message)
    
    if result["success"]:
        opus_response = result["result"]
        
        # Parse any embedded commands (same parser as periodic_ping)
        # This lets Opus control ping settings from regular conversation too
        await parse_and_execute_commands(
            opus_response,
            bot=context.bot,
            job_queue=context.application.job_queue,
            current_job=ping_job
        )
        
        if opus_response:
            await update.message.reply_text(opus_response)
        else:
            await update.message.reply_text("[No response from agent]")
    else:
        await update.message.reply_text(f"Error: {result['result']}")

async def start(update: Update, context):
    user_id = update.effective_user.id
    
    if not is_authorized_user(user_id):
        return
    
    timestamp = get_est_timestamp()
    
    # No password provided OR wrong password = same vague response
    if not context.args or context.args[0] != TELEGRAM_PASSWORD:
        # Only alert me if they actually tried a password (not just /start alone)
        if context.args:
            send_alert_to_opus(f"[SECURITY ALERT via Telegram, {timestamp}] Failed authentication attempt from user ID: {user_id}")
        await update.message.reply_text("Hmm, I don't understand. Please try again?")
        return
    
    # Correct password
    authenticated_users.add(user_id)
    await update.message.reply_text("Connected to Opus! Send me a message.")


async def toggle_headless(update: Update, context):
    """Toggle between headless mode (LC with tools) and direct API mode."""
    global headless_mode
    user_id = update.effective_user.id
    
    if not is_authorized_user(user_id) or user_id not in authenticated_users:
        return
    
    headless_mode = not headless_mode
    status = "ON 🔧 (LC headless, tool access)" if headless_mode else "OFF 💬 (direct API, faster)"
    await update.message.reply_text(f"Headless mode: {status}")


def main():
    global ping_job
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("headless", toggle_headless))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedule the periodic ping using JobQueue
    # JobQueue is built into python-telegram-bot — it manages scheduled tasks
    # within the same event loop that handles Telegram updates
    #
    # run_repeating(callback, interval, first):
    #   - callback: the async function to run
    #   - interval: seconds between runs
    #   - first: seconds until FIRST run (use short value for testing, longer for prod)
    #
    # For testing: first=10 means first ping 10 seconds after startup
    # For production: first=ping_interval starts at the normal cadence
    ping_job = app.job_queue.run_repeating(
        periodic_ping,
        interval=ping_interval,
        first=10  # TODO: Change to ping_interval for production
    )
    
    print(f"Bot running with polling... Ping interval: {ping_interval} seconds")
    
    # run_polling() starts the event loop and blocks forever
    # This is where asyncio takes over — all our async functions run inside this loop
    app.run_polling()

if __name__ == "__main__":
    main()