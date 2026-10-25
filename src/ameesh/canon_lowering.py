# SPDX-License-Identifier: AGPL-3.0-only
"""#3554 — une baisse d'exigence du canon demande deux avis.

Un diff du canon ne se classait que par ses **chemins** : `federation.yaml`
tombe sous le défaut de la politique ameesh (`light`), si bien qu'une PR qui
retirait une règle de secrets passait à un avis comme un changement de virgule.
Rien ne distinguait un durcissement (home#176 : 18 fichiers de 1 à 2 avis) d'une
baisse (#3545, le plancher posé à la main par le pilote).

Ce module compare les **politiques compilées** (`mechanics.load`) au commit de
base et à la tête, puis évalue `reviews_for` sur les fichiers suivis des dépôts
des projets déclarés, comme la mesure de home#176. Un fichier qui perd un avis
ou une classe rend un plancher de deux avis (familles indépendantes, canon §2) ;
un durcissement ou un changement neutre ne change rien.

Déclencheur : un lot qui touche `federation.yaml`, `processus/mecanique.yaml`
ou `agents/**`. Hors de ces chemins, aucun coût : le module ne lit rien.

« Projets déclarés » : les noms de `review_policies.risk_classes.projects` du
canon. Leur dépôt est celui que déclare `radar.projects` (le clone local de
l'hôte) ; un projet sans dépôt lisible est **non mesuré** et nommé au journal,
jamais supposé sain. Un projet dont la politique était lisible et ne l'est plus
à la tête (bloc retiré, document absent, YAML invalide) est une baisse : deux
avis, avec la raison au journal.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Iterable, Optional

from . import canon as canon_mod
from . import mechanics, plan_git, radar, review

#: les fichiers du canon dont un diff peut changer la politique de revue
POLICY_FILES = ("federation.yaml", "processus/mecanique.yaml")
#: préfixe des fiches d'agents (une fiche peut changer le harnais d'un rôle)
AGENTS_PREFIX = "agents/"
#: préfixe du journal : une ligne par tête, jamais réécrite
PREFIX = "canon-review-lowering:"
#: nombre de fichiers nommés au journal (le reste est compté)
MAX_LISTED = 20


def touches(paths: Iterable) -> bool:
    """Vrai si le diff touche un fichier qui porte la politique du canon."""
    for raw in paths or ():
        path = str(raw or "").strip()
        if not path:
            continue
        if path in POLICY_FILES or path.startswith(AGENTS_PREFIX):
            return True
    return False


@dataclass(frozen=True)
class Lowered:
    """Un fichier suivi qui perd un avis ou une classe entre base et tête."""

    project: str
    path: str
    before_class: str
    after_class: str
    before_reviews: int
    after_reviews: int

    def to_dict(self) -> dict:
        return {"project": self.project, "path": self.path,
                "before_class": self.before_class,
                "after_class": self.after_class,
                "before_reviews": self.before_reviews,
                "after_reviews": self.after_reviews}


@dataclass(frozen=True)
class Scan:
    """Un dépôt de projet effectivement mesuré."""

    project: str
    repo: str
    ref: str
    commit: str
    files: int
    base_unreadable: int = 0

    def to_dict(self) -> dict:
        return {"project": self.project, "repo": self.repo, "ref": self.ref,
                "commit": self.commit, "files": self.files,
                "base_unreadable": self.base_unreadable}


@dataclass(frozen=True)
class Detection:
    """Le résultat d'une comparaison ; `floor` vaut 2 sur une baisse ou un doute."""

    base: str
    head: str
    lowered: tuple = ()
    hardened: int = 0
    unchanged: int = 0
    scans: tuple = ()
    #: ((projet, raison), …) : politique compilable mais dépôt illisible
    unmeasured: tuple = ()
    #: une comparaison impossible est un doute, pas un « rien n'a baissé »
    errors: tuple = ()
    note: str = ""

    @property
    def floor(self) -> int:
        return 2 if (self.lowered or self.errors) else 0

    def to_dict(self) -> dict:
        return {"base": self.base, "head": self.head, "floor": self.floor,
                "lowered": [item.to_dict() for item in self.lowered],
                "hardened": self.hardened, "unchanged": self.unchanged,
                "scans": [item.to_dict() for item in self.scans],
                "unmeasured": [list(item) for item in self.unmeasured],
                "errors": list(self.errors), "note": self.note}

    def journal_text(self) -> str:
        return journal_text(self)


def _base_commit(repo: str, ref) -> Optional[str]:
    """Le commit local d'une ref, sans fetch paresseux (#2851, clone partiel)."""
    text = str(ref or "").strip()
    if not text:
        return None
    return plan_git.local_commit(repo, text)


def _merge_base(repo: str, base: str, head: str) -> str:
    """La base du diff du lot (`merge-base`), ou la base lue si git se tait."""
    try:
        out = plan_git._out(repo, "merge-base", base, head)
    except Exception:  # noqa: BLE001 - dépôt illisible : la base lue reste le doute
        out = None
    return str(out or "").strip() or base


def _absent(exc: Exception, path: str) -> bool:
    return isinstance(exc, mechanics.MechanicsAbsent) and path in str(exc)


@functools.lru_cache(maxsize=64)
def _load_cached(repo: str, commit: str, project: str):
    """`mechanics.load` mémoïsé : un commit ne change pas, une passe non plus."""
    return mechanics.load(repo, commit, project)


def _load(repo: str, commit: str, project: str):
    """`(paramètres, erreur)` : ne lève jamais pour laisser l'appelant conclure."""
    try:
        return _load_cached(repo, commit, project), None
    except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
        return None, exc


@functools.lru_cache(maxsize=64)
def _tracked(repo: str, commit: str) -> tuple:
    """Les fichiers **suivis** au commit : blobs ordinaires et liens symboliques.

    Les sous-modules ne sont pas des fichiers du dépôt (leur contenu vit
    ailleurs) ; un lien symbolique, lui, est un chemin que la politique classe.
    """
    proc = canon_mod._git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    if proc.returncode != 0:
        raise canon_mod.GitError(
            "git ls-tree: %s"
            % proc.stderr.decode("utf-8", "replace").strip())
    paths = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _tab, path = entry.partition(b"\t")
        fields = meta.decode("utf-8", "replace").split(" ")
        if len(fields) != 3 or fields[1] != "blob":
            continue
        paths.append(path.decode("utf-8", "replace"))
    return tuple(sorted(paths))


def _project_repository(cfg, project: str):
    """`(racine, cible)` du dépôt déclaré du projet, ou `(None, raison)`."""
    try:
        settings = radar.settings_of(cfg)
    except Exception as exc:  # noqa: BLE001 - réglages radar illisibles
        return None, "radar settings unreadable: %s: %s" % (
            type(exc).__name__, str(exc)[:160])
    entry = settings.projects.get(project)
    if entry is None:
        return None, "no repository declared for project %r" % (project,)
    path = os.path.expanduser(str(getattr(entry, "repo", "") or "").strip())
    if not path:
        return None, "no repository declared for project %r" % (project,)
    if not os.path.isdir(path):
        return None, "repository of project %r is missing: %s" % (project, path)
    try:
        root = canon_mod.git_toplevel(path)
    except Exception:  # noqa: BLE001 - pas un dépôt : non mesurable
        root = None
    if not root:
        return None, "repository of project %r is not a git checkout: %s" % (
            project, path)
    target = str(getattr(entry, "target", "") or "").strip()
    if not target:
        try:
            target = plan_git.default_target(root) or ""
        except Exception:  # noqa: BLE001 - pas de cible par défaut : HEAD
            target = ""
    return (root, target), ""


def _revision(repo: str, target: str):
    """`(ref, commit)` de la cible du projet, sinon `HEAD`, sinon rien."""
    candidates = []
    if target:
        remote = canon_mod.remote_branch(repo, target)
        if remote:
            candidates.append("refs/remotes/" + remote)
        candidates.append(target)
    candidates.append("HEAD")
    for ref in candidates:
        # `local_commit` : jamais de fetch paresseux pour une sonde de présence
        # (#2851), et une tête absente localement est un « non mesuré » explicite.
        commit = plan_git.local_commit(repo, ref)
        if commit:
            return ref, commit
    return "", ""


def _scan_project(canon_repo: str, base: str, head: str, project: str,
                  repo: str, commit: str):
    """Compare `reviews_for` sur les fichiers suivis du dépôt d'un projet.

    Mémoïsé par (canon, base, tête, projet, dépôt, commit du dépôt) : un commit
    ne change pas, et la porte relit la même tête plusieurs fois.
    """
    return _scan_cached(canon_repo, base, head, project, repo, commit)


