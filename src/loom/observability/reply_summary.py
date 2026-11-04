"""Small, content-free summaries of a request and its reply.

Three helpers, all pure and never raising:

* ``summarize_sse`` / ``summarize_message``: what the model sent back
  (block types, tool names, thinking size, stop_reason). Tool arguments and
  text are never kept, only names and character counts.
* ``conversation_position``: where this request sits in the conversation.
  ``turn_index`` counts the human messages so far; ``hop_index`` counts the
  model replies since the last human message (0 = the model is answering the
  human directly, 3 = third tool round-trip). Worked out from the request
  body alone, so it needs no stored state and survives restarts.
* ``parse_prefix_mismatch``: splits Anthropic's
  ``anthropic-thinking-prefix-mismatch`` header ("k=v; k=v") into fields.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

_MAX_TOOLS = 64


def _empty() -> dict:
    return {
        "stop_reason": None,
        "block_types": [],
        "tool_names": [],
        "thinking_chars": 0,
        "text_chars": 0,
        "output_tokens": None,
    }


def _finish(out: dict) -> dict:
    out["tool_names"] = out["tool_names"][:_MAX_TOOLS]
    out["tool_count"] = len(out["tool_names"])
    out["thinking"] = "thinking" in out["block_types"] or "redacted_thinking" in out["block_types"]
    return out


def summarize_sse(lines: Iterable[str]) -> Optional[dict]:
    """Summarize an Anthropic SSE stream. None when no Anthropic event is seen."""
    try:
        out = _empty()
        seen = False
        for line in lines:
            if not isinstance(line, str) or not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "content_block_start":
                seen = True
                block = event.get("content_block") or {}
                btype = block.get("type") or "unknown"
                out["block_types"].append(btype)
                if btype in ("tool_use", "server_tool_use", "mcp_tool_use") and block.get("name"):
                    out["tool_names"].append(str(block["name"])[:64])
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    out["text_chars"] += len(delta.get("text") or "")
                elif dtype == "thinking_delta":
                    out["thinking_chars"] += len(delta.get("thinking") or "")
            elif etype == "message_delta":
                seen = True
                sr = (event.get("delta") or {}).get("stop_reason")
                if sr:
                    out["stop_reason"] = sr
                ot = (event.get("usage") or {}).get("output_tokens")
                if isinstance(ot, int):
                    out["output_tokens"] = ot
            elif etype == "message_start":
                seen = True
        return _finish(out) if seen else None
    except Exception:
        return None


def summarize_message(result: Any) -> Optional[dict]:
    """Summarize a non-streamed Anthropic message body."""
    try:
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            return None
        out = _empty()
        out["stop_reason"] = result.get("stop_reason")
        for block in result["content"]:
            if not isinstance(block, dict):
                continue
            btype = block.get("type") or "unknown"
            out["block_types"].append(btype)
            if btype in ("tool_use", "server_tool_use", "mcp_tool_use") and block.get("name"):
                out["tool_names"].append(str(block["name"])[:64])
            elif btype == "text":
                out["text_chars"] += len(block.get("text") or "")
            elif btype == "thinking":
                out["thinking_chars"] += len(block.get("thinking") or "")
        ot = (result.get("usage") or {}).get("output_tokens")
        if isinstance(ot, int):
            out["output_tokens"] = ot
        return _finish(out)
    except Exception:
        return None


def _is_human(msg: dict) -> bool:
    """A user message that carries no tool results was typed by a person (or a job)."""
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def conversation_position(messages: Any) -> dict:
    """Return turn_index, hop_index and message_count for a messages list."""
    try:
        if not isinstance(messages, list):
            return {"turn_index": None, "hop_index": None, "message_count": None}
        turn = 0
        hop = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user" and _is_human(msg):
                turn += 1
                hop = 0
            elif role == "assistant":
                hop += 1
        return {"turn_index": turn, "hop_index": hop, "message_count": len(messages)}
    except Exception:
        return {"turn_index": None, "hop_index": None, "message_count": None}


def _to_int(val: Optional[str]) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def parse_prefix_mismatch(value: Any) -> Optional[dict]:
    """Split ``kind=...; pattern=...; removed=1`` into a dict of known fields."""
    try:
        if not isinstance(value, str) or not value.strip():
            return None
        fields: dict[str, str] = {}
        for part in value.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip()] = v.strip()
        return {
            "kind": (fields.get("kind") or fields.get("origin") or None),
            "pattern": fields.get("pattern"),
            "item": fields.get("item"),
            "removed": _to_int(fields.get("removed")),
            "inserted": _to_int(fields.get("inserted")),
            "modified": _to_int(fields.get("modified")),
            "messages_delta": _to_int(fields.get("messages_delta")),
        }
    except Exception:
        return None
