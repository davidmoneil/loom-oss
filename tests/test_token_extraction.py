"""Tests for gateway token/skill extraction helpers."""

from loom.gateway.app import _extract_skill, _extract_tokens


def test_extract_tokens_anthropic_cache_split():
    usage = {
        "input_tokens": 500,
        "output_tokens": 200,
        "cache_read_input_tokens": 18000,
        "cache_creation_input_tokens": 2500,
    }
    tokens_in, tokens_out, cache_read, cache_creation = _extract_tokens(usage)
    assert tokens_in == 500 + 18000 + 2500
    assert tokens_out == 200
    assert cache_read == 18000
    assert cache_creation == 2500


def test_extract_tokens_no_cache_fields():
    tokens_in, tokens_out, cache_read, cache_creation = _extract_tokens(
        {"prompt_tokens": 100, "completion_tokens": 50}
    )
    assert (tokens_in, tokens_out, cache_read, cache_creation) == (100, 50, 0, 0)


def test_extract_tokens_non_dict():
    assert _extract_tokens(None) == (0, 0, 0, 0)


def test_extract_skill_from_command_block():
    messages = [
        {"role": "user", "content": "<command-name>/end-session</command-name>\nrun it"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "done"}]},
    ]
    assert _extract_skill(messages) == "end-session"


def test_extract_skill_most_recent_wins():
    messages = [
        {"role": "user", "content": "<command-name>/tasks</command-name>"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "<command-name>/end-session</command-name>"}]},
    ]
    assert _extract_skill(messages) == "end-session"


def test_extract_skill_absent():
    assert _extract_skill([{"role": "user", "content": "hello"}]) is None
    assert _extract_skill(None) is None