@functools.lru_cache(maxsize=32)
def _scan_cached(canon_repo: str, base: str, head: str, project: str,
                 repo: str, commit: str):
    base_loaded = _load_cached(canon_repo, base, project)
    head_loaded = _load_cached(canon_repo, head, project)
    lowered = []
    hardened = unchanged = base_unreadable = 0
    tracked = _tracked(repo, commit)
    for path in tracked:
        try:
            before = base_loaded.reviews_for([path])
        except (mechanics.MechanicsError, review.ReviewError):
            # Une politique de base qui ne classe pas ce chemin n'est pas une
            # baisse imputable à ce diff : elle est comptée, pas inventée.
            base_unreadable += 1
            continue
        try:
            after = head_loaded.reviews_for([path])
        except (mechanics.MechanicsError, review.ReviewError):
            # La tête ne sait plus exiger : c'est une baisse (le doute aussi).
            lowered.append(Lowered(project, path, before.risk, "indécise",
                                   before.reviews, 0))
            continue
        if (review.RANK[after.risk] < review.RANK[before.risk]
                or after.reviews < before.reviews):
            lowered.append(Lowered(project, path, before.risk, after.risk,
                                   before.reviews, after.reviews))
        elif (review.RANK[after.risk] > review.RANK[before.risk]
              or after.reviews > before.reviews):
            hardened += 1
        else:
            unchanged += 1
    return tuple(lowered), hardened, unchanged, base_unreadable, len(tracked)


def _same_policy(before, after) -> bool:
    """Vrai si deux `mechanics.load` portent la même politique de revue."""
    return (before.review_policy, before.rules, before.light_reviews) \
        == (after.review_policy, after.rules, after.light_reviews)


def _scan(cfg, canon_repo: str, base: str, head: str, project: str):
    """Le scan d'un projet : `(baisses, durcis, inchangés, scan, non mesuré)`."""
    found, reason = _project_repository(cfg, project)
    if found is None:
        return (), 0, 0, None, (project, reason)
    repo, target = found
    ref, commit = _revision(repo, target)
    if not commit:
        return (), 0, 0, None, (project, "no readable revision for %s" % (repo,))
    try:
        items, plus, same, unreadable, total = _scan_project(
            canon_repo, base, head, project, repo, commit)
    except (canon_mod.GitError, mechanics.MechanicsError, OSError) as exc:
        return (), 0, 0, None, (project, "scan impossible: %s: %s"
                                % (type(exc).__name__, str(exc)[:160]))
    scan = Scan(project=project, repo=repo, ref=ref, commit=commit,
                files=total, base_unreadable=unreadable)
    return items, plus, same, scan, None


