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
