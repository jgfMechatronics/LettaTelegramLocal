"""Shared Agent Home client logic — single source of truth for the SSE protocol.

Used by both the Telegram bridge (async) and group chat (sync). Provides:
- send_message_async / send_message_sync: POST /agents/{id}/messages and
  consume the SSE stream, accumulating the text response (thinking excluded)
- resolve_agent_id_by_name: look up an agent ID from the registry by name

Both drivers share _ResponseAccumulator, which owns the event-format logic
(PartStartEvent/PartDeltaEvent parsing, thinking-part detection). Drivers
only differ in transport (sync vs async httpx).

Error convention: hard failures raise AgentStreamError; timeouts return
whatever text accumulated plus a notice (partial response beats none).
"""

import json
from collections.abc import Awaitable, Callable

import httpx

DEFAULT_TIMEOUT_SECONDS = 480  # agent runs can be long (agentic tool use)


class AgentStreamError(RuntimeError):
    """Hard failure during an agent run (HTTP error, stream Error event, connection error)."""


class _ResponseAccumulator:
    """Feed SSE lines from an agent run stream; accumulate the text response.

    Thinking parts are excluded — only text parts contribute. Sets .error
    when an Error event arrives so drivers can surface the failure.
    """

    def __init__(self):
        self.text_parts: list[str] = []
        self._in_thinking = False
        self._event_type = None
        self._data_lines = []
        self.error: str | None = None

    def feed(self, line: str) -> None:
        """Process one line from the SSE stream."""
        if line.startswith("event:"):
            self._event_type = line[6:].strip()
            self._data_lines = []
        elif line.startswith("data:"):
            self._data_lines.append(line[5:].strip())
        elif line == "" and self._event_type:
            self._process_event("\n".join(self._data_lines))
            self._event_type = None
            self._data_lines = []

    def _process_event(self, data_str: str) -> None:
        try:
            data = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            data = {}

        if self._event_type == "PartStartEvent":
            part = data.get("part", {})
            part_kind = part.get("part_kind")
            if part_kind == "thinking":
                self._in_thinking = True
            elif part_kind == "text":
                self._in_thinking = False
                if content := part.get("content", ""):
                    self.text_parts.append(content)
        elif self._event_type == "PartDeltaEvent":
            if not self._in_thinking:
                if content := data.get("delta", {}).get("content_delta", ""):
                    self.text_parts.append(content)
        elif self._event_type == "Error":
            self.error = data.get("message", "Unknown error")

    def text(self) -> str:
        return "".join(self.text_parts).strip()


def _timeout_text(acc: _ResponseAccumulator, timeout_seconds: float) -> str:
    """Format partial response on timeout — accumulated text plus a notice."""
    suffix = f"\n[timed out after {timeout_seconds}s]"
    text = acc.text()
    return (text + suffix) if text else suffix.strip()


async def send_message_async(server_url: str, agent_id: str, message: str,
                             timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                             on_text_delta: Callable[[str], Awaitable[None]] | None = None) -> str:
    """Send a message and return the agent's text response (thinking excluded).

    Raises AgentStreamError on hard failures. On timeout, returns partial
    text plus a notice.

    on_text_delta: optional async callback invoked with the full accumulated
    text buffer after each text delta. Enables progressive consumption of
    output mid-run (e.g. the telegram bridge's tag scanner).
    """
    url = f"{server_url}/agents/{agent_id}/messages"
    acc = _ResponseAccumulator()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, json={"message": message}) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]
                    raise AgentStreamError(f"HTTP {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    parts_before = len(acc.text_parts)
                    acc.feed(line)
                    if acc.error is not None:
                        raise AgentStreamError(acc.error)
                    if on_text_delta is not None and len(acc.text_parts) != parts_before:
                        await on_text_delta("".join(acc.text_parts))
    except httpx.TimeoutException:
        return _timeout_text(acc, timeout_seconds)
    except httpx.RequestError as e:
        raise AgentStreamError(f"Connection error: {e}") from e
    return acc.text()


def send_message_sync(server_url: str, agent_id: str, message: str,
                      timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Sync twin of send_message_async — same semantics, blocking transport."""
    url = f"{server_url}/agents/{agent_id}/messages"
    acc = _ResponseAccumulator()
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            with client.stream("POST", url, json={"message": message}) as response:
                if response.status_code != 200:
                    detail = response.read().decode(errors="replace")[:200]
                    raise AgentStreamError(f"HTTP {response.status_code}: {detail}")
                for line in response.iter_lines():
                    acc.feed(line)
                    if acc.error is not None:
                        raise AgentStreamError(acc.error)
    except httpx.TimeoutException:
        return _timeout_text(acc, timeout_seconds)
    except httpx.RequestError as e:
        raise AgentStreamError(f"Connection error: {e}") from e
    return acc.text()


def _find_agent_by_name(agents: list, agent_name: str, server_url: str) -> str:
    """Return the ID for agent_name (case-insensitive), or raise with the
    available names listed."""
    for agent in agents:
        if agent.get("name", "").lower() == agent_name.lower():
            return agent["id"]
    available = ", ".join(sorted(a.get("name", "?") for a in agents))
    raise AgentStreamError(
        f"Agent {agent_name!r} not found on {server_url} — available: {available}"
    )


def resolve_agent_id_by_name_sync(server_url: str, agent_name: str) -> str:
    """Look up an agent ID by name from the Agent Home registry."""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{server_url}/agents")
            resp.raise_for_status()
            return _find_agent_by_name(resp.json(), agent_name, server_url)
    except httpx.HTTPError as e:
        raise AgentStreamError(f"Registry lookup failed: {e}") from e


async def resolve_agent_id_by_name_async(server_url: str, agent_name: str) -> str:
    """Async twin of resolve_agent_id_by_name_sync."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{server_url}/agents")
            resp.raise_for_status()
            return _find_agent_by_name(resp.json(), agent_name, server_url)
    except httpx.HTTPError as e:
        raise AgentStreamError(f"Registry lookup failed: {e}") from e