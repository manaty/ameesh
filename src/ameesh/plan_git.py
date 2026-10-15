# SPDX-License-Identifier: AGPL-3.0-only
"""Fusion d'un lot constatée par le CONTENU (lot L29), dans un clone git local.

    ameesh work sync-merges --git-dir D [--target T] [--repo owner/repo]
                            [--dry-run] [--json]

Sans `--target` (#1005), la cible est celle du lot (`branch_target`), sinon
celle du dépôt (`git config ameesh.target`), sinon sa branche par défaut
(`origin/HEAD`, puis main/master) ; quand rien n'est déclaré et qu'elle ne
porte pas le gel, les refs d'intégration reconnues (`develop`, `main`,
`master`, `release/…`) sont essayées et la fermeture nomme celle qui a livré.

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

import os
import re
import shutil
import tempfile

from . import storage
from . import work as work_mod
from .canon import GitError, _git

#: cible historique du relevé de contenu (`--target` explicite) ; sans lui,
#: c'est la cible déclarée du lot ou du dépôt qui fait foi (#1005)
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
        raise MergeProbeError("git log %s: %s" % (" ".join(log_args),
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


def _applies(repo: str, tree: str, patch: bytes, *, reverse: bool = False) -> bool:
    """Le diff `patch` s'applique (ou se défait, `reverse`) sur l'arbre `tree`.

    L'arbre est porté par un index temporaire (`GIT_INDEX_FILE`) : ni le
    worktree ni l'index réels du dépôt ne sont lus ou écrits. Un arbre
    illisible rend False — l'appelant ne conclut alors pas au retrait."""
    scratch = tempfile.mkdtemp(prefix="ameesh-apply-")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        if _git(repo, "read-tree", tree, env=env).returncode != 0:
            return False
        args = ["apply", "--cached", "--check"]
        if reverse:
            args.append("--reverse")
        return _git(repo, *args, "-", stdin=patch, env=env).returncode == 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _introduced_range(repo: str, sha: str, head: str) -> tuple[str, str] | None:
    """Le gel et sa base commune avec la cible avant sa première introduction.

    Un gel vide (`commit --allow-empty`) ne porte aucun diff propre : la
    preuve de contenu du lot part de cette base, sans les autres membres de
    la fusion (#1776). None quand l'entrée n'est pas une fusion (avance
    rapide), quand `sha` n'est pas ancêtre de `head`, ou quand git ne peut
    pas la lire — un doute ne conclut jamais au retrait."""
    if _git(repo, "merge-base", "--is-ancestor", sha, head).returncode != 0:
        return None
    out = _out(repo, "rev-list", "--topo-order", "--reverse", "--merges",
               "--ancestry-path", "%s..%s" % (sha, head))
    if not out:
        return None
    # Traverser aussi les branches de paquet : --first-parent masque leur
    # fusion du lot. La première entrée topologique par un parent secondaire
    # isole le lot, plutôt que le diff de tous les membres du paquet.
    for entry in out.split():
        parents = (_out(repo, "rev-list", "--parents", "-n", "1", entry) or "").split()
        if len(parents) >= 3 and _git(
                repo, "merge-base", "--is-ancestor", sha, parents[1]).returncode == 1:
            base = _out(repo, "merge-base", parents[1], sha)
            return (base, sha) if base else None
    return None


def _content_gone(repo: str, sha: str, head: str) -> str | None:
    """`sha` est ancêtre de `head`, mais son contenu n'y est plus : la raison,
    sinon None.

    Une composition retirée du paquet (revert) laisse le commit ancêtre et
    défait son diff : l'ascendance seule ne prouve alors plus rien (constat
    qa-01, #498 / 043e14d8e). Le contenu est jugé là quand le diff du gel se
    défait sur l'arbre de la cible, et retiré quand il s'y réapplique
    proprement. Un gel vide ne porte aucun diff : sa preuve est le delta de
    son entrée dans la cible (#1776). Arbres évolués (les deux échouent) :
    seuls les fichiers ajoutés par le lot et disparus, ou supprimés et
    revenus, prouvent le retrait — un doute ne conclut jamais au retrait."""
    parent = _out(repo, "rev-parse", "--verify", "--quiet", sha + "^1")
    if parent is None:
        return None
    proof, proof_parent = sha, parent
    diff = _git(repo, "diff", "--no-color", "--binary", proof_parent, proof)
    if diff.returncode != 0:
        return None
    if not diff.stdout.strip():
        # gel vide : isoler son contenu à son entrée, y compris dans un paquet.
        entry = _introduced_range(repo, sha, head)
        if entry is None:
            return None
        proof_parent, proof = entry
        diff = _git(repo, "diff", "--no-color", "--binary", proof_parent, proof)
        if diff.returncode != 0 or not diff.stdout.strip():
            return None
    forward = _applies(repo, head, diff.stdout)
    reverse = _applies(repo, head, diff.stdout, reverse=True)
    if forward and not reverse:
        return "its diff applies to the tree again"
    if reverse and not forward:
        return None
    # les deux échouent (arbres évolués) : seuls les chemins ajoutés disparus
    # ou supprimés revenus prouvent le retrait
    status = _out(repo, "diff", "--name-status", "-z", proof_parent, proof)
    fields = iter((status or "").split("\0"))
    for kind in fields:
        path = next(fields, "")
        if kind.startswith(("R", "C")):
            next(fields, None)       # les renommages/copies portent deux chemins
        if kind not in ("A", "D") or not path:
            continue
        present = _out(repo, "rev-parse", "--verify", "--quiet",
                       "%s:%s" % (head, path)) is not None
        if kind == "A" and not present:
            return "the file %s it added is no longer in the tree" % path
        if kind == "D" and present:
            return "the file %s it deleted is back in the tree" % path
    return None


def probe(repo: str, sha: str, target: str) -> dict:
    """Le commit `sha` est-il entré dans `target` ? Rend `{merged, how, ref, detail}`.

    `how` : ancestor | patch-id | squash ; `ref` : le commit de la cible qui
    porte le contenu (le gel lui-même s'il est ancêtre). Un ancêtre dont le
    contenu a été retiré (revert d'une composition) n'est pas fusionné."""
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    if not _SHA_RE.match(sha or ""):
        return {"merged": False, "how": None, "ref": None, "detail": "unreadable commit"}
    frozen = _commit(repo, sha)
    if frozen is None:
        return {"merged": False, "how": None, "ref": None,
                "detail": "commit %s not in the repository" % sha[:12]}
    if _git(repo, "merge-base", "--is-ancestor", frozen, head).returncode == 0:
        gone = _content_gone(repo, frozen, head)
        if gone:
            return {"merged": False, "how": None, "ref": None,
                    "detail": "%s is an ancestor of %s but its content is no longer in the "
                              "tree (composition reverted): %s" % (frozen[:12], target, gone)}
        return {"merged": True, "how": "ancestor", "ref": frozen,
                "detail": "%s is an ancestor of %s" % (frozen[:12], target)}
    base = _out(repo, "merge-base", frozen, head)
    if not base:
        return {"merged": False, "how": None, "ref": None, "detail": "no common base"}
    try:
        return _by_patch_id(repo, frozen, head, base, target)
    except _NoVerbatim as exc:
        return {"merged": False, "how": None, "ref": None,
                "detail": "git patch-id --verbatim unavailable (git >= 2.40 required: %s): "
                          "only ancestry proves the merge" % (exc or "refused")}


def _by_patch_id(repo: str, frozen: str, head: str, base: str, target: str) -> dict:
    on_target = _patch_ids(repo, "--max-count=%d" % MAX_TARGET_COMMITS, "%s..%s" % (base, head))
    mine = _patch_ids(repo, "%s..%s" % (head, frozen))
    if mine and all(pid in on_target for pid in mine):
        # `git log` rend les commits du plus récent au plus ancien : le premier
        # patch-id est celui du dernier commit du gel
        last = on_target[next(iter(mine))]
        return {"merged": True, "how": "patch-id", "ref": last,
                "detail": "%d frozen commit(s) found by patch-id in %s"
                          % (len(mine), target)}
    squash = _diff_patch_id(repo, base, frozen)
    if squash and squash in on_target:
        return {"merged": True, "how": "squash", "ref": on_target[squash],
                "detail": "frozen diff found as one commit of %s (squash)" % target}
    missing = sum(1 for pid in mine if pid not in on_target)
    return {"merged": False, "how": None, "ref": None,
            "detail": "%d/%d frozen commit(s) missing from %s" % (missing, len(mine), target)}


def latest_freeze(rows: list[dict]) -> dict | None:
    """Le dernier gel d'un lot (jalon `frozen` le plus récent), ou None.

    Lui seul fait autorité : s'il n'a pas de commit, rien n'est prouvable."""
    freezes = [r for r in rows if r.get("kind") == "frozen"]
    if not freezes:
        return None
    return max(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))


def _content_refs(row: dict, target: str | None, *, declared_repo: str | None,
                  default: str | None, candidates: list[str]) -> list[tuple[str, bool, bool]]:
    """#1005 : les refs où chercher le contenu gelé d'un lot, dans l'ordre, et
    `(ref, hors cible, ferme)`.

    Une cible explicite (`--target`) ou déclarée par le lot (`branch_target`)
    est seule à foi : elle ferme, jamais une autre ref. Sinon la cible
    déclarée du dépôt (`ameesh.target`) fait foi de même, et les refs
    d'intégration ne servent qu'à signaler le résidu. Sans aucune cible
    déclarée : la cible par défaut du clone (`origin/HEAD`, puis main/master),
    puis les refs d'intégration, qui ferment en se nommant. Sans cible par
    défaut, les seules refs d'intégration examinées ferment."""
    own = str(row.get("branch_target") or "").strip()
    if target is not None:
        return [(target, False, True)]
    if own:
        return [(own, False, True)]
    if default is None:
        return [(ref, bool(index), True) for index, ref in enumerate(candidates)]
    to_try = [(default, False, True)]
    to_try += [(ref, True, declared_repo is None) for ref in candidates]
    return to_try


def sync_merges(db, repo_dir: str, *, target: str | None = None, dry_run: bool = False,
                limit: int = 1000) -> dict:
    """Ferme les lots dont le contenu gelé est entré dans la cible.

    Sans `target` explicite (#1005), la cible est celle du lot
    (`branch_target`), sinon celle du dépôt (`git config ameesh.target`),
    sinon sa cible par défaut (`origin/HEAD`, puis main/master) ; quand rien
    n'est déclaré et que cette cible ne porte pas le gel, les refs
    d'intégration reconnues (`develop`, `main`, `master`, `release/…`) sont
    essayées à leur tour et la fermeture nomme celle qui a livré. Une cible
    explicite ou déclarée garde le sens d'origine : seule elle ferme, et son
    absence est une erreur (`target %r not found in …`)."""
    st = storage.of(db)
    if target is not None:
        try:
            if _commit(repo_dir, target) is None:
                raise MergeProbeError("target %r not found in %s" % (target, repo_dir))
        except GitError as exc:
            raise MergeProbeError(str(exc)) from exc
        declared_repo, default, candidates = None, target, []
    else:
        try:
            declared_repo = declared_target(repo_dir)
            default = default_target(repo_dir)
            candidates = target_candidates(repo_dir, default)
        except GitError as exc:
            raise MergeProbeError(str(exc)) from exc
        if default is None and candidates:
            # aucun `origin/HEAD` ni main/master : la première ref
            # d'intégration tient lieu de cible par défaut
            default, candidates = candidates[0], candidates[1:]
    rows = [r for r in st.work.items(state=None, assignee=None, limit=limit)
            if r["state"] not in work_mod.MERGED_STATES and r["state"] != "closed"]
    declared: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    results: list[dict] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        lot = int(row["id"])
        freeze = latest_freeze(declared.get(lot, []))
        sha = ((freeze or {}).get("sha") or "").strip()
        entry = {"work_item": lot, "package": row.get("package_id"),
                 "state": row["state"], "result": "no-commit", "how": None, "ref": None,
                 "target": None, "off_target": None,
                 "detail": ("last freeze has no commit (milestone frozen --sha)" if freeze
                            else "no freeze declared (milestone frozen --sha)")}
        if not sha:
            results.append(entry)
            continue
        refs = _content_refs(row, target, declared_repo=declared_repo, default=default,
                             candidates=candidates)
        if not refs:
            entry.update(result="no-target",
                         detail="no target: `git config %s <branch>` in %s, or --target"
                                % (TARGET_GIT_KEY, repo_dir))
            results.append(entry)
            continue
        found_target = None              # la ref qui a livré le gel
        found_proof = None
        off_default = None               # la cible par défaut qu'elle n'est pas
        first_ref = first_proof = None   # la ref autoritaire, pour le compte rendu
        residue = None                   # une ref qui la porte sans pouvoir fermer
        error = None
        for ref, off, closes in refs:
            try:
                proof = probe(repo_dir, sha, ref)
            except (MergeProbeError, GitError) as exc:
                if off:
                    residue = residue or "%s is unreadable (%s)" % (ref, exc)
                    continue
                error = str(exc)
                break
            if first_proof is None:
                first_ref, first_proof = ref, proof
            if not proof["merged"]:
                continue
            if closes:
                found_target, found_proof, off_default = ref, proof, (default if off else None)
            else:
                residue = ("%s carries the frozen content (%s), but the declared target "
                           "%s does not: declare `git config %s %s` to close it explicitly"
                           % (ref, proof["how"], default, TARGET_GIT_KEY, ref))
            break
        if error is not None:
            entry.update(result="error", detail=error)
            results.append(entry)
            continue
        if found_target is None:
            entry.update(target=first_ref, sha=sha, result="open",
                         detail=first_proof["detail"] if first_proof else entry["detail"])
            if residue:
                entry.update(result="off-target", detail=residue)
            results.append(entry)
            continue
        entry.update(target=found_target, off_target=off_default, how=found_proof["how"],
                     ref=found_proof["ref"], sha=sha, result="open",
                     detail=found_proof["detail"])
        if dry_run:
            entry["result"] = "would-merge"
        else:
            done = work_mod.close_merged(
                db, lot, sha=entry["ref"] or "", actor="git:%s" % found_target,
                source="content (%s)" % entry["how"], frozen_id=int(freeze["id"]))
            entry["result"] = done["result"]
            detail = "%s — %s" % (entry["detail"], done["detail"])
            entry["detail"] = detail
        results.append(entry)
    return {"git_dir": repo_dir, "target": target if target is not None else (default or ""),
            "auto": target is None, "off_target": default if target is None else None,
            "dry_run": bool(dry_run), "results": results,
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
# Correctif du 2026-10-11 : une équipe fusionne aussi en local, sans PR, des
# lots SANS branche. Le relevé examine désormais tout lot ouvert assigné à un
# agent de l'hôte, avec ou sans branche : à défaut de preuve par la branche,
# un commit de FUSION de la cible, postérieur à la création du lot, le
# désigne par une ligne `ameesh-work: <id>` (n'importe où dans le message,
# même forme que dans le corps d'une PR) ou, seulement si le dépôt l'active
# (`git config ameesh.lotRef hash`), par `#<id>` dans son TITRE. Ailleurs
# « (#45) » est un numéro de PR GitHub : il fermerait le mauvais lot.
#
# Lecture seule du dépôt, aucun `fetch`. La cible : celle du lot, sinon
# `git config ameesh.target` du dépôt (une valeur introuvable est une
# erreur, jamais un repli silencieux sur main), sinon `origin/HEAD`, sinon
# main. #746 : quand ni le lot ni le dépôt ne déclarent de cible et que
# celle-ci (`origin/HEAD`, figé à la création du clone) ne porte pas la
# fusion, le relevé essaie les refs d'intégration reconnues à leur nom
# (`develop`, `main`, `master`, `release/…`) et ferme sur la première qui la
# porte ; jamais une branche de travail (`agent/`, `lot/`, `review/`…), où
# une fusion n'est pas une livraison.

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
#: #312 r1 : corps de PR lus au plus par relevé (`gh`, 60 s chacun au pire)
MAX_PR_BODY_READS = 20
#: #312 r1 : après un échec de `gh` sur une PR, plus de lecture de son corps
#: pendant ce délai, doublé à chaque échec suivant (plafond), oublié après
#: une lecture réussie ; et au plus un échec par dépôt et par relevé
PR_BODY_BACKOFF_S = 60.0
PR_BODY_BACKOFF_MAX_S = 3600.0
#: (dépôt, PR) → (pas avant, délai courant) ; état du processus (relevés successifs)
_pr_body_backoff: dict = {}
#: #312 r2 : (dépôt, PR) → corps déjà lu d'une PR fusionnée (il ne change
#: plus) : relu sans appel ni lecture comptée, les relevés suivants lisent
#: les PR suivantes ; état du processus, borné
_pr_body_cache: dict = {}
PR_BODY_CACHE_MAX = 5000
#: clé de configuration git du dépôt qui nomme la cible par défaut
TARGET_GIT_KEY = "ameesh.target"
DEFAULT_TARGETS = ("origin/main", "main", "origin/master", "master")
#: #746 : refs d'intégration du remote `origin` où un lot peut être livré,
#: quand la cible déclarée (celle du lot ou `ameesh.target`) ne le porte pas.
#: Liste blanche fermée, essayée dans cet ordre : ni une branche de travail
#: (`agent/`, `lot/`, `review/`…), ni un autre remote — une fusion citante
#: dans une branche de travail (empilement) fermerait le lot à tort.
INTEGRATION_BRANCHES = ("develop", "main", "master")
#: #746 : préfixes d'intégration admis (`release/1.10.0`)
INTEGRATION_PREFIXES = ("release/",)
#: #746 : refs d'intégration essayées au plus par dépôt et par relevé
MAX_TARGET_CANDIDATES = 8
#: une ligne `ameesh-work: <id>` désigne un lot précis (message d'un commit de
#: fusion, ou corps d'une PR : `plan_github`) ; le mot `final` après le numéro
#: déclare cette PR comme la dernière étape du lot (#759)
WORK_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?([1-9][0-9]{0,17})"
    r"(?:[ \t]+(final))?[ \t]*$")
