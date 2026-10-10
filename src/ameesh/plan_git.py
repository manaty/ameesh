# SPDX-License-Identifier: AGPL-3.0-only
"""Fusion d'un lot constatée par le CONTENU (lot L29), dans un clone git local.

    ameesh work sync-merges --git-dir D [--target origin/main] [--repo owner/repo]
                            [--dry-run] [--json]

Le nom d'un lot change (branche renommée entre gel et fusion), la fusion se
fait par rebase, squash ou cherry-pick sans commit de fusion reconnaissable :
le nom ne prouve rien. Seul le **dernier gel** d'un lot fait autorité (le
jalon `frozen` le plus récent de L10, avec son commit) : un gel antérieur
déjà fusionné ne prouve rien sur le travail courant. Pour chaque lot ouvert
qui a un tel gel, la fusion est constatée, dans cet ordre :

1. **ancêtre** : le commit du dernier gel est ancêtre de la branche cible ;
2. **patch-id exact** (rebase, cherry-pick) : chaque commit du gel absent
   de la cible (`cible..gel`, hors fusions) a son `git patch-id --verbatim`
   parmi ceux des commits de la cible depuis leur base commune ; ou
   (**squash**) le diff entier du gel (base..gel) a le patch-id d'un commit
   de la cible. `--verbatim` ne normalise AUCUN espace (`--stable` ignore les
   espaces, même dans une chaîne : `"a b"` et `"ab"` y sont égaux) ; seuls
   les numéros de ligne des en-têtes de bloc sont ignorés. Un git sans
   `--verbatim` (< 2.40) ne prouve que par l'ancêtre : faux négatif plutôt
   que faux positif ;
3. à défaut, si `--repo` est donné, **la PR liée est fusionnée**
   (`plan_github.sync_github`, lecture seule de GitHub).

La fermeture est conditionnée en base au gel examiné : si un nouveau gel est
déclaré entre l'examen et l'écriture, rien n'est fermé (`refrozen`).

Lecture seule du dépôt (`git merge-base`, `rev-list`, `log -p`, `patch-id`) ;
aucun `fetch` : la cible est lue telle que le clone la connaît. Un lot
absorbé par un autre ou remplacé se clôt par `ameesh work close <id>
--superseded-by <id>` ; jamais de réouverture d'un lot fermé.
"""
from __future__ import annotations

import re

from . import storage
from . import work as work_mod
from .canon import GitError, _git

DEFAULT_TARGET = "origin/main"
#: commits de la cible examinés au plus depuis la base commune (borne de coût)
MAX_TARGET_COMMITS = 5000
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class MergeProbeError(RuntimeError):
    """Dépôt ou cible illisible : message actionnable."""


class _NoVerbatim(Exception):
    """`git patch-id --verbatim` indisponible : aucune preuve par le contenu."""


#: le patch-id exact : aucun espace normalisé (voir le docstring du module)
PATCH_ID = ("patch-id", "--verbatim")


def _patch_id_run(repo: str, data: bytes):
    proc = _git(repo, *PATCH_ID, stdin=data)
    if proc.returncode != 0:
        raise _NoVerbatim(proc.stderr.decode("utf-8", "replace").strip())
    return proc


def _out(repo: str, *args: str) -> str | None:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _commit(repo: str, rev: str) -> str | None:
    return _out(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")


def _patch_ids(repo: str, *log_args: str) -> dict[str, str]:
    """{patch-id: commit} des commits non-fusions de `git log -p <log_args>`."""
    log = _git(repo, "log", "-p", "--no-merges", "--no-color", "--format=commit %H",
               *log_args)
    if log.returncode != 0:
        raise MergeProbeError("git log %s : %s" % (" ".join(log_args),
                                                   log.stderr.decode("utf-8", "replace").strip()))
    if not log.stdout.strip():
        return {}
    return _parse_patch_ids(_patch_id_run(repo, log.stdout))


def _parse_patch_ids(proc) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.setdefault(parts[0], parts[1])
    return out


def _diff_patch_id(repo: str, base: str, head: str) -> str | None:
    diff = _git(repo, "diff", "--no-color", base, head)
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    ids = _parse_patch_ids(_patch_id_run(
        repo, b"commit " + head.encode() + b"\n" + diff.stdout))
    return next(iter(ids), None)


def probe(repo: str, sha: str, target: str) -> dict:
    """Le commit `sha` est-il entré dans `target` ? Rend `{merged, how, ref, detail}`.

    `how` : ancestor | patch-id | squash ; `ref` : le commit de la cible qui
    porte le contenu (le gel lui-même s'il est ancêtre)."""
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("cible %r introuvable dans %s" % (target, repo))
    if not _SHA_RE.match(sha or ""):
        return {"merged": False, "how": None, "ref": None, "detail": "commit illisible"}
    frozen = _commit(repo, sha)
    if frozen is None:
        return {"merged": False, "how": None, "ref": None,
                "detail": "commit %s absent du dépôt" % sha[:12]}
    if _git(repo, "merge-base", "--is-ancestor", frozen, head).returncode == 0:
        return {"merged": True, "how": "ancestor", "ref": frozen,
                "detail": "%s ancêtre de %s" % (frozen[:12], target)}
    base = _out(repo, "merge-base", frozen, head)
    if not base:
        return {"merged": False, "how": None, "ref": None, "detail": "aucune base commune"}
    try:
        return _by_patch_id(repo, frozen, head, base, target)
    except _NoVerbatim as exc:
        return {"merged": False, "how": None, "ref": None,
                "detail": "git patch-id --verbatim indisponible (git >= 2.40 requis : %s) : "
                          "seul l'ancêtre prouve la fusion" % (exc or "refus")}


def _by_patch_id(repo: str, frozen: str, head: str, base: str, target: str) -> dict:
    on_target = _patch_ids(repo, "--max-count=%d" % MAX_TARGET_COMMITS, "%s..%s" % (base, head))
    mine = _patch_ids(repo, "%s..%s" % (head, frozen))
    if mine and all(pid in on_target for pid in mine):
        # `git log` rend les commits du plus récent au plus ancien : le premier
        # patch-id est celui du dernier commit du gel
        last = on_target[next(iter(mine))]
        return {"merged": True, "how": "patch-id", "ref": last,
                "detail": "%d commit(s) du gel retrouvé(s) par patch-id dans %s"
                          % (len(mine), target)}
    squash = _diff_patch_id(repo, base, frozen)
    if squash and squash in on_target:
        return {"merged": True, "how": "squash", "ref": on_target[squash],
                "detail": "diff du gel retrouvé en un commit de %s (squash)" % target}
    missing = sum(1 for pid in mine if pid not in on_target)
    return {"merged": False, "how": None, "ref": None,
            "detail": "%d/%d commit(s) du gel absents de %s" % (missing, len(mine), target)}


def latest_freeze(rows: list[dict]) -> dict | None:
    """Le dernier gel d'un lot (jalon `frozen` le plus récent), ou None.

    Lui seul fait autorité : s'il n'a pas de commit, rien n'est prouvable."""
    freezes = [r for r in rows if r.get("kind") == "frozen"]
    if not freezes:
        return None
    return max(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))


def sync_merges(db, repo_dir: str, *, target: str = DEFAULT_TARGET, dry_run: bool = False,
                limit: int = 1000) -> dict:
    """Ferme les lots dont le contenu gelé est entré dans `target`."""
    try:
        if _commit(repo_dir, target) is None:
            raise MergeProbeError("cible %r introuvable dans %s" % (target, repo_dir))
    except GitError as exc:
        raise MergeProbeError(str(exc)) from exc
    st = storage.of(db)
    rows = [r for r in st.work.items(state=None, assignee=None, limit=limit)
            if r["state"] not in work_mod.MERGED_STATES and r["state"] != "closed"]
    declared: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    results: list[dict] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        freeze = latest_freeze(declared.get(int(row["id"]), []))
        sha = ((freeze or {}).get("sha") or "").strip()
        entry = {"work_item": int(row["id"]), "package": row.get("package_id"),
                 "state": row["state"], "result": "no-commit", "how": None, "ref": None,
                 "detail": ("dernier gel sans commit (jalon frozen --sha)" if freeze
                            else "aucun gel déclaré (jalon frozen --sha)")}
        if sha:
            try:
                found = probe(repo_dir, sha, target)
            except GitError as exc:
                raise MergeProbeError(str(exc)) from exc
            entry.update(how=found["how"], ref=found["ref"], detail=found["detail"],
                         sha=sha, result="open")
        if entry["how"]:
            if dry_run:
                entry["result"] = "would-merge"
            else:
                done = work_mod.close_merged(
                    db, int(row["id"]), sha=entry["ref"] or "", actor="git:%s" % target,
                    source="contenu (%s)" % entry["how"], frozen_id=int(freeze["id"]))
                entry["result"] = done["result"]
                entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
        results.append(entry)
    return {"git_dir": repo_dir, "target": target, "dry_run": bool(dry_run),
            "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged")}


# --------------------------------------------------------------------------
# L118 : fusion d'une BRANCHE constatée, avec ou sans PR
# --------------------------------------------------------------------------
#
# Une équipe qui fusionne directement sur sa branche cible ne passe ni par
# une PR (`sync-github`) ni par un gel déclaré (`sync-merges`) : ses lots
# restaient ouverts après la fusion. Un lot peut porter sa branche
# (`work add|assign --branch`, ou déduite d'un message `mail send --lot` qui
# cite une branche `agent/…`) ; le relevé périodique de l'exécuteur
# (`sync_branches`) la cherche dans le dépôt de l'assigné — son dossier de
# travail au registre, posé par `policy.work_dirs`/`work_roots` du canon
# (L35) : tous les worktrees d'un dépôt partagent ses références.
#
# Une branche tout juste créée depuis sa cible en est déjà « ancêtre » : ce
# n'est pas une fusion. La fusion est constatée, dans cet ordre :
#
# 1. la pointe de la branche est ancêtre de la cible SANS être sur sa chaîne
#    de premiers parents (un commit de fusion l'y a fait entrer) ;
# 2. la pointe est sur cette chaîne (avance rapide, ou branche sans travail) :
#    le dernier commit vu EN AVANCE par un relevé précédent (`branch_head`)
#    est entré dans la cible (ancêtre, patch-id ou squash : `probe`) ;
# 3. branche en avance : son contenu est entré par patch-id ou squash
#    (`probe`) ;
# 4. branche supprimée : le dernier commit vu (`probe`), sinon un commit de
#    fusion de la cible dont le message cite la branche (nom entier).
#
# Lecture seule du dépôt, aucun `fetch`. La cible : celle du lot, sinon
# `git config ameesh.target` du dépôt, sinon `origin/HEAD`, sinon main.

#: une branche citée dans un message (`agent/…`)
CITED_BRANCH_RE = re.compile(r"(?<![\w./-])(agent/[A-Za-z0-9._/-]*[A-Za-z0-9_-])")
#: nom de référence admis pour une branche ou une cible
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: caractères qui prolongent un nom de branche (citation à nom entier)
_REF_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
#: commits de fusion lus au plus pour une citation
MAX_MERGE_MENTIONS = 200
#: lots examinés au plus par relevé
MAX_BRANCH_LOTS = 1000
#: clé de configuration git du dépôt qui nomme la cible par défaut
TARGET_GIT_KEY = "ameesh.target"
DEFAULT_TARGETS = ("origin/main", "main", "origin/master", "master")


def check_ref(name: str, what: str = "branche") -> str:
    """Un nom de branche (ou de cible) admissible, sinon ValueError."""
    text = (name or "").strip()
    if not _REF_RE.match(text) or ".." in text or "//" in text or text.endswith(
            ("/", ".", ".lock")) or "@{" in text:
        raise ValueError("%s invalide : %r (lettres, chiffres, . _ / -)" % (what, name))
    return text


def cited_branch(text: str) -> str | None:
    """La branche `agent/…` citée par un message, si elle est UNIQUE (deux
    branches citées : on ne devine pas)."""
    found = {m.rstrip(".") for m in CITED_BRANCH_RE.findall(text or "")}
    found = {b for b in found if b != "agent/"}
    return next(iter(found)) if len(found) == 1 else None


def default_target(repo: str) -> str | None:
    """La cible par défaut du dépôt : `git config ameesh.target`, sinon la
    branche par défaut du dépôt distant (`origin/HEAD`), sinon main/master."""
    configured = _out(repo, "config", "--get", TARGET_GIT_KEY)
    if configured and _commit(repo, configured):
        return configured
    head = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and _commit(repo, head):
        return head
    for candidate in DEFAULT_TARGETS:
        if _commit(repo, candidate):
            return candidate
    return None


def _tip(repo: str, branch: str) -> str | None:
    for ref in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        found = _commit(repo, ref)
        if found:
            return found
    return None


def _on_first_parent(repo: str, commit: str, head: str) -> bool:
    chain = _out(repo, "rev-list", "--first-parent", "--max-count=%d" % MAX_TARGET_COMMITS,
                 head)
    return bool(chain) and commit in chain.split()


def _mentions(message: str, branch: str) -> bool:
    start = message.find(branch)
    while start >= 0:
        end = start + len(branch)
        before = message[start - 1] if start else " "
        after = message[end] if end < len(message) else " "
        if before not in _REF_CHARS and (after not in _REF_CHARS or (
                after == "." and (end + 1 >= len(message)
                                  or message[end + 1] not in _REF_CHARS))):
            return True
        start = message.find(branch, start + 1)
    return False


def merge_mentioning(repo: str, branch: str, head: str,
                     since_ts: float | None = None) -> str | None:
    """Le plus récent commit de FUSION de `head` dont le message cite la
    branche (nom entier : `agent/x` ne cite pas `agent/x-2`), ou None. Un
    commit ordinaire qui cite la branche (compte rendu de revue) ne compte pas."""
    args = ["log", "--merges", "--format=%H%x00%B%x1e", "-F", "--grep=%s" % branch,
            "--max-count=%d" % MAX_MERGE_MENTIONS]
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    out = _out(repo, *args, head)
    for record in (out or "").split("\x1e"):
        sha, _sep, message = record.strip().partition("\x00")
        if sha and _mentions(message, branch):
            return sha
    return None


def branch_probe(repo: str, branch: str, target: str, *, seen_head: str | None = None,
                 since_ts: float | None = None) -> dict:
    """La branche est-elle entrée dans `target` ? Rend `{merged, how, ref, tip,
    ahead, detail}` ; `ahead` : la pointe a du travail absent de la cible
    (l'appelant la retient comme `branch_head`)."""
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("cible %r introuvable dans %s" % (target, repo))
    tip = _tip(repo, branch)
    out = {"merged": False, "how": None, "ref": None, "tip": tip, "ahead": False}

    def seen() -> dict | None:
        if not seen_head:
            return None
        found = probe(repo, seen_head, target)
        if found["merged"]:
            return dict(out, merged=True, how="vu-" + found["how"], ref=found["ref"],
                        detail="dernier commit vu en avance (%s) : %s"
                               % (seen_head[:12], found["detail"]))
        return None

    def mention(why: str) -> dict:
        sha = merge_mentioning(repo, branch, head, since_ts)
        if sha:
            return dict(out, merged=True, how="message", ref=sha,
                        detail="commit de fusion %s de %s qui cite %s" % (sha[:12], target,
                                                                          branch))
        return dict(out, detail=why)

    if tip is None:
        return seen() or mention("branche %s introuvable (ni locale ni origin/)" % branch)
    if _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0:
        if not _on_first_parent(repo, tip, head):
            return dict(out, merged=True, how="ancestor", ref=tip,
                        detail="%s (%s) entrée dans %s par un commit de fusion"
                               % (branch, tip[:12], target))
        return seen() or mention("%s n'a aucun travail propre : sa pointe %s est sur la "
                                 "ligne de %s" % (branch, tip[:12], target))
    out["ahead"] = True
    found = probe(repo, tip, target)
    if found["merged"]:
        return dict(out, merged=True, how=found["how"], ref=found["ref"],
                    detail="%s : %s" % (branch, found["detail"]))
    return dict(out, detail="%s en avance sur %s (%s)" % (branch, target, tip[:12]))


