"""Inline compression: savings measurement, loom tags, cache reuse, sessions."""

import uuid

from loom.gateway.app import (
    _compress_messages_inline,
    _strip_loom_tag,
    derive_session_id,
)
from loom.storage import LoomStorage

FILLER = (
    "So basically what happened is that the deployment process, you know, "
    "actually completed successfully and everything worked fine in the end. "
) * 20


def _messages(n: int) -> list[dict]:
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"turn {i}: {FILLER}"})
    return msgs


class FakeProcessor:
    """Deterministic stand-in: halves the text for anything past age 0.3."""

    def compress_graduated(self, text: str, age_ratio: float):
        if age_ratio < 0.3:
            return text, "full"
        return text[: len(text) // 2], "medium"


def test_savings_measured_and_tagged():
    msgs = _messages(8)
    out, before, after, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after > 0
    # Last 2 messages untouched
    assert out[-1] == msgs[-1] and out[-2] == msgs[-2]
    # An old message (age_ratio >= 0.3) got compressed and tagged
    _, tier = _strip_loom_tag(out[4]["content"])
    assert tier == "medium"


def test_tagged_messages_not_recompressed():
    msgs = _messages(8)
    out1, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    # Second pass over the already-compressed conversation
    out2, before2, after2, _ = _compress_messages_inline(FakeProcessor(), out1)
    for m1, m2 in zip(out1[:-2], out2[:-2]):
        if _strip_loom_tag(
            m1["content"] if isinstance(m1["content"], str) else ""
        )[1]:
            assert m2["content"] == m1["content"]
    assert before2 == after2  # tagged content counts as already-saved


def test_cache_roundtrip(tmp_path):
    store = LoomStorage(db_path=str(tmp_path / "cache.db"))
    store.connect()
    msgs = _messages(8)
    out1, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs, store)

    class ExplodingProcessor:
        def compress_graduated(self, text, age_ratio):
            raise AssertionError("should have hit the cache")

    # Same original messages -> cache supplies the compressed text.
    out2, _, _, _ = _compress_messages_inline(
        ExplodingProcessor(), _messages(8), store
    )
    assert [m["content"] for m in out2] == [m["content"] for m in out1]
    store.close()


def test_short_conversations_untouched():
    msgs = _messages(2)
    out, before, after, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert out == msgs and before == 0 and after == 0


def test_derive_session_id_stable():
    msgs = [{"role": "user", "content": "hello world " + uuid.uuid4().hex}]
    a = derive_session_id(msgs, "pytest")
    b = derive_session_id(msgs + [{"role": "assistant", "content": "hi"}], "pytest")
    assert a == b and a.startswith("gw-")
    assert derive_session_id(msgs, "other") != a
    assert derive_session_id([], "pytest") == "unknown"


def test_block_content_messages():
    msgs = _messages(6)
    # Index 2 of 6 -> age_ratio 0.4, old enough to compress.
    msgs[2]["content"] = [{"type": "text", "text": FILLER}]
    out, before, after, by_type = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after
    # The block list stays a block list, text compressed in place.
    assert isinstance(out[2]["content"], list)
    assert out[2]["content"][0]["type"] == "text"
    assert len(out[2]["content"][0]["text"]) < len(FILLER)
    assert by_type["text"]["before"] > by_type["text"]["after"]
    assert derive_session_id(msgs, "pytest").startswith("gw-")


def _tool_conversation() -> list[dict]:
    """8-message conversation with tool_use/tool_result on old-enough turns.

    Indices 3 and 4 of 8 give age_ratios 0.43 and 0.57 — past the 0.3
    graduated-compression threshold.
    """
    msgs = _messages(8)
    msgs[3] = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": FILLER},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "bash",
                "input": {"cmd": "ls -la /very/important/path"},
            },
        ],
    }
    msgs[4] = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01", "content": FILLER},
        ],
    }
    return msgs


def test_tool_result_text_compressed_structure_preserved():
    """tool_result text is compressed while the block structure survives."""
    msgs = _tool_conversation()
    out, before, after, by_type = _compress_messages_inline(FakeProcessor(), msgs)

    # Structure preserved: lists stay lists, block types and ids intact.
    assert isinstance(out[3]["content"], list)
    assert isinstance(out[4]["content"], list)
    tool_use = out[3]["content"][1]
    assert tool_use == msgs[3]["content"][1]  # tool_use verbatim
    tool_result = out[4]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_01"

    # The tool_result content string got compressed and tagged.
    assert isinstance(tool_result["content"], str)
    assert len(tool_result["content"]) < len(FILLER)
    _, tier = _strip_loom_tag(tool_result["content"])
    assert tier is not None

    assert before > after
    assert by_type["tool_result"]["before"] > by_type["tool_result"]["after"]
    assert by_type["tool_use"]["before"] == by_type["tool_use"]["after"]


def test_tool_result_block_list_content():
    """tool_result whose content is a list of text blocks compresses in place."""
    msgs = _messages(8)
    msgs[4] = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_02",
                "content": [
                    {"type": "text", "text": FILLER},
                    {"type": "image", "source": {"type": "base64", "data": "xx"}},
                ],
            },
        ],
    }
    out, before, after, _ = _compress_messages_inline(FakeProcessor(), msgs)
    inner = out[4]["content"][0]["content"]
    assert isinstance(inner, list) and len(inner) == 2
    assert inner[0]["type"] == "text"
    assert len(inner[0]["text"]) < len(FILLER)
    assert inner[1] == msgs[4]["content"][0]["content"][1]  # image untouched
    assert before > after


def test_tool_results_opt_out():
    """compress_tool_results=False restores the old skip behavior."""
    msgs = _tool_conversation()
    out, before, after, _ = _compress_messages_inline(
        FakeProcessor(), msgs, compress_tool_results=False
    )
    assert out[4]["content"] == msgs[4]["content"]


def test_short_tool_results_untouched():
    """Tiny tool_result payloads (< threshold) pass through verbatim."""
    msgs = _messages(8)
    msgs[4] = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_03", "content": "ok"},
        ],
    }
    out, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert out[4]["content"] == msgs[4]["content"]


def test_tool_result_not_recompressed():
    """A compressed tool_result is not compressed again on the next turn."""
    msgs = _tool_conversation()
    out1, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    out2, _, _, _ = _compress_messages_inline(FakeProcessor(), out1)
    assert out2[4]["content"] == out1[4]["content"]


def test_recent_tool_results_untouched():
    """The last 2 messages (active context) are never compressed."""
    msgs = _messages(8)
    msgs[7] = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_04", "content": FILLER},
        ],
    }
    out, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert out[7]["content"] == msgs[7]["content"]
