# SPDX-License-Identifier: AGPL-3.0-only
"""Sauvegarde des sessions hors de l'appareil (lot L53, décision 0032 §2, étude v2 A7).

Les sessions font partie de la persona : la perte d'un appareil ne doit pas
faire perdre ce qu'elle sait. À la fin de chaque tour, l'exécuteur archive les
fichiers de la session (motifs `ameesh.backup.session_files` du descripteur de
harnais, relatifs au dossier du compte), les **chiffre** pour les destinataires
configurés, et les dépose dans la cible : un dossier (monté, synchronisé) ou un
stockage d'objets S3.

Configuration de l'hôte (`session_backup`, jamais de secret) :

    {"target": "s3://seau/prefixe" | "/chemin/absolu",
     "recipients": ["<empreinte gpg>", …],   # obligatoires sauf "encrypt": false
     "encrypt": true,
     "min_interval_s": 300}                   # au plus une copie par session et par intervalle

Clés du stockage S3, dans l'environnement de l'exécuteur :
`AMEESH_BACKUP_S3_ACCESS_KEY`, `AMEESH_BACKUP_S3_SECRET_KEY`,
`AMEESH_BACKUP_S3_REGION`, `AMEESH_BACKUP_S3_ENDPOINT` (défaut
`https://s3.<région>.scw.cloud`). Une clé en écriture seule suffit pour
sauvegarder ; restaurer demande la lecture.

Nom d'une copie : `<hôte>/<agent>/<session>/<horodatage>.tar.gz[.gpg]`.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass

DEFAULT_MIN_INTERVAL_S = 300.0
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


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
    """La clé `session_backup` de l'hôte ; None si absente (sauvegarde désactivée).

    Fail closed : chiffrement demandé (défaut) sans destinataire, ou cible
    relative, lèvent `BackupError` au lieu de copier en clair ou n'importe où.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise BackupError("session_backup : objet attendu")
    target = str(raw.get("target") or "").strip()
    if not target:
        raise BackupError("session_backup.target manquant")
    if not (target.startswith("s3://") or os.path.isabs(target)):
        raise BackupError("session_backup.target : s3://seau/prefixe ou chemin absolu")
    encrypt = raw.get("encrypt", True)
    if not isinstance(encrypt, bool):
        raise BackupError("session_backup.encrypt : vrai ou faux")
    recipients = raw.get("recipients") or []
    if not isinstance(recipients, list) or not all(isinstance(r, str) and r for r in recipients):
        raise BackupError("session_backup.recipients : liste d'empreintes gpg")
    if encrypt and not recipients:
        raise BackupError("session_backup : chiffrement demandé sans destinataire "
                          "(recipients) — une session contient tout ce que l'agent a lu")
    try:
        interval = float(raw.get("min_interval_s", DEFAULT_MIN_INTERVAL_S))
    except (TypeError, ValueError):
        raise BackupError("session_backup.min_interval_s : nombre attendu")
    return BackupConfig(target=target.rstrip("/"), recipients=tuple(recipients),
                        encrypt=encrypt, min_interval_s=max(0.0, interval))


# --------------------------------------------------------------------------
# fichiers d'une session
# --------------------------------------------------------------------------

def session_paths(home: str, patterns, session: str) -> list[str]:
    """Fichiers et dossiers de la session `session` sous `home` (chemins absolus).

    L'identifiant n'entre dans un motif que s'il est sûr (pas de joker, pas de
    `/`) : un identifiant forgé ne doit pas élargir la sauvegarde.
    """
    if not session or not _SAFE.match(session) or any(c in session for c in "*?[]"):
        raise BackupError("identifiant de session inutilisable : %r" % session)
    home = os.path.realpath(home)
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(home, pattern.replace("{session}", session)))):
            real = os.path.realpath(path)
            if real != home and real.startswith(home + os.sep) and real not in found:
                found.append(real)
    return found


def fingerprint(paths: list[str]) -> str:
    """Empreinte bon marché (chemins, tailles, dates) : rien n'a changé = pas de copie."""
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
    """tar.gz des chemins, nommés relativement au dossier du compte ; les
    verrous des harnais (`*.lock`) sont exclus."""
    home = os.path.realpath(home)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in paths:
            tar.add(path, arcname=os.path.relpath(path, home),
                    filter=lambda ti: None if fnmatch.fnmatch(ti.name, "*.lock") else ti)
    return buf.getvalue()


def encrypt(data: bytes, recipients) -> bytes:
    argv = ["gpg", "--batch", "--yes", "--quiet", "--trust-model", "always", "--encrypt"]
    for r in recipients:
        argv += ["--recipient", r]
    proc = subprocess.run(argv, input=data, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise BackupError("chiffrement gpg impossible : %s"
                          % proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return proc.stdout


def decrypt(data: bytes) -> bytes:
    proc = subprocess.run(["gpg", "--batch", "--quiet", "--decrypt"], input=data,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise BackupError("déchiffrement gpg impossible (clé privée absente de cet hôte ?) : %s"
                          % proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return proc.stdout


# --------------------------------------------------------------------------
# cibles : dossier ou S3
# --------------------------------------------------------------------------

def _s3(cfg: BackupConfig, env) -> tuple[str, str, str, str]:
    m = re.match(r"^s3://([^/]+)(?:/(.*))?$", cfg.target)
    if not m:
        raise BackupError("cible S3 illisible : %s" % cfg.target)
    access = env.get("AMEESH_BACKUP_S3_ACCESS_KEY")
    secret = env.get("AMEESH_BACKUP_S3_SECRET_KEY")
    region = env.get("AMEESH_BACKUP_S3_REGION") or "fr-par"
    if not access or not secret:
        raise BackupError("clés S3 absentes (AMEESH_BACKUP_S3_ACCESS_KEY / _SECRET_KEY)")
    endpoint = (env.get("AMEESH_BACKUP_S3_ENDPOINT")
                or "https://s3.%s.scw.cloud" % region).rstrip("/")
    prefix = (m.group(2) or "").strip("/")
    return endpoint + "/" + m.group(1), prefix, "%s:%s" % (access, secret), region


def _curl(args: list[str], data: bytes | None = None, timeout: float = 600) -> bytes:
    proc = subprocess.run(["curl", "-fsS", "--retry", "3", *args], input=data,
                          capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise BackupError("stockage S3 : %s" % proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return proc.stdout


def put(cfg: BackupConfig, key: str, data: bytes, env=None) -> str:
    env = os.environ if env is None else env
    if not cfg.is_s3:
        path = os.path.join(cfg.target, key)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".copie-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path
    base, prefix, user, region = _s3(cfg, env)
    url = "%s/%s" % (base, "/".join(p for p in (prefix, key) if p))
    _curl(["-X", "PUT", "--data-binary", "@-", "--user", user,
           "--aws-sigv4", "aws:amz:%s:s3" % region, url], data=data)
    return url


def list_keys(cfg: BackupConfig, prefix: str, env=None) -> list[str]:
    """Clés des copies sous `prefix` (relatif à la cible), triées."""
    env = os.environ if env is None else env
    if not cfg.is_s3:
        root = os.path.join(cfg.target, prefix)
        out = []
        for d, _dirs, files in os.walk(root):
            for f in files:
                if not f.startswith(".copie-"):
                    out.append(os.path.relpath(os.path.join(d, f), cfg.target))
        return sorted(out)
    base, bprefix, user, region = _s3(cfg, env)
    full = "/".join(p for p in (bprefix, prefix) if p)
    keys, token = [], None
    while True:
        query = "list-type=2&prefix=%s" % full + ("&continuation-token=%s" % token if token else "")
        body = _curl(["--user", user, "--aws-sigv4", "aws:amz:%s:s3" % region,
                      "%s?%s" % (base, query)]).decode("utf-8", "replace")
        keys += re.findall(r"<Key>([^<]+)</Key>", body)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not m:
            break
        token = m.group(1)
    strip = (bprefix + "/") if bprefix else ""
    return sorted(k[len(strip):] if k.startswith(strip) else k for k in keys)


def get(cfg: BackupConfig, key: str, env=None) -> bytes:
    env = os.environ if env is None else env
    if not cfg.is_s3:
        with open(os.path.join(cfg.target, key), "rb") as fh:
            return fh.read()
    base, prefix, user, region = _s3(cfg, env)
    return _curl(["--user", user, "--aws-sigv4", "aws:amz:%s:s3" % region,
                  "%s/%s" % (base, "/".join(p for p in (prefix, key) if p))])


# --------------------------------------------------------------------------
# sauvegarde et restauration
# --------------------------------------------------------------------------

def _state_path(state_dir: str, agent: str) -> str:
    return os.path.join(state_dir, "session-backup", "%s.json" % agent)


def backup(cfg: BackupConfig, *, host: str, agent: str, home: str, patterns, session: str,
           state_dir: str, env=None, now: float | None = None, force: bool = False) -> dict:
    """Une copie de la session si elle a changé (et pas plus d'une par intervalle).

    Rend `{"status": "copiee"|"inchangee"|"trop_tot"|"introuvable", …}`.
    """
    now = time.time() if now is None else now
    for name in (host, agent):
        if not _SAFE.match(name or ""):
            raise BackupError("nom inutilisable dans une clé de copie : %r" % name)
    paths = session_paths(home, patterns, session)
    if not paths:
        return {"status": "introuvable", "session": session}
    state_file = _state_path(state_dir, agent)
    try:
        with open(state_file, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    last = state.get(session) or {}
    fp = fingerprint(paths)
    if not force and last.get("fingerprint") == fp:
        return {"status": "inchangee", "session": session}
    if not force and now - float(last.get("at") or 0) < cfg.min_interval_s:
        return {"status": "trop_tot", "session": session}
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
    return {"status": "copiee", "session": session, "key": key, "where": where,
            "bytes": len(data), "files": len(paths)}


def latest(cfg: BackupConfig, *, agent: str, session: str, host: str | None = None,
           env=None) -> str | None:
    """Clé de la dernière copie de cette session (tous hôtes, ou un seul)."""
    keys = [k for k in list_keys(cfg, host + "/" if host else "", env)
            if k.split("/")[1:3] == [agent, session]]
    return max(keys, key=lambda k: k.rsplit("/", 1)[-1]) if keys else None


def restore(cfg: BackupConfig, key: str, home: str, *, env=None, overwrite: bool = False) -> list[str]:
    """Restaure une copie sous `home` ; refuse d'écraser une session existante
    sans `overwrite`, et tout chemin qui sortirait de `home`."""
    data = get(cfg, key, env)
    if key.endswith(".gpg"):
        data = decrypt(data)
    home = os.path.realpath(home)
    restored = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            target = os.path.realpath(os.path.join(home, m.name))
            if not target.startswith(home + os.sep) or m.issym() or m.islnk():
                raise BackupError("copie refusée : chemin hors du dossier du compte (%s)" % m.name)
        if not overwrite:
            files = [m.name for m in members if m.isfile()
                     and os.path.exists(os.path.join(home, m.name))]
            if files:
                raise BackupError("la session existe déjà sur cet hôte (%s) : --overwrite pour "
                                  "la remplacer" % ", ".join(files[:3]))
        if hasattr(tarfile, "data_filter"):   # Python ≥ 3.12 (et correctifs de 3.9+)
            tar.extractall(home, filter="data")
        else:                                  # chemins déjà contrôlés ci-dessus
            tar.extractall(home)
        restored = [m.name for m in members if m.isfile()]
    return restored


# --------------------------------------------------------------------------
# ligne de commande : ameesh session list|backup|restore
# --------------------------------------------------------------------------

USAGE = """\
ameesh session list <agent> [--host H]           copies de ses sessions dans la cible
ameesh session backup <agent> [--force]          copie de sa session courante, tout de suite
ameesh session restore <agent> [--session S] [--key K] [--home DOSSIER] [--overwrite]
                                                 restaure la dernière copie (ou K) dans le
                                                 dossier du compte de la session (ou DOSSIER)
"""


def _agent_home(cfg, row: dict) -> tuple[str, tuple[str, ...]]:
    from . import accounts, harnesses
    harness = row.get("harness") or ""
    descriptor = harnesses.get(harness)
    patterns = tuple(getattr(descriptor, "session_files", ()) or ())
    profile = accounts.by_name(accounts.profiles(cfg, harness), row.get("session_account"))
    if profile is not None:
        return profile.home(), patterns
    return os.path.abspath(os.path.expanduser(
        os.environ.get(accounts.config_env_of(harness)) or accounts.default_home_of(harness))), \
        patterns


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
    p.add_argument("--home")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(rest)
    cfg = load_config()
    try:
        conf = parse(cfg.session_backup)
    except BackupError as exc:
        print("ameesh session : %s" % exc, file=sys.stderr)
        return 2
    if conf is None:
        print("ameesh session : aucune cible de sauvegarde (clé `session_backup` de l'hôte)",
              file=sys.stderr)
        return 2
    try:
        if sub == "list":
            keys = [k for k in list_keys(conf, (args.host + "/") if args.host else "")
                    if k.split("/")[1:2] == [args.agent]]
            for k in keys:
                print(k)
            if not keys:
                print("aucune copie pour %s" % args.agent)
            return 0
        db = db_mod.connect(cfg)
        try:
            rows = db.query("SELECT harness, session_id, session_account FROM agent_registry "
                            "WHERE name = %s", (args.agent,))
            row = rows[0] if rows else None
        finally:
            db.close()
        if not row:
            print("ameesh session : agent inconnu %s" % args.agent, file=sys.stderr)
            return 2
        home, patterns = _agent_home(cfg, row)
        if sub == "backup":
            if not row.get("session_id"):
                print("ameesh session : %s n'a pas de session" % args.agent, file=sys.stderr)
                return 2
            r = backup(conf, host=cfg.host, agent=args.agent, home=home, patterns=patterns,
                       session=row["session_id"], state_dir=cfg.state_dir, force=args.force)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["status"] in ("copiee", "inchangee", "trop_tot") else 1
        if sub == "restore":
            session = args.session or row.get("session_id")
            key = args.key or latest(conf, agent=args.agent, session=session or "")
            if not key:
                print("ameesh session : aucune copie de %s (session %s)" % (args.agent, session),
                      file=sys.stderr)
                return 1
            files = restore(conf, key, args.home or home, overwrite=args.overwrite)
            print("restauré %s : %d fichier(s) sous %s" % (key, len(files), args.home or home))
            return 0
    except BackupError as exc:
        print("ameesh session : %s" % exc, file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2