def sync_branches(db, *, host: str | None = None, agents=None, dry_run: bool = False,
                  actor: str = "", limit: int = MAX_BRANCH_LOTS) -> dict:
    """L118 : ferme les lots ouverts dont la branche est entrée dans sa cible.

    Le dépôt d'un lot est le dossier de travail de son assigné au registre ;
    avec `host`, seuls les assignés de cet hôte sont examinés (les dossiers
    des autres hôtes n'y sont pas), avec `agents`, seulement ceux-là. La
    fermeture passe par `work.close_merged` (idempotente, jamais de
    réouverture) et s'écrit dans le fil du lot. Rend le compte rendu."""
    import os

    from . import fil, registry

    st = storage.of(db)
    results: list[dict] = []
    for row in st.work.open_with_branch(limit):
        assignee = (row.get("assignee") or "").strip()
        entry = {"work_item": int(row["id"]), "branch": row["branch"],
                 "target": row.get("branch_target"), "assignee": assignee or None,
                 "result": "skipped", "how": None, "ref": None, "detail": ""}
        if agents is not None and assignee not in agents:
            continue
        reg = registry.get(db, assignee) if assignee and ":" not in assignee else None
        if reg is None:
            if host is None:
                entry["detail"] = "aucun agent assigné : dépôt inconnu"
                results.append(entry)
            continue
        if host is not None and (reg.get("host") or "") != host:
            continue
        repo = os.path.expanduser(reg.get("cwd") or "")
        if not repo or not os.path.isdir(repo):
            entry.update(result="no-repo", detail="dossier de travail de %s absent (%s)"
                         % (assignee, repo or "non déclaré"))
            results.append(entry)
            continue
        try:
            target = row.get("branch_target") or default_target(repo)
            if not target:
                entry.update(result="no-target", detail="aucune cible : `git config %s "
                             "<branche>` dans %s, ou --target" % (TARGET_GIT_KEY, repo))
                results.append(entry)
                continue
            entry["target"] = target
            found = branch_probe(repo, row["branch"], target, seen_head=row.get("branch_head"),
                                 since_ts=row.get("created_ts"))
        except (MergeProbeError, GitError) as exc:
            entry.update(result="error", detail=str(exc))
            results.append(entry)
            continue
        entry.update(how=found["how"], ref=found["ref"], detail=found["detail"], result="open",
                     repo=repo)
        if not found["merged"]:
            if found["ahead"] and found["tip"] and found["tip"] != row.get("branch_head") \
                    and not dry_run:
                st.work.set_branch_head(int(row["id"]), row["branch"], found["tip"])
            results.append(entry)
            continue
        if dry_run:
            entry["result"] = "would-merge"
            results.append(entry)
            continue
        done = work_mod.close_merged(
            db, int(row["id"]), sha=found["ref"] or "", actor=actor or "git:%s" % target,
            source="branche %s (%s)" % (row["branch"], found["how"]))
        entry.update(result=done["result"], detail="%s — %s" % (found["detail"], done["detail"]))
        if done["result"] == "merged":
            text = ("Lot #%d « %s » livré : la branche %s est entrée dans %s (%s, commit %s). "
                    "Fermé par le relevé des branches de l'exécuteur."
                    % (int(row["id"]), row.get("title") or "", row["branch"], target,
                       found["how"], (found["ref"] or "?")[:12]))
            fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
                       text=text, project=fil.project_for(db.cfg, fil.agent_project(reg)),
                       lot=str(row["id"]), meta={"kind": "event"})
        results.append(entry)
    return {"host": host, "dry_run": bool(dry_run), "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged")}
