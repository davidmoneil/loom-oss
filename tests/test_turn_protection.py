"""Turn-aware recency protection and calls-per-prompt metric (AIProjects-vzt4)."""

import json
from types import SimpleNamespace

from loom.gateway.app import _compress_messages_inline, _is_user_prompt, _turn_shape
from loom.storage.base import _calls_per_prompt

FILLER = "lorem ipsum dolor sit amet " * 40


class HalvingProcessor:
    def compress_graduated(self, text: str, age_ratio: float):
        if age_ratio < 0.3:
            return text, "full"
        return text[: len(text) // 2], "medium"


def _cfg(protect_turns=0, turn_max=40, window=6):
    return SimpleNamespace(compression=SimpleNamespace(
        tool_result_protect_window=window,
        loop_detected_protect_multiplier=1,
        image_offload_budget_bytes=0,
        head_protect_window=1,
        protect_user_turns=protect_turns,
        protect_turn_max_messages=turn_max,
    ))


def _agentic(prompts: int, calls_per_prompt: int) -> list[dict]:
    msgs = []
    for p in range(prompts):
        msgs.append({"role": "user", "content": f"prompt {p}: {FILLER}"})
        for c in range(calls_per_prompt):
            tid = f"p{p}c{c}"
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"echo {tid}"}}]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": f"{tid} {FILLER}"}]})
    return msgs


def test_is_user_prompt_excludes_tool_results():
    assert _is_user_prompt({"role": "user", "content": "hi"})
    assert _is_user_prompt({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    assert not _is_user_prompt({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
        {"type": "text", "text": "hook note"}]})
    assert not _is_user_prompt({"role": "assistant", "content": "hi"})


def test_turn_shape_counts():
    shape = _turn_shape(_agentic(3, 4))
    assert shape["user_prompts"] == 3
    assert shape["tool_calls"] == 12
    assert shape["tool_calls_this_turn"] == 4
    assert shape["prompt_indices"] == [0, 9, 18]


def test_current_turn_fully_protected():
    msgs = _agentic(2, 10)  # 42 messages; current turn = 21 messages
    out, *_ = _compress_messages_inline(HalvingProcessor(), msgs, config=_cfg(protect_turns=1))
    turn_start = _turn_shape(msgs)["prompt_indices"][-1]
    assert all(out[i] is msgs[i] for i in range(turn_start, len(msgs)))
    assert out[2] is not msgs[2]  # earlier turn still compressed


def test_message_window_only_without_turn_protection():
    msgs = _agentic(2, 10)
    out, *_ = _compress_messages_inline(HalvingProcessor(), msgs, config=_cfg(protect_turns=0))
    turn_start = _turn_shape(msgs)["prompt_indices"][-1]
    assert any(out[i] is not msgs[i] for i in range(turn_start, len(msgs) - 6))


def test_turn_protection_capped():
    msgs = _agentic(1, 40)  # one giant turn, 81 messages
    stats: dict = {}
    _compress_messages_inline(HalvingProcessor(), msgs, config=_cfg(protect_turns=1, turn_max=20), stats=stats)
    assert stats["protected_recency"] == 20
    assert stats["protected_turn"] == 14
    assert stats["turn"] == {"user_prompts": 1, "tool_calls": 40, "tool_calls_this_turn": 40}


def test_calls_per_prompt_groups_by_session_and_turn():
    def row(sid, prompts, calls):
        return {"session_id": sid, "skip_reasons": json.dumps(
            {"turn": {"user_prompts": prompts, "tool_calls": 0, "tool_calls_this_turn": calls}})}
    rows = [row("a", 1, 1), row("a", 1, 5), row("a", 2, 3), row("b", 1, 10),
            row(None, 1, 99), {"session_id": "c", "skip_reasons": None}]
    out = _calls_per_prompt(rows)
    assert out["turns"] == 3 and out["sessions"] == 2
    assert out["avg"] == 6.0 and out["max"] == 10 and out["p50"] == 5
