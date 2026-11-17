# SPDX-License-Identifier: AGPL-3.0-only
"""Neutral persona memory component (L54, decision 0029).

Memory lives in a persona Git repository, outside the mesh database. The
harness reads MEMORY.md and memories/<topic>.md, then appends its own journal.
This component designates files and transports commits; the persona's own
session performs consolidation. No paid model calls or native memory store."""
from __future__ import annotations

import os
import configparser
import json
import logging
import tempfile
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


#: A memory repository is cloned by the isolated Git environment built by
#: `_git_env`: it removes every `GIT_CONFIG*` variable and sets
#: `GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_GLOBAL=/dev/null`. The `gh` credential
#: helper installed by the executor units is therefore invisible, and HTTPS
#: has no way to authenticate. The only supported credential is the
#: host-managed `persona_memory.ssh_key`, used by an SSH URL. This isolation is
#: deliberate (EXPLOITATION.md, persona memory): do not loosen it to make an
#: HTTPS repository work.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
#: scp-like SSH syntax (`git@github.com:owner/repo.git`), not a Windows drive.
_SCP_RE = re.compile(r"^(?:[^/@:]+@)?[^/:]+:[^/]")


def transport_kind(repository: str) -> str:
    """`ssh`, `http`, a named scheme, or `local` for a Git URL/path."""
    value = (repository or "").strip()
    match = _SCHEME_RE.match(value)
    if match:
        scheme = match.group(1).lower()
        if scheme in ("ssh", "git+ssh"):
            return "ssh"
        if scheme == "file":
            return "local"
        return scheme
    if _SCP_RE.match(value) and not re.match(r"^[A-Za-z]:[\\/]", value):
        return "ssh"
    return "local"


def _display_repository(value: str) -> str:
    """Never echo credentials embedded in a repository URL (userinfo)."""
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", str(value))


def transport_problem(repository: str | None, cfg_raw: dict | None, *,
                      check_key: bool = True) -> str | None:
    """Why this host cannot clone `repository`, or None when it can.

    `check_key=False` judges the URL only (another host's key file is not
    readable from here); the local check also requires an existing, readable
    `persona_memory.ssh_key`, the transport's only credential source.
    """
    if not repository or not str(repository).strip():
        return "no memory repository is declared"
    value = str(repository).strip()
    shown_repo = _display_repository(value)
    kind = transport_kind(value)
    if kind == "local":
        # A local path needs no credential; tests and mirrors use one.
        return None
    if kind != "ssh":
        match = _SCHEME_RE.match(value)
        scheme = match.group(1).lower() if match else kind
        shown = ("HTTPS" if scheme in ("http", "https")
                 else "the %s:// protocol" % scheme)
        return (
            "memory.repository %s uses %s, which cannot authenticate: the memory "
            "Git environment ignores global/system configuration and credential "
            "helpers (deliberate isolation); use an SSH URL "
            "(git@github.com:<owner>/<repo>.git)" % (shown_repo, shown))
    if not check_key:
        return None
    key = cfg_raw.get("ssh_key") if isinstance(cfg_raw, dict) else None
    if not key:
        return (
            "memory.repository %s is an SSH URL but this host sets no "
            "persona_memory.ssh_key, the only credential source of the memory "
            "transport" % shown_repo)
    key_path = os.path.expanduser(str(key))
    if not os.path.isfile(key_path):
        return "persona_memory.ssh_key %s does not exist on this host" % key
    if not os.access(key_path, os.R_OK):
        return "persona_memory.ssh_key %s is not readable by the executor" % key
    return None


