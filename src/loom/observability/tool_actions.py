"""Classify tool calls into content-free action labels.

``classify(tool_name, input_dict) -> (label, signature)``. Neither value ever
contains file paths, argument values, quotes or ``=``: loom stays content-free.
Bash rules are data (``loom/rules/default_actions.yaml``); other tools are
mapped by family in code. Never raises.
"""

from __future__ import annotations

import os
import re
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "default_actions.yaml"
KNOWN_STATUSES = ("active", "shadow")
MAX_SIG = 40

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WORD = re.compile(r"^[a-z][a-z0-9._-]*$")
_SEPARATORS = {"&&", "||", ";", "|", "&", ";;", "|&"}
_WRAPPERS = {"sudo", "nohup", "time", "command", "exec", "nice", "env", "stdbuf", "doas"}
_GIT_OPT_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

_EXT_GROUPS = {
    "code": {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "h", "cpp", "rb", "sh", "css", "html", "sql"},
    "docs": {"md", "txt", "rst", "pdf", "adoc"},
    "config": {"yaml", "yml", "json", "toml", "ini", "cfg", "conf", "env", "lock"},
    "data": {"csv", "jsonl", "parquet", "db", "sqlite", "log", "xml"},
}
_READ_VERBS = ("read", "list", "search", "get", "query", "fetch", "find", "describe", "show", "view", "check", "status")
_WRITE_VERBS = ("create", "update", "write", "delete", "remove", "set", "add", "edit", "send", "post", "put", "patch", "run", "exec")
_FAMILY = {
    "Read": "file.read", "NotebookRead": "file.read",
    "Edit": "file.edit", "Write": "file.edit", "MultiEdit": "file.edit", "NotebookEdit": "file.edit",
    "Grep": "search.text", "Glob": "search.files",
}
_PATH_KEYS = ("file_path", "path", "notebook_path", "pattern")


def sanitize(text: Any, max_len: int = MAX_SIG) -> str:
    """Lowercase; keep only [a-z0-9._-] and single spaces; cap length."""
    s = str(text).lower()
    s = re.sub(r"[^a-z0-9._ -]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].strip()


def load_rules(path: Path | str = RULES_PATH) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return list(data.get("rules", []))


@lru_cache(maxsize=1)
def _default_rules() -> tuple[tuple[dict, ...], dict[str, frozenset]]:
    try:
        rules = load_rules()
    except Exception:  # missing/corrupt rule file must not break callers
        rules = []
    subs: dict[str, set] = {}
    for r in rules:
        m = r.get("match") or {}
        for a in m.get("argv0", []):
            subs.setdefault(a, set()).update(m.get("sub", []))
    return tuple(rules), {k: frozenset(v) for k, v in subs.items()}


def _first_command(command: str) -> list[str] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    toks = list(lex)
    segments: list[list[str]] = [[]]
    for t in toks:
        if t in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(t)
    for seg in segments:
        argv = _strip_prefix(seg)
        if not argv:
            continue
        if argv[0] in ("cd", "pushd", "export", "set", "source", ".", "true", ":"):
            continue
        return argv
    return None


def _strip_prefix(argv: list[str]) -> list[str]:
    argv = list(argv)
    while argv:
        head = argv[0]
        if _ENV_ASSIGN.match(head):
            argv.pop(0)
        elif head in _WRAPPERS:
            argv.pop(0)
            while argv and (argv[0].startswith("-") or _ENV_ASSIGN.match(argv[0])):
                argv.pop(0)
        elif head == "timeout":
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                argv.pop(0)
            if argv and re.match(r"^\d+[smhd]?$", argv[0]):
                argv.pop(0)
        else:
            break
    return argv


def _subcommand(argv0: str, rest: list[str]) -> str | None:
    i = 0
    while i < len(rest):
        t = rest[i]
        if argv0 == "git" and t in _GIT_OPT_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t if _WORD.match(t) else None
    return None


def _match(rule: dict, argv0: str, sub: str | None) -> bool:
    m = rule.get("match") or {}
    if argv0 not in m.get("argv0", []):
        return False
    subs = m.get("sub")
    return not subs or sub in subs


def _classify_bash(inp: dict, rules=None) -> tuple[str, str]:
    command = inp.get("command")
    if not isinstance(command, str) or not command.strip():
        return "bash.other", "bash"
    try:
        argv = _first_command(command)
    except ValueError:
        return "bash.other", "bash"
    if not argv:
        return "bash.other", "bash"
    argv0 = os.path.basename(argv[0])
    sub = _subcommand(argv0, argv[1:])
    rule_list, known_subs = rules if rules is not None else _default_rules()
    sig_sub = sub if sub and sub in known_subs.get(argv0, ()) else None
    signature = sanitize(f"{argv0} {sig_sub}" if sig_sub else argv0)
    if not signature:
        return "bash.other", "bash"
    for r in rule_list:
        if r.get("status", "active") == "active" and _match(r, argv0, sub):
            return r["label"], signature
    return "bash.other", signature


def _path_class(inp: dict) -> str:
    raw = next((inp[k] for k in _PATH_KEYS if isinstance(inp.get(k), str) and inp[k]), None)
    if raw is None:
        return "unknown"
    ext = os.path.splitext(raw.rstrip("/"))[1].lstrip(".").lower()
    for group, exts in _EXT_GROUPS.items():
        if ext in exts:
            return group
    parts = [p for p in raw.split("/") if p and p not in (".", "..", "~")]
    return sanitize("dir-" + parts[0], MAX_SIG) if parts else "unknown"


def _mcp(tool_name: str) -> tuple[str, str]:
    parts = tool_name.split("__", 2)
    server = sanitize(parts[1]) if len(parts) > 1 else "unknown"
    tool = parts[2].lower() if len(parts) > 2 else ""
    words = [w for w in re.split(r"[_\-]+", tool) if w]
    verb = "call"
    for w in words:
        if w in _WRITE_VERBS:
            verb = "write"
            break
        if w in _READ_VERBS:
            verb = "read"
            break
    return f"mcp.{server or 'unknown'}.{verb}", sanitize(f"{server} {verb}")


def classify(tool_name: Any, input_dict: Any = None, *, rules=None) -> tuple[str, str]:
    """Return (label, signature). Never raises."""
    try:
        name = tool_name if isinstance(tool_name, str) and tool_name else "unknown"
        inp = input_dict if isinstance(input_dict, dict) else {}
        if name == "Bash":
            return _classify_bash(inp, rules)
        if name.startswith("mcp__"):
            return _mcp(name)
        if name in _FAMILY:
            return _FAMILY[name], _path_class(inp)
        fam = sanitize(name) or "unknown"
        return f"{fam}.other" if name not in ("Agent", "Skill", "WebFetch", "WebSearch", "Task") else fam, fam
    except Exception:
        fam = sanitize(tool_name) if isinstance(tool_name, str) else ""
        fam = fam or "unknown"
        return f"{fam}.other", fam
