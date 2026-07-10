import pytest
from fastapi.testclient import TestClient


# --- Gateway smoke tests ---

def test_health():
    from loom.gateway.app import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")


def test_models_endpoint():
    from loom.gateway.app import app
    client = TestClient(app)
    resp = client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    # Endpoint may return OpenAI-style {"object": "list", "data": [...]} or a bare list
    assert isinstance(data, (list, dict))


def test_config_endpoint():
    from loom.gateway.app import app
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 200


# --- Content type detection ---

def test_detect_log_output():
    from loom.compression.processor import ContentProcessor
    proc = ContentProcessor()
    log_content = "\n".join([
        "2024-01-15T10:00:00Z INFO Starting service",
        "2024-01-15T10:00:01Z INFO Loaded config",
        "2024-01-15T10:00:02Z WARN Slow query detected",
        "2024-01-15T10:00:03Z ERROR Connection refused",
        "2024-01-15T10:00:04Z INFO Retrying...",
    ])
    assert proc.detect_content_type(log_content) == "log_output"


def test_detect_code():
    from loom.compression.processor import ContentProcessor
    proc = ContentProcessor()
    code = '''
import os

class MyClass:
    def method(self):
        pass

    def other(self, x: int) -> str:
        return str(x)
'''
    assert proc.detect_content_type(code) == "code"


def test_compress_graduated_full():
    from loom.compression.processor import ContentProcessor
    proc = ContentProcessor()
    text = "This is recent content"
    result, tier = proc.compress_graduated(text, age_ratio=0.1)
    assert tier == "full"
    assert result == text


def test_compress_graduated_light():
    from loom.compression.processor import ContentProcessor
    proc = ContentProcessor()
    text = (
        "It is important to note that the system is basically working. "
        "Actually, the performance is really quite good."
    )
    result, tier = proc.compress_graduated(text, age_ratio=0.4)
    assert tier == "light"
    assert len(result) < len(text)


# --- Routing models ---

def test_routing_recommendation_to_dict():
    from loom.routing.models import RoutingRecommendation
    rec = RoutingRecommendation(
        model="claude-sonnet-4",
        temperature=0.0,
        seed_strategy="none",
        constraint_level_min=1,
        expected_determinism=0.95,
        confidence_interval=(0.90, 0.98),
        provider="anthropic",
        routing_reason="eqrt_standard",
    )
    d = rec.to_dict()
    assert d["model"] == "claude-sonnet-4"
    assert d["routing_reason"] == "eqrt_standard"


# --- Provider registry ---

def test_provider_registry_default():
    from loom.routing.providers import ProviderRegistry
    reg = ProviderRegistry.default()
    assert reg.resolve("sonnet") is not None
    assert reg.resolve("haiku") is not None


def test_provider_registry_cost():
    from loom.routing.providers import ProviderRegistry
    reg = ProviderRegistry.default()
    cost = reg.get_cost("haiku")
    assert cost is not None
    assert cost > 0


# --- Detection ---

def test_detection_simple():
    from loom.detection.engine import DetectionEngine, extract_features
    features = extract_features("Hello, summarize this text for me please.", 1)
    assert features.token_estimate > 0
    assert features.complexity_score < 50


def test_task_classification():
    from loom.detection.engine import classify_task_type
    assert classify_task_type([{"role": "user", "content": "summarize this document"}]) == "summarization"
    assert classify_task_type([{"role": "user", "content": "write a function to sort"}]) == "code_generation"
    assert classify_task_type([{"role": "user", "content": "hello"}]) == "general"


# --- Entropy detection ---

def test_high_entropy():
    from loom.compression.processor import _is_high_entropy
    assert _is_high_entropy("550e8400-e29b-41d4-a716-446655440000")  # UUID
    assert not _is_high_entropy("hello world")
    assert not _is_high_entropy("short")


