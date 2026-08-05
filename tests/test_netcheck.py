"""Outbound-URL validation policy (SSRF guard for compression.llm_url)."""

import pytest

from loom.netcheck import UnsafeURLError, validate_outbound_url


def test_loopback_always_allowed():
    validate_outbound_url("http://localhost:11434")
    validate_outbound_url("http://127.0.0.1:11434")


def test_bad_scheme_rejected():
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("file:///etc/passwd")
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("gopher://example.com")


def test_private_rejected_by_default_allowed_with_opt_in():
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("http://192.168.1.50:11434")
    validate_outbound_url("http://192.168.1.50:11434", allow_private=True)


def test_metadata_endpoint_always_rejected():
    # Link-local (cloud metadata) is blocked even with the private opt-in.
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeURLError):
        validate_outbound_url(
            "http://169.254.169.254/latest/meta-data/", allow_private=True
        )


def test_no_host_rejected():
    with pytest.raises(UnsafeURLError):
        validate_outbound_url("http://")
