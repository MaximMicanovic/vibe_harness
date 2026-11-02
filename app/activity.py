"""Live activity feed of a card's vibe session (spec: hold C and click a card).

The session LOG is the CLI's final output — for a headless run it stays almost
empty until the session ends. The vibe journal is the opposite: it is appended
*while* the session runs, so it is the only place that answers "what is it
doing right now".

``$VIBE_HOME/logs/session/unified/<session-id>/journal/*.jsonl`` carries, per
line, one of:

- ``payload.delta[].entry.content[].text`` — assistant output (a ``message``
  entry with ``role: "assistant"``);
- ``payload.delta[].entry.text`` with an id starting ``reasoning-`` — the
  model's thinking;
- ``payload.request`` with ``kind: "tool"`` and ``call.arguments`` — a tool
  call;
- ``payload.result`` — the tool result (``tool_succeeded`` / ``tool_failed``);
- ``payload.delta[].entry.detail.display.statusText`` — e.g. "Running
  file_system.bash".

This module turns those lines into a small event feed the popup can render.
The cursor is a LINE COUNT, not a byte offset: the client passes back the
number of lines it has consumed and gets only what was appended since. A first
call (cursor 0) returns the last ``DEFAULT_LIMIT`` events, so opening the popup
on a long-finished session shows its tail instead of replaying everything.

Everything is defensive: a missing journal, an unreadable file, a vibe upgrade
changing the schema or a half-written line just means "no event". The feed is
a display nicety and must never turn into an API error.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

#: Longest text/summary one event carries. A tool result can be megabytes of
#: stdout; the popup shows a gist, not a log viewer.
MAX_TEXT = 2000

#: Events returned by one call (and by a first call with no cursor).
DEFAULT_LIMIT = 200

#: Hard ceiling for a caller asking for more.
MAX_LIMIT = 500

#: Argument keys that make a readable one-line gist of a tool call, in order.
_SUMMARY_KEYS = ("command", "path", "query", "url", "pattern", "prompt", "file_path")

#: Result keys that make a readable one-line gist of a tool result, in order.
_RESULT_KEYS = ("stdout", "stderr", "output", "error", "content")


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    """`text` trimmed to at most `limit` characters (an ellipsis when cut)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _iso_ms(ms: float) -> str | None:
    """A journal millisecond timestamp as an ISO string (None when unusable)."""
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _entry_at(entry: dict) -> str | None:
    """The timestamp a projection entry carries, if any."""
    for key in ("createdAt", "updatedAt"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _iso_ms(value)
    return None


def _file_at(path: Path) -> str | None:
    """The journal file's mtime — the fallback for records with no timestamp."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _content_text(content) -> str:
    """The text of a message's content blocks (a string is taken as-is)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _arguments_summary(arguments) -> str:
    """A one-line gist of a tool call's arguments: the command for a shell
    call, else the JSON."""
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, dict):
        return ""
    for key in _SUMMARY_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _result_summary(result) -> str:
    """A one-line gist of a tool result: its output, else its JSON."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        for key in _RESULT_KEYS:
            value = structured.get(key)
            if isinstance(value, str) and value.strip():
                return value
    content = result.get("content")
    if isinstance(content, (str, list)):
        text = _content_text(content)
        if text.strip():
            return text
    try:
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _delta_events(payload: dict) -> list[dict]:
    """Events carried by a projection delta: text, reasoning and status."""
    out: list[dict] = []
    delta = payload.get("delta")
    if not isinstance(delta, list):
        return out
    for change in delta:
        if not isinstance(change, dict):
            continue
        entry = change.get("entry")
        if not isinstance(entry, dict):
            continue
        at = _entry_at(entry)
        kind = entry.get("type")
        if kind == "message":
            if entry.get("role") != "assistant":
                continue  # the prompt is not activity
            text = _content_text(entry.get("content"))
            if text.strip():
                out.append({"kind": "text", "at": at, "text": _clip(text)})
        elif kind == "reasoning" or str(entry.get("id") or "").startswith("reasoning-"):
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                out.append({"kind": "reasoning", "at": at, "text": _clip(text)})
        elif kind == "effect":
            detail = entry.get("detail")
            display = detail.get("display") if isinstance(detail, dict) else None
            status = display.get("statusText") if isinstance(display, dict) else None
            if isinstance(status, str) and status.strip():
                out.append({"kind": "status", "at": at, "text": _clip(status)})
    return out


def _tool_call_events(payload: dict) -> list[dict]:
    """The tool call an action intent carries (payload.request, kind "tool")."""
    if payload.get("kind") != "tool":
        return []
    request = payload.get("request")
    call = request.get("call") if isinstance(request, dict) else None
    if not isinstance(call, dict):
        return []
    name = call.get("name")
    if not isinstance(name, str) or not name.strip():
        return []
    return [{"kind": "tool", "at": None, "tool": name,
             "summary": _clip(_arguments_summary(call.get("arguments")))}]


def _tool_result_events(payload: dict) -> list[dict]:
    """The tool result an action result carries (payload.result)."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    kind = result.get("type")
    if kind not in ("tool_succeeded", "tool_failed"):
        return []
    return [{"kind": "result", "at": None, "ok": kind == "tool_succeeded",
             "summary": _clip(_result_summary(result.get("result")))}]


def parse_journal_line(line: str) -> list[dict]:
    """The events one journal line carries (usually zero or one).

    `at` is None when the record itself carries no timestamp — `read_activity`
    fills it from the last known one. Never raises: a garbage or half-written
    line is simply no event.
    """
    line = (line or "").strip()
    if not line:
        return []
    try:
        record = json.loads(line)
    except ValueError:
        return []
    if not isinstance(record, dict):
        return []
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    return (_delta_events(payload) + _tool_call_events(payload)
            + _tool_result_events(payload))


def read_activity(journal_dir: Path, cursor: int = 0,
                  limit: int = DEFAULT_LIMIT) -> dict:
    """The events appended after `cursor` journal lines.

    `cursor` counts lines across every journal file, in name order; the
    returned cursor is the new total. A first call (cursor 0) returns the last
    `limit` events. A cursor past the end means a different (fresh) session
    journal, so the tail is shown again instead of an empty feed.

    Never raises: a missing or unreadable journal is an empty feed.
    """
    try:
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        cursor = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        cursor = 0
    try:
        files = sorted(journal_dir.glob("*.jsonl"))
    except OSError:
        return {"cursor": 0, "events": []}

    total = 0
    numbered: list[tuple[int, dict]] = []   # (line number, event)
    last_at: str | None = None
    for path in files:
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_at = _file_at(path)
        for line in data.splitlines():
            total += 1
            events = parse_journal_line(line)
            for event in events:
                if event.get("at"):
                    last_at = event["at"]
                else:
                    event["at"] = last_at or file_at
                numbered.append((total, event))

    if cursor > total:
        cursor = 0   # a fresh journal: the old cursor means nothing here
    events = [event for (line_no, event) in numbered if line_no > cursor]
    if len(events) > limit:
        events = events[-limit:]
    return {"cursor": total, "events": events}