#: L118b : une ligne `ameesh-work-partial: <id>` (corps d'une PR, message d'un
#: commit de fusion) : la PR ne livre qu'une partie du lot, qui reste ouvert
#: (lots parapluie, #139)
PARTIAL_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work-partial[ \t]*:[ \t]*#?([1-9][0-9]{0,17})[ \t]*$")
#: L118b : le titre d'un commit de fusion d'une branche `lot/<id>-…` : celui de
#: GitHub (« Merge pull request #N from owner/lot/<id>-… », `pr` : N) ou
#: d'une fusion locale (« Merge branch 'lot/<id>-…' [into …] »)
LOT_BRANCH_MERGE_RE = re.compile(
    r"^Merge (?:pull request #(?P<pr>[1-9][0-9]{0,17}) from [A-Za-z0-9_.-]+/"
    r"|(?:remote-tracking )?branch '(?:origin/)?)"
    r"(?P<branch>lot/(?P<lot>[1-9][0-9]{0,17})(?:-[A-Za-z0-9._/-]*)?)'?"
    r"(?: into [^\s]+)?[ \t]*$")
#: #1005 : le numéro de PR d'un commit de fusion GitHub, QUELLE QUE SOIT la
#: branche fusionnée (« Merge pull request #N from owner/<branche> ») : la PR
#: porteuse d'un gel, même sans citer le lot, a un corps à lire
GITHUB_MERGE_PR_RE = re.compile(r"^Merge pull request #([1-9][0-9]{0,17}) from \S+")
#: `#<id>` dans le titre d'un commit de fusion (« Merge #93 … », « … (#91) ») ;
#: ni `PR#9`, ni `depot#9`, ni `##9`, ni `#9a`
HASH_REF_RE = re.compile(r"(?<![\w/#&])#([1-9][0-9]{0,17})(?!\w)")
#: clé de configuration git du dépôt : comment ses commits de fusion citent un
#: lot en plus de `ameesh-work:` — `hash` : `#<id>` dans le titre
LOT_REF_GIT_KEY = "ameesh.lotRef"
LOT_REF_HASH = "hash"
#: commits de fusion lus au plus par dépôt et par relevé (les plus récents)
MAX_MERGE_SCAN = 500
#: #1005 B2 : fusions lues au plus sur la chaîne première-parente d'une cible
#: pour trouver le porteur d'un gel (la ligne de trop dit la troncature) ;
#: au-delà, le porteur reste indéterminé et le lot ouvert
MAX_CARRIER_SCAN = 500
#: écart d'horloge admis (s) entre la base (création du lot) et le poste qui
#: a fait le commit de fusion (date à la seconde)
CLOCK_SLACK_S = 120.0


def check_ref(name: str, what: str = "branch") -> str:
    """Un nom de branche (ou de cible) admissible, sinon ValueError."""
    text = (name or "").strip()
    if not _REF_RE.match(text) or ".." in text or "//" in text or text.endswith(
            ("/", ".", ".lock")) or "@{" in text:
        raise ValueError("invalid %s: %r (letters, digits, . _ / -)" % (what, name))
    return text


def cited_branch(text: str) -> str | None:
    """La branche `agent/…` citée par un message, si elle est UNIQUE (deux
    branches citées : on ne devine pas)."""
    found = {m.rstrip(".") for m in CITED_BRANCH_RE.findall(text or "")}
    found = {b for b in found if b != "agent/"}
    return next(iter(found)) if len(found) == 1 else None


def declared_target(repo: str) -> str | None:
    """La cible déclarée du dépôt (`git config ameesh.target`), ou None :
    une cible déclarée fait foi, le relevé ne cherche pas ailleurs."""
    return _out(repo, "config", "--get", TARGET_GIT_KEY) or None


def default_target(repo: str) -> str | None:
    """La cible par défaut du dépôt : `git config ameesh.target`, sinon la
    branche par défaut du dépôt distant (`origin/HEAD`), sinon main/master.

    Une cible configurée mais introuvable (faute de frappe, `origin/develop`
    jamais récupérée) lève MergeProbeError : se rabattre sur main ferait
    chercher les fusions au mauvais endroit, sans le dire."""
    configured = declared_target(repo)
    if configured:
        if _commit(repo, configured):
            return configured
        raise MergeProbeError("%s = %s not found in %s (no fetch is done: "
                              "fix the value or fetch the branch)"
                              % (TARGET_GIT_KEY, configured, repo))
    head = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and _commit(repo, head):
        return head
    for candidate in DEFAULT_TARGETS:
        if _commit(repo, candidate):
            return candidate
    return None


def integration_branch(name: str) -> bool:
    """#746 : la ref est-elle une ref d'intégration de la liste blanche
    (`develop`, `main`, `master`, `release/*`) ? Rien d'autre : ni une
    branche de travail, ni un autre remote."""
    low = short_branch(name).lower()
    return bool(low) and (low in INTEGRATION_BRANCHES
                          or low.startswith(INTEGRATION_PREFIXES))


def target_candidates(repo: str, primary: str | None,
                      limit: int = MAX_TARGET_CANDIDATES) -> list[str]:
    """#746 : les refs d'intégration du remote `origin` à essayer après
    `primary` (la cible par défaut du clone), quand celle-ci ne porte pas la
    fusion : la liste blanche `INTEGRATION_BRANCHES`, puis `release/*` de la
    plus récente à la plus ancienne, au plus `limit`.

    Lecture seule, sans fetch ; aucun autre remote, aucune branche de
    travail : une branche `agent/…` ou `lot/…` née de la cible porte ses
    commits de fusion sans que le lot y soit livré."""
    out = _out(repo, "for-each-ref", "--sort=-committerdate",
               "--format=%(refname:short)", "refs/remotes/origin")
    prime = short_branch(primary) if primary else None
    found = []
    for ref in (out or "").split():
        name = short_branch(ref)
        if not name or name == "HEAD" or name == prime:
            continue
        if integration_branch(name):
            found.append(ref)
    order = {name: i for i, name in enumerate(INTEGRATION_BRANCHES)}
    found.sort(key=lambda ref: order.get(short_branch(ref), len(order)))
    return found[:limit]


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
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    tip = _tip(repo, branch)
    out = {"merged": False, "how": None, "ref": None, "tip": tip, "ahead": False}

    def seen() -> dict | None:
        if not seen_head:
            return None
        found = probe(repo, seen_head, target)
        if found["merged"]:
            return dict(out, merged=True, how="vu-" + found["how"], ref=found["ref"],
                        detail="last commit seen ahead (%s): %s"
                               % (seen_head[:12], found["detail"]))
        return None

    def mention(why: str) -> dict:
        sha = merge_mentioning(repo, branch, head, since_ts)
        if sha:
            gone = _content_gone(repo, sha, head)
            if gone:
                return dict(out, detail="merge commit %s that cites %s was reverted: %s"
                                        % (sha[:12], branch, gone))
            return dict(out, merged=True, how="message", ref=sha,
                        detail="merge commit %s of %s that cites %s" % (sha[:12], target,
                                                                          branch))
        return dict(out, detail=why)

    if tip is None:
        return seen() or mention("branch %s not found (neither local nor origin/)" % branch)
    if _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0:
        if not _on_first_parent(repo, tip, head):
            gone = _content_gone(repo, tip, head)
            if gone:
                return dict(out, detail="%s (%s) entered %s through a merge commit but its "
                                        "content is no longer in the tree (composition "
                                        "reverted): %s" % (branch, tip[:12], target, gone))
            return dict(out, merged=True, how="ancestor", ref=tip,
                        detail="%s (%s) entered %s through a merge commit"
                               % (branch, tip[:12], target))
        return seen() or mention("%s has no work of its own: its tip %s is on the "
                                 "line of %s" % (branch, tip[:12], target))
    out["ahead"] = True
    found = probe(repo, tip, target)
    if found["merged"]:
        return dict(out, merged=True, how=found["how"], ref=found["ref"],
                    detail="%s: %s" % (branch, found["detail"]))
    return dict(out, detail="%s ahead of %s (%s)" % (branch, target, tip[:12]))


def lot_ref_hash(repo: str) -> bool:
    """`git config ameesh.lotRef hash` : les commits de fusion du dépôt citent
    leurs lots par `#<id>` dans leur titre (à n'activer que si ces numéros sont
    des lots ameesh, jamais des PR GitHub)."""
    return (_out(repo, "config", "--get", LOT_REF_GIT_KEY) or "").strip().lower() == LOT_REF_HASH


def merge_commits(repo: str, target: str, since_ts: float | None = None,
                  limit: int = MAX_MERGE_SCAN, first_parent: bool = False) -> list[dict]:
    """Les commits de FUSION de `target` (au plus `limit`, du plus récent au
    plus ancien, postérieurs à `since_ts`) : `{sha, ts, message, parents}`.
    Avec `first_parent`, seulement ceux de la CHAÎNE première-parente de
    `target` (#1005 B2 : le porteur d'un gel est une fusion de la cible,
    jamais une fusion imbriquée dans une branche porteuse) ; les citations,
    elles, gardent toutes les fusions."""
    args = ["log", "--merges", "--format=%H%x00%ct%x00%P%x00%B%x1e", "--max-count=%d" % int(limit)]
    if first_parent:
        args.append("--first-parent")
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    proc = _git(repo, *args, target)
    if proc.returncode != 0:
        raise MergeProbeError("git log %s: %s" % (
            target, proc.stderr.decode("utf-8", "replace").strip()))
    rows = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        sha, _sep, rest = record.strip().partition("\x00")
        stamp, _sep, rest = rest.partition("\x00")
        parents, _sep, message = rest.partition("\x00")
        if sha:
            try:
                ts = float(stamp)
            except ValueError:
                ts = 0.0
            rows.append({"sha": sha, "ts": ts, "message": message.strip(),
                         "parents": parents.split()})
    return rows


def cited_lots(message: str, *, hash_refs: bool = False) -> dict[int, str]:
    """`{lot: forme}` des lots que désigne le message d'un commit de fusion :
    une ligne `ameesh-work: <id>` (forme `ameesh-work`), le titre de la fusion
    d'une branche `lot/<id>-…` (forme `lot-branch`, L118b), et, avec
    `hash_refs`, `#<id>` dans le titre (forme `#id`) — pas dans le corps, qui
    peut recopier les titres des commits fusionnés. Un lot déclaré partiel
    (`ameesh-work-partial: <id>`) n'est pas désigné."""
    found: dict[int, str] = {}
    lines = (message or "").strip().splitlines()
    title = lines[0].strip() if lines else ""
    for match in WORK_TRAILER_RE.finditer(message or ""):
        found.setdefault(int(match.group(1)), "ameesh-work")
    branch = LOT_BRANCH_MERGE_RE.match(title)
    if branch:
        found.setdefault(int(branch.group("lot")), "lot-branch")
    if hash_refs:
        for match in HASH_REF_RE.finditer(title):
            found.setdefault(int(match.group(1)), "#id")
    for match in PARTIAL_TRAILER_RE.finditer(message or ""):
        found.pop(int(match.group(1)), None)
    return found


def merge_pr_number(message: str) -> int | None:
    """Le numéro de la PR d'un commit de fusion GitHub d'une branche
    `lot/<id>-…` (son corps n'est pas dans le message : `partial_of_pr`)."""
    match = _lot_branch_merge(message)
    return int(match.group("pr")) if match and match.group("pr") else None


def merge_pull_number(message: str) -> int | None:
    """#1005 B2 : le numéro de la PR d'un commit de fusion GitHub, quelle que
    soit la branche fusionnée (`merge_pr_number` ne voit que `lot/<id>-…`).
    La PR PORTEUSE d'un gel — celle qui l'a fait entrer dans la cible — a un
    corps à lire avant de retenir la preuve par le contenu."""
    lines = (message or "").strip().splitlines()
    match = GITHUB_MERGE_PR_RE.match(lines[0].strip() if lines else "")
    return int(match.group(1)) if match else None


def merge_branch(message: str) -> str | None:
    """La branche `lot/<id>-…` que fusionne un commit de fusion (son titre)."""
    match = _lot_branch_merge(message)
    return match.group("branch") if match else None


def _lot_branch_merge(message: str):
    lines = (message or "").strip().splitlines()
    return LOT_BRANCH_MERGE_RE.match(lines[0].strip() if lines else "")


def short_branch(name: str | None) -> str:
    """`lot/12-x` de `refs/heads/lot/12-x`, `origin/lot/12-x`… (comparaison)."""
    name = (name or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def partial_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare ne livrer qu'en partie (L118b)."""
    return {int(m.group(1)) for m in PARTIAL_TRAILER_RE.finditer(body or "")}


def final_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare livrer en dernier : une ligne
    `ameesh-work: <id> final` (#759). Une PR finale ferme le lot, même si une
    autre PR ouverte ou une branche déclarée y reste liée ; `ameesh-work-partial:`
    l'emporte (une PR partielle ne ferme jamais)."""
    return {int(m.group(1)) for m in WORK_TRAILER_RE.finditer(body or "")
            if m.group(2)}


def merge_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                 hash_refs: bool = False) -> dict | None:
    """Le PREMIER commit de fusion postérieur à `since_ts` (création du lot,
    à `CLOCK_SLACK_S` près) qui désigne le lot `lot` (`cited_lots`), avec
    `how`, ou None."""
    found = None
    for merge in merges:                     # du plus récent au plus ancien
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            found = dict(merge, how=how)
    return found


def merges_partial(merges: list[dict], lot: int, *,
                   since_ts: float | None = None) -> list[dict]:
    """#312 r1 : les commits de fusion postérieurs à `since_ts` dont le
    message déclare le lot partiel (`ameesh-work-partial: <id>`), du plus
    ancien au plus récent."""
    return [m for m in reversed(merges)
            if not (since_ts and m["ts"] < float(since_ts) - CLOCK_SLACK_S)
            and int(lot) in partial_lots(m["message"])]


def merges_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                  hash_refs: bool = False) -> list[dict]:
    """Les commits de fusion postérieurs à `since_ts` (à `CLOCK_SLACK_S` près)
    qui désignent le lot (`cited_lots`), du plus ancien au plus récent, avec
    `how` (L118b : le premier dont la PR n'est pas partielle ferme le lot)."""
    out = []
    for merge in reversed(merges):           # du plus ancien au plus récent
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            out.append(dict(merge, how=how))
    return out


def _citation(merge: dict, lot: int, target: str, *, sha: bool = True) -> str:
    """Comment la fusion a été constatée, en clair (compte rendu ; sans le
    commit, `sha=False`, pour la note du lot, qui le porte déjà)."""
    title = (merge["message"].splitlines() or [""])[0].strip()
    if merge["how"] == "ameesh-work":
        how = "carries \"ameesh-work: %d\"" % lot
    elif merge["how"] == "lot-branch":
        how = "merges the branch lot/%d-…" % lot
    elif merge["how"] == "message":
        # #746 : citation à nom entier de la branche du lot (pas de trailer)
        how = "merges the branch %s" % (merge.get("branch") or "?")
    else:
        how = "cites #%d in its title (%s %s)" % (lot, LOT_REF_GIT_KEY, LOT_REF_HASH)
    return "merge commit %sof %s that %s (\"%s\")" % (
        "%s " % merge["sha"][:12] if sha else "", target, how,
        title[:80] + ("…" if len(title) > 80 else ""))


def implicit_branch(repo: str, lot: int) -> str | None:
    """L118b : la branche `lot/<id>` ou `lot/<id>-…` d'un lot qui n'en porte
    pas, locale ou `origin/`, si elle est UNIQUE (deux candidates : on ne
    devine pas)."""
    out = _out(repo, "for-each-ref", "--format=%(refname)",
               "refs/heads/lot/%d" % lot, "refs/heads/lot/%d-*" % lot,
               "refs/remotes/origin/lot/%d" % lot, "refs/remotes/origin/lot/%d-*" % lot)
    names = set()
    for ref in (out or "").split():
        for prefix in ("refs/heads/", "refs/remotes/origin/"):
            if ref.startswith(prefix):
                names.add(ref[len(prefix):])
    return next(iter(names)) if len(names) == 1 else None


def github_slug(repo: str) -> str | None:
    """`owner/repo` du dépôt `origin` s'il est sur GitHub, sinon None."""
    url = (_out(repo, "remote", "get-url", "origin") or "").strip()
    match = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
                     r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def gh_pr_body(repo: str, number: int) -> str:
    """Le corps de la PR `number` du dépôt GitHub de `repo` (`gh`) ; lève
    GitError s'il est illisible."""
    from . import plan_github
    slug = github_slug(repo)
    if not slug:
        raise GitError("origin of %s is not a GitHub repository" % repo)
    try:
        data = plan_github._json(plan_github.Gh(), ["pr", "view", str(int(number)),
                                                    "--repo", slug, "--json", "body"])
    except plan_github.GithubError as exc:
        raise GitError(str(exc)) from exc
    return (data or {}).get("body") or ""


def _moved_past(repo: str, seen: str | None, seen_ts: float | None, tip: str,
                merge: dict, merge_seen_ts: float | None) -> bool:
    """#312 r2, L118d : la branche a-t-elle livré du travail au-delà de la
    fusion partielle `merge` ? Il faut que le relevé l'ait vue en avance sur
    sa cible avec une pointe `seen` (`branch_head`) qui n'était pas dans la
    branche fusionnée (second parent) ni dans la cible d'alors, puis la voir
    entrée dans la cible (ancêtre de la pointe fusionnée `tip`). Une branche
    seulement avancée jusqu'à la cible, sans travail nouveau, n'y suffit pas :
    son nouveau sommet est la fusion partielle elle-même ou la suite de la
    cible. #346 : la pointe doit avoir été relevée (`seen_ts`) après que le
    relevé a vu la fusion (`merge_seen_ts`), deux instants de l'horloge de la
    base, jamais une date git, que son auteur choisit ; et toute erreur de git
    laisse le lot ouvert (« non ancêtre » : code 1 seulement)."""
    parents = merge.get("parents") or []
    if len(parents) < 2 or not seen or seen_ts is None or merge_seen_ts is None \
            or float(seen_ts) <= float(merge_seen_ts):
        return False
    for outside in (parents[1], merge["sha"]):
        # ni dans la branche fusionnée, ni dans la cible d'alors
        if _git(repo, "merge-base", "--is-ancestor", seen, outside).returncode != 1:
            return False
    return _git(repo, "merge-base", "--is-ancestor", seen, tip).returncode == 0


def _read_pr_body(pr_body, repo: str, number: int, sweep: dict):
    """#312 r1 : le corps de la PR, ou la GitError qui le rend illisible —
    sans appel pendant le recul de cette PR, après un échec de `gh` sur ce
    dépôt dans ce relevé, ni au-delà de `MAX_PR_BODY_READS` lectures : une
    PR toujours illisible ne cache pas longtemps les suivantes."""
    import time
    if (repo, number) in _pr_body_cache:
        return _pr_body_cache[(repo, number)]
    now = time.time()
    until, delay = _pr_body_backoff.get((repo, number), (0.0, 0.0))
    if now < until:
        return GitError("gh failed on this PR; next read in %ds" % int(until - now + 0.999))
    if repo in sweep["failed"]:
        return GitError("gh already failed in this sweep; the next sweep goes on")
    if sweep["reads"] >= MAX_PR_BODY_READS:
        return GitError("%d PR bodies read in this sweep; the next sweep goes on"
                        % MAX_PR_BODY_READS)
    sweep["reads"] += 1
    try:
        body = pr_body(repo, number)
    except GitError as exc:
        delay = min(delay * 2, PR_BODY_BACKOFF_MAX_S) if delay else PR_BODY_BACKOFF_S
        _pr_body_backoff[(repo, number)] = (time.time() + delay, delay)
        sweep["failed"].add(repo)
        return exc
    _pr_body_backoff.pop((repo, number), None)
    if len(_pr_body_cache) >= PR_BODY_CACHE_MAX:
        _pr_body_cache.pop(next(iter(_pr_body_cache)))     # le plus ancien lu
    _pr_body_cache[(repo, number)] = body
    return body


def _off_target_citations(repo: str, lot: int, row: dict, all_merges: list,
                          *, hash_refs: bool, tips: dict) -> tuple[list, tuple | None]:
    """#746 : les fusions d'une ref d'intégration essayée HORS cible qui
    livrent le lot, du plus ancien au plus récent, et la dernière qui se
    déclare partielle.

    Une fusion ne compte que si elle est postérieure à la création du lot
    (à `CLOCK_SLACK_S` près) et cite le lot : trailer `ameesh-work: <id>`
    (ou `#<id>` si le dépôt l'active), ou citation à nom entier de la
    branche du lot. Un lot à branche enregistrée n'est cité que par une
    fusion qui a réellement fusionné SA branche (deuxième parent = sa
    pointe) : une fusion dans une autre branche de travail (empilement)
    ne ferme pas. `tips` : cache `(dépôt, branche) → pointe`, consulté
    seulement quand une citation est trouvée."""
    since = row.get("created_ts")
    mine = short_branch(row["branch"]) if row.get("branch") else None
    citing, partial, tip = [], None, None
    for merge in reversed(all_merges):            # du plus ancien au plus récent
        if since and merge["ts"] < float(since) - CLOCK_SLACK_S:
            continue
        declared_partial = int(lot) in partial_lots(merge["message"])
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how == "lot-branch" and mine and merge_branch(merge["message"]) != mine:
            continue                    # #312 r1 : une autre lot/<id>-… ne compte pas
        if how is None and mine and _mentions(merge["message"], mine):
            how = "message"             # la fusion cite la branche enregistrée
        if how is None and not declared_partial:
            continue
        if mine:
            if tip is None:
                if (repo, mine) not in tips:
                    tips[(repo, mine)] = _tip(repo, mine)
                tip = tips[(repo, mine)]
            parents = merge.get("parents") or []
            if len(parents) < 2 or parents[1] != tip:
                continue
        if declared_partial:
            partial = (merge, "merge commit %s declares itself partial "
                       "(\"ameesh-work-partial: %d\"): lot kept open"
                       % (merge["sha"][:12], lot))
            continue
        citing.append(dict(merge, how=how, branch=mine))
    return citing, partial


def _is_ancestor(repo: str, sha: str, ref: str) -> bool:
    """`git merge-base --is-ancestor` : une commande, sans lecture d'objet."""
    return _git(repo, "merge-base", "--is-ancestor", sha, ref).returncode == 0


def _content_ancestor(repo: str, sha: str, target: str) -> bool:
    """#1005 : le commit gelé est-il ancêtre de la cible ? C'est la preuve par
    contenu la moins chère (une commande git), celle d'une fusion `--no-ff`
    (ou d'une avance rapide) poussée sur la branche d'intégration sans citer
    le lot. Le patch-id exact (rebase, cherry-pick, squash) reste au relevé de
    contenu (`sync-merges`), dont c'est le travail et le coût assumé."""
    head = _commit(repo, target)
    frozen = _commit(repo, sha)
    if head is None or frozen is None:
        return False
    return _is_ancestor(repo, frozen, head)


