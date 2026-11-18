"""Small, content-free summaries of a request and its reply.

Three helpers, all pure and never raising:

* ``summarize_sse`` / ``summarize_message``: what the model sent back
  (block types, tool names, thinking size, stop_reason). Tool arguments and
  text are never kept, only names and character counts.
* ``turn_shape`` / ``conversation_position``: where this request sits in the
  conversation. ``turn_index`` counts the human prompts so far; ``hop_index``
  counts the model replies since the last prompt (0 = the model is answering
  the human directly, 3 = third tool round-trip). Compression uses the same
  ``turn_shape``, so there is one definition of a turn.
* ``parse_prefix_mismatch``: splits Anthropic's
  ``anthropic-thinking-prefix-mismatch`` header ("k=v; k=v") into fields.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

_MAX_TOOLS = 64
_TOOL_BLOCKS = ("tool_use", "server_tool_use", "mcp_tool_use")
_MAX_INPUT_CHARS = 200_000


def _classify_action(name: str, input_dict: Any, out: dict) -> None:
    """Append the content-free action label and signature for one tool call.

    The input is only read here, never stored. Lists stay parallel to tool_names.
    """
    try:
        from loom.observability.tool_actions import classify

        label, sig = classify(name, input_dict if isinstance(input_dict, dict) else {})
    except Exception:
        label, sig = "unknown.other", "unknown"
    out["tool_actions"].append(label)
    out["tool_signatures"].append((str(name)[:64], label, sig))


def _empty() -> dict:
    return {
        "stop_reason": None,
        "block_types": [],
        "tool_names": [],
        "tool_actions": [],
        "tool_signatures": [],
        "thinking_chars": 0,
        "text_chars": 0,
        "output_tokens": None,
    }


def _finish(out: dict) -> dict:
    out["tool_names"] = out["tool_names"][:_MAX_TOOLS]
    out["tool_actions"] = out["tool_actions"][:_MAX_TOOLS]
    out["tool_signatures"] = out["tool_signatures"][:_MAX_TOOLS]
    out["tool_count"] = len(out["tool_names"])
    out["thinking"] = "thinking" in out["block_types"] or "redacted_thinking" in out["block_types"]
    return out


def summarize_sse(lines: Iterable[str]) -> Optional[dict]:
    """Summarize an Anthropic SSE stream. None when no Anthropic event is seen."""
    try:
        out = _empty()
        seen = False
        # index -> [tool_name, input_json_fragments, size]; dropped once classified.
        pending: dict = {}
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
                if btype in _TOOL_BLOCKS and block.get("name"):
                    out["tool_names"].append(str(block["name"])[:64])
                    start_input = block.get("input")
                    pending[event.get("index")] = [
                        str(block["name"]), [], 0,
                        start_input if isinstance(start_input, dict) and start_input else None,
                    ]
            elif etype == "content_block_stop":
                entry = pending.pop(event.get("index"), None)
                if entry is not None:
                    parsed = entry[3]
                    if entry[1]:
                        try:
                            parsed = json.loads("".join(entry[1]))
                        except ValueError:
                            parsed = None
                    _classify_action(entry[0], parsed, out)
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "input_json_delta":
                    entry = pending.get(event.get("index"))
                    if entry is not None and entry[2] < _MAX_INPUT_CHARS:
                        frag = delta.get("partial_json")
                        if isinstance(frag, str):
                            entry[1].append(frag)
                            entry[2] += len(frag)
                elif dtype == "text_delta":
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
        # Streams cut off before content_block_stop still get a label.
        for entry in pending.values():
            _classify_action(entry[0], None, out)
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
            if btype in _TOOL_BLOCKS and block.get("name"):
                out["tool_names"].append(str(block["name"])[:64])
                _classify_action(block["name"], block.get("input"), out)
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


def is_user_prompt(msg: dict) -> bool:
    """True for a genuine user prompt: a user message carrying text and no
    tool_result block (tool results ride in user-role messages too)."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            has_text = True
    return has_text


def turn_shape(messages: list) -> dict:
    """Prompt/tool-call shape of a conversation. The one place Loom counts turns.

    Returns user_prompts (genuine prompts), tool_calls (all tool_use blocks),
    tool_calls_this_turn (tool_use blocks since the latest prompt),
    hops_this_turn (model replies since the latest prompt, i.e. API round-trips)
    and prompt_indices (message index of each prompt, oldest first). Used by
    compression for turn-aware protection and recorded per request.
    """
    prompt_indices: list[int] = []
    tool_calls = 0
    this_turn = 0
    hops = 0
    for idx, msg in enumerate(messages or []):
        if is_user_prompt(msg):
            prompt_indices.append(idx)
            this_turn = 0
            hops = 0
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        hops += 1
        content = msg.get("content")
        if isinstance(content, list):
            n_calls = sum(
                1 for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            )
            tool_calls += n_calls
            this_turn += n_calls
    return {
        "user_prompts": len(prompt_indices),
        "tool_calls": tool_calls,
        "tool_calls_this_turn": this_turn,
        "hops_this_turn": hops,
        "prompt_indices": prompt_indices,
    }


def conversation_position(messages: Any) -> dict:
    """turn_index (= user_prompts), hop_index (= hops_this_turn),
    tool_calls_this_turn and message_count for a messages list."""
    empty = {"turn_index": None, "hop_index": None,
             "tool_calls_this_turn": None, "message_count": None}
    try:
        if not isinstance(messages, list):
            return empty
        shape = turn_shape(messages)
        return {
            "turn_index": shape["user_prompts"],
            "hop_index": shape["hops_this_turn"],
            "tool_calls_this_turn": shape["tool_calls_this_turn"],
            "message_count": len(messages),
        }
    except Exception:
        return empty


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
