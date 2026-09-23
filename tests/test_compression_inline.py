"""Inline compression: savings measurement, loom tags, cache reuse, sessions."""

import uuid
from types import SimpleNamespace

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
    out, before, after, _, _loop = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after > 0
    # Last 2 messages untouched
    assert out[-1] == msgs[-1] and out[-2] == msgs[-2]
    # Index 4 of 8 (age_ratio 1-4/7 ~= 0.43) is old enough to compress, even
    # though it's close to the protected recency tail (idx 6,7).
    _, tier = _strip_loom_tag(out[4]["content"])
    assert tier == "medium"


def test_tagged_messages_not_recompressed():
    msgs = _messages(8)
    out1, _, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    # Second pass over the already-compressed conversation
    out2, before2, after2, _, _ = _compress_messages_inline(FakeProcessor(), out1)
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
    out1, _, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs, store)

    class ExplodingProcessor:
        def compress_graduated(self, text, age_ratio):
            raise AssertionError("should have hit the cache")

    # Same original messages -> cache supplies the compressed text.
    out2, _, _, _, _ = _compress_messages_inline(
        ExplodingProcessor(), _messages(8), store
    )
    assert [m["content"] for m in out2] == [m["content"] for m in out1]
    store.close()


def test_short_conversations_untouched():
    msgs = _messages(2)
    out, before, after, _, _loop = _compress_messages_inline(FakeProcessor(), msgs)
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
    # Index 2 of 6 -> age_ratio 1-2/5 = 0.6, old enough to compress.
    msgs[2]["content"] = [{"type": "text", "text": FILLER}]
    out, before, after, by_type, _loop = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after
    # The block list stays a block list, text compressed in place.
    assert isinstance(out[2]["content"], list)
    assert out[2]["content"][0]["type"] == "text"
    assert len(out[2]["content"][0]["text"]) < len(FILLER)
    assert by_type["text"]["before"] > by_type["text"]["after"]
    assert derive_session_id(msgs, "pytest").startswith("gw-")


def _tool_conversation() -> list[dict]:
    """8-message conversation with tool_use/tool_result on old-enough turns.

    Indices 3 and 4 of 8 give age_ratios 0.57 and 0.43 — past the 0.3
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
    out, before, after, by_type, _loop = _compress_messages_inline(FakeProcessor(), msgs)

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
    out, before, after, _, _loop = _compress_messages_inline(FakeProcessor(), msgs)
    inner = out[4]["content"][0]["content"]
    assert isinstance(inner, list) and len(inner) == 2
    assert inner[0]["type"] == "text"
    assert len(inner[0]["text"]) < len(FILLER)
    assert inner[1] == msgs[4]["content"][0]["content"][1]  # image untouched
    assert before > after


def _image_message(idx: int, b64_len: int, image_url_shape: bool = False) -> dict:
    data = "A" * b64_len
    block = (
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}
        if image_url_shape
        else {"type": "image", "source": {"type": "base64", "data": data}}
    )
    return {"role": "user" if idx % 2 == 0 else "assistant", "content": [block]}


def test_image_offload_disabled_by_default():
    """No config (or budget=0) preserves prior behavior: images always
    pass through untouched, however large."""
    msgs = _messages(8)
    msgs[2] = _image_message(2, b64_len=10_000)
    out, before, after, by_type, _loop = _compress_messages_inline(FakeProcessor(), msgs)
    assert out[2]["content"] == msgs[2]["content"]
    assert "image_offload" not in by_type


def test_image_offload_over_budget_replaced():
    """An image past the configured byte budget is swapped for a text
    placeholder; images within budget stay untouched."""
    msgs = _messages(8)
    msgs[2] = _image_message(2, b64_len=10_000)  # ~7500 decoded bytes
    stats: dict = {}
    out, before, after, by_type, _loop = _compress_messages_inline(
        FakeProcessor(),
        msgs,
        config=_config_stub(protect_window=2, image_offload_budget_bytes=100),
        stats=stats,
    )
    block = out[2]["content"][0]
    assert block["type"] == "text"
    assert "omitted" in block["text"]
    assert by_type["image_offload"]["before"] > by_type["image_offload"]["after"]
    assert stats["image_offloaded"] == 1
    assert before > after


def test_image_offload_under_budget_untouched():
    msgs = _messages(8)
    msgs[2] = _image_message(2, b64_len=10_000)
    out, *_ = _compress_messages_inline(
        FakeProcessor(),
        msgs,
        config=_config_stub(protect_window=2, image_offload_budget_bytes=1_000_000),
    )
    assert out[2]["content"] == msgs[2]["content"]


def test_image_offload_openai_shape():
    """The OpenAI image_url/data-URI shape is recognized too."""
    msgs = _messages(8)
    msgs[2] = _image_message(2, b64_len=10_000, image_url_shape=True)
    out, *_ = _compress_messages_inline(
        FakeProcessor(),
        msgs,
        config=_config_stub(protect_window=2, image_offload_budget_bytes=100),
    )
    assert out[2]["content"][0]["type"] == "text"


def test_tool_results_opt_out():
    """compress_tool_results=False restores the old skip behavior."""
    msgs = _tool_conversation()
    out, before, after, _, _loop = _compress_messages_inline(
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
    out, _, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert out[4]["content"] == msgs[4]["content"]


def test_tool_result_not_recompressed():
    """A compressed tool_result is not compressed again on the next turn."""
    msgs = _tool_conversation()
    out1, _, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    out2, _, _, _, _ = _compress_messages_inline(FakeProcessor(), out1)
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
    out, _, _, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert out[7]["content"] == msgs[7]["content"]


def _config_stub(
    protect_window: int = 6,
    loop_multiplier: int = 3,
    image_offload_budget_bytes: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        compression=SimpleNamespace(
            tool_result_protect_window=protect_window,
            loop_detected_protect_multiplier=loop_multiplier,
            image_offload_budget_bytes=image_offload_budget_bytes,
        )
    )


def test_protect_window_distinguishes_6_from_2():
    """window=6 protects 6 recent messages; window=2 only protects 2 —
    a conversation-length regression from 6 to 2 must be observable."""
    msgs = _messages(12)

    out6, *_ = _compress_messages_inline(
        FakeProcessor(), msgs, config=_config_stub(protect_window=6),
    )
    for idx in range(6, 12):
        assert out6[idx] == msgs[idx]

    out2, *_ = _compress_messages_inline(
        FakeProcessor(), msgs, config=_config_stub(protect_window=2),
    )
    # Indices n-6..n-3 (6,7,8,9): protected under window=6, newly eligible
    # under window=2 — this is the exact gap test_recent_tool_results_
    # untouched above cannot see (it only checks indices window=2 already
    # protects). Age ratio grows toward the *older* end of the eligible
    # range (idx=0 is oldest), so only 6,7 (age_ratio 0.45/0.36) clear the
    # 0.3 threshold; 8,9 (0.27/0.18) are still recent enough to stay full.
    for idx in (6, 7):
        assert out2[idx] != msgs[idx]
    for idx in (8, 9):
        assert out2[idx] == msgs[idx]


def test_loop_detection_widens_protect_window():
    """3+ identical tool_use calls (same name+input) trigger loop detection
    and widen the protect window enough to spare messages a plain
    window=6 would otherwise compress."""
    msgs = _messages(12)
    for i in (1, 3, 9):
        msgs[i] = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": FILLER},
                {
                    "type": "tool_use",
                    "id": f"toolu_{i}",
                    "name": "bash",
                    "input": {"cmd": "ls -la"},
                },
            ],
        }

    stats: dict = {}
    out, _, _, _, is_looping = _compress_messages_inline(
        FakeProcessor(), msgs, config=_config_stub(protect_window=6, loop_multiplier=3),
        stats=stats,
    )
    assert is_looping is True
    assert stats["loop_detected"] is True
    # Index 5 (age_ratio 1-5/11 ~= 0.55) is old enough to compress and sits
    # inside a plain window=6 (protect_cutoff=6), but the loop-widened
    # window (6*3=18 > n=12) protects the entire conversation instead.
    assert out[5] == msgs[5]


def test_stats_plumbing_keys_present():
    """Passing a stats dict surfaces per-stage counters; omitting it
    (the default) is a no-op, matching prior behavior exactly."""
    msgs = _messages(8)
    stats: dict = {}
    out, before, after, _, is_looping = _compress_messages_inline(
        FakeProcessor(), msgs, stats=stats,
    )
    assert before > after

    # Default protect_window (no config) is 2 for backward compat.
    assert stats["msgs_total"] == 8
    assert stats["protected_recency"] == 2
    assert stats["loop_detected"] is False
    # Age ratio grows toward idx=0 (the oldest message), but idx=0 is the
    # founding message and head-protected. Indices 1-4 (age_ratio >= 0.3)
    # compress to "medium"; index 5, closest to the recent tail, doesn't.
    assert stats["protected_head"] == 1
    assert stats["applied_medium"] == 4
    assert stats["unchanged"] == 1

    # No stats dict passed -> no crash, no behavior change.
    out_nostats, before2, after2, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    assert (before2, after2) == (before, after)
    assert out_nostats == out


def test_record_metrics_skip_reasons_roundtrip(tmp_path):
    """skip_reasons is stored and comes back out through the compression
    summary aggregation added in base.py's _summarize_compression."""
    store = LoomStorage(db_path=str(tmp_path / "skip_reasons.db"))
    store.connect()
    try:
        store.record_metrics(
            request_id="req-skip-1",
            model="claude-x",
            provider="anthropic",
            tokens_in=100,
            tokens_out=50,
            latency_ms=12.3,
            cost=0.001,
            compressed=True,
            compression_ratio=0.5,
            message_count=8,
            source="pytest",
            tokens_saved=50,
            skip_reasons='{"msgs_total":8,"applied_medium":3,"loop_detected":true}',
        )
        row = store.conn.execute(
            "SELECT skip_reasons FROM metrics WHERE request_id = ?",
            ("req-skip-1",),
        ).fetchone()
        assert row["skip_reasons"] == (
            '{"msgs_total":8,"applied_medium":3,"loop_detected":true}'
        )

        summary = store.get_compression_summary(days=30)
        assert summary["skip_reasons"]["msgs_total"] == 8
        assert summary["skip_reasons"]["applied_medium"] == 3
        assert summary["skip_reasons"]["loop_detected"] == 1
    finally:
        store.close()


