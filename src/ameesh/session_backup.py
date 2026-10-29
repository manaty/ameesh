# SPDX-License-Identifier: AGPL-3.0-only
"""Off-device session backups (L53).

Archive harness session files, encrypt by default and store in a local folder
or S3-compatible storage. S3 requires an explicitly configured endpoint and
region; credentials come from the runner environment. No storage is enabled
without host configuration. Keys: <host>/<agent>/<session>/<timestamp>.tar.gz[.gpg]."""
from __future__ import annotations

import fnmatch
import glob
import gzip
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass

DEFAULT_MIN_INTERVAL_S = 300.0
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
# SigV4 signs the request body under x-amz-content-sha256. curl 7.88 (Debian 12)
# signs that header when supplied but never adds it itself; Scaleway refuses
# the request without it. A GET/list has no body and names the empty hash.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupConfig:
    target: str
    recipients: tuple[str, ...] = ()
    encrypt: bool = True
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S

    @property
    def is_s3(self) -> bool:
        return self.target.startswith("s3://")


def parse(raw) -> BackupConfig | None:
    """Validate host session_backup configuration; disabled when absent. Fail closed on missing encryption recipients or relative targets."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise BackupError("session_backup: expected an object")
    target = str(raw.get("target") or "").strip()
    if not target:
        raise BackupError("session_backup.target is required")
    if not (target.startswith("s3://") or os.path.isabs(target)):
        raise BackupError("session_backup.target: expected s3://bucket/prefix or an absolute path")
    encrypt = raw.get("encrypt", True)
    if not isinstance(encrypt, bool):
        raise BackupError("session_backup.encrypt: expected true or false")
    recipients = raw.get("recipients") or []
    if not isinstance(recipients, list) or not all(isinstance(r, str) and r for r in recipients):
        raise BackupError("session_backup.recipients: expected a list of GPG recipients")
    if encrypt and not recipients:
        raise BackupError("session_backup: encryption requires recipients "
                          "(recipients); sessions contain everything the agent has read")
    try:
        interval = float(raw.get("min_interval_s", DEFAULT_MIN_INTERVAL_S))
    except (TypeError, ValueError):
        raise BackupError("session_backup.min_interval_s: expected a number")
    if not math.isfinite(interval) or interval < 0:
        raise BackupError("session_backup.min_interval_s: expected a finite nonnegative number")
    return BackupConfig(target=target.rstrip("/") or "/", recipients=tuple(recipients),
                        encrypt=encrypt, min_interval_s=max(0.0, interval))

def _has_symlink(home: str, path: str) -> bool:
    relative = os.path.relpath(path, home)
    current = home
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def session_paths(home: str, patterns, session: str) -> list[str]:
    """Find session files under the account home; reject unsafe session identifiers."""
    if not session or not _SAFE.match(session) or any(c in session for c in "*?[]"):
        raise BackupError("unsafe session identifier: %r" % session)
    home = os.path.realpath(home)
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(home, pattern.replace("{session}", session)))):
            # Keep the matched path: resolving a rollout symlink would turn
            # an unrelated credential file into an apparently valid session.
            if _has_symlink(home, path):
                continue
            real = os.path.realpath(path)
            if real != home and real.startswith(home + os.sep) and real not in found:
                found.append(real)
    return found


def fingerprint(paths: list[str]) -> str:
    """Hash file paths, sizes and timestamps to skip unchanged sessions."""
    h = hashlib.sha256()
    for top in paths:
        walk = [(top, [], [])] if os.path.isfile(top) else os.walk(top)
        for root, _dirs, files in walk:
            entries = [root] if os.path.isfile(root) else [os.path.join(root, f) for f in files]
            for f in sorted(entries):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                h.update(("%s\0%d\0%d\n" % (f, st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()


def archive(home: str, paths: list[str]) -> bytes:
    """Archive paths relative to the account home, excluding harness lock files."""
    home = os.path.realpath(home)
    buf = io.BytesIO()
    def safe_member(member):
        path = os.path.join(home, member.name)
        if member.issym() or member.islnk() or _has_symlink(home, path):
            return None
        if not (member.isfile() or member.isdir()):
            return None
        if not os.path.realpath(path).startswith(home + os.sep):
            raise BackupError("session archive path escapes account home")
        return None if fnmatch.fnmatch(member.name, "*.lock") else member

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in paths:
            if (_has_symlink(home, path)
                    or not os.path.realpath(path).startswith(home + os.sep)):
                raise BackupError("unsafe session archive path")
            tar.add(path, arcname=os.path.relpath(path, home),
                    filter=safe_member)
    return buf.getvalue()


def encrypt(data: bytes, recipients) -> bytes:
    # Encrypting to public keys needs no agent. On an agent VM, which holds no
    # private key, autostart would recreate the very gpg-agent the deployment
    # masks (#3320); decrypt keeps its own path and never asks for this.
    argv = ["gpg", "--batch", "--yes", "--quiet", "--no-autostart",
            "--trust-model", "always", "--encrypt"]
    for r in recipients:
        argv += ["--recipient", r]
    proc = subprocess.run(argv, input=data, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise BackupError("GPG encryption failed: %s"
                          % proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return proc.stdout


def decrypt(data: bytes) -> bytes:
    proc = subprocess.run(["gpg", "--batch", "--quiet", "--decrypt"], input=data,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise BackupError("GPG decryption failed (private key missing on this host?): %s"
                          % proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return proc.stdout

def _s3(cfg: BackupConfig, env) -> tuple[str, str, str, str]:
    m = re.match(r"^s3://([^/]+)(?:/(.*))?$", cfg.target)
    if not m:
        raise BackupError("invalid S3 target: %s" % cfg.target)
    access = env.get("AMEESH_BACKUP_S3_ACCESS_KEY")
    secret = env.get("AMEESH_BACKUP_S3_SECRET_KEY")
    region = env.get("AMEESH_BACKUP_S3_REGION")
    endpoint = (env.get("AMEESH_BACKUP_S3_ENDPOINT") or "").rstrip("/")
    if not endpoint or not region:
        raise BackupError("S3 requires AMEESH_BACKUP_S3_ENDPOINT and AMEESH_BACKUP_S3_REGION")
    parts = urllib.parse.urlsplit(endpoint)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise BackupError("S3 endpoint must be an HTTPS URL without credentials, query or fragment")
    if not access or not secret:
        raise BackupError("S3 credentials missing (AMEESH_BACKUP_S3_ACCESS_KEY / _SECRET_KEY)")
    prefix = (m.group(2) or "").strip("/")
    return endpoint + "/" + m.group(1), prefix, "%s:%s" % (access, secret), region


def _curl(args: list[str], data: bytes | None = None, timeout: float = 600,
          *, user: str) -> bytes:
    # Uploads already use stdin. Keep authentication in a private curl config
    # file, removed even on timeout; neither argv nor exceptions contain it.
    if any(ord(c) < 32 or ord(c) == 127 for c in user):
        raise BackupError("invalid S3 credentials")
    quoted = user.replace("\\", "\\\\").replace('"', '\\"')
    fd, path = tempfile.mkstemp(prefix="ameesh-s3-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write('user = "' + quoted + '"\n')
        try:
            proc = subprocess.run(["curl", "-q", "--config", path, "-fsS", "--retry", "3",
                                   "-H", "x-amz-content-sha256: "
                                   + hashlib.sha256(data or b"").hexdigest(), *args],
                                  input=data, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackupError("S3 storage timed out") from None
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            for credential in (user, *user.split(":", 1)):
                if credential:
                    message = message.replace(credential, "[redacted]")
            raise BackupError("S3 storage failed: %s" % message[-300:])
        return proc.stdout
    finally:
        os.unlink(path)


def _local_path(cfg: BackupConfig, key: str) -> str:
    root = os.path.realpath(cfg.target)
    path = os.path.realpath(os.path.join(root, key))
    if not key or os.path.isabs(key) or os.path.commonpath([root, path]) != root or path == root:
        raise BackupError("backup key must stay inside the target directory")
    return path


def put(cfg: BackupConfig, key: str, data: bytes, env=None) -> str:
    env = os.environ if env is None else env
    if not cfg.is_s3:
        path = _local_path(cfg, key)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".backup-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path
    base, prefix, user, region = _s3(cfg, env)
    url = "%s/%s" % (base, urllib.parse.quote("/".join(p for p in (prefix, key) if p), safe="/"))
    _curl(["-X", "PUT", "--data-binary", "@-",
           "--aws-sigv4", "aws:amz:%s:s3" % region, url], data=data, user=user)
    return url


def list_keys(cfg: BackupConfig, prefix: str, env=None) -> list[str]:
    """List backup keys below the target-relative prefix, sorted."""
    env = os.environ if env is None else env
    if not cfg.is_s3:
        root = _local_path(cfg, prefix) if prefix else cfg.target
        out = []
        for d, _dirs, files in os.walk(root):
            for f in files:
                if not f.startswith(".backup-"):
                    out.append(os.path.relpath(os.path.join(d, f), cfg.target))
        return sorted(out)
    base, bprefix, user, region = _s3(cfg, env)
    full = "/".join(p for p in (bprefix, prefix) if p)
    keys, token = [], None
    while True:
        params = {"list-type": "2", "prefix": full}
        if token:
            params["continuation-token"] = token
        query = urllib.parse.urlencode(params)
        body = _curl(["--aws-sigv4", "aws:amz:%s:s3" % region,
                      "%s?%s" % (base, query)], user=user).decode("utf-8", "replace")
        try:
            document = ET.fromstring(body)
        except ET.ParseError as exc:
            raise BackupError("invalid S3 listing response") from exc
        keys += [node.text for node in document.iter()
                 if node.tag.rsplit("}", 1)[-1] == "Key" and node.text]
        token = next((node.text for node in document.iter()
                      if node.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), None)
        if not token:
            break
    strip = (bprefix + "/") if bprefix else ""
    return sorted(k[len(strip):] if k.startswith(strip) else k for k in keys)


def get(cfg: BackupConfig, key: str, env=None) -> bytes:
    env = os.environ if env is None else env
    if not cfg.is_s3:
        with open(_local_path(cfg, key), "rb") as fh:
            return fh.read()
    base, prefix, user, region = _s3(cfg, env)
    return _curl(["--aws-sigv4", "aws:amz:%s:s3" % region,
                  "%s/%s" % (base, urllib.parse.quote("/".join(p for p in (prefix, key) if p), safe="/"))],
                 user=user)

def _state_path(state_dir: str, agent: str) -> str:
    return os.path.join(state_dir, "session-backup", "%s.json" % agent)


def backup(cfg: BackupConfig, *, host: str, agent: str, home: str, patterns, session: str,
           state_dir: str, env=None, now: float | None = None, force: bool = False) -> dict:
    """Copy a changed session subject to its minimum backup interval. Return an English status."""
    now = time.time() if now is None else now
    for name in (host, agent):
        if not _SAFE.match(name or ""):
            raise BackupError("unsafe name in backup key: %r" % name)
    paths = session_paths(home, patterns, session)
    if not paths:
        return {"status": "not_found", "session": session}
    state_file = _state_path(state_dir, agent)
    try:
        with open(state_file, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    last = state.get(session) or {}
    fp = fingerprint(paths)
    if not force and last.get("fingerprint") == fp:
        return {"status": "unchanged", "session": session}
    if not force and now - float(last.get("at") or 0) < cfg.min_interval_s:
        return {"status": "too_soon", "session": session}
    data = archive(home, paths)
    suffix = ".tar.gz"
    if cfg.encrypt:
        data, suffix = encrypt(data, cfg.recipients), ".tar.gz.gpg"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    key = "%s/%s/%s/%s%s" % (host, agent, session, stamp, suffix)
    where = put(cfg, key, data, env)
    state[session] = {"fingerprint": fp, "at": now, "key": key}
    os.makedirs(os.path.dirname(state_file), mode=0o700, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_file)
    return {"status": "copied", "session": session, "key": key, "where": where,
            "bytes": len(data), "files": len(paths)}


def latest(cfg: BackupConfig, *, agent: str, session: str, host: str | None = None,
           env=None) -> str | None:
    """Find the latest backup for this agent and session, optionally on one host."""
    keys = [k for k in list_keys(cfg, host + "/" if host else "", env)
            if k.split("/")[1:3] == [agent, session]]
    return max(keys, key=lambda k: k.rsplit("/", 1)[-1]) if keys else None


def restore(cfg: BackupConfig, key: str, home: str, *, env=None, overwrite: bool = False) -> list[str]:
    """Restore under the account home; reject escaping paths, links and implicit overwrites."""
    data = get(cfg, key, env)
    if key.endswith(".gpg"):
        data = decrypt(data)
    # Tar readers may stop at the tar end marker before consuming the gzip
    # trailer. Verify the entire stream (CRC and length) before any extraction.
    try:
        data = gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise BackupError("backup rejected: invalid or incomplete gzip stream") from exc
    home = os.path.realpath(home)
    restored = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        members = tar.getmembers()
        for m in members:
            target = os.path.realpath(os.path.join(home, m.name))
            if (not target.startswith(home + os.sep) or os.path.isabs(m.name)
                    or not (m.isfile() or m.isdir())):
                raise BackupError("backup rejected: unsafe account-home path (%s)" % m.name)
        if not overwrite:
            files = [m.name for m in members if m.isfile()
                     and os.path.exists(os.path.join(home, m.name))]
            if files:
                raise BackupError("session already exists on this host (%s): use --overwrite to "
                                  "replace it" % ", ".join(files[:3]))
        if hasattr(tarfile, "data_filter"):
            tar.extractall(home, filter="data")
        else:
            tar.extractall(home)
        restored = [m.name for m in members if m.isfile()]
    return restored

USAGE = """\
ameesh session list <agent> [--host H]            list off-device session backups
ameesh session backup <agent> [--force] [--home DIR]  back up the current session now
ameesh session restore <agent> [--session S] [--key K] [--home DIR] [--overwrite]
                                                  [--allow-other-agent]
                                                  restore the latest backup (or K)
                                                  to the account home (or DIR)
