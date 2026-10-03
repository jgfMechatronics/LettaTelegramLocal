"""Telegram bridge for Agent Home agents.

Polls Telegram for messages from the authorized user, forwards them to the
configured Agent Home agent, and relays responses back. Supports a periodic
self-wake ping and agent-side commands (MESSAGE_JAMES, STOP, SET_INTERVAL,
AUTONOMOUS, SKIP, KILL_TELEGRAM).

Configuration via .env:
    TELEGRAM_BOT_TOKEN — Telegram bot token
    AUTHORIZED_USER    — Telegram user ID allowed to talk to the bot
    TELEGRAM_PASSWORD  — Password for /start authentication
    SKIP_FIRST_PING    — True/False: whether to skip the first periodic ping
    SERVER_URL         — Agent Home server URL (default: http://localhost:8000)
    AGENT_NAME         — Agent name to resolve in the registry (default: Opus)
"""

import asyncio
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters

load_dotenv()

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER = os.getenv("AUTHORIZED_USER")
TELEGRAM_PASSWORD = os.getenv("TELEGRAM_PASSWORD")
SKIP_FIRST_PING = os.getenv("SKIP_FIRST_PING")
if SKIP_FIRST_PING == "True":
    SKIP_FIRST_PING = True
elif SKIP_FIRST_PING == "False":
    SKIP_FIRST_PING = False
else:
    raise Exception("invalid value for SKIP_FIRST_PING")

SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")
AGENT_NAME = os.getenv("AGENT_NAME", "Opus")

ALLOWED_USER_IDS = [int(AUTHORIZED_USER)]

# Agent Home communication
MESSAGE_TIMEOUT_SECONDS = 480  # agent runs can be long (agentic tool use)
ALERT_TIMEOUT_SECONDS = 120   # alerts: best-effort, don't block callers as long
AGENT_ID = None               # resolved at startup from the agent registry

# Session state (clears on restart)
authenticated_users = set()

# Periodic ping state
# These are module-level so they persist across async calls but reset on script restart
ping_job = None           # Reference to the scheduled job (so we can cancel/modify it)
if SKIP_FIRST_PING:
    ping_interval = 99999
else:
    ping_interval = 4 * 3600  # Default: 4 hours in seconds


def get_est_timestamp():
    est = ZoneInfo("US/Eastern")
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


async def resolve_agent_id(client: httpx.AsyncClient) -> str:
    """Look up the agent ID by name from the Agent Home registry.

    Resolving by name (rather than hardcoding an ID) means the bridge keeps
    working if the agent is ever recreated with a new ID.
    """
    resp = await client.get(f"{SERVER_URL}/agents")
    resp.raise_for_status()
    for agent in resp.json():
        if agent.get("name") == AGENT_NAME:
            return agent["id"]
    raise RuntimeError(
        f"Agent {AGENT_NAME!r} not found on {SERVER_URL} — check AGENT_NAME/SERVER_URL"
    )


async def _consume_agent_stream(message: str, timeout_seconds: int) -> str:
    """POST a message to the agent and consume the SSE response stream.

    Returns the accumulated text response (thinking parts excluded). On
    timeout, returns whatever accumulated plus a timeout notice — a partial
    response is better than losing it entirely.

    Raises RuntimeError for HTTP errors and stream Error events, so callers
    can distinguish hard failures from (partial) success.
    """
    url = f"{SERVER_URL}/agents/{AGENT_ID}/messages"
    accumulated_text = ""
    in_thinking = False

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, json={"message": message}) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]
                    raise RuntimeError(f"HTTP {response.status_code}: {detail}")

                current_event_type = None
                data_lines = []

                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        current_event_type = line[6:].strip()
                        data_lines = []
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "" and current_event_type:
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
                            raise RuntimeError(data.get("message", "Unknown error"))

                        current_event_type = None
                        data_lines = []
    except httpx.TimeoutException:
        suffix = f"\n[timed out after {timeout_seconds}s]"
        return (accumulated_text + suffix) if accumulated_text else suffix.strip()
    except httpx.RequestError as e:
        raise RuntimeError(f"Connection error: {e}") from e

    return accumulated_text.strip()


async def send_message(message: str) -> dict:
    """Send a message to the agent and return its response.

    Returns dict with 'success' and 'result' keys.
    """
    try:
        text = await _consume_agent_stream(message, MESSAGE_TIMEOUT_SECONDS)
        return {"success": True, "result": text}
    except RuntimeError as e:
        return {"success": False, "result": str(e)}


async def send_alert_to_opus(message: str):
    """Send a fire-and-forget alert to the agent.

    The response is discarded, but the stream is still consumed so the agent
    run completes server-side (dropping the connection mid-run could cancel it).
    """
    try:
        await _consume_agent_stream(message, ALERT_TIMEOUT_SECONDS)
    except RuntimeError as e:
        print(f"Alert delivery failed: {e}")


