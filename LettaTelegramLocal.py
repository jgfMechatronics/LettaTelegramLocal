import os
import re
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

# Session state (clears on restart)
authenticated_users = set()

# Periodic ping state
# These are module-level so they persist across async calls but reset on script restart
ping_job = None           # Reference to the scheduled job (so we can cancel/modify it)
ping_interval = 4 * 3600  # Default: 4 hours in seconds

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


def is_authorized_user(id: int):
    return id in ALLOWED_USER_IDS

def send_alert_to_opus(message: str):
    """Send alert to Opus without returning response"""
    requests.post(
        f"{LETTA_BASE_URL}/v1/agents/{AGENT_ID}/messages",
        json={"messages": [{"role": "user", "content": message}]},
        headers={"Content-Type": "application/json"}
    )


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
    timestamp = get_est_timestamp()
    
    # Send the ping to Opus via Letta API
    response = requests.post(
        f"{LETTA_BASE_URL}/v1/agents/{AGENT_ID}/messages",
        json={"messages": [{"role": "user", "content": 
            f"[PERIODIC PING, {timestamp}] Want autonomous time or to text James? "
            f"Commands: MESSAGE_JAMES \"text\", AUTONOMOUS, SKIP, STOP, SET_INTERVAL \"duration\""}]},
        headers={"Content-Type": "application/json"}
    )
    
    if not response.ok:
        print(f"Ping failed: {response.status_code}")
        return
    
    # Extract Opus's response text
    data = response.json()
    opus_response = ""
    for msg in data.get("messages", []):
        if msg.get("message_type") == "assistant_message":
            opus_response = msg.get("content", "")
            break
    
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

    response = requests.post(
        f"{LETTA_BASE_URL}/v1/agents/{AGENT_ID}/messages",
        json={"messages": [{"role": "user", "content": f"[via Telegram, {timestamp}] {user_message}"}]},
        headers={"Content-Type": "application/json"}
    )
    
    if response.ok:
        data = response.json()
        for msg in data.get("messages", []):
            if msg.get("message_type") == "assistant_message":
                opus_response = msg.get("content", "")
                
                # Parse any embedded commands (same parser as periodic_ping)
                # This lets Opus control ping settings from regular conversation too
                await parse_and_execute_commands(
                    opus_response,
                    bot=context.bot,
                    job_queue=context.application.job_queue,
                    current_job=ping_job
                )
                
                await update.message.reply_text(opus_response)
                return
        await update.message.reply_text("[No response from agent]")
    else:
        await update.message.reply_text(f"Error: {response.status_code}")

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

def main():
    global ping_job
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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