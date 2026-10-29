"""Content log records the system prompt and request settings (#161)."""

import json

from loom.gateway.app import _request_params
from loom.observability.logger import AuditLogger

MSGS = [{"role": "user", "content": "hi"}]
PARAMS = {"tools": ["Bash"], "max_tokens": 100}


class _Scanner:
    """Stand-in DLP scanner: masks the word SECRET."""

    def apply(self, text, source=None, provider=None):
        return text.replace("SECRET", "[REDACTED]"), []

    def sanitize_log_entry(self, entry):
        return json.loads(json.dumps(entry).replace("SECRET", "[REDACTED]"))


def _logger(tmp_path, scanner=None):
    lg = AuditLogger(
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        content_path=str(tmp_path / "content.jsonl"),
    )
    lg._scanner = scanner
    return lg


def _records(tmp_path):
    path = tmp_path / "content.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_full_logs_system_and_params(tmp_path):
    lg = _logger(tmp_path)
    lg.log_content("r1", "m", "s", "p", messages=MSGS, content_logging="full",
                   system="Reply in JSON.", request_params=PARAMS)
    rec = _records(tmp_path)[0]
    assert rec["system"] == "Reply in JSON."
    assert rec["request_params"] == PARAMS


def test_after_scan_scans_system_blocks(tmp_path):
    lg = _logger(tmp_path, _Scanner())
    system = [{"type": "text", "text": "key SECRET here"}, {"type": "text", "text": "plain"}]
    lg.log_content("r1", "m", "s", "p", messages=MSGS, content_logging="after_scan", system=system)
    rec = _records(tmp_path)[0]
    assert rec["system"][0]["text"] == "key [REDACTED] here"
    assert rec["system"][1]["text"] == "plain"


def test_after_scan_scans_system_string(tmp_path):
    lg = _logger(tmp_path, _Scanner())
    lg.log_content("r1", "m", "s", "p", messages=MSGS, content_logging="after_scan", system="SECRET")
    assert _records(tmp_path)[0]["system"] == "[REDACTED]"


def test_off_and_metadata_write_nothing(tmp_path):
    lg = _logger(tmp_path)
    for level in ("off", "metadata"):
        lg.log_content("r1", "m", "s", "p", messages=MSGS, content_logging=level,
                       system="x", request_params=PARAMS)
    assert _records(tmp_path) == []


def test_request_params_tool_names_only():
    body = {"max_tokens": 50, "temperature": 0, "messages": [],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                      {"type": "function", "function": {"name": "lookup"}}]}
    assert _request_params(body) == {"max_tokens": 50, "temperature": 0, "tools": ["Bash", "lookup"]}
    assert _request_params({"messages": []}) is None
