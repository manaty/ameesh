# SPDX-License-Identifier: AGPL-3.0-only
"""Dépôt de travail de l'exécuteur médié (lot L113).

L'appareil prêté n'a ni identifiant de forge, ni clone du dépôt. Le travail
passe par le serveur du mesh :

1. **à la prise du bail**, `GET /api/exec/v1/work/{agent}/bundle` rend
   l'archive tar du commit de départ : la branche de l'agent
   (`agent/<nom>`) si elle existe sur la forge, sinon la branche de base du
   dépôt. L'appareil en fait un dépôt git local dont le premier commit est
   **recréé à l'identique** des deux côtés (même arbre, auteur, date et
   message fixes) : son identifiant est annoncé par `X-Ameesh-Device-Base`
   et l'appareil le vérifie ;
2. **après chaque tour, et à la fin du bail**, l'appareil envoie le paquet
   git (`git bundle`) de ses commits depuis le dernier envoi
   (`POST /api/exec/v1/work/{agent}/bundle`, `X-Ameesh-Base`). Le serveur
   rejoue chaque commit sur son propre historique (même arbre, même auteur,
   même message ; le committer est l'exécuteur) et pousse la branche
   `agent/<nom>` avec ses propres identifiants, en avance rapide seulement
   (`--force-with-lease`) ;
3. **à la fin du bail**, l'appareil efface le dossier de travail.

Bornes : un paquet ≤ `contrat.MAX_BUNDLE_BYTES` ; historique linéaire (pas
de commit de fusion) ; pas de sous-module (un lien de sous-module ne passe
pas par une archive). Le bail (owner, epoch, en-têtes) est recontrôlé en
base à chaque appel : bail vivant, de cet exécuteur, sur son hôte.

Configuration du serveur (`ameesh serve --work-repos FICHIER`) :

    {"agents": {"inge-front": {"repo": "/srv/git/site.git", "base": "main"}},
     "default": {"repo": "git@forge:org/site.git", "base": "main"}}

`repo` est une URL ou un chemin que `git` du serveur sait joindre (ses
propres clés) ; le cache (`--work-cache`) garde un clone nu par dépôt.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from typing import Any, Callable, Mapping, Optional

from . import contrat
from .contrat import Fence

log = logging.getLogger("ameesh.exec.depot")

#: branche de l'agent sur la forge
BRANCH_PREFIX = "agent/"
#: identité et date fixes du commit de base (recréé à l'identique)
BASE_IDENTITY = {"GIT_AUTHOR_NAME": "ameesh", "GIT_AUTHOR_EMAIL": "ameesh@invalid",
                 "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_NAME": "ameesh",
                 "GIT_COMMITTER_EMAIL": "ameesh@invalid",
                 "GIT_COMMITTER_DATE": "@0 +0000"}
#: options git communes : ni signature, ni crochet, ni éditeur
GIT_OPTS = ("-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
            "-c", "core.autocrlf=false", "-c", "core.symlinks=true",
            "-c", "core.fileMode=true", "-c", "init.defaultBranch=work")
#: branche locale de l'appareil
DEVICE_BRANCH = "work"
#: méta-données de l'appareil, dans `.git`
DEVICE_META = "ameesh-depot.json"

_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class DepotError(RuntimeError):
    """Refus du dépôt de travail ; `code` est un code de `contrat.ERRORS`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def base_message(commit: str) -> str:
    return "ameesh : base %s" % commit


def _env(extra: Optional[Mapping[str, str]] = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k in ("GIT_SSH_COMMAND", "GIT_SSH")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra or {})
    return env


def git(cwd: str, *args: str, env: Optional[Mapping[str, str]] = None,
        input: Optional[bytes] = None, check: bool = True,
        timeout: float = 300.0) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *GIT_OPTS, *args], cwd=cwd, env=_env(env), input=input,
                          capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DepotError("internal", "git %s : %s" % (
            args[0], proc.stderr.decode("utf-8", "replace").strip()[:500]))
    return proc


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


# ==========================================================================
# serveur
# ==========================================================================

def tree_archive(repo: str, commit: str) -> bytes:
    """L'archive tar EXACTE de l'arbre de `commit` (sans les attributs
    `export-ignore` ou `export-subst` de `git archive`) : fichiers, modes
    (exécutable ou non) et liens symboliques. Un sous-module est refusé."""
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).stdout
    entries = []
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        meta, _, path = raw.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        if kind != "blob":
            raise DepotError("bad_args", "sous-module non pris en charge : %s"
                             % path.decode("utf-8", "replace"))
        entries.append((mode, sha, path.decode("utf-8", "surrogateescape")))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        if entries:
            batch = git(repo, "cat-file", "--batch",
                        input=("\n".join(sha for _m, sha, _p in entries) + "\n").encode()).stdout
            pos = 0
            for mode, sha, path in entries:
                end = batch.index(b"\n", pos)
                header = batch[pos:end].split()
                size = int(header[2])
                data = batch[end + 1:end + 1 + size]
                pos = end + 1 + size + 1
                info = tarfile.TarInfo(path)
                info.mtime = 0
                info.uname = info.gname = ""
                if mode == "120000":
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode("utf-8", "surrogateescape")
                    info.mode = 0o777
                    tar.addfile(info)
                else:
                    info.size = size
                    info.mode = 0o755 if mode == "100755" else 0o644
                    tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class RepoMap:
    """Quel dépôt (et quelle branche de base) pour quel agent."""

    def __init__(self, data: Mapping[str, Any]):
        self.agents = dict(data.get("agents") or {})
        self.default = data.get("default")

    @classmethod
    def from_file(cls, path: str) -> "RepoMap":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def get(self, agent: str) -> Optional[dict]:
        entry = self.agents.get(agent) or self.default
        if not entry or not entry.get("repo"):
            return None
        return {"repo": str(entry["repo"]), "base": str(entry.get("base") or "main")}