def _content_carrier(repo: str, sha: str, target: str) -> tuple[dict | None, bool]:
    """#1005 B2 : la fusion qui a FAIT ENTRER le gel `sha` dans la cible — la
    plus ancienne fusion de la CHAÎNE PREMIÈRE-PARENTE de `target` dont la
    première parente ne le porte pas encore et dont une autre parente le porte.
    C'est la PR porteuse, celle dont le corps doit être lu avant de retenir la
    preuve par le contenu.

    La recherche est faite séparément sur la cible (`first_parent`) : la
    fenêtre des citations, bornée par `since_all`, peut ne pas contenir la
    fusion porteuse, et une fusion IMBRIQUÉE dans une branche porteuse (fusion
    locale `--no-ff` puis PR GitHub) n'est jamais sur cette chaîne — la
    retenir ferait lire le mauvais message et fermer le lot sans lire la PR.

    Rend `(porteur, fenêtre_tronquée)`. `(None, False)` : le gel est entré
    autrement (avance rapide, poussée directe), il n'y a aucun corps de PR à
    qualifier — même tronquée, la fenêtre part de la fusion la plus récente,
    et si elle ne porte pas le gel, aucune fusion plus ancienne ne le porte.
    `(None, True)` : la limite de lecture est atteinte sans que le porteur
    soit connu (il peut être plus ancien) : indéterminé, le lot reste ouvert."""
    merges = merge_commits(repo, target, limit=MAX_CARRIER_SCAN + 1, first_parent=True)
    truncated = len(merges) > MAX_CARRIER_SCAN
    if not merges or not _is_ancestor(repo, sha, merges[0]["sha"]):
        return None, False
    # du plus récent au plus ancien ; « porte le gel » y est monotone (une
    # fusion récente de la chaîne descend de toutes les plus anciennes) : la
    # plus ancienne qui le porte est trouvée par dichotomie
    lo, hi = 0, len(merges) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _is_ancestor(repo, sha, merges[mid]["sha"]):
            lo = mid
        else:
            hi = mid - 1
    carrier = merges[lo]
    parents = carrier.get("parents") or []
    if parents and _is_ancestor(repo, sha, parents[0]):
        # le gel était déjà sur la chaîne avant cette fusion : il est entré
        # par une avance rapide ou une poussée directe, sans corps de PR —
        # sauf si la fenêtre s'arrête là, le porteur peut être plus ancien
        return None, bool(truncated and lo == len(merges) - 1)
    return carrier, False


def _sweep_target(repo: str, lot: int, row: dict, branch: str | None, target: str, *,
                  st, since_all: float | None, hashes: dict, merges: dict, bodies: dict,
                  sweep: dict, pr_body, dry_run: bool, implicits: dict, tips: dict,
                  freeze: dict | None = None, off_target: bool = False,
                  closing: bool = True) -> dict:
    """L'examen d'un lot contre UNE cible, extrait de `sync_branches` (#746)
    pour essayer la cible par défaut puis, si elle ne porte pas la fusion,
    les refs d'intégration du dépôt.

    `off_target` : la ref n'est ni la cible déclarée ni la cible par défaut ;
    seule une citation prouve alors la livraison, jamais le seul voisinage
    de la branche. `closing` : cette ref peut fermer le lot (une cible
    déclarée ne ferme jamais ailleurs : la preuve n'y est qu'un résidu).
    #1005 : à défaut de branche ou de citation, l'ancêtre du dernier gel
    (`freeze`, id et sha) prouve la livraison, après lecture du corps de sa
    PR porteuse quand la preuve ferme (B2, cherchée sur la chaîne
    première-parente de la cible) ; une fenêtre de lecture tronquée sans
    porteur connu laisse le lot ouvert. La fermeture est conditionnée à
    ce gel (B1). Rend `{found, branch, implicit, row}` ; lève
    MergeProbeError/GitError comme l'examen d'origine."""
    freeze_sha = ((freeze or {}).get("sha") or "").strip() or None
    found = None
    implicit = False
    if not branch:
        if lot not in implicits:
            implicits[lot] = implicit_branch(repo, lot)
        branch = implicits[lot]
        implicit = bool(branch)
    if off_target:
        found = {"merged": False, "how": None, "ref": None, "ahead": False, "tip": None,
                 "detail": "no merge commit of %s delivers the lot" % target}
    elif branch:
        found = branch_probe(repo, branch, target, seen_head=row.get("branch_head"),
                             since_ts=row.get("created_ts"))
    if repo not in hashes:
        hashes[repo] = lot_ref_hash(repo)
    if (repo, target) not in merges:
        merges[(repo, target)] = merge_commits(repo, target, since_all)
    all_merges = merges[(repo, target)]
    # L118b : une fusion GitHub de `lot/<id>-…` ne ferme le lot que si
    # sa PR ne se déclare pas partielle ; la première qui livre tout
    unreadable = None                # pourquoi une PR reste illisible
    partial = None                   # la dernière fusion partielle, et pourquoi
    cited = None
    if off_target:
        # #746 : hors cible, une citation (trailer, `#<id>`, nom entier de
        # la branche) prouve la livraison ; le lot à branche enregistrée
        # exige en plus que la fusion ait porté SA branche
        citing, partial = _off_target_citations(repo, lot, row, all_merges,
                                                hash_refs=hashes[repo], tips=tips)
    else:
        citing = merges_citing(all_merges, lot, since_ts=row.get("created_ts"),
                               hash_refs=hashes[repo])
        if row.get("branch"):
            # #312 r1 : une branche enregistrée est seule à compter ; la
            # fusion d'une autre `lot/<id>-…` ne ferme pas le lot
            mine = short_branch(row["branch"])
            citing = [m for m in citing if m["how"] != "lot-branch"
                      or merge_branch(m["message"]) == mine]
        # #312 r1 : une fusion locale qui se déclare partielle retient
        # aussi la preuve par la branche
        for merge in merges_partial(all_merges, lot, since_ts=row.get("created_ts")):
            partial = (merge, "merge commit %s declares itself partial "
                       "(\"ameesh-work-partial: %d\"): lot kept open"
                       % (merge["sha"][:12], lot))
    reverted = None
    for merge in citing:
        gone = _content_gone(repo, merge["sha"], _commit(repo, target))
        if gone:
            reverted = "merge commit %s of %s was reverted: %s" % (
                merge["sha"][:12], target, gone)
            continue
        number = merge_pr_number(merge["message"])
        if not number:
            cited = merge
            break
        if (repo, number) not in bodies:
            bodies[(repo, number)] = _read_pr_body(pr_body, repo, number, sweep)
        body = bodies[(repo, number)]
        if isinstance(body, GitError):
            # #312 r1 : une PR plus récente lisible et complète peut
            # encore fermer le lot
            unreadable = unreadable or (
                "PR #%d merged, but its body is unreadable (%s): not closed until "
                "it says whether it is partial" % (number, body))
            continue
        if lot in partial_lots(body):
            if partial is None or merge["ts"] >= partial[0]["ts"]:
                partial = (merge, "PR #%d declares itself partial "
                           "(\"ameesh-work-partial: %d\"): lot kept open"
                           % (number, lot))
            continue
        cited = merge
        break
    # une branche enregistrée déjà prouvée fusionnée ferme comme avant
    # quand le corps est illisible (hôte sans gh) : seule la preuve
    # implicite attend de savoir ; une fusion partielle retient toujours
    proven = bool(found and found["merged"] and row.get("branch"))
    if partial and partial[0]["sha"] != row.get("partial_merge") and not off_target:
        # #346 r2 : l'instant où le relevé voit la fusion partielle
        # pour la première fois, à l'horloge de la base ; #403 : le
        # dry-run juge comme le vrai relevé, sans écrire
        if not dry_run:
            st.work.set_partial_seen(lot, partial[0]["sha"])
        row = dict(row, partial_merge=partial[0]["sha"], partial_seen_ts=None)
    if partial and found and found["merged"] and found["tip"] \
            and _moved_past(repo, row.get("branch_head"), row.get("branch_head_ts"),
                            found["tip"], partial[0], row.get("partial_seen_ts")):
        # #312 r2 : seule la dernière fusion de la branche compte ; la
        # branche vue depuis en avance, puis fusionnée (avance rapide),
        # a livré la suite (L118d : pas une simple avance jusqu'à la cible)
        partial = None
    held = partial[1] if partial else unreadable
    # #1005 : ni la branche ni une citation ne prouvent la livraison ; le
    # dernier gel, ancêtre de la cible, la prouve par le contenu (fusion
    # --no-ff poussée sur la branche d'intégration, comme #560/#905). B2 : le
    # corps de la PR PORTEUSE — celle qui a fait entrer le gel — est lu AVANT
    # de retenir la preuve quand elle ferme ; partielle pour ce lot ou
    # illisible, le lot reste ouvert. Sur une ref qui ne ferme pas (cible
    # déclarée), la preuve n'est qu'un résidu signalé : rien à qualifier.
    content = None
    if freeze_sha and cited is None and not held and not (found and found["merged"]) \
            and _content_ancestor(repo, freeze_sha, target):
        if closing:
            # le porteur est cherché sur la chaîne première-parente de la
            # CIBLE, séparément de la fenêtre des citations (B2, R2 cx3) :
            # indéterminé au-delà de la limite, le lot reste ouvert
            carrier, truncated = _content_carrier(repo, freeze_sha, target)
            number = merge_pull_number(carrier["message"]) if carrier else None
            if number:
                if (repo, number) not in bodies:
                    bodies[(repo, number)] = _read_pr_body(pr_body, repo, number, sweep)
                body = bodies[(repo, number)]
                if isinstance(body, GitError):
                    unreadable = ("PR #%d carries the last freeze %s, but its body is "
                                  "unreadable (%s): not closed until it says whether it "
                                  "is partial" % (number, freeze_sha[:12], body))
                elif lot in partial_lots(body):
                    partial = (carrier, "PR #%d carries the last freeze %s and declares "
                               "itself partial (\"ameesh-work-partial: %d\"): lot kept open"
                               % (number, freeze_sha[:12], lot))
            elif carrier is None and truncated:
                unreadable = ("the last freeze %s is an ancestor of %s, but its carrier "
                              "merge is older than the %d merges read on the first-parent "
                              "chain: not closed until it is known"
                              % (freeze_sha[:12], target, MAX_CARRIER_SCAN))
        held = partial[1] if partial else unreadable
        if not held:
            content = {"merged": True, "how": "content-ancestor", "ref": freeze_sha,
                       "tip": None, "ahead": False, "kind": "content",
                       "detail": "the last freeze %s is an ancestor of %s (content)"
                                 % (freeze_sha[:12], target),
                       "note": "the last freeze %s entered %s as an ancestor (content)"
                               % (freeze_sha[:12], target)}
            if freeze.get("id") is not None:
                content["frozen_id"] = int(freeze["id"])
    if held and cited is None and (partial or not proven):
        found = dict(found or {"tip": None, "ahead": False, "ref": None},
                     merged=False, how="partial", detail=held)
    if found is None or not found["merged"]:
        if cited:
            found = dict(found or {"tip": None, "ahead": False}, merged=True,
                         how=cited["how"], ref=cited["sha"],
                         detail=_citation(cited, lot, target),
                         note=_citation(cited, lot, target, sha=False))
        elif reverted:
            # #1776 : un contenu retiré (revert d'une composition) prime sur
            # la preuve par contenu : l'ascendance du gel ne prouve plus rien.
            found = dict(found or {"tip": None, "ahead": False}, merged=False,
                         how=None, ref=None, detail=reverted)
        elif content is not None:
            found = content
        elif found is None:
            found = {"merged": False, "how": None, "ref": None, "tip": None,
                     "ahead": False,
                     "detail": "no merge commit of %s names the lot "
                               "(line \"ameesh-work: %d\", nor a merge of lot/%d-…%s)"
                               % (target, lot, lot, ", nor \"#%d\" in a title" % lot
                                  if hashes[repo] else "")}
    return {"found": found, "branch": branch, "implicit": implicit, "row": row}


