"""LLM prose compression: opt-in, endpoint formats, extractive fallback."""

import json
from unittest import mock

import pytest

from loom.compression.processor import ContentProcessor
from loom.config import CompressionConfig, LoomConfig

PROSE = (
    "The deployment finished after several retries and the team decided to "
    "keep the new configuration. There were some concerns about latency.\n\n"
    "After reviewing the metrics we concluded that the p99 stayed under "
    "200ms. The rollback plan was therefore not needed at this time.\n\n"
    "Next steps include updating the runbook and closing task 42. Exit "
    "code 0 was reported by the final verification step of the pipeline."
) * 3


def _cfg(**kw) -> LoomConfig:
    return LoomConfig(compression=CompressionConfig(**kw))


class _FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_disabled_by_default_uses_extractive():
    proc = ContentProcessor(_cfg())
    with mock.patch("httpx.post") as m:
        out = proc._compress_prose(PROSE)
        m.assert_not_called()
    # Extractive: first sentence of each paragraph survives.
    assert "The deployment finished" in out
    assert len(out) < len(PROSE)


def test_enabled_uses_ollama_format():
    proc = ContentProcessor(_cfg(llm_prose=True))
    summary = "Deployment kept; p99 <200ms; no rollback; task 42 closed; exit 0."
    with mock.patch(
        "httpx.post", return_value=_FakeResponse({"response": summary})
    ) as m:
        out = proc._compress_prose(PROSE)
    assert out == summary
    url = m.call_args[0][0]
    assert url.endswith("/api/generate")
    body = m.call_args[1]["json"]
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False


def test_enabled_openai_compatible_url():
    proc = ContentProcessor(_cfg(llm_prose=True, llm_url="http://localhost:4001/v1"))
    summary = "Short summary."
    with mock.patch(
        "httpx.post",
        return_value=_FakeResponse({"choices": [{"message": {"content": summary}}]}),
    ) as m:
        out = proc._compress_prose(PROSE)
    assert out == summary
    assert m.call_args[0][0].endswith("/v1/chat/completions")


@pytest.mark.parametrize(
    "failure",
    [
        mock.Mock(side_effect=OSError("connection refused")),
        mock.Mock(return_value=_FakeResponse({"response": ""})),
        mock.Mock(return_value=_FakeResponse({"response": PROSE * 2})),
    ],
    ids=["endpoint-down", "empty-response", "output-not-smaller"],
)
def test_llm_failure_falls_back_to_extractive(failure):
    proc = ContentProcessor(_cfg(llm_prose=True))
    with mock.patch("httpx.post", failure):
        out = proc._compress_prose(PROSE)
    assert "The deployment finished" in out
    assert len(out) < len(PROSE)


def test_graduated_medium_band_routes_prose_through_llm():
    """compress_graduated at medium age uses the LLM path when enabled."""
    proc = ContentProcessor(_cfg(llm_prose=True))
    summary = "All good; exit 0."
    with mock.patch(
        "httpx.post", return_value=_FakeResponse({"response": summary})
    ):
        text, tier = proc.compress_graduated(PROSE, age_ratio=0.6)
    assert tier == "medium"
    assert summary in text  # status-signal block may be appended


def test_private_llm_url_rejected_without_opt_in():
    """A non-loopback private llm_url falls back to extractive compression."""
    proc = ContentProcessor(_cfg(llm_prose=True, llm_url="http://192.168.1.50:11434"))
    with mock.patch("httpx.post") as m:
        out = proc._compress_prose(PROSE)
        m.assert_not_called()  # rejected before any HTTP request
    assert "The deployment finished" in out


def test_private_llm_url_allowed_with_opt_in():
    proc = ContentProcessor(
        _cfg(
            llm_prose=True,
            llm_url="http://192.168.1.50:11434",
            allow_private_llm_url=True,
        )
    )
    summary = "LAN Ollama summary."
    with mock.patch(
        "httpx.post", return_value=_FakeResponse({"response": summary})
    ):
        out = proc._compress_prose(PROSE)
    assert out == summary


def test_no_config_object_is_safe():
    proc = ContentProcessor()  # no config at all
    out = proc._compress_prose(PROSE)
    assert len(out) < len(PROSE)
