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
    out, before, after = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after > 0
    # Last 2 messages untouched
    assert out[-1] == msgs[-1] and out[-2] == msgs[-2]
    # An old message (age_ratio >= 0.3) got compressed and tagged
    _, tier = _strip_loom_tag(out[4]["content"])
    assert tier == "medium"


def test_tagged_messages_not_recompressed():
    msgs = _messages(8)
    out1, _, _ = _compress_messages_inline(FakeProcessor(), msgs)
    # Second pass over the already-compressed conversation
    out2, before2, after2 = _compress_messages_inline(FakeProcessor(), out1)
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
    out1, _, _ = _compress_messages_inline(FakeProcessor(), msgs, store)

    class ExplodingProcessor:
        def compress_graduated(self, text, age_ratio):
            raise AssertionError("should have hit the cache")

    # Same original messages -> cache supplies the compressed text.
    out2, _, _ = _compress_messages_inline(ExplodingProcessor(), _messages(8), store)
    assert [m["content"] for m in out2] == [m["content"] for m in out1]
    store.close()


def test_short_conversations_untouched():
    msgs = _messages(2)
    out, before, after = _compress_messages_inline(FakeProcessor(), msgs)
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
    msgs[0]["content"] = [{"type": "text", "text": FILLER}]
    out, before, after = _compress_messages_inline(FakeProcessor(), msgs)
    assert before > after
    assert derive_session_id(msgs, "pytest").startswith("gw-")
