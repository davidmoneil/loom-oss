"""Heavy-tier stubs must keep context, never a bare status keyword (#122)."""

from loom.compression.processor import ContentProcessor
from loom.config import LoomConfig


def _proc() -> ContentProcessor:
    return ContentProcessor(LoomConfig())


def _git_sweep() -> str:
    lines = ["480c121 Merge pull request #120 from x/fix/build-start-check"]
    lines += [f"{i:04d} feat: change {i} landed cleanly" for i in range(120)]
    lines.append("1 test failed: test_flaky_network (retried, passed)")
    return "\n".join(lines)


def test_status_signals_are_whole_lines_not_keywords():
    signals = _proc()._extract_status_signals(_git_sweep())
    assert "1 test failed: test_flaky_network (retried, passed)" in signals
    assert all(len(s.split()) > 1 for s in signals)


def test_heavy_stub_keeps_head_preview_and_eviction_marker():
    content = _git_sweep()
    text, tier = _proc().compress_graduated(content, 0.95)
    assert tier == "heavy"
    assert text.startswith("480c121 Merge pull request #120")
    assert "tokens evicted by loom compression" in text
    assert text.strip() not in ("[Status: fail]", "[Status: done]")
    assert len(text) < len(content)


def test_long_status_line_is_windowed():
    content = ("x " * 400) + "exit code: 1 " + ("y " * 400)
    signals = _proc()._extract_status_signals(content)
    assert any("exit code: 1" in s and len(s) <= 170 for s in signals)
