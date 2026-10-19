#!/bin/bash
# Launch Telegram bridges for multiple agents.
#
# Usage:
#   ./launch_all.sh              # Launch all agents (Opus, Sonnet, Haiku)
#   ./launch_all.sh Opus Sonnet  # Launch specific agents only
#
# Each agent needs its own bot token in the .env file:
#   TELEGRAM_BOT_TOKEN_OPUS=...
#   TELEGRAM_BOT_TOKEN_SONNET=...
#   TELEGRAM_BOT_TOKEN_HAIKU=...
#
# Common settings (AUTHORIZED_USER, TELEGRAM_PASSWORD, etc.) are shared.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env to get the bot tokens
source .env

# Default: all agents
DEFAULT_AGENTS=("Opus" "Sonnet" "Haiku")

# Use command line args if provided, otherwise default
if [ $# -gt 0 ]; then
    AGENTS=("$@")
else
    AGENTS=("${DEFAULT_AGENTS[@]}")
fi

# Track child PIDs for cleanup
PIDS=()

cleanup() {
    echo "Shutting down all bridges..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
        fi
    done
    wait
    echo "All bridges stopped."
}

trap cleanup EXIT INT TERM

# Launch each agent
for agent in "${AGENTS[@]}"; do
    # Get the token variable name for this agent (e.g., TELEGRAM_BOT_TOKEN_OPUS)
    token_var="TELEGRAM_BOT_TOKEN_${agent^^}"
    token="${!token_var:-}"
    
    if [ -z "$token" ]; then
        echo "Warning: No token found for $agent ($token_var not set), skipping..."
        continue
    fi
    
    echo "Launching bridge for $agent..."
    TELEGRAM_BOT_TOKEN="$token" AGENT_NAME="$agent" \
        systemd-inhibit \
            --what=sleep:idle \
            --who="Agent Home Telegram Bridge ($agent)" \
            --why="Bridge running — $agent needs message access" \
            uv run python LettaTelegramLocal.py &
    
    PIDS+=($!)
done

if [ ${#PIDS[@]} -eq 0 ]; then
    echo "No agents launched! Check your .env for TELEGRAM_BOT_TOKEN_<AGENT> variables."
    exit 1
fi

echo "All bridges launched. Press Ctrl+C to stop."
wait
