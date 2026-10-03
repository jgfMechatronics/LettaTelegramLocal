"""Autonomous smoke test for the Agent Home clients (telegram bridge + group chat).

Verifies agent_home_client (sync + async drivers) and the bridge's command
parsing against a live local Agent Home server.

Setup (from Agent-Home_dev):
    AGENT_HOME_DB_PATH=/tmp/tg_test/db.sqlite ./start_server.sh
    curl -s -X POST http://localhost:8008/agents -H "Content-Type: application/json" \
        -d '{"name": "Opus", "system_instructions": "You are a helpful test agent. Follow instructions exactly.", \
             "config": {"model_name": "together:zai-org/GLM-5.3-Flash", "tool_names": [], "soft_compaction_limit": 100000}}'

Run:
    uv run python test_bridge.py
"""

import asyncio
import sys

sys.path.insert(0, "/home/gossf/git/LettaTelegramLocal")

from agent_home_client import (
    AgentStreamError,
    resolve_agent_id_by_name_sync,
    send_message_async,
    send_message_sync,
)

import LettaTelegramLocal as bridge

SERVER_URL = "http://localhost:8008"


async def test_registry_resolution():
    agent_id = resolve_agent_id_by_name_sync(SERVER_URL, "opus")  # lowercase — case-insensitive
    print(f"[PASS] registry resolution (sync, lowercase): {agent_id}")

    # Timestamp helper — exercises zoneinfo/tzdata (regression guard: the
    # /start handler crashed with ZoneInfoNotFoundError when tzdata was absent)
    ts = bridge.get_est_timestamp()
    assert "EST" in ts and ":" in ts, f"bad timestamp: {ts!r}"
    print(f"[PASS] get_est_timestamp: {ts!r}")
    return agent_id


async def test_send_message(agent_id):
    # Async driver (bridge path)
    result = await send_message_async(SERVER_URL, agent_id, "Say exactly: HELLO_ASYNC and nothing else.")
    assert "HELLO_ASYNC" in result, f"async driver: {result!r}"
    print(f"[PASS] send_message_async: {result[:60]!r}")

    # Sync driver (GC path)
    result = send_message_sync(SERVER_URL, agent_id, "Say exactly: HELLO_SYNC and nothing else.")
    assert "HELLO_SYNC" in result, f"sync driver: {result!r}"
    print(f"[PASS] send_message_sync: {result[:60]!r}")


async def test_bridge_wrappers(agent_id):
    # Bridge wrappers use module globals from .env (production port 8000);
    # point them at the local test server for this test.
    bridge.SERVER_URL = SERVER_URL
    bridge.AGENT_ID = agent_id
    result = await bridge.send_message("Say exactly: HELLO_BRIDGE and nothing else.")
    assert result["success"], f"bridge send_message failed: {result['result']}"
    assert "HELLO_BRIDGE" in result["result"], f"bridge: {result['result']!r}"
    print(f"[PASS] bridge send_message wrapper: {result['result'][:60]!r}")

    await bridge.send_alert_to_opus("[TEST ALERT] Bridge smoke test alert.")
    print("[PASS] send_alert: completed without exception")


async def test_not_found_error():
    try:
        resolve_agent_id_by_name_sync(SERVER_URL, "does-not-exist")
        raise AssertionError("expected AgentStreamError")
    except AgentStreamError as e:
        assert "not found" in str(e) and "available:" in str(e), str(e)
        print(f"[PASS] registry not-found error lists available agents: {str(e)[:80]}...")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None):
        self.sent.append((chat_id, text))


class FakeJob:
    def __init__(self):
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_repeating(self, callback, interval, first=None):
        self.jobs.append((interval, first))
        return FakeJob()


class FakeApplication:
    def __init__(self):
        self.stopped = False

    def stop_running(self):
        self.stopped = True


async def test_commands():
    bot = FakeBot()
    bridge.authenticated_users.add(int(bridge.AUTHORIZED_USER))

    response = (
        "Some intro text.\n"
        'MESSAGE_JAMES "Bridge smoke test message"\n'
        'SET_INTERVAL "2 hours"\n'
        "AUTONOMOUS\n"
        "STOP\n"
    )
    executed = await bridge.parse_and_execute_commands(
        response, bot=bot, job_queue=FakeJobQueue(), current_job=FakeJob()
    )
    assert executed.get("MESSAGE_JAMES") == "Bridge smoke test message", executed
    assert executed.get("SET_INTERVAL") == 7200, executed
    assert executed.get("AUTONOMOUS") is True, executed
    assert executed.get("STOP") is True, executed
    assert bot.sent == [(int(bridge.AUTHORIZED_USER), "Bridge smoke test message")], bot.sent
    print(f"[PASS] command parsing: {list(executed.keys())}")

    app = FakeApplication()
    executed = await bridge.parse_and_execute_commands("KILL_TELEGRAM", bot=bot, application=app)
    assert executed.get("KILL_TELEGRAM") is True and app.stopped is True
    print("[PASS] KILL_TELEGRAM: application.stop_running() called")


async def main():
    agent_id = await test_registry_resolution()
    await test_send_message(agent_id)
    await test_bridge_wrappers(agent_id)
    await test_not_found_error()
    await test_tag_sender_unit()
    await test_tag_progressive_live(agent_id)
    await test_commands()
    print("\n=== ALL TESTS PASSED ===")


async def test_tag_sender_unit():
    """TelegramTagSender: progressive sends as tags close, none for partials.

    Mirrors the real contract: scan() receives the full accumulated buffer
    each time (it grows as the run streams).
    """
    bridge.authenticated_users.add(int(bridge.AUTHORIZED_USER))
    bot = FakeBot()
    chat_id = int(bridge.AUTHORIZED_USER)
    sender = bridge.TelegramTagSender(bot, chat_id)

    # Partial tag — not sent yet
    buffer = "working on it... <telegram>progress update"
    await sender.scan(buffer)
    assert sender.sent_count == 0 and bot.sent == [], (sender.sent_count, bot.sent)

    # Tag closes — sent, untagged preamble NOT sent
    buffer += "</telegram> more private notes..."
    await sender.scan(buffer)
    assert sender.sent_count == 1, sender.sent_count
    assert bot.sent == [(chat_id, "progress update")], bot.sent

    # Second tag closes — progressive: only the new one
    buffer += "<telegram>second update</telegram>"
    await sender.scan(buffer)
    assert sender.sent_count == 2
    assert bot.sent[-1] == (chat_id, "second update"), bot.sent
    print(f"[PASS] TelegramTagSender unit: partials held, completes sent progressively ({sender.sent_count} sends)")


async def test_tag_progressive_live(agent_id):
    """Live: agent wraps output in tags → sent progressively, updates_sent=1."""
    bridge.SERVER_URL = SERVER_URL
    bridge.AGENT_ID = agent_id
    bridge.authenticated_users.add(int(bridge.AUTHORIZED_USER))
    bot = FakeBot()
    chat_id = int(bridge.AUTHORIZED_USER)

    result = await bridge.send_message(
        'Reply with exactly: <telegram>TAGGED_LIVE_OK</telegram> and nothing else.',
        bot=bot,
        chat_id=chat_id,
    )
    assert result["success"], f"live tagged send failed: {result['result']}"
    assert result["updates_sent"] == 1, f"expected 1 update, got {result['updates_sent']}: {result['result']!r}"
    assert bot.sent and bot.sent[0][1] == "TAGGED_LIVE_OK", bot.sent
    print(f"[PASS] live progressive tag send: {bot.sent[0][1]!r} (updates_sent=1)")

    # No-tag fallback: updates_sent=0, full text in result
    result = await bridge.send_message(
        "Say exactly: UNTAGGED_FALLBACK_OK and nothing else.",
        bot=bot,
        chat_id=chat_id,
    )
    assert result["success"]
    assert result["updates_sent"] == 0, f"expected 0 updates, got {result['updates_sent']}"
    assert "UNTAGGED_FALLBACK_OK" in result["result"], result["result"]
    print("[PASS] live no-tag fallback: updates_sent=0, full text returned")


if __name__ == "__main__":
    asyncio.run(main())