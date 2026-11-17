"""Intent is classified per human turn, not per request (AIProjects-58uc)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.gateway.app import _classify_task_type

TOOLS = {"tools": [{"name": "Read"}]}

PROMPT = {"role": "user", "content": "please summarize this file"}
TOOL_USE = {"role": "assistant", "content": [
    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}
TOOL_RESULT = {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t1",
     "content": "def foo(): write a function class"}]}


def test_tools_request_classified_by_latest_prompt():
    assert _classify_task_type(TOOLS, [PROMPT]) == "summarization"


def test_tool_result_hop_inherits_turn_task_type():
    msgs = [PROMPT, TOOL_USE, TOOL_RESULT]
    assert _classify_task_type(TOOLS, msgs) == "summarization"


def test_new_prompt_reclassifies():
    msgs = [PROMPT, TOOL_USE, TOOL_RESULT,
            {"role": "user", "content": [{"type": "text", "text": "translate it"}]}]
    assert _classify_task_type(TOOLS, msgs) == "translation"


def test_always_returns_a_task_type():
    for body, msgs in [({}, []), (TOOLS, []), (TOOLS, [TOOL_RESULT]),
                       ({}, [PROMPT]), ({"tools": []}, [{"role": "user", "content": ""}])]:
        result = _classify_task_type(body, msgs)
        assert isinstance(result, str) and result