class WorkDepot:
    """Route `extra_routes["bundle"]` du serveur : rend l'archive, reçoit
    le paquet, pousse la branche de l'agent.

    `lease_check(principal, agent, owner, epoch) -> bool` : le bail est-il
    vivant, de cet exécuteur, sur son hôte (en base) ? `cache` : dossier des
    clones nus et de l'état par agent."""

    def __init__(self, repos: RepoMap, cache: str,
                 lease_check: Callable[[Any, str, str, int], bool]):
        self.repos = repos
        self.cache = cache
        self.lease_check = lease_check
        self._locks: dict = {}
        self._guard = threading.Lock()
        os.makedirs(os.path.join(cache, "repos"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(cache, "state"), mode=0o700, exist_ok=True)

    # -- outillage --------------------------------------------------------------
    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _mirror(self, repo: str) -> str:
        path = os.path.join(self.cache, "repos",
                            hashlib.sha256(repo.encode()).hexdigest()[:24] + ".git")
        if not os.path.isdir(path):
            tmp = path + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            git(self.cache, "init", "-q", "--bare", tmp)
            git(tmp, "remote", "add", "origin", repo)
            os.rename(tmp, path)
        git(path, "fetch", "-q", "--prune", "origin",
            "+refs/heads/*:refs/remotes/origin/*", timeout=600.0)
        return path

    def _state_path(self, agent: str) -> str:
        return os.path.join(self.cache, "state", agent + ".json")

    def _load(self, agent: str) -> dict:
        try:
            with open(self._state_path(agent), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, agent: str, state: Mapping) -> None:
        path = self._state_path(agent)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(path + ".tmp", path)

    @staticmethod
    def _rev(repo: str, ref: str) -> Optional[str]:
        proc = git(repo, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False)
        return _out(proc) if proc.returncode == 0 else None

    # -- route ----------------------------------------------------------------------
    def __call__(self, app, principal, method: str, sub: str, headers: Mapping[str, str],
                 body: bytes):
        from .serveur import Response, _error
        if principal.kind != "executor":
            return _error("token_invalid", "jeton d'accès attendu")
        agent = sub[len("/work/"):-len("/bundle")]
        h = {k.lower(): v for k, v in headers.items()}
        owner = h.get(contrat.HDR_LEASE_OWNER.lower(), "")
        epoch_text = h.get(contrat.HDR_LEASE_EPOCH.lower(), "")
        if not _AGENT_RE.match(agent) or not owner or not epoch_text.isdigit():
            return _error("bad_args", "agent, owner et epoch du bail attendus")
        fence = Fence(agent, owner, int(epoch_text))
        try:
            if not self.lease_check(principal, agent, owner, fence.epoch):
                return _error("forbidden_scope", "bail perdu ou d'un autre exécuteur")
            entry = self.repos.get(agent)
            if entry is None:
                return _error("op_not_allowed", "aucun dépôt de travail pour %s" % agent)
            with self._lock(agent):
                if method == "GET":
                    raw, extra = self.checkout(entry, fence, principal)
                    return Response(200, raw=raw, headers=dict(
                        extra, **{"Content-Type": "application/x-tar"}))
                if method == "POST":
                    base = h.get(contrat.HDR_BASE.lower(), "")
                    return Response(200, self.receive(entry, fence, principal, base, body))
                return _error("op_not_allowed", "méthode non servie")
        except DepotError as exc:
            log.warning("dépôt de travail %s (%s) : %s", agent, method, str(exc)[:300])
            return _error(exc.code if exc.code in contrat.ERRORS else "internal", str(exc))
        except subprocess.TimeoutExpired:
            return _error("unavailable", "git trop lent")

    # -- GET --------------------------------------------------------------------------
    def checkout(self, entry: Mapping, fence: Fence, principal) -> tuple:
        mirror = self._mirror(entry["repo"])
        branch = BRANCH_PREFIX + fence.agent
        remote_branch = self._rev(mirror, "refs/remotes/origin/" + branch)
        commit = remote_branch or self._rev(mirror, "refs/remotes/origin/" + entry["base"])
        if commit is None:
            raise DepotError("bad_args", "branche de base %s absente" % entry["base"])
        raw = tree_archive(mirror, commit)
        tree = _out(git(mirror, "rev-parse", commit + "^{tree}"))
        device_base = _out(git(mirror, "commit-tree", tree, "-m", base_message(commit),
                               env=BASE_IDENTITY))
        # le commit de base de l'appareil reste joignable : prérequis du paquet
        git(mirror, "update-ref", "refs/ameesh/device/%s" % fence.agent, device_base)
        self._save(fence.agent, {"epoch": fence.epoch, "owner": fence.owner,
                                 "repo": entry["repo"], "branch": branch,
                                 "remote_head": remote_branch or "",
                                 "map": {device_base: commit}})
        return raw, {contrat.HDR_COMMIT: commit, contrat.HDR_DEVICE_BASE: device_base,
                     contrat.HDR_BRANCH: branch}

    # -- POST ---------------------------------------------------------------------------
    def receive(self, entry: Mapping, fence: Fence, principal, base: str,
                bundle: bytes) -> dict:
        state = self._load(fence.agent)
        if state.get("epoch") != fence.epoch or state.get("repo") != entry["repo"]:
            raise DepotError("forbidden_scope", "aucune archive rendue sous ce bail")
        parent = (state.get("map") or {}).get(base)
        if not _SHA_RE.match(base or "") or parent is None:
            raise DepotError("idempotency_mismatch", "base inconnue du serveur : %s" % base)
        if not bundle:
            raise DepotError("bad_args", "paquet vide")
        mirror = self._mirror(entry["repo"])
        incoming = "refs/ameesh/incoming/%s" % fence.agent
        with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".bundle") as fh:
            fh.write(bundle)
            fh.flush()
            proc = git(mirror, "bundle", "verify", "-q", fh.name, check=False)
            if proc.returncode != 0:
                raise DepotError("bad_args", "paquet illisible ou prérequis absent : %s"
                                 % proc.stderr.decode("utf-8", "replace").strip()[:300])
            git(mirror, "fetch", "-q", "--no-tags", fh.name,
                "+refs/heads/%s:%s" % (DEVICE_BRANCH, incoming))
        head = _out(git(mirror, "rev-parse", incoming))
        if head == base:
            return {"schema": contrat.SCHEMA_BUNDLE, "branch": state["branch"],
                    "commit": parent, "device_head": head, "commits": 0}
        if git(mirror, "merge-base", "--is-ancestor", base, head, check=False).returncode:
            raise DepotError("bad_args", "le paquet ne descend pas de la base %s" % base)
        commits = _out(git(mirror, "rev-list", "--reverse", "--parents",
                           "%s..%s" % (base, head))).splitlines()
        new = parent
        for line in commits:
            shas = line.split()
            if len(shas) != 2:
                raise DepotError("bad_args", "historique linéaire attendu (fusion %s)" % shas[0])
            new = self._replay(mirror, shas[0], new, principal)
        lease = "refs/heads/%s:%s" % (state["branch"], state.get("remote_head") or "")
        proc = git(mirror, "push", "-q", "--force-with-lease=" + lease, "origin",
                   "%s:refs/heads/%s" % (new, state["branch"]), check=False, timeout=600.0)
        if proc.returncode != 0:
            raise DepotError("unavailable", "poussée de %s refusée : %s" % (
                state["branch"], proc.stderr.decode("utf-8", "replace").strip()[:300]))
        state["map"] = {head: new}   # la base suivante est la tête reçue
        state["remote_head"] = new
        git(mirror, "update-ref", "refs/ameesh/device/%s" % fence.agent, head)
        self._save(fence.agent, state)
        log.info("dépôt de travail : %s ← %d commit(s), %s", state["branch"], len(commits),
                 new[:12])
        return {"schema": contrat.SCHEMA_BUNDLE, "branch": state["branch"], "commit": new,
                "device_head": head, "commits": len(commits)}

    @staticmethod
    def _replay(mirror: str, commit: str, parent: str, principal) -> str:
        fmt = "%T%x00%an%x00%ae%x00%ad%x00%cd%x00%B"
        raw = git(mirror, "show", "-s", "--date=raw", "--format=" + fmt, commit).stdout
        tree, an, ae, ad, cd, message = raw.decode("utf-8", "surrogateescape").split("\0", 5)
        env = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
               "GIT_COMMITTER_NAME": "ameesh (exécuteur %s)" % principal.executor_id,
               "GIT_COMMITTER_EMAIL": "exec-%s@ameesh.invalid" % principal.executor_id,
               "GIT_COMMITTER_DATE": cd}
        return _out(git(mirror, "commit-tree", tree, "-p", parent, "-F", "-",
                        env=env, input=message.rstrip("\n").encode("utf-8", "surrogateescape")
                        + b"\n"))