def test_anthropic_auth_header_style():
    """OAuth tokens use Authorization: Bearer; API keys use x-api-key."""
    from loom.gateway.providers.anthropic import AnthropicBackend

    backend = AnthropicBackend("https://api.anthropic.com")
    oauth = backend._headers("sk-ant-oat01-abc123")
    assert oauth["Authorization"] == "Bearer sk-ant-oat01-abc123"
    assert "x-api-key" not in oauth
    assert oauth["anthropic-beta"] == "oauth-2025-04-20"

    key = backend._headers("sk-ant-api03-xyz")
    assert key["x-api-key"] == "sk-ant-api03-xyz"
    assert "Authorization" not in key


# --- Session fingerprinting ---

def test_extract_session_signals_basic():
    """_extract_session_signals returns a valid session_id and metadata."""
    from loom.gateway.app import _extract_session_signals

    messages = [{"role": "user", "content": "Hello world"}]
    signals = _extract_session_signals(messages, "default")
    assert signals["session_id"].startswith("gw-")
    assert len(signals["session_id"]) == 3 + 16  # 'gw-' + 16 hex chars
    assert signals["client_type"] == "api"  # no headers
    assert signals["user_id"] == ""
    assert signals["api_key_suffix"] == ""
    assert signals["system_hash"] == ""


def test_extract_session_signals_different_keys():
    """Different API keys produce different session_ids."""
    from loom.gateway.app import _extract_session_signals

    msgs = [{"role": "user", "content": "fix the build"}]
    s1 = _extract_session_signals(
        msgs, "default",
        headers={"x-api-key": "sk-ant-api03-AAAAAAAAbbbbcccc"},
    )
    s2 = _extract_session_signals(
        msgs, "default",
        headers={"x-api-key": "sk-ant-api03-XXXXXXXXyyyyzzzz"},
    )
    assert s1["session_id"] != s2["session_id"]
    assert s1["api_key_suffix"] == "bbbbcccc"
    assert s2["api_key_suffix"] == "yyyyzzzz"


def test_extract_session_signals_metadata_user_id():
    """metadata.user_id is used in the fingerprint and stored."""
    from loom.gateway.app import _extract_session_signals

    msgs = [{"role": "user", "content": "fix the build"}]
    s1 = _extract_session_signals(
        msgs, "default",
        body={"metadata": {"user_id": "alice@corp.com"}},
    )
    s2 = _extract_session_signals(
        msgs, "default",
        body={"metadata": {"user_id": "bob@corp.com"}},
    )
    assert s1["session_id"] != s2["session_id"]
    assert s1["user_id"] == "alice@corp.com"
    assert s2["user_id"] == "bob@corp.com"


def test_extract_session_signals_system_hash():
    """Different system prompts produce different session_ids."""
    from loom.gateway.app import _extract_session_signals

    msgs = [{"role": "user", "content": "fix the build"}]
    s1 = _extract_session_signals(
        msgs, "default",
        body={"system": "You are helping with project A."},
    )
    s2 = _extract_session_signals(
        msgs, "default",
        body={"system": "You are helping with project B."},
    )
    assert s1["session_id"] != s2["session_id"]
    assert s1["system_hash"] != s2["system_hash"]
    assert len(s1["system_hash"]) == 12


def test_extract_session_signals_claude_code_detection():
    """Claude Code is detected from beta header."""
    from loom.gateway.app import _extract_session_signals

    msgs = [{"role": "user", "content": "hello"}]
    signals = _extract_session_signals(
        msgs, "default",
        headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    assert signals["client_type"] == "claude-code"


def test_extract_session_signals_no_user_message():
    """Returns 'unknown' when there is no user message."""
    from loom.gateway.app import _extract_session_signals

    signals = _extract_session_signals(
        [{"role": "assistant", "content": "hi"}], "default",
    )
    assert signals["session_id"] == "unknown"


def test_derive_session_id_legacy_compat():
    """derive_session_id still returns a string (backward compat)."""
    from loom.gateway.app import derive_session_id

    sid = derive_session_id(
        [{"role": "user", "content": "hello"}], "default",
    )
    assert isinstance(sid, str)
    assert sid.startswith("gw-")