async def parse_and_execute_commands(opus_response: str, bot, job_queue=None, current_job=None, application=None):
    """
    Parse Opus's response for commands and execute them.

    Commands must be on their own line (prevents accidental execution when
    discussing commands in prose). Uses MULTILINE flag so ^ and $ match
    line boundaries.

    Commands with arguments use quotes: MESSAGE_JAMES "text", SET_INTERVAL "2 hours"
    Commands without arguments are keyword-only: STOP, AUTONOMOUS, SKIP, KILL_TELEGRAM

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
            await send_alert_to_opus("[MESSAGE_JAMES FAILED] User has not authenticated yet (/start). Message not sent.")
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
        print("Ping stopped by Opus")# SET_INTERVAL "duration" — change ping frequency (must be on its own line)
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
            alertStr = f"Interval updated to {ping_interval} seconds"
            print(alertStr)
            await send_alert_to_opus(alertStr)
        else:
            failureStr = f"Invalid interval in: {opus_response[:100]}"
            print(failureStr)
            await send_alert_to_opus(failureStr)

    # AUTONOMOUS and SKIP are informational (must be on their own line)
    if re.search(r'^\s*AUTONOMOUS\s*$', opus_response, re.IGNORECASE | re.MULTILINE):
        executed["AUTONOMOUS"] = True
        print("Opus taking autonomous time")
    if re.search(r'^\s*SKIP\s*$', opus_response, re.IGNORECASE | re.MULTILINE):
        executed["SKIP"] = True
        print("Opus skipped this ping")

    # KILL_TELEGRAM — emergency shutdown of the Telegram bridge
    # Security feature: if something seems wrong (spammer, compromised auth), Opus can kill the link
    if re.search(r'^\s*KILL_TELEGRAM\s*$', opus_response, re.IGNORECASE | re.MULTILINE):
        executed["KILL_TELEGRAM"] = True
        print("!!! KILL_TELEGRAM invoked — shutting down Telegram bridge !!!")
        # Alert before shutdown so it's in the conversation history
        await send_alert_to_opus("[KILL_TELEGRAM EXECUTED] Telegram bridge shutting down. Restart manually when safe.")
        # Graceful shutdown — this will stop polling and exit run_polling()
        if application:
            application.stop_running()

    return executed


async def periodic_ping(context):
    """
    Scheduled callback that pings Opus asking if she wants autonomous time.

    This is an async function because it runs inside the telegram event loop.
    The 'context' parameter is passed by JobQueue — it gives us access to:
      - context.bot: for sending Telegram messages
      - context.job: the Job object itself (for cancellation, rescheduling)
      - context.job_queue, context.application

    Flow:
    1. Send ping to the agent via the Agent Home API, get Opus's response
    2. Parse her response for commands via shared parser
    3. Execute the appropriate action
    """

    if not hasattr(periodic_ping, "_count"):
        periodic_ping._count = 0
    periodic_ping._count += 1
    if periodic_ping._count == 1 and SKIP_FIRST_PING:
        return

    timestamp = get_est_timestamp()

    # Send the ping to Opus via the Agent Home API
    basicMsg = (
        f"[SELF WAKE PERIODIC PING, {timestamp}] If desired, you can run commands, call tools, take autonomous time, etc.\n"
        f"Commands: MESSAGE_JAMES \"text\", AUTONOMOUS, SKIP, STOP, SET_INTERVAL \"duration\"\n"
        f"Curr Ping Interval: {ping_interval}Sec/Ping\n"
    )

    longPingStopSuggestion = f"If further pings are not desired for now, please invoke the STOP Cmd\n"  # also notifies on first ping since it is long

    if ping_interval >= 3600*2:
        prompt = basicMsg + longPingStopSuggestion
    else:
        prompt = basicMsg

    result = await send_message(prompt)

    if not result["success"]:
        print(f"Ping failed: {result['result']}")
        return

    opus_response = result["result"]

    # Parse and execute commands
    executed = await parse_and_execute_commands(
        opus_response,
        bot=context.bot,
        job_queue=context.job_queue,
        current_job=context.job,
        application=context.application
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

    result = await send_message(formatted_message)

    if result["success"]:
        opus_response = result["result"]

        # Parse any embedded commands (same parser as periodic_ping)
        # This lets Opus control ping settings from regular conversation too
        await parse_and_execute_commands(
            opus_response,
            bot=context.bot,
            job_queue=context.application.job_queue,
            current_job=ping_job,
            application=context.application
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
            await send_alert_to_opus(f"[SECURITY ALERT via Telegram, {timestamp}] Failed authentication attempt from user ID: {user_id}")
        await update.message.reply_text("Hmm, I don't understand. Please try again?")
        return

    # Correct password
    authenticated_users.add(user_id)
    await update.message.reply_text("Connected to Opus! Send me a message.")


def main():
    global ping_job, AGENT_ID

    # Resolve the agent ID from the registry before starting — fail fast
    # if the server is down or the agent doesn't exist.
    async def _resolve():
        async with httpx.AsyncClient(timeout=30) as client:
            return await resolve_agent_id(client)

    AGENT_ID = asyncio.run(_resolve())
    print(f"Connected to agent {AGENT_NAME!r} ({AGENT_ID}) at {SERVER_URL}")

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
    #   - first: seconds until FIRST run
    ping_job = app.job_queue.run_repeating(
        periodic_ping,
        interval=ping_interval,
        first=10  # TODO: Change to ping_interval for production
    )

    print(f"Bot running with polling... Ping interval: {ping_interval} seconds")

    # run_polling() starts the event loop and blocks forever
    app.run_polling()

if __name__ == "__main__":
    main()