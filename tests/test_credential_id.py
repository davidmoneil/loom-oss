"""Per-credential identity on rate-limit rows (AIProjects-bjmx)."""

import hashlib

from loom.gateway.providers.anthropic import _credential_id_from_headers
from loom.storage.sqlite import LoomStorage


def _fp(v):
    return hashlib.sha256(v.encode()).hexdigest()[:8]


def test_fingerprint_is_hash_prefix_not_value():
    api = _credential_id_from_headers({"x-api-key": "sk-ant-api03-AAA"})
    oauth = _credential_id_from_headers({"Authorization": "Bearer sk-ant-oat01-BBB"})
    assert api == _fp("sk-ant-api03-AAA")
    assert oauth == _fp("sk-ant-oat01-BBB")
    assert "AAA" not in api and len(api) == 8
    assert _credential_id_from_headers({"content-type": "x"}) is None


def test_storage_filters_and_lists_credentials(tmp_path):
    st = LoomStorage(str(tmp_path / "l.db"))
    for i, cred in enumerate(["aaaa1111", "bbbb2222", "aaaa1111"]):
        st.record_rate_limits(
            f"r{i}", "anthropic", ratelimit={"ratelimit_unified_5h_utilization": 0.1 * (i + 1)},
            auth_type="oauth", credential_id=cred,
        )
    creds = st.get_rate_limit_credentials("anthropic")
    assert {c["credential_id"] for c in creds} == {"aaaa1111", "bbbb2222"}
    cur = st.get_rate_limit_current("anthropic", "oauth", credential_id="bbbb2222")
    assert cur["credential_id"] == "bbbb2222"
    assert st.get_rate_limit_current("anthropic", credential_id="zzzz") is None
    trend = st.get_rate_limit_trend(48, "anthropic", "oauth", credential_id="aaaa1111")
    assert sum(t["samples"] for t in trend) == 2
