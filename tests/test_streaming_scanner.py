"""Tests for streaming DLP scanning of buffered Ollama NDJSON responses."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.gateway.app import GatewayState, _scan_ollama_stream
from loom.scanner.engine import SensitiveDataScanner


def make_scanner():
    return SensitiveDataScanner({"scanner": {"enabled": True, "sanitize_logs": False}})


def make_state(scanner=None):
    state = GatewayState()
    state.scanner = scanner
    return state


async def _collect(gen):
    return [chunk async for chunk in gen]


async def _aiter(lines):
    for line in lines:
        yield line


def run_stream(upstream_lines, gw, text_key="response"):
    gen = _scan_ollama_stream(
        _aiter(upstream_lines),
        gw,
        request_id="req-1",
        source="test",
        provider="ollama",
        model="qwen3:8b",
        text_key=text_key,
    )
    return asyncio.run(_collect(gen))


def ndjson(*objs):
    return [(json.dumps(o) + "\n").encode("utf-8") for o in objs]


class TestGenerateFormat:
    def test_clean_text_passes_through_unmodified(self):
        gw = make_state(make_scanner())
        lines = ndjson(
            {"response": "Hello ", "done": False},
            {"response": "world", "done": True},
        )
        out = run_stream(lines, gw)
        assert out == lines

    def test_sensitive_text_gets_redacted(self):
        gw = make_state(make_scanner())
        lines = ndjson(
            {"response": "My SSN is ", "done": False},
            {"response": "123-45-6789", "done": True},
        )
        out = run_stream(lines, gw)
        assert len(out) == 2

        first = json.loads(out[0])
        assert "[REDACTED:ssn]" in first["response"]
        assert first["done"] is False

        final = json.loads(out[1])
        assert final["response"] == ""
        assert final["done"] is True

    def test_no_scanner_passes_through_unmodified(self):
        gw = make_state(scanner=None)
        lines = ndjson({"response": "SSN: 123-45-6789", "done": True})
        out = run_stream(lines, gw)
        assert out == lines


class TestChatFormat:
    def test_sensitive_chat_message_gets_redacted(self):
        gw = make_state(make_scanner())
        lines = ndjson(
            {"message": {"role": "assistant", "content": "Card: "}, "done": False},
            {"message": {"role": "assistant", "content": "4111111111111111"}, "done": True},
        )
        out = run_stream(lines, gw, text_key="message")
        assert len(out) == 2

        first = json.loads(out[0])
        assert "****-****-****-1111" in first["message"]["content"]
        assert first["done"] is False

        final = json.loads(out[1])
        assert final["message"]["content"] == ""
        assert final["done"] is True

    def test_clean_chat_message_passes_through(self):
        gw = make_state(make_scanner())
        lines = ndjson(
            {"message": {"role": "assistant", "content": "just chatting"}, "done": True},
        )
        out = run_stream(lines, gw, text_key="message")
        assert out == lines


class TestMalformedInput:
    def test_non_json_lines_pass_through_when_no_detection(self):
        gw = make_state(make_scanner())
        lines = [b"not json at all\n"]
        out = run_stream(lines, gw)
        assert out == lines

    def test_empty_stream_yields_nothing(self):
        gw = make_state(make_scanner())
        out = run_stream([], gw)
        assert out == []
