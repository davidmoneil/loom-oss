import re

import pytest
import yaml

from loom.observability.tool_actions import (
    KNOWN_STATUSES, RULES_PATH, classify, load_rules,
)

B = "Bash"
CASES = [
    (B, {"command": "git commit -m 'fix: thing'"}, "vcs.commit", "git commit"),
    (B, {"command": "cd /home/x/repo && git status"}, "vcs.read", "git status"),
    (B, {"command": "git -C /tmp/r log --oneline -5"}, "vcs.read", "git log"),
    (B, {"command": "git diff HEAD~1 | head -50"}, "vcs.read", "git diff"),
    (B, {"command": "git push origin feat"}, "vcs.commit", "git push"),
    (B, {"command": "git stash pop"}, "vcs.write", "git"),
    (B, {"command": "gh pr create --title t --body b"}, "vcs.pr", "gh"),
    (B, {"command": "sudo docker exec -it web bash"}, "container.exec", "docker exec"),
    (B, {"command": "docker ps -a"}, "container.read", "docker ps"),
    (B, {"command": "docker logs --tail 20 web"}, "container.read", "docker logs"),
    (B, {"command": "docker restart web"}, "container.manage", "docker"),
    (B, {"command": "docker-compose up -d"}, "container.manage", "docker-compose"),
    (B, {"command": "systemctl restart nginx"}, "container.manage", "systemctl"),
    (B, {"command": "psql -h db -c 'select 1'"}, "db.query", "psql"),
    (B, {"command": "sqlite3 data.db .tables"}, "db.query", "sqlite3"),
    (B, {"command": "curl -s https://example.com/api?token=abc"}, "http.call", "curl"),
    (B, {"command": "wget http://x.y/z"}, "http.call", "wget"),
    (B, {"command": "rg -n pattern src/"}, "search.text", "rg"),
    (B, {"command": "grep -r foo ."}, "search.text", "grep"),
    (B, {"command": "find . -name '*.py'"}, "search.text", "find"),
    (B, {"command": "cat /etc/hosts"}, "file.read", "cat"),
    (B, {"command": "ls -la /tmp"}, "file.read", "ls"),
    (B, {"command": "tail -f /var/log/x.log"}, "file.read", "tail"),
    (B, {"command": "python3 script.py --flag"}, "script.python", "python3"),
    (B, {"command": "FOO=bar python -c 'print(1)'"}, "script.python", "python"),
    (B, {"command": "node build.js"}, "script.node", "node"),
    (B, {"command": "npx tsc --noEmit"}, "script.node", "npx"),
    (B, {"command": "pulse show AIProjects-abc"}, "task.pulse", "pulse"),
    (B, {"command": "nexus-label add x y z"}, "task.pulse", "nexus-label"),
    (B, {"command": "pip install requests"}, "pkg.install", "pip install"),
    (B, {"command": "npm install --save left-pad"}, "pkg.install", "npm install"),
    (B, {"command": "npm test"}, "test.run", "npm test"),
    (B, {"command": "pytest -x tests/"}, "test.run", "pytest"),
    (B, {"command": "timeout 30 pytest tests/test_a.py"}, "test.run", "pytest"),
    (B, {"command": "cd a && cd b && pytest"}, "test.run", "pytest"),
    (B, {"command": "echo hi"}, "bash.other", "echo"),
    (B, {"command": "mkdir -p /tmp/secret=value"}, "bash.other", "mkdir"),
    (B, {"command": "/usr/bin/git status"}, "vcs.read", "git status"),
    (B, {"command": "API_KEY=sekret curl https://x"}, "http.call", "curl"),
    (B, {"command": "nohup python3 serve.py &"}, "script.python", "python3"),
    ("Read", {"file_path": "/home/u/proj/src/main.py"}, "file.read", "code"),
    ("Read", {"file_path": "/home/u/notes.md"}, "file.read", "docs"),
    ("Edit", {"file_path": "/home/u/proj/config.yaml"}, "file.edit", "config"),
    ("Write", {"file_path": "/srv/data/out.csv"}, "file.edit", "data"),
    ("Write", {"file_path": "/srv/Makefile"}, "file.edit", "dir-srv"),
    ("Grep", {"pattern": "foo", "path": "/a/b"}, "search.text", "dir-a"),
    ("Glob", {"pattern": "**/*.py"}, "search.files", "code"),
    ("mcp__github__list_issues", {}, "mcp.github.read", "github read"),
    ("mcp__pulse__create_task", {"title": "x"}, "mcp.pulse.write", "pulse write"),
    ("mcp__x__frobnicate", {}, "mcp.x.call", "x call"),
    ("Agent", {"prompt": "do things"}, "agent", "agent"),
    ("WebFetch", {"url": "https://a.b/c"}, "webfetch", "webfetch"),
    ("Skill", {"skill": "foo"}, "skill", "skill"),
    ("TodoWrite", {}, "todowrite.other", "todowrite"),
]


@pytest.mark.parametrize("tool,inp,label,sig", CASES)
def test_table(tool, inp, label, sig):
    assert classify(tool, inp) == (label, sig)


def test_enough_cases():
    assert len(CASES) >= 40


MALFORMED = [
    (B, None), (B, {}), (B, {"command": ""}), (B, {"command": None}),
    (B, {"command": 42}), (B, {"command": "echo 'unterminated"}),
    (B, {"command": "&& ||"}), (B, {"command": "   "}), (B, "str"),
    (B, {"command": "cd /x"}), ("Read", None), ("Read", {"file_path": 5}),
    ("mcp__", {}), ("mcp__a", {}), (None, None), ("", {}), (123, [1]),
]


@pytest.mark.parametrize("tool,inp", MALFORMED)
def test_malformed_never_raises(tool, inp):
    label, sig = classify(tool, inp)
    assert isinstance(label, str) and isinstance(sig, str) and label
    if tool == B:
        assert label == "bash.other"


def test_signature_hygiene():
    secrets = ["sekret", "token=abc", "/home/x", "secret=value", "fix: thing"]
    for tool, inp, _l, _s in CASES:
        label, sig = classify(tool, inp)
        for s in (label, sig):
            assert len(sig) <= 40
            assert not re.search(r"[/='\"]", s), (tool, inp, s)
            assert re.fullmatch(r"[a-z0-9._ -]*", sig)
            for sec in secrets:
                assert sec not in s


def test_signature_length_capped():
    _, sig = classify(B, {"command": "a" * 200 + " arg"})
    assert len(sig) <= 40


def test_rule_file_schema():
    rules = load_rules(RULES_PATH)
    assert rules
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))
    for r in rules:
        assert r["status"] in KNOWN_STATUSES
        assert r["label"] and r["match"]["argv0"]
    assert yaml.safe_load(RULES_PATH.read_text())["version"] == 1


def test_shadow_rule_does_not_apply():
    rules = ([{"id": "x", "label": "custom.git", "status": "shadow", "match": {"argv0": ["git"]}}], {"git": frozenset()})
    assert classify(B, {"command": "git status"}, rules=rules)[0] == "bash.other"
