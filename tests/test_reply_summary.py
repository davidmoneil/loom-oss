"""Reply summary, turn/hop position and prefix-mismatch parsing (issue #168)."""

import json

from loom.observability.ratelimit_sink import REPLY_COLUMNS, REPLY_RAW_KEY, build_row
from loom.observability.reply_summary import (
    conversation_position,
    parse_prefix_mismatch,
    summarize_message,
    summarize_sse,
)


def _sse(*events):
    lines = []
    for e in events:
        lines.append(f"event: {e['type']}")
        lines.append("data: " + json.dumps(e))
        lines.append("")
    return lines


TOOL_TURN = _sse(
    {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 5}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "need to list files"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Listing."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": \"cat secret\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
)


def test_summarize_sse_tool_turn():
    s = summarize_sse(TOOL_TURN)
    assert s["stop_reason"] == "tool_use"
    assert s["block_types"] == ["thinking", "text", "tool_use"]
    assert s["tool_names"] == ["Bash"]
    assert s["tool_count"] == 1
    assert s["thinking"] is True
    assert s["thinking_chars"] == len("need to list files")
    assert s["text_chars"] == len("Listing.")
    assert s["output_tokens"] == 42


def test_summary_never_holds_tool_arguments_or_text():
    blob = json.dumps(summarize_sse(TOOL_TURN))
    assert "cat secret" not in blob
    assert "need to list files" not in blob
    assert "Listing." not in blob


def test_summarize_sse_ignores_non_anthropic():
    assert summarize_sse(["data: {\"choices\": []}", "data: [DONE]"]) is None
    assert summarize_sse([]) is None


def test_summarize_message_end_turn():
    s = summarize_message({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"output_tokens": 3},
    })
    assert s["stop_reason"] == "end_turn"
    assert s["tool_names"] == [] and s["thinking"] is False
    assert summarize_message("nope") is None


def _tool_result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}


def test_conversation_position_counts_hops_since_human():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]},
        _tool_result(),
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]},
        _tool_result(),
    ]
    assert conversation_position(msgs) == {"turn_index": 2, "hop_index": 2, "message_count": 7}
    assert conversation_position(msgs[:3])["hop_index"] == 0
    assert conversation_position(None)["turn_index"] is None


def test_parse_prefix_mismatch():
    v = ("kind=blocks_removed; pattern=tail_kept; block=messages.2.content.0; "
         "item=message_assistant; removed=604; inserted=0; modified=0; messages_delta=-254")
    p = parse_prefix_mismatch(v)
    assert p["kind"] == "blocks_removed" and p["pattern"] == "tail_kept"
    assert p["removed"] == 604 and p["messages_delta"] == -254
    assert parse_prefix_mismatch("origin=other; block=messages.2.content.0")["kind"] == "other"
    assert parse_prefix_mismatch(None) is None


_RL = {"raw_headers": {"anthropic-thinking-prefix-mismatch": "kind=blocks_modified; pattern=system_block_rerendered; modified=1"}}


def test_build_row_default_rides_in_raw_headers():
    row = build_row(
        source="t", method="POST", path="/v1/messages", status_code=200, model="m",
        ratelimit=_RL, reply=summarize_sse(TOOL_TURN),
        position={"turn_index": 3, "hop_index": 2, "message_count": 9},
    )
    assert row["stop_reason"] == "tool_use"
    raw = json.loads(row["raw_headers"])
    rep = raw[REPLY_RAW_KEY]
    assert rep["tool_names"] == "Bash" and rep["hop_index"] == 2 and rep["turn_index"] == 3
    assert rep["prefix_mismatch_pattern"] == "system_block_rerendered"
    assert not any(c in row for c in REPLY_COLUMNS)


def test_build_row_reply_columns():
    row = build_row(
        source="t", method="POST", path="/v1/messages", status_code=200, model="m",
        ratelimit=_RL, reply=summarize_sse(TOOL_TURN),
        position={"turn_index": 1, "hop_index": 0, "message_count": 1},
        reply_columns=True,
    )
    assert row["tool_names"] == "Bash" and row["tool_count"] == 1
    assert row["block_types"] == "thinking,text,tool_use"
    assert row["hop_index"] == 0 and row["prefix_mismatch_modified"] == 1
    assert REPLY_RAW_KEY not in json.loads(row["raw_headers"])


def test_build_row_without_reply_is_unchanged():
    row = build_row(source="t", method="POST", path="/v1/messages", status_code=200,
                    model="m", ratelimit={"raw_headers": {"a": "b"}})
    assert json.loads(row["raw_headers"]) == {"a": "b"}


# ------------------------------------------------- end-to-end through the app
import pytest  # noqa: E402

from loom.gateway.app import GatewayState, _record_request, _wrapped_stream  # noqa: E402
from loom.observability import AuditLogger  # noqa: E402


class _SpySink:
    def __init__(self):
        self.calls = []

    def emit(self, **kw):
        self.calls.append(kw)


def _meta(position):
    return dict(
        request_id="r1", method="POST", path="/v1/messages", source="cc",
        provider="anthropic", model="m", requested_model=None, task_type="t",
        routing_reason="x", position=position,
    )


@pytest.mark.asyncio
async def test_stream_path_reaches_sink_and_audit(tmp_path):
    st = GatewayState()
    st.audit = AuditLogger(str(tmp_path / "audit.jsonl"), str(tmp_path / "metrics.jsonl"))
    sink = _SpySink()
    st.ratelimit_sink = sink

    class _B:
        _last_ratelimit = {"upstream_request_id": "req_1", "raw_headers": {"x": "y"}}

    async def upstream():
        yield ("\n".join(TOOL_TURN) + "\n").encode()

    pos = {"turn_index": 4, "hop_index": 3, "message_count": 12}
    out = [c async for c in _wrapped_stream(st, upstream(), _meta(pos), 0.0, backend=_B())]
    assert out  # bytes forwarded unchanged
    call = sink.calls[0]
    assert call["stop_reason"] == "tool_use"
    assert call["reply"]["tool_names"] == ["Bash"]
    assert call["position"] == pos
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert rec["stop_reason"] == "tool_use" and rec["tool_names"] == ["Bash"]
    assert rec["turn_index"] == 4 and rec["hop_index"] == 3


def test_non_stream_result_fills_reply(tmp_path):
    st = GatewayState()
    st.audit = AuditLogger(str(tmp_path / "audit.jsonl"), str(tmp_path / "metrics.jsonl"))
    sink = _SpySink()
    st.ratelimit_sink = sink
    _record_request(
        st, status_code=200, latency_ms=1.0,
        ratelimit={"upstream_request_id": "r"},
        result={"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]},
        **{k: v for k, v in _meta(None).items() if k != "position"},
    )
    assert sink.calls[0]["stop_reason"] == "end_turn"
    assert sink.calls[0]["reply"]["block_types"] == ["text"]