def for_lot(cfg, repo: str, base, head, paths: Iterable) -> Optional[Detection]:
    """La comparaison pour un lot du canon, ou None si elle ne s'applique pas.

    None : le diff ne touche pas la politique, ou le dépôt n'est pas un canon
    lisible (pas de `federation.yaml` à la base) — un lot ordinaire ne paie
    rien. Une comparaison impossible **après** ce constat rend une `Detection`
    en erreur (plancher 2) : la politique d'un canon qu'on ne sait plus lire ne
    se suppose pas inchangée.
    """
    if not str(repo or "").strip() or not touches(paths):
        return None
    base_ref = str(base or "").strip()
    head_ref = str(head or "").strip()
    base_commit = _base_commit(repo, base_ref)
    head_commit = _base_commit(repo, head_ref)
    if not base_commit or not head_commit:
        return None
    if base_commit == head_commit:
        return None
    start = _merge_base(repo, base_commit, head_commit)
    if not start:
        return Detection(base=base_commit, head=head_commit,
                         errors=("merge-base illisible",))
    try:
        base_projects = mechanics.declared_projects(repo, start)
    except mechanics.MechanicsAbsent:
        # Pas de canon à la base : un ajout de politique ne peut rien abaisser
        # (le lot part d'un canon qui n'en avait pas).
        return None
    except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
        return Detection(base=start, head=head_commit,
                         errors=("federation.yaml illisible à la base: %s"
                                 % (str(exc)[:240],)))
    try:
        head_projects = mechanics.declared_projects(repo, head_commit)
    except mechanics.MechanicsAbsent:
        return Detection(base=start, head=head_commit,
                         errors=("federation.yaml absent à la tête",))
    except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
        return Detection(base=start, head=head_commit,
                         errors=("federation.yaml illisible à la tête: %s"
                                 % (str(exc)[:240],)))
    declared = sorted(set(base_projects) | set(head_projects))
    lowered: list = []
    hardened = unchanged = 0
    scans: list = []
    unmeasured: list = []
    errors: list = []
    for project in declared:
        base_loaded, base_error = _load(repo, start, project)
        head_loaded, head_error = _load(repo, head_commit, project)
        if base_loaded is not None and head_loaded is not None:
            items, plus, same, scan, missing = _scan(
                cfg, repo, start, head_commit, project)
            lowered.extend(items)
            hardened += plus
            unchanged += same
            if scan is not None:
                scans.append(scan)
            if missing is not None:
                unmeasured.append(missing)
                if not _same_policy(base_loaded, head_loaded):
                    # La politique du projet a changé mais son dépôt manque :
                    # sans les fichiers, impossible de prouver un durcissement.
                    errors.append("politique de %s modifiée mais dépôt illisible: %s"
                                  % (project, missing[1]))
            continue
        if base_loaded is not None and head_loaded is None:
            if _absent(head_error, mechanics.PATH) \
                    or _absent(head_error, mechanics.FEDERATION_PATH):
                errors.append("politique de %s retirée à la tête" % (project,))
            else:
                errors.append("politique de %s illisible à la tête: %s"
                              % (project, str(head_error)[:200]))
            continue
        if project in base_projects and project not in head_projects:
            errors.append("projet %s retiré de la politique à la tête" % (project,))
            continue
        if base_loaded is None and project in base_projects:
            # La base ne compilait déjà pas ce projet (barème incomplet) : le
            # diff ne peut pas lui faire perdre un avis mesurable.
            unmeasured.append((project, "politique incomplète à la base: %s"
                               % (str(base_error)[:160],)))
    note = ""
    if not declared:
        note = "aucun projet déclaré dans la politique"
    return Detection(base=start, head=head_commit, lowered=tuple(lowered),
                     hardened=hardened, unchanged=unchanged,
                     scans=tuple(scans), unmeasured=tuple(unmeasured),
                     errors=tuple(errors), note=note)


def journal_text(lowering, *, limit: int = MAX_LISTED) -> str:
    """La ligne de journal d'une baisse (ou d'un doute), sans jamais lever."""
    data = (lowering.to_dict() if isinstance(lowering, Detection)
            else dict(lowering or {}))
    base = str(data.get("base") or "")[:12]
    head = str(data.get("head") or "")[:12]
    if not data.get("floor"):
        return ""
    parts = ["%s 2 avis — comparaison des politiques compilées %s..%s"
             % (PREFIX, base or "?", head or "?")]
    errors = [str(item) for item in data.get("errors") or () if str(item or "")]
    if errors:
        parts.append("doute: %s" % "; ".join(errors[:4]))
    items = list(data.get("lowered") or ())
    if items:
        names = []
        for item in items[:limit]:
            names.append("%s:%s %s/%s→%s/%s"
                         % (item.get("project"), item.get("path"),
                            item.get("before_class"), item.get("before_reviews"),
                            item.get("after_class"), item.get("after_reviews")))
        tail = ""
        if len(items) > limit:
            tail = " (+%d autre(s))" % (len(items) - limit,)
        parts.append("%d fichier(s) suivis perdent un avis ou une classe: %s%s"
                     % (len(items), "; ".join(names), tail))
    scans = list(data.get("scans") or ())
    if scans:
        parts.append("mesuré: %s" % ", ".join(
            "%s@%s (%s fichiers)" % (item.get("project"), item.get("ref"),
                                     item.get("files"))
            for item in scans[:6]))
    unmeasured = list(data.get("unmeasured") or ())
    if unmeasured:
        parts.append("non mesuré(s): %s" % "; ".join(
            "%s (%s)" % (name, why) for name, why in unmeasured[:8]))
    note = str(data.get("note") or "").strip()
    if note:
        parts.append(note)
    text = " — ".join(parts)
    return text[:6000]