def _signal_off_target(db, st, lot: int, assignee: str, reg, note: str, key: str,
                       *, actor: str = "") -> None:
    """#746 : une citation hors cible (ou une ref illisible) n'est jamais
    silencieuse : une note au journal du lot et un message dans son fil, une
    fois par observation (`key`), sans jamais fermer le lot."""
    from . import fil
    if any(key in (event.get("note") or "")
           for event in st.work.events(int(lot), 40)):
        return
    try:
        work_mod.note(db, lot, note, actor=actor)
    except work_mod.WorkError:
        return                          # le lot a changé : rien à journaliser
    fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
               text=note, project=fil.project_for(db.cfg, fil.agent_project(reg)),
               lot=str(lot), meta={"kind": "event"})


def sync_branches(db, *, host: str | None = None, agents=None, dry_run: bool = False,
                  actor: str = "", limit: int = MAX_BRANCH_LOTS, pr_body=None) -> dict:
    """L118 : ferme les lots ouverts dont la fusion dans leur cible est constatée.

    Examinés : les lots ouverts qui portent une branche ou ont un assigné.
    Le dépôt d'un lot est le dossier de travail de son assigné au registre ;
    avec `host`, seuls les assignés de cet hôte sont examinés (les dossiers
    des autres hôtes n'y sont pas), avec `agents`, seulement ceux-là. Preuve :
    la branche du lot (`branch_probe`), sinon un commit de fusion de la cible
    qui désigne le lot (`ameesh-work: <id>`, ou `#<id>` si le dépôt l'active :
    `merge_citing`). La fermeture passe par `work.close_merged` (idempotente,
    jamais de réouverture) avec le commit de fusion et une note qui dit comment
    elle a été constatée, et s'écrit dans le fil du lot. Rend le compte rendu.

    L118b : un lot sans branche prend pour branche implicite son unique
    `lot/<id>[-…]` (`implicit_branch`), et la fusion GitHub d'une branche
    `lot/<id>-…` le désigne (« Merge pull request #N from owner/lot/<id>-… »).
    Le corps de cette PR (`pr_body(repo, N)`, défaut : `gh`) est lu : une PR
    qui se déclare partielle (`ameesh-work-partial: <id>`) ne ferme pas le
    lot, et un corps illisible le laisse ouvert (un lot fermé n'est jamais
    rouvert : on ne ferme pas sans savoir).

    #746 : une cible déclarée — celle du lot (`branch_target`) ou
    `ameesh.target` du dépôt — fait foi : une fusion constatée ailleurs
    n'est qu'un résidu signalé (note au lot, message à l'assigné), jamais
    une fermeture. Quand aucune des deux n'est déclarée et que la cible par
    défaut (`origin/HEAD`, figé à la création du clone) ne porte pas la
    fusion, les refs d'intégration du remote (`develop`, `main`, `master`,
    `release/*`) sont essayées à leur tour : une fusion citante y ferme le
    lot, avec la ref trouvée dans la note. Aucune autre ref (branche de
    travail, autre remote), aucun fetch.

    #1005 : à défaut de branche et de citation, le dernier gel du lot,
    ancêtre de la ref examinée, prouve la livraison par le contenu (une
    fusion `--no-ff` poussée sur la branche d'intégration, comme #560/#905,
    sans citer le lot) : le relevé la ferme, en nommant la ref. Le patch-id
    exact (rebase, cherry-pick, squash) reste le travail de `sync-merges`.
    B2 : le corps de la PR PORTEUSE de ce gel (`merge_pull_number`, quelle
    que soit sa branche) est lu comme celui d'une PR citante — partielle pour
    le lot ou illisible, la preuve n'est pas retenue. Le porteur est cherché
    sur la chaîne première-parente de la cible, indépendamment de la fenêtre
    des citations (une fusion imbriquée dans une branche porteuse n'est pas
    la fusion de la cible) ; quand la limite de lecture est atteinte sans
    porteur connu, le lot reste ouvert. B1 : la fermeture est
    conditionnée au jalon `frozen` examiné (`frozen_id`) : un regel
    concurrent, vu après la lecture, laisse le lot ouvert (`refrozen`)."""
    import os

    from . import fil, registry

    st = storage.of(db)
    rows = st.work.open_for_sweep(limit)
    # #1005 : le dernier gel de chaque lot — id ET sha (B1 : la fermeture est
    # conditionnée à ce jalon précis) — lu une fois, comme le relevé de contenu
    marks: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        marks.setdefault(int(ms["work_item_id"]), []).append(ms)
    freezes: dict = {}
    for lot, items in marks.items():
        latest = latest_freeze(items) or {}
        if (latest.get("sha") or "").strip():
            freezes[lot] = latest
    # les commits de fusion lus une fois par dépôt et par cible : depuis le
    # lot ouvert le plus ancien, chaque lot ne retient que les siens
    since_all = min((float(r["created_ts"]) - CLOCK_SLACK_S for r in rows
                     if r.get("created_ts")), default=None)
    targets: dict = {}
    declared: dict = {}                      # #746 : le dépôt déclare sa cible ?
    candidates: dict = {}                    # #746 : (dépôt, cible) → refs d'intégration
    implicits: dict = {}                     # #746 : branche implicite, une fois par lot
    tips: dict = {}                          # #746 : (dépôt, branche) → pointe
    hashes: dict = {}
    merges: dict = {}
    regs: dict = {}                          # une lecture du registre par assigné
    bodies: dict = {}                        # (dépôt, PR) → corps, ou l'erreur
    sweep = {"reads": 0, "failed": set()}    # lectures de corps de ce relevé
    pr_body = pr_body or gh_pr_body
    results: list[dict] = []
    for row in rows:
        lot = int(row["id"])
        branch = row.get("branch") or None
        assignee = (row.get("assignee") or "").strip()
        entry = {"work_item": lot, "branch": branch,
                 "target": row.get("branch_target"), "assignee": assignee or None,
                 "result": "skipped", "how": None, "ref": None, "detail": ""}
        if agents is not None and assignee not in agents:
            continue
        if assignee and ":" not in assignee and assignee not in regs:
            regs[assignee] = registry.get(db, assignee)
        reg = regs.get(assignee) if assignee and ":" not in assignee else None
        if reg is None:
            if host is None and branch:
                entry["detail"] = "no agent assigned: repository unknown"
                results.append(entry)
            continue
        if host is not None and (reg.get("host") or "") != host:
            continue
        repo = os.path.expanduser(reg.get("cwd") or "")
        if not repo or not os.path.isdir(repo):
            entry.update(result="no-repo", detail="working directory of %s missing (%s)"
                         % (assignee, repo or "not declared"))
            results.append(entry)
            continue
        try:
            # #746 : une cible déclarée — celle du lot, ou `ameesh.target` du
            # dépôt — fait foi ; sinon la cible par défaut du clone
            # (`origin/HEAD`, figé à sa création), puis les refs d'intégration
            # du remote, essayées seulement si elle ne porte pas la fusion.
            if row.get("branch_target"):
                primary, explicit = row["branch_target"], True
            else:
                if repo not in targets:
                    try:
                        targets[repo] = default_target(repo)
                    except MergeProbeError as exc:
                        targets[repo] = exc
                if isinstance(targets[repo], MergeProbeError):
                    raise targets[repo]
                if not targets[repo]:
                    entry.update(result="no-target", detail="no target: `git config %s "
                                 "<branch>` in %s, or --target" % (TARGET_GIT_KEY, repo))
                    results.append(entry)
                    continue
                if repo not in declared:
                    declared[repo] = bool(declared_target(repo))
                primary, explicit = targets[repo], declared[repo]
            # #990 : la liste dépend de la cible primaire, propre au lot — un
            # cache par dépôt la figerait pour les lots suivants
            if (repo, primary) not in candidates:
                candidates[(repo, primary)] = target_candidates(repo, primary)
            off_refs = candidates[(repo, primary)]
            # une cible déclarée ne ferme jamais ailleurs : les refs hors
            # cible n'y sont examinées que pour signaler le résidu
            to_try = [(primary, False, True)]
            to_try += [(ref, True, explicit is False) for ref in off_refs]
            outcome = None                   # l'examen retenu (cible par défaut)
            off_outcome = None               # une citation hors cible (jamais fermée)
            partial_off = None               # une fusion partielle hors cible
            unreadable = []                  # (ref, pourquoi) : le résidu, jamais silencieux
            for target, off, close in to_try:
                try:
                    probe = _sweep_target(
                        repo, lot, row, branch, target, st=st, since_all=since_all,
                        hashes=hashes, merges=merges, bodies=bodies, sweep=sweep,
                        pr_body=pr_body, dry_run=dry_run, implicits=implicits,
                        tips=tips, freeze=freezes.get(lot), off_target=off,
                        closing=close)
                except (MergeProbeError, GitError) as exc:
                    if off:
                        unreadable.append((target, str(exc)))
                    elif outcome is None:
                        outcome = {"error": str(exc), "target": target, "off": off}
                    continue
                if probe["found"]["merged"]:
                    if close:
                        outcome = dict(probe, target=target, off=off)
                    else:
                        off_outcome = dict(probe, target=target, off=off)
                    break
                if off and probe["found"]["how"] == "partial" and partial_off is None:
                    partial_off = (target, probe["found"]["detail"])
                if outcome is None:
                    outcome = dict(probe, target=target, off=off)
        except (MergeProbeError, GitError) as exc:
            entry.update(result="error", detail=str(exc))
            results.append(entry)
            continue
        if outcome.get("error"):
            entry.update(target=outcome["target"], result="error", detail=outcome["error"])
            results.append(entry)
            continue
        found = outcome["found"]
        branch = outcome["branch"]
        implicit = outcome["implicit"]
        row = outcome["row"]
        target = outcome["target"]
        entry.update(branch=branch, target=target, repo=repo, how=found["how"],
                     ref=found["ref"], detail=found["detail"], result="open")
        if found["merged"] and outcome["off"]:
            # #746 : la livraison est constatée hors de la cible par défaut
            # (clone sans `ameesh.target`, `origin/HEAD` figé) : la note dit
            # la ref trouvée et quoi déclarer pour l'expliciter
            entry["off_target"] = primary
            entry["detail"] = "%s (merged into %s, off the default target %s: declare " \
                              "`git config %s %s` to make it explicit)" % (
                                  entry["detail"], target, primary, TARGET_GIT_KEY, target)
        if not found["merged"]:
            # #746 : le résidu n'est jamais silencieux — une citation sur une
            # ref d'intégration alors qu'une cible est déclarée, ou une ref
            # illisible : note au lot et message à l'assigné, jamais de
            # fermeture
            note, key = None, None
            if off_outcome:
                note = ("merge sweep: %s, but the declared target %s does not carry "
                        "it; the lot is kept open — declare `git config %s %s` to "
                        "close it explicitly" % (
                            off_outcome["found"]["detail"], primary, TARGET_GIT_KEY,
                            off_outcome["target"]))
                key = (off_outcome["found"]["ref"] or off_outcome["target"])[:12]
            elif unreadable:
                note = ("merge sweep: unreadable integration ref%s %s; the lot is kept "
                        "open — fix or declare the target (`git config %s <branch>`)" % (
                            "s" if len(unreadable) > 1 else "",
                            ", ".join("%s (%s)" % pair for pair in unreadable),
                            TARGET_GIT_KEY))
                key = "unreadable integration ref"
            if note:
                entry.update(result="off-target", detail=note)
                if not dry_run:
                    _signal_off_target(db, st, lot, assignee, reg, note, key,
                                       actor=actor or "git:%s" % target)
            elif partial_off:
                # #746 : une fusion partielle hors cible ne ferme pas, mais
                # se dit (comme la fusion partielle de la cible)
                entry.update(how="partial", detail=partial_off[1])
            stale = row.get("branch_head_ts") is None or (
                row.get("partial_merge") and (row.get("partial_seen_ts") is None
                                              or row["branch_head_ts"] <= row["partial_seen_ts"]))
            if branch and found["ahead"] and found["tip"] and not dry_run \
                    and (found["tip"] != row.get("branch_head") or stale):
                # #346 r2 : une pointe inchangée relevée avant la fusion
                # partielle l'est de nouveau, puisqu'elle est encore là
                if implicit:
                    # L118b : la branche implicite devient celle du lot (journal)
                    try:
                        work_mod.set_branch(db, lot, branch, actor=actor or "git:%s" % target,
                                            source="the merge sweep: lot/%d branch" % lot)
                    except work_mod.WorkError:
                        pass
                st.work.set_branch_head(lot, branch, found["tip"])
            results.append(entry)
            continue
        if dry_run:
            entry["result"] = "would-merge"
            results.append(entry)
            continue
        if found.get("note"):
            source = "the merge sweep, %s" % found["note"]
        else:
            source = "branch %s (%s)" % (branch, found["how"])
        if outcome["off"]:
            # #746 : la cible par défaut ne portait pas la fusion ; la ref
            # trouvée est dite, et quoi déclarer pour l'expliciter
            source = ("%s — merged into %s, off the default target %s: declare "
                      "`git config %s %s` to make it explicit"
                      % (source, target, primary, TARGET_GIT_KEY, target))
        done = work_mod.close_merged(
            db, lot, sha=found["ref"] or "", actor=actor or "git:%s" % target,
            source=source, frozen_id=found.get("frozen_id"))
        entry.update(result=done["result"], detail="%s — %s" % (entry["detail"], done["detail"]))
        if done["result"] == "merged":
            if found.get("kind") == "content":
                delivered = "its last freeze %s is an ancestor of %s" % (
                    (found["ref"] or "?")[:12], target)
            elif found.get("note"):
                delivered = "a merge commit of %s names it" % target
            else:
                delivered = "branch %s entered %s" % (branch, target)
            text = ("Lot #%d \"%s\" delivered: %s (%s, commit %s). Closed by the runner's "
                    "merge sweep."
                    % (lot, row.get("title") or "", delivered, found["how"],
                       (found["ref"] or "?")[:12]))
            if outcome["off"]:
                text = ("%s Merged into %s, off the default target %s: declare "
                        "`git config %s %s` to make it explicit."
                        % (text, target, primary, TARGET_GIT_KEY, target))
            fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
                       text=text, project=fil.project_for(db.cfg, fil.agent_project(reg)),
                       lot=str(lot), meta={"kind": "event"})
        results.append(entry)
    return {"host": host, "dry_run": bool(dry_run), "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged")}