def lease_checker(pool) -> Callable[[Any, str, str, int], bool]:
    """Le bail (agent, owner, epoch) est-il vivant, de cet exécuteur, sur
    son hôte ? Lu en base par le réservoir du serveur."""
    def check(principal, agent: str, owner: str, epoch: int) -> bool:
        if not owner.startswith("exec:%s:" % principal.executor_id):
            return False
        with pool.connection() as conn:
            rows = conn.query(
                "SELECT 1 FROM agent_registry WHERE name = %s AND lease_owner = %s"
                "   AND lease_epoch = %s AND lease_expires_at > clock_timestamp()"
                "   AND host = %s", (agent, owner, int(epoch), principal.host))
        return bool(rows)
    return check


# ==========================================================================
# appareil
# ==========================================================================

def _meta_path(workdir: str) -> str:
    return os.path.join(workdir, ".git", DEVICE_META)


def _read_meta(workdir: str) -> dict:
    with open(_meta_path(workdir), encoding="utf-8") as fh:
        return json.load(fh)


def _write_meta(workdir: str, meta: Mapping) -> None:
    with open(_meta_path(workdir), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def _safe_extract(raw: bytes, dest: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        members = tar.getmembers()
        for m in members:
            parts = m.name.split("/")
            if m.name.startswith("/") or ".." in parts or parts[0] == ".git" \
                    or not (m.isfile() or m.issym() or m.isdir()):
                raise DepotError("bad_args", "entrée d'archive refusée : %s" % m.name)
        try:
            tar.extractall(dest, members=members, filter="tar")
        except TypeError:  # Python < 3.12 : pas de filtre
            tar.extractall(dest, members=members)


def checkout(transport, fence: Fence, workdir: str, *, author: Optional[str] = None) -> dict:
    """Prise du bail : archive du serveur → dépôt git local dans `workdir`
    (créé ; doit être absent ou vide). Rend les méta-données (commit du
    serveur, base de l'appareil, branche)."""
    raw, headers = transport.get_work(fence)
    h = {k.lower(): v for k, v in headers.items()}
    commit = h.get(contrat.HDR_COMMIT.lower(), "")
    expected = h.get(contrat.HDR_DEVICE_BASE.lower(), "")
    branch = h.get(contrat.HDR_BRANCH.lower(), BRANCH_PREFIX + fence.agent)
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    if os.listdir(workdir):
        raise DepotError("bad_args", "dossier de travail non vide : %s" % workdir)
    git(workdir, "init", "-q")
    _safe_extract(raw, workdir)
    git(workdir, "add", "-A", "-f")
    tree = _out(git(workdir, "write-tree"))
    base = _out(git(workdir, "commit-tree", tree, "-m", base_message(commit), env=BASE_IDENTITY))
    if expected and base != expected:
        raise DepotError("internal", "commit de base différent du serveur (%s ≠ %s) : arbre "
                         "non reproduit" % (base[:12], expected[:12]))
    git(workdir, "update-ref", "refs/heads/" + DEVICE_BRANCH, base)
    git(workdir, "symbolic-ref", "HEAD", "refs/heads/" + DEVICE_BRANCH)
    name = author or fence.agent
    git(workdir, "config", "user.name", name)
    git(workdir, "config", "user.email", "%s@ameesh.invalid" % name)
    meta = {"agent": fence.agent, "epoch": fence.epoch, "commit": commit, "base": base,
            "pushed": base, "branch": branch}
    _write_meta(workdir, meta)
    return meta


def push(transport, fence: Fence, workdir: str, *, commit_pending: bool = False) -> Optional[dict]:
    """Envoie les commits de l'appareil depuis le dernier envoi ; rend la
    réponse du serveur, ou None s'il n'y a rien à envoyer. `commit_pending`
    (fin du bail) : le travail non commité l'est d'abord."""
    meta = _read_meta(workdir)
    if commit_pending:
        git(workdir, "add", "-A")
        if git(workdir, "diff", "--cached", "--quiet", check=False).returncode == 1:
            git(workdir, "commit", "-q", "--no-verify", "-m",
                "ameesh : travail non commité à la fin du bail")
    head = _out(git(workdir, "rev-parse", "refs/heads/" + DEVICE_BRANCH))
    if head == meta["pushed"]:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "travail.bundle")
        git(workdir, "bundle", "create", "-q", path, "refs/heads/" + DEVICE_BRANCH,
            "^" + meta["pushed"])
        with open(path, "rb") as fh:
            data = fh.read()
    reply = transport.put_work(fence, meta["pushed"], data)
    meta["pushed"] = reply.get("device_head") or head
    meta["server_commit"] = reply.get("commit")
    _write_meta(workdir, meta)
    return reply


def wipe(workdir: str) -> None:
    """Fin du bail : le dossier de travail est effacé."""
    shutil.rmtree(workdir, ignore_errors=True)


__all__ = ["DepotError", "RepoMap", "WorkDepot", "checkout", "lease_checker", "push",
           "tree_archive", "wipe"]
