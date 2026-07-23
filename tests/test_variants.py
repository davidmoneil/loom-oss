"""Variant store: original preservation, relevance scoring, null fallback."""

import hashlib

from loom.compression.variants import NullVariantStore, create_variant_store
from loom.config import CompressionConfig
from loom.gateway.app import (
    _compress_messages_inline,
    _score_messages_by_relevance,
    _strip_loom_tag,
)

FILLER = (
    "So basically what happened is that the deployment process, you know, "
    "actually completed successfully and everything worked fine in the end. "
) * 20


def _messages(n: int) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}: {FILLER}",
        }
        for i in range(n)
    ]


class FakeProcessor:
    def compress_graduated(self, text: str, age_ratio: float):
        if age_ratio < 0.3:
            return text, "full"
        return text[: len(text) // 2], "medium"


class MemoryVariantStore:
    """In-memory stand-in matching the Neo4jVariantStore interface."""

    enabled = True

    def __init__(self, indexed: set[str] | None = None):
        self.variants: dict[str, dict] = {}
        self.indexed = indexed or set()

    def put_variant(self, content_hash, original_text, compressed_text,
                    tier, tokens_before, tokens_after, source_hint=""):
        self.variants[content_hash] = {
            "original": original_text,
            "compressed": compressed_text,
            "tier": tier,
        }

    def get_original(self, content_hash):
        v = self.variants.get(content_hash)
        return v["original"] if v else None

    def is_indexed(self, content_hash):
        return content_hash in self.indexed

    def close(self):
        pass


def _rel_hash(text: str) -> str:
    return hashlib.sha256(text[:512].encode()).hexdigest()[:16]


def test_originals_preserved_and_resolvable():
    store = MemoryVariantStore()
    msgs = _messages(8)
    out, _, _, _, _ = _compress_messages_inline(
        FakeProcessor(), msgs, variants=store
    )
    # A compressed message's pointer tag resolves back to its original.
    body = out[4]["content"]
    _, tier = _strip_loom_tag(body)
    assert tier == "medium"
    pointer = body.rsplit("<!--loom:compressed:medium:", 1)[1].rstrip("->")
    assert store.get_original(pointer) == msgs[4]["content"]


def test_relevance_skips_indexed_content():
    msgs = _messages(8)
    idx_hash = _rel_hash(msgs[4]["content"])
    store = MemoryVariantStore(indexed={idx_hash})

    out, _, _, _, _ = _compress_messages_inline(
        FakeProcessor(), msgs, variants=store
    )
    # Indexed message: age 4/7 = 0.57 -> discounted to 0.32... still >= 0.3?
    # 0.571 - 0.25 = 0.321 -> compressed but less aggressively is not
    # observable with the fake processor's binary tiers, so instead verify
    # scoring directly and that an earlier indexed message (age 0.43) is
    # pushed below the 0.3 threshold and left uncompressed.
    scores = _score_messages_by_relevance(msgs, store)
    assert scores.get(4) == 0.85

    idx3 = _rel_hash(msgs[3]["content"])
    store2 = MemoryVariantStore(indexed={idx3})
    out2, _, _, _, _ = _compress_messages_inline(
        FakeProcessor(), msgs, variants=store2
    )
    # age 3/7 = 0.43 -> 0.18 after discount -> below 0.3 -> untouched
    assert out2[3]["content"] == msgs[3]["content"]
    # Non-indexed neighbor at the same age band still compresses.
    _, tier = _strip_loom_tag(out2[4]["content"])
    assert tier == "medium"


def test_no_store_means_no_scores():
    assert _score_messages_by_relevance(_messages(4), None) == {}
    assert _score_messages_by_relevance(_messages(4), NullVariantStore()) == {}


def test_create_variant_store_fallbacks():
    # Unset backend -> null store.
    assert isinstance(create_variant_store(CompressionConfig()), NullVariantStore)
    # neo4j backend without URI -> null store.
    cfg = CompressionConfig(variant_store="neo4j")
    assert isinstance(create_variant_store(cfg), NullVariantStore)
    # neo4j backend with unreachable URI -> null store, no raise.
    cfg = CompressionConfig(
        variant_store="neo4j", neo4j_uri="bolt://127.0.0.1:1"
    )
    assert isinstance(create_variant_store(cfg), NullVariantStore)


def test_store_errors_never_break_compression():
    class ExplodingStore(MemoryVariantStore):
        def put_variant(self, *a, **kw):
            raise RuntimeError("graph down")

        def is_indexed(self, content_hash):
            raise RuntimeError("graph down")

    msgs = _messages(8)
    out, before, after, _, _loop = _compress_messages_inline(
        FakeProcessor(), msgs, variants=ExplodingStore()
    )
    assert before > after  # compression still happened