"""


def _session_patterns(row: dict) -> tuple[str, ...]:
    """Les motifs de fichiers de session déclarés par le harnais de l'agent."""
    from . import harnesses
    descriptor = harnesses.get(row.get("harness") or "")
    return tuple(getattr(descriptor, "session_files", ()) or ())


def _agent_home(cfg, row: dict, agent: str = "") -> str:
    """Le home du propriétaire, seulement s'il est prouvable (N de relecture #731).

    Le propriétaire est le compte de session inscrit au registre. Seul un
    profil `config_dir` qui déclare explicitement son `path` prouve son home.
    Sans profil, pour un compte par défaut (`config_dir` sans `path`) ou pour
    un profil à clé d'API, `Profile.home()` le prendrait dans l'environnement
    de l'appelant (`config_env_of`, home par défaut partagé du harnais) : on
    refuse. `--home` reste la désignation explicite, faite par l'appelant,
    d'un home qu'il assume.
    """
    from . import accounts
    harness = row.get("harness") or ""
    account = row.get("session_account")
    profile = accounts.by_name(accounts.profiles(cfg, harness), account)
    if profile is not None and profile.kind == "config_dir" and profile.path:
        return profile.home()
    reason = ("no account profile %s" % (repr(account) if account else "(none)")
              if profile is None else
              "account profile %s declares no path" % repr(profile.name))
    raise BackupError(
        "cannot prove the home of %s: %s for harness %s "
        "(use --home to designate the account home)"
        % (agent or "the session owner", reason, harness or "?"))


def key_owner(key: str) -> str:
    """Read the persona from the canonical host/persona/session/archive key."""
    parts = key.split("/")
    if (len(parts) != 4 or not all(_SAFE.fullmatch(part) for part in parts)
            or not parts[3].endswith((".tar.gz", ".tar.gz.gpg"))):
        raise BackupError("invalid backup key: expected host/agent/session/archive.tar.gz[.gpg]")
    return parts[1]


def _same_home(left: str, right: str) -> bool:
    """True when both paths designate the same directory, symlinks resolved (L731).

    A path that cannot be resolved reads as different: the caller then refuses
    without `--allow-other-agent`, never guesses."""
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return False


def restore_agent(db, cfg, conf, args) -> tuple[str, str, list[str]]:
    """Lock both destination and key owner through extraction; refuse active turns."""
    owner = key_owner(args.key) if args.key else args.agent
    allow_other = bool(getattr(args, "allow_other_agent", False))
    if owner != args.agent and not allow_other:
        raise BackupError("backup belongs to %s, not %s: use --allow-other-agent "
                          "for an explicit cross-agent restore" % (owner, args.agent))
    with db.transaction() as tx:
        locked = {}
        # Stable ordering avoids deadlocks for opposing cross-agent restores.
        for agent in sorted({args.agent, owner}):
            rows = tx.query(
                "SELECT harness, session_id, session_account, status, "
                "(lease_owner IS NOT NULL AND (lease_expires_at IS NULL "
                "OR lease_expires_at > clock_timestamp())) AS leased "
                "FROM agent_registry WHERE name = %s FOR UPDATE", (agent,))
            if not rows:
                raise BackupError("unknown agent %s" % agent)
            row = rows[0]
            if row["status"] == "running" or row["leased"]:
                raise BackupError("cannot restore an active agent: stop %s first" % agent)
            locked[agent] = row
        row = locked[args.agent]
        if args.home:
            home = args.home
            # L731: an explicit destination home must be the key owner's own
            # home. Anything else — another agent's home, or a directory whose
            # owner we cannot prove (N #782) — is a foreign home and needs the
            # same explicit flag as a foreign key, before any download.
            if not allow_other:
                try:
                    owner_home = _agent_home(cfg, locked[owner], owner)
                except Exception as exc:  # fail closed: an unprovable home is refused
                    raise BackupError("cannot identify the home of %s (%s): an explicit "
                                      "destination home needs --allow-other-agent"
                                      % (owner, exc)) from exc
                if not _same_home(home, owner_home):
                    raise BackupError("destination home %s is not the home of %s: use "
                                      "--allow-other-agent for an explicit restore into "
                                      "another home" % (home, owner))
        else:
            home = _agent_home(cfg, row, args.agent)
        session = args.session or row.get("session_id")
        key = args.key or latest(conf, agent=args.agent, session=session or "")
        if not key:
            raise BackupError("no backup for %s (session %s)" % (args.agent, session))
        if key_owner(key) != owner:
            raise BackupError("backup owner does not match the locked agent")
        return key, home, restore(conf, key, home, overwrite=args.overwrite)


def main(argv) -> int:
    import argparse
    import sys
    from . import db as db_mod
    from .config import load as load_config
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    p = argparse.ArgumentParser(prog="ameesh session %s" % sub)
    p.add_argument("agent")
    p.add_argument("--host")
    p.add_argument("--force", action="store_true")
    p.add_argument("--session")
    p.add_argument("--key")
    p.add_argument("--home", help="account home to use instead of the declared one")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--allow-other-agent", action="store_true",
                   help="explicitly allow a key owned by another agent, or a destination "
                        "home that is not the key owner's (L731); both agents must be idle")
    args = p.parse_args(rest)
    cfg = load_config()
    try:
        conf = parse(cfg.session_backup)
    except BackupError as exc:
        print("ameesh session: %s" % exc, file=sys.stderr)
        return 2
    if conf is None:
        print("ameesh session: no backup target (host session_backup configuration)",
              file=sys.stderr)
        return 2
    try:
        if sub == "list":
            keys = [k for k in list_keys(conf, (args.host + "/") if args.host else "")
                    if k.split("/")[1:2] == [args.agent]]
            for k in keys:
                print(k)
            if not keys:
                print("no backups for %s" % args.agent)
            return 0
        db = db_mod.connect(cfg)
        try:
            if sub == "restore":
                key, home, files = restore_agent(db, cfg, conf, args)
                print("restored %s: %d file(s) under %s" % (key, len(files), home))
                return 0
            rows = db.query("SELECT harness, session_id, session_account FROM agent_registry "
                            "WHERE name = %s", (args.agent,))
            row = rows[0] if rows else None
        finally:
            db.close()
        if not row:
            print("ameesh session: unknown agent %s" % args.agent, file=sys.stderr)
            return 2
        patterns = _session_patterns(row)
        home = args.home or _agent_home(cfg, row, args.agent)
        if sub == "backup":
            if not row.get("session_id"):
                print("ameesh session: %s n'a pas de session" % args.agent, file=sys.stderr)
                return 2
            r = backup(conf, host=cfg.host, agent=args.agent, home=home, patterns=patterns,
                       session=row["session_id"], state_dir=cfg.state_dir, force=args.force)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["status"] in ("copied", "unchanged", "too_soon") else 1
    except BackupError as exc:
        print("ameesh session: %s" % exc, file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2
