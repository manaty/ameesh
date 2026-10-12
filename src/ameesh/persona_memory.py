# SPDX-License-Identifier: AGPL-3.0-only
"""Neutral persona memory component (L54, decision 0029).

Memory lives in a persona Git repository, outside the mesh database. The
harness reads MEMORY.md and memories/<topic>.md, then appends its own journal.
This component designates files and transports commits; the persona's own
session performs consolidation. No paid model calls or native memory store."""
from __future__ import annotations

import os
import re
import shutil
import shlex
import subprocess
import time

INDEX = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 8000
GIT_TIMEOUT = 120

SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
        r"(?i)\b(pass(word)?|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}",
    )
]


class MemoryError(RuntimeError):
    pass


def copy_dir(state_dir: str, agent: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}", agent):
        raise MemoryError("unsafe persona name")
    return os.path.join(state_dir, "memory", agent)


def _git_env(cfg_raw: dict | None) -> dict:
    env = os.environ.copy()
    key = (cfg_raw or {}).get("ssh_key")
    if key:
        env["GIT_SSH_COMMAND"] = "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes" % shlex.quote(str(key))
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(path: str, *args: str, env=None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, env=env, errors="replace")
    if check and proc.returncode != 0:
        raise MemoryError("git %s : %s" % (" ".join(args[:2]),
                                           (proc.stderr or proc.stdout).strip()[-300:]))
    return proc


def open_copy(repository: str, state_dir: str, agent: str, *, cfg_raw=None) -> str:
    """Open or refresh a private local working copy of the persona repository."""
    path = copy_dir(state_dir, agent)
    env = _git_env(cfg_raw)
    if os.path.isdir(os.path.join(path, ".git")):
        existing = _git(path, "remote", "get-url", "origin").stdout.strip()
        same = (os.path.realpath(existing) == os.path.realpath(repository)
                if os.path.exists(repository) else existing == repository)
        if not same:
            raise MemoryError("working copy belongs to a different memory repository")
        os.chmod(path, 0o700)
        _git(path, "pull", "--rebase", "--autostash", "-q", env=env)
        return path
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    proc = subprocess.run(["git", "clone", "-q", "--", repository, path], capture_output=True,
                          text=True, timeout=GIT_TIMEOUT, env=env)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise MemoryError("memory clone failed: %s" % proc.stderr.strip()[-300:])
    os.chmod(path, 0o700)
    _git(path, "config", "user.name", agent)
    _git(path, "config", "user.email", "%s@personas.ameesh.invalid" % agent)
    return path


def index_text(path: str) -> str:
    try:
        with open(os.path.join(path, INDEX), encoding="utf-8") as fh:
            lines = fh.read(INDEX_MAX_BYTES).splitlines()[:INDEX_MAX_LINES]
    except OSError:
        return ""
    return "\n".join(lines).strip()


def journal_path(path: str, session: str | None) -> str:
    stamp = session or time.strftime("session-%Y%m%dT%H%M%S")
    return os.path.join(path, "journal", "%s.md" % re.sub(r"[^A-Za-z0-9._-]", "-", stamp))


def preface(path: str, agent: str, session: str | None) -> str:
    """Designate the memory repository and journal in the first session prompt."""
    index = index_text(path)
    revision = _git(path, "rev-parse", "HEAD").stdout.strip()
    lines = [
        "PERSONA MEMORY (%s). Your durable memory repository is %s." % (agent, path),
        "Loaded revision: %s" % revision,
        "Index (%s):" % INDEX,
        index or "(empty: no memories yet)",
        "Read memories/<topic>.md when relevant to your task.",
        "Write only to %s, by appending what you learn that will be useful later "
        "(decisions, human preferences, pitfalls, references). Never store secrets "
        "(keys, tokens or passwords). The memory component commits and pushes at "
        "turn end; your own session consolidates journals at lot completion or handoff."
        % os.path.relpath(journal_path(path, session), path),
        "",
    ]
    return "\n".join(lines) + "\n"