def test_founding_message_survives_long_session():
    """The first message (task framing) is always the oldest, so it sits at
    age_ratio=1.0 and would be evicted to a status line in any long session.
    It must survive verbatim regardless of session length."""
    msgs = _messages(20)
    stats: dict = {}
    out, *_ = _compress_messages_inline(
        FakeProcessor(), msgs, config=_config_stub(protect_window=6), stats=stats,
    )
    assert out[0] == msgs[0]
    assert out[1] != msgs[1]
    assert stats["protected_head"] == 1


def test_founding_message_survives_real_processor():
    """End-to-end with the real ContentProcessor: a short founding task
    containing status words must not be reduced to a heavy-tier stub."""
    from loom.compression.processor import ContentProcessor

    task = "Investigate the service. Confirm the run finished and was closed."
    msgs = [{"role": "user", "content": task}] + _messages(19)
    out, *_ = _compress_messages_inline(
        ContentProcessor(), msgs, config=_config_stub(protect_window=6),
    )
    assert out[0]["content"] == task


def test_head_protect_window_configurable():
    """head_protect_window=0 restores the old behavior; larger values
    shield more leading messages."""
    msgs = _messages(20)
    cfg = _config_stub(protect_window=6)
    cfg.compression.head_protect_window = 3
    out, *_ = _compress_messages_inline(FakeProcessor(), msgs, config=cfg)
    for idx in range(3):
        assert out[idx] == msgs[idx]
    assert out[3] != msgs[3]

    cfg.compression.head_protect_window = 0
    out0, *_ = _compress_messages_inline(FakeProcessor(), msgs, config=cfg)
    assert out0[0] != msgs[0]


class _HeavyProcessor:
    """Like the real processor's heavy band: age_ratio>=0.7 -> 'heavy'."""

    def compress_graduated(self, text: str, age_ratio: float):
        if age_ratio < 0.7:
            return text, "full"
        return "[stub]", "heavy"


def test_heavy_on_head_counter_when_head_protect_disabled():
    """Message 0 is always the oldest (age_ratio=1.0). With head protection
    disabled it is no longer shielded and lands in the heavy tier -- the
    same founding-message eviction #112 fixed for the default config. The
    heavy_on_head counter must record this so a misconfigured
    head_protect_window is visible on the dashboard rather than only
    surfacing as a failed run."""
    msgs = _messages(20)
    cfg = _config_stub(protect_window=6)
    cfg.compression.head_protect_window = 0
    stats: dict = {}
    out, *_ = _compress_messages_inline(
        _HeavyProcessor(), msgs, config=cfg, stats=stats,
    )
    assert out[0]["content"] != msgs[0]["content"]
    assert stats["heavy_on_head"] == 1


def test_heavy_on_head_counter_absent_when_head_protected():
    """With the default head_protect_window (1), message 0 never reaches
    the heavy tier, so the counter is never incremented."""
    msgs = _messages(20)
    stats: dict = {}
    out, *_ = _compress_messages_inline(
        _HeavyProcessor(), msgs, config=_config_stub(protect_window=6), stats=stats,
    )
    assert out[0] == msgs[0]
    assert stats.get("heavy_on_head", 0) == 0