def canon_findings(cfg, canon, host: str | None = None, *, placements=None,
                   check_key: bool | None = None) -> list:
    """Warnings for memory repositories that cannot be cloned on `host`.

    Only agents admitted on that host are judged: a memory repository is a
    property of the persona, but the transport (URL and key file) is a
    property of the host. A check never judges another host's key file — its
    configuration is unknown here — so the key requirement is local, while a
    non-SSH URL is refused everywhere. Warning, not error: the persona still
    runs, without memory, and the runner tells it so in its turn.
    """
    from . import canon as canon_mod
    from . import placement as placement_mod
    where = host or getattr(cfg, "host", "") or ""
    if not where:
        return []
    local = where == (getattr(cfg, "host", "") or "")
    if check_key is None:
        check_key = local
    try:
        verdicts = (placement_mod.verdicts(canon, where) if placements is None
                    else [v for v in placements if v.host == where])
        admitted = {v.agent for v in verdicts if v.ok}
    except Exception:  # noqa: BLE001 - a diagnostic never breaks the check
        return []
    out = []
    agents = getattr(canon, "agents", ())
    if not isinstance(agents, (list, tuple)):
        return out
    for agent in agents:
        repository = getattr(agent, "memory_repository", None)
        if not repository or agent.title not in admitted:
            continue
        problem = transport_problem(
            repository, getattr(cfg, "persona_memory", None) if local else None,
            check_key=check_key)
        if not problem:
            continue
        fiche = getattr(agent, "fiche", None)
        out.append(canon_mod.Finding(
            "agent-memory-transport", canon_mod.WARNING,
            "memory.repository of %s cannot be cloned on %s: %s — the persona "
            "runs without memory (docs/EXPLOITATION.md, persona memory)"
            % (agent.title, where, problem),
            member=getattr(fiche, "member", ""), path=getattr(fiche, "path", ""),
            agent=agent.title, host=where,
            untrusted=bool(getattr(fiche, "untrusted", False))))
    return out


def failure_code(reason: str) -> str:
    """A stable code for a memory-unavailable cause (event data, dedup)."""
    text = str(reason or "").lower()
    if "persona_memory.ssh_key" in text:
        return ("ssh_key_missing" if "sets no" in text
                else "ssh_key_unreadable")
    if "cannot authenticate" in text or "uses https" in text or "uses http" in text:
        return "https_transport"
    if "share uid" in text or "memory transport disabled" in text:
        return "isolation"
    if "clone failed" in text or "could not read" in text or "not found" in text:
        return "clone_failed"
    return "unknown"


def unavailable_preface(reason: str) -> str:
    """The visible line a turn carries when its memory could not be opened."""
    text = " ".join(str(reason or "unknown cause").split())[:300]
    return (
        "PERSONA MEMORY UNAVAILABLE for this turn: %s\n"
        "Your durable memory was not loaded; any journal written this turn may "
        "not be synchronized. The host configuration must be fixed: an SSH "
        "memory.repository and persona_memory.ssh_key (`ameesh canon check`).\n"
        % text)


def isolation_policy(cfg, canon_id: str | None = None) -> str:
    """Only an approved Host card can opt into shared-UID memory transport."""
    from . import canon as canon_mod
    for canon in canon_mod.load_configured(cfg):
        if not canon.readable or not canon.owns(canon_id):
            continue
        host = canon.host(cfg.host)
        if (host is not None and not host.fiche.untrusted
                and host.fiche.data.get("memory_isolation") == "shared-uid-accepted"):
            source = next((s for s in canon.sources if s.member == host.fiche.member), None)
            if source is not None and source.mode == "git":
                branch = canon_mod.remote_branch(source.repo, source.rev)
                approved = canon_mod.git_commit(source.repo, "refs/remotes/" + branch) if branch else None
                if approved and canon_mod.git_contains(source.repo, source.commit, approved):
                    return "shared-uid-accepted"
    return "required"


def require_isolation(cfg, db, agent: str) -> None:
    rows = db.query("SELECT name, canon, responsible FROM agent_registry WHERE name = %s",
                    (agent,))
    if not rows:
        raise MemoryError("unknown persona")
    if isolation_policy(cfg, rows[0].get("canon")) == "shared-uid-accepted":
        return
    peers = db.query(
        "SELECT name, responsible FROM agent_registry WHERE host = %s AND name <> %s "
        "AND (status = 'running' OR (lease_owner IS NOT NULL AND "
        "(lease_expires_at IS NULL OR lease_expires_at > clock_timestamp())))",
        (cfg.host, agent))
    # #7106 : sur un hôte partagé, les UID sont vérifiés (liaison du tenant
    # écrite par le superviseur) : des personas de tenants distincts sont
    # isolées ; même tenant, UID inconnu ou processus sous un autre UID :
    # refus, comme avant.
    from . import tenancy
    if tenancy.host_policy(db, cfg.host).get("shared"):
        problem = tenancy.isolation_problem(db, cfg.host, rows[0], peers)
        if problem:
            raise MemoryError("memory transport disabled on shared host %s: %s"
                              % (cfg.host, problem))
        return
    # The registry has no verified per-persona OS UID. All active peers on this
    # host are conservatively treated as sharing the executor's UID; 0700 does
    # not provide isolation between them. Actual UID isolation is separate work.
    if peers:
        raise MemoryError("memory transport disabled: another active persona on this host "
                          "may share UID %s; an approved Host memory_isolation exception "
                          "is required" % os.getuid())