def secrets_in(path: str) -> list[str]:
    """Find likely secrets in the staged Git snapshot, before committing it."""
    names = _git(path, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").stdout
    found = []
    for name in filter(None, names.split("\0")):
        staged = _git(path, "show", ":" + name, check=False)
        if staged.returncode or any(pattern.search(staged.stdout) for pattern in SECRET_PATTERNS):
            found.append(name)
    return found



def sync(path: str, message: str, *, cfg_raw=None) -> dict:
    """Commit changes and push, preserving the working copy on failure."""
    if not os.path.isdir(os.path.join(path, ".git")):
        raise MemoryError("no open memory copy: %s" % path)
    _git(path, "add", "-A")
    changed = bool(_git(path, "diff", "--cached", "--quiet", check=False).returncode)
    if changed:
        suspects = secrets_in(path)
        if suspects:
            _git(path, "reset", "-q")
            return {"status": "secret", "files": suspects}
        _git(path, "commit", "-q", "-m", message)
    # Retry an earlier committed-but-unpushed turn even when no file changed.
    ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False)
    env = _git_env(cfg_raw)
    push = _git(path, "push", "-q", env=env, check=False)
    if push.returncode != 0:
        pull = _git(path, "pull", "--rebase", "-q", env=env, check=False)
        push = _git(path, "push", "-q", env=env, check=False) if pull.returncode == 0 else pull
    if push.returncode != 0:
        return {"status": "push_rejected", "error": (push.stderr or "").strip()[-300:]}
    if not changed and ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return {"status": "unchanged"}
    return {"status": "pushed", "commit": _git(path, "rev-parse", "--short", "HEAD").stdout.strip()}



def close(path: str, *, cfg_raw=None) -> dict:
    """Push remaining work and remove the copy only after confirmed synchronization."""
    if not os.path.isdir(path):
        return {"status": "absent"}
    r = sync(path, "memory: close working copy", cfg_raw=cfg_raw)
    if r["status"] in ("unchanged", "pushed"):
        ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            shutil.rmtree(path)
            return {"status": "removed"}
    return {"status": "kept", "reason": r}


CONSOLIDATION_PROMPT = (
    "MEMORY TURN. Consolidate your persona memory in %(path)s: read the journals "
    "(journal/*.md), and create or update a memory in memories/<topic>.md for each "
    "durable fact (frontmatter: type user|feedback|project|reference, modified), then "
    "add one line to MEMORY.md (at most 200 lines). Remove memories that are no "
    "longer true. Keep journals as archives. Never store secrets. Your session "
    "performs this consolidation; the memory component commits and pushes at turn end."
)


def consolidation_prompt(path: str) -> str:
    return CONSOLIDATION_PROMPT % {"path": path}



USAGE = """\
ameesh memory status <persona>        repository, working copy and unpushed changes
ameesh memory consolidate <persona>   ask its session to consolidate journals into memories
ameesh memory close <persona>         push remaining work, then remove the local copy
"""


def main(argv) -> int:
    import sys
    from . import db as db_mod, mail
    from .config import load as load_config
    if len(argv) != 2 or argv[0] not in ("status", "consolidate", "close"):
        print(USAGE, file=sys.stderr if argv and argv[0] not in ("-h", "--help") else sys.stdout)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    sub, agent = argv
    cfg = load_config()
    db = db_mod.connect(cfg)
    try:
        rows = db.query("SELECT memory_repository FROM agent_registry WHERE name = %s", (agent,))
        if not rows:
            print("ameesh memory: unknown persona %s" % agent, file=sys.stderr)
            return 2
        repo = rows[0].get("memory_repository")
        path = copy_dir(cfg.state_dir, agent)
        if sub == "status":
            print("persona: %s\nrepository: %s\ncopy: %s" % (
                agent, repo or "none (no memory.repository in its persona record)",
                path if os.path.isdir(path) else "none"))
            if os.path.isdir(os.path.join(path, ".git")):
                state = _git(path, "status", "--short", check=False).stdout.strip()
                ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip()
                print("uncommitted: %s\nunpushed: %s commit(s)" % (
                    "none" if not state else "%d file(s)" % len(state.splitlines()),
                    ahead or "?"))
            return 0
        if not repo:
            print("ameesh memory: %s has no memory repository" % agent, file=sys.stderr)
            return 2
        if sub == "consolidate":
            if not os.path.isdir(os.path.join(path, ".git")):
                path = open_copy(repo, cfg.state_dir, agent, cfg_raw=cfg.persona_memory)
            mail.send(db, "memory", agent, consolidation_prompt(path), kind="request")
            print("memory turn requested for %s" % agent)
            return 0
        r = close(path, cfg_raw=cfg.persona_memory)
        print("copy for %s: %s" % (agent, r["status"]))
        return 0 if r["status"] in ("removed", "absent") else 1
    except MemoryError as exc:
        print("ameesh memory: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
