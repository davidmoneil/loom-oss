"""Size-based rotation for AuditLogger JSONL files."""

import json
import os

from loom.observability.logger import AuditLogger


def _logger(tmp_path, **kw):
    return AuditLogger(
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        content_path=str(tmp_path / "content.jsonl"),
        **kw,
    )


def _content(lg, i):
    lg.log_content(
        f"r{i}", "m", "s", "p",
        messages=[{"role": "user", "content": "x" * 200}],
        content_logging="full",
    )


def _metrics(lg, i):
    lg.log_metrics(f"r{i}", "m", 1, 1, 1.0, 0.0)


def test_rotates_and_bounds_backups(tmp_path):
    lg = _logger(tmp_path, max_bytes=1000, backup_count=2)
    for i in range(60):
        _content(lg, i)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["content.jsonl", "content.jsonl.1", "content.jsonl.2"]
    assert all(os.path.getsize(tmp_path / f) < 2000 for f in files)


def test_disabled_when_max_bytes_zero(tmp_path):
    lg = _logger(tmp_path, max_bytes=0)
    for i in range(20):
        _content(lg, i)
    assert [p.name for p in tmp_path.iterdir()] == ["content.jsonl"]


def test_zero_backups_discards_old(tmp_path):
    lg = _logger(tmp_path, max_bytes=1000, backup_count=0)
    for i in range(30):
        _content(lg, i)
    assert [p.name for p in tmp_path.iterdir()] == ["content.jsonl"]


def test_content_has_its_own_limits(tmp_path):
    lg = _logger(tmp_path, max_bytes=0, content_max_bytes=1000, content_backup_count=1)
    for i in range(60):
        _content(lg, i)
        _metrics(lg, i)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["content.jsonl", "content.jsonl.1", "metrics.jsonl"]


def test_content_paths_newest_first_and_findable(tmp_path):
    lg = _logger(tmp_path, max_bytes=1000, backup_count=3)
    for i in range(12):
        _content(lg, i)
    paths = lg.content_paths()
    assert paths[0] == str(tmp_path / "content.jsonl")
    assert paths[1:] == [str(tmp_path / f"content.jsonl.{n}") for n in range(1, len(paths))]
    ids = set()
    for p in paths:
        with open(p) as f:
            ids |= {json.loads(line)["request_id"] for line in f}
    assert ids == {f"r{i}" for i in range(12)}


def test_unrelated_suffix_never_rotated(tmp_path):
    keep = tmp_path / "content.jsonl.pre-4bp2"
    keep.write_text("history\n")
    lg = _logger(tmp_path, max_bytes=1000, backup_count=1)
    for i in range(60):
        _content(lg, i)
    assert keep.read_text() == "history\n"
    assert str(keep) not in lg.content_paths()


def test_rotation_failure_does_not_raise(tmp_path, monkeypatch):
    lg = _logger(tmp_path, max_bytes=1000, backup_count=2)
    for i in range(10):
        _content(lg, i)

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(os, "replace", boom)
    _content(lg, 99)  # must not raise; line still appended
    assert "r99" in (tmp_path / "content.jsonl").read_text()


def test_content_endpoint_reads_rotated_backup(tmp_path):
    from fastapi.testclient import TestClient

    from loom.gateway.app import app

    lg = _logger(tmp_path, max_bytes=1000, backup_count=3)
    for i in range(12):
        _content(lg, i)
    assert "r0" not in (tmp_path / "content.jsonl").read_text()
    with TestClient(app) as client:
        gw = app.state.gateway
        saved = gw.audit
        gw.audit = lg
        try:
            old = client.get("/api/audit/r0/content")
            new = client.get("/api/audit/r11/content")
            missing = client.get("/api/audit/nope/content")
        finally:
            gw.audit = saved
    assert old.status_code == 200 and old.json()["request_id"] == "r0"
    assert new.status_code == 200 and new.json()["request_id"] == "r11"
    assert missing.status_code == 404