def copy_dir(state_dir: str, agent: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}", agent):
        raise MemoryError("unsafe persona name")
    return os.path.join(state_dir, "memory", agent)


def _git_env(cfg_raw: dict | None, inherited=None) -> dict:
    env = dict(os.environ if inherited is None else inherited)
    # Ignore configuration injected by the process environment or global/system
    # files. The configured SSH key remains the transport credential source.
    for key in list(env):
        if key.startswith("GIT_CONFIG") or key in (
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(key)
    key = (cfg_raw or {}).get("ssh_key")
    if key:
        env["GIT_SSH_COMMAND"] = "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes" % shlex.quote(str(key))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _git(path: str, *args: str, env=None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                           "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
                           "-C", path, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, env=_git_env(None, env), errors="replace")
    if check and proc.returncode != 0:
        raise MemoryError("git %s : %s" % (" ".join(args[:2]),
                                           (proc.stderr or proc.stdout).strip()[-300:]))
    return proc


def _origin_url(path: str) -> str:
    """Read clone metadata as data; never execute Git in an inactive copy."""
    parser = configparser.RawConfigParser(strict=False)
    try:
        with open(os.path.join(path, ".git", "config"), encoding="utf-8") as stream:
            parser.read_file(stream)
        value = parser.get('remote "origin"', "url").strip()
        return json.loads(value) if value.startswith('"') else value
    except (OSError, ValueError, configparser.Error) as exc:
        raise MemoryError("memory copy has no readable origin URL") from exc


def _clone(repository: str, path: str, env: dict) -> None:
    proc = _git(os.path.dirname(path), "clone", "-q", "--no-local", "--template=", "--", repository, path,
                env=env, check=False)
    if proc.returncode != 0:
        raise MemoryError("memory clone failed: %s" % proc.stderr.strip()[-300:])


def _safe_copy(path: str, repository: str | None = None) -> None:
    """Reject links before reading memory or asking Git to transport it."""
    if os.path.realpath(path) != os.path.abspath(path):
        raise MemoryError("memory copy contains a symlink in its path")
    if not os.path.isdir(os.path.join(path, ".git")) or os.path.islink(os.path.join(path, ".git")):
        raise MemoryError("no safe open memory copy")
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                raise MemoryError("memory copy contains a symlink")
    if repository is not None:
        existing = _origin_url(path)
        same = (os.path.realpath(existing) == os.path.realpath(repository)
                if os.path.exists(repository) else existing == repository)
        if not same:
            raise MemoryError("working copy belongs to a different memory repository")


def open_copy(repository: str, state_dir: str, agent: str, *, cfg_raw=None) -> str:
    """Open or refresh a private local working copy of the persona repository."""
    # Refuse before any Git call: an HTTPS URL cannot authenticate in the
    # isolated memory environment, and an SSH URL without a configured key has
    # no credential source. The caller gets a named cause instead of a raw
    # Git error, and can tell the persona why its memory is missing.
    problem = transport_problem(repository, cfg_raw)
    if problem:
        raise MemoryError(problem)
    path = copy_dir(state_dir, agent)
    env = _git_env(cfg_raw)
    if os.path.lexists(path) and os.path.realpath(path) != os.path.abspath(path):
        raise MemoryError("memory copy contains a symlink in its path")
    existing = os.path.isdir(os.path.join(path, ".git"))
    if existing:
        _safe_copy(path, repository)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # A stopped persona's entire copy, including Git configuration and local
    # commits, is writable by any peer sharing its UID. Never autostash/rebase
    # that state into the next activation; reconstruct from the published repo.
    with tempfile.TemporaryDirectory(prefix="ameesh-memory-activation-") as staging:
        fresh = os.path.join(staging, "copy")
        _clone(repository, fresh, env)
        _safe_copy(fresh, repository)
        os.chmod(fresh, 0o700)
        _git(fresh, "config", "user.name", agent)
        _git(fresh, "config", "user.email", "%s@personas.ameesh.invalid" % agent)
        if existing:
            logging.getLogger(__name__).warning(
                "[%s] memory activation: discarded local state; restored published repository", agent)
            shutil.rmtree(path)
        elif os.path.lexists(path):
            raise MemoryError("memory path exists without a safe repository")
        shutil.move(fresh, path)
    return path


def index_text(path: str) -> str:
    _safe_copy(path)
    try:
        fd = os.open(os.path.join(path, INDEX), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fh:
            lines = fh.read(INDEX_MAX_BYTES).splitlines()[:INDEX_MAX_LINES]
    except OSError:
        return ""
    return "\n".join(lines).strip()


def journal_path(path: str, session: str | None) -> str:
    _safe_copy(path)
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



def _destructive_deletions(path: str, deleted: set[str], baseline: str) -> bool:
    if any(name.startswith("journal/") for name in deleted):
        return True
    tracked = set(filter(None, _git(path, "ls-tree", "-r", "--name-only", "-z", baseline).stdout.split("\0")))
    return bool(tracked and tracked <= deleted)


def _outgoing_guard(path: str) -> dict | None:
    commits = _git(path, "rev-list", "--reverse", "@{u}..HEAD").stdout.splitlines()
    for revision in commits:
        # Every outgoing snapshot is transported, even if a later commit removes
        # its secret. Inspect commits made by the persona session as well.
        tree = _git(path, "ls-tree", "-r", "-z", revision).stdout.split("\0")
        if any(entry.startswith("120000 ") for entry in tree):
            raise MemoryError("outgoing memory commit contains a symlink")
        names = _git(path, "diff-tree", "--root", "-m", "-r", "--no-commit-id",
                     "--name-only", "--diff-filter=ACMR", "-z", revision).stdout.split("\0")
        suspects = []
        for name in filter(None, names):
            content = _git(path, "show", revision + ":" + name, check=False)
            if content.returncode or any(p.search(content.stdout) for p in SECRET_PATTERNS):
                suspects.append(name)
        if suspects:
            return {"status": "secret", "files": sorted(set(suspects))}
        deleted = set(filter(None, _git(path, "diff-tree", "-m", "-r", "--no-commit-id",
                                        "--name-only", "--diff-filter=D", "-z", revision).stdout.split("\0")))
        parent = _git(path, "rev-parse", revision + "^", check=False)
        if deleted and parent.returncode == 0 and _destructive_deletions(path, deleted, parent.stdout.strip()):
            return {"status": "destructive", "files": sorted(deleted)}
    return None


def sync(path: str, message: str, *, cfg_raw=None, repository: str | None = None) -> dict:
    """Commit changes and push, preserving the working copy on failure."""
    _safe_copy(path, repository)
    url = repository or _origin_url(path)
    problem = transport_problem(url, cfg_raw)
    if problem:
        raise MemoryError(problem)
    branch = _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()
    target = _git(path, "config", "--get", "branch." + branch + ".merge").stdout.strip()
    if not target.startswith("refs/heads/"):
        raise MemoryError("memory copy has no upstream branch")
    tracking = "refs/remotes/origin/" + target[len("refs/heads/"):]
    _git(path, "add", "-A")
    deleted = set(filter(None, _git(path, "diff", "--cached", "--name-only",
                                    "--diff-filter=D", "-z").stdout.split("\0")))
    if deleted and _destructive_deletions(path, deleted, "HEAD"):
        _git(path, "reset", "-q")
        return {"status": "destructive", "files": sorted(deleted)}
    changed = bool(_git(path, "diff", "--cached", "--quiet", check=False).returncode)
    if changed:
        suspects = secrets_in(path)
        if suspects:
            _git(path, "reset", "-q")
            return {"status": "secret", "files": suspects}
        _git(path, "commit", "-q", "-m", message)
    local_head = _git(path, "rev-parse", "HEAD").stdout.strip()
    env = _git_env(cfg_raw)
    # Git rewrites even explicit push URLs using local pushInsteadOf rules.
    # Only this new clone may fetch the published repository or push to it.
    with tempfile.TemporaryDirectory(prefix="ameesh-memory-transport-") as staging:
        transport = os.path.join(staging, "copy")
        _clone(url, transport, env)
        _safe_copy(transport, url)
        current = _git(transport, "symbolic-ref", "--short", "HEAD").stdout.strip()
        _git(transport, "config", "branch." + current + ".merge", target)
        _git(transport, "config", "user.name", os.path.basename(path))
        _git(transport, "config", "user.email", os.path.basename(path) + "@personas.ameesh.invalid")
        _git(transport, "fetch", "-q", "--no-tags", "--", path, "HEAD", env=env)
        _git(transport, "reset", "--hard", "-q", "FETCH_HEAD")
        refused = _outgoing_guard(transport)
        if refused:
            return refused
        rebase = _git(transport, "rebase", "-q", tracking, env=env, check=False)
        if rebase.returncode:
            return {"status": "push_rejected", "error": rebase.stderr.strip()[-300:]}
        refused = _outgoing_guard(transport)
        if refused:
            return refused
        ahead = _git(transport, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
        push = _git(transport, "push", "-q", "--", url, "HEAD:" + target, env=env, check=False)
        if push.returncode:
            fetch = _git(transport, "fetch", "-q", "--", url, "+" + target + ":" + tracking,
                         env=env, check=False)
            rebase = (_git(transport, "rebase", "-q", tracking, env=env, check=False)
                      if fetch.returncode == 0 else fetch)
            if rebase.returncode == 0:
                refused = _outgoing_guard(transport)
                if refused:
                    return refused
                push = _git(transport, "push", "-q", "--", url, "HEAD:" + target,
                            env=env, check=False)
            else:
                push = rebase
        if push.returncode:
            return {"status": "push_rejected", "error": push.stderr.strip()[-300:]}
        published = _git(transport, "rev-parse", "HEAD").stdout.strip()
        # Import objects without a fetch in the active copy, where url.* rules
        # could also redirect transport. Update its tracking ref for retry/close.
        _safe_copy(path, repository)
        def import_missing(source, destination):
            # Existing object files are immutable and may be mode 0444.
            if not os.path.exists(destination):
                return shutil.copy2(source, destination)
            return destination
        shutil.copytree(os.path.join(transport, ".git", "objects"),
                        os.path.join(path, ".git", "objects"), dirs_exist_ok=True,
                        copy_function=import_missing)
        _git(path, "update-ref", tracking, published)
        if published != local_head:
            _git(path, "rebase", "--autostash", "-q", tracking, env=env)
        if not changed and ahead == "0":
            return {"status": "unchanged"}
        return {"status": "pushed", "commit": published[:7]}



def close(path: str, *, cfg_raw=None, repository: str | None = None) -> dict:
    """Push remaining work and remove the copy only after confirmed synchronization."""
    if not os.path.isdir(path):
        return {"status": "absent"}
    r = sync(path, "memory: close working copy", cfg_raw=cfg_raw, repository=repository)
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
    from . import db as db_mod, identity, mail
    from .config import load as load_config
    if len(argv) != 2 or argv[0] not in ("status", "consolidate", "close"):
        print(USAGE, file=sys.stderr if argv and argv[0] not in ("-h", "--help") else sys.stdout)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    sub, agent = argv
    cfg = load_config()
    db = db_mod.connect(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if (not binding.ok or binding.source not in ("runner", "session") or binding.name != agent):
            raise MemoryError("a verified session bound to the requested persona is required")
        rows = db.query(
            "SELECT memory_repository, status, (lease_owner IS NOT NULL AND "
            "(lease_expires_at IS NULL OR lease_expires_at > clock_timestamp())) AS leased "
            "FROM agent_registry WHERE name = %s", (agent,))
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
                _safe_copy(path, repo)
                state = _git(path, "status", "--short", check=False).stdout.strip()
                ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip()
                print("uncommitted: %s\nunpushed: %s commit(s)" % (
                    "none" if not state else "%d file(s)" % len(state.splitlines()),
                    ahead or "?"))
            return 0
        if not repo:
            print("ameesh memory: %s has no memory repository" % agent, file=sys.stderr)
            return 2
        require_isolation(cfg, db, agent)
        active = rows[0].get("status") == "running" or rows[0].get("leased")
        if sub == "consolidate":
            if not active or not os.path.isdir(os.path.join(path, ".git")):
                path = open_copy(repo, cfg.state_dir, agent, cfg_raw=cfg.persona_memory)
            _safe_copy(path, repo)
            mail.send(db, "memory", agent, consolidation_prompt(path), kind="request")
            print("memory turn requested for %s" % agent)
            return 0
        if not active and os.path.isdir(os.path.join(path, ".git")):
            path = open_copy(repo, cfg.state_dir, agent, cfg_raw=cfg.persona_memory)
        r = close(path, cfg_raw=cfg.persona_memory, repository=repo)
        print("copy for %s: %s" % (agent, r["status"]))
        return 0 if r["status"] in ("removed", "absent") else 1
    except MemoryError as exc:
        print("ameesh memory: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
