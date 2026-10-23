# SPDX-License-Identifier: AGPL-3.0-only
"""L539 — la classe de revue d'un LOT : light, normal, sensitive ou release.

Le canon (processus/lots.md §8, `review_policies.risk_classes`) déclare les
portées par projet ; ce module rend la classe du **lot**, c'est-à-dire du diff
qu'il emporte depuis sa cible — `merge-base(target, frozen_sha)..frozen_sha` —
et non d'un ensemble de fichiers donné par l'appelant.

Deux axes, dans cet ordre :

* **release** : la cible est `release/*`, ou le lot est de type `release` ; un
  lot release ne se fusionne jamais tout seul (vol. B) ;
* le diff gelé, classé par la politique du projet du lot (`row["app"]`), tous
  les canons configurés lus — un chemin sensible pour l'un l'emporte, et un
  canon qui ne déclare pas ce projet ne dit rien de lui.

Tout doute rend `sensitive`, jamais `light` : lot non gelé, dépôt ou politique
illisibles, diff impossible à lire. La classe n'est qu'une **lecture** : elle
n'autorise rien à elle seule — la file de fusion garde le gel, la CI verte sur
la tête gelée, les portes et le bail.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import canon as canon_mod
from . import plan_git, review as review_mod, work

LIGHT = "light"
NORMAL = "normal"
SENSITIVE = "sensitive"
RELEASE = "release"
#: la plus haute l'emporte entre deux lectures (un chemin sensible pour l'un)
RANK = {LIGHT: 0, NORMAL: 1, SENSITIVE: 2, RELEASE: 3}
#: classe rendue dans le doute : jamais une classe rassurante par défaut.
#: Le drapeau `doubt` dit si la classe est un doute ou une lecture : deux
#: lectures de la même tête se comparent par le maximum, jamais en rétrogradant
#: une classe calculée (revue #544 r1).
DOUBT = SENSITIVE


def _normalized_target(ref) -> str:
    """Une cible ramenée à sa branche : `origin/x`, `refs/heads/x` et
    `refs/remotes/origin/x` valent `x` (revue #544 r1)."""
    text = str(ref or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def release_reason(row: dict, *targets) -> str:
    """Pourquoi ce lot est « release » (chaîne vide sinon).

    L'axe release ne dépend ni du dépôt ni du canon : une cible `release/*` —
    celle portée par le lot **ou la cible résolue à l'atterrissage**, écrite
    `origin/release/1.10` ou `refs/heads/release/1.10` — ou un lot de type
    `release` se lit sur la ligne du lot seule.
    """
    for candidate in tuple(targets) + (row.get("branch_target"),):
        value = _normalized_target(candidate)
        if value == RELEASE or value.startswith(RELEASE + "/"):
            return "target %s" % value
    if str(row.get("type") or "").strip().lower() == RELEASE:
        return "lot type release"
    return ""


def _ref(canon) -> str:
    sources = getattr(canon, "sources", None) or []
    return str(getattr(sources[0], "commit", "") or "") if sources else ""


def repository(cfg, item: dict) -> Optional[str]:
    """Le chemin du dépôt du lot, par son projet (`radar.projects`), ou None.

    Jamais d'exception : un hôte sans réglages radar n'a pas de dépôt à
    proposer, et l'appelant rend un doute.
    """
    from . import radar
    name = str(item.get("app") or "").strip()
    if cfg is None or not name:
        return None
    try:
        project = radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 - réglages radar illisibles : pas de dépôt
        return None
    return getattr(project, "repo", None) or None


def default_base(cfg, item: dict, repo: str, *, declared_only: bool = False) -> Optional[str]:
    """La cible du lot : `branch_target`, puis la cible du projet, puis celle
    du dépôt (`plan_git.default_target`, `origin/HEAD` ou main/master).

    `declared_only` (#2660) : ne retenir que les cibles **déclarées** — lot,
    projet, puis `git config ameesh.target` — et rendre None sans elles,
    jamais la branche par défaut du clone. Un clone nexlink a `origin/HEAD`
    sur main alors que son projet intègre sur develop : ce repli ferait
    vérifier un numéro de migration contre la mauvaise branche en silence.
    """
    explicit = str(item.get("branch_target") or "").strip()
    if explicit:
        return explicit
    from . import radar
    project = None
    name = str(item.get("app") or "").strip()
    if cfg is not None and name:
        try:
            project = radar.settings_of(cfg).projects.get(name)
        except Exception:  # noqa: BLE001 - même repli que `repository`
            project = None
    target = str(getattr(project, "target", "") or "").strip() if project is not None else ""
    if target:
        return target
    if declared_only:
        try:
            return plan_git.declared_target(repo)
        except Exception:  # noqa: BLE001 - dépôt illisible : l'appelant doutera
            return None
    try:
        return plan_git.default_target(repo)
    except Exception:  # noqa: BLE001 - dépôt illisible : l'appelant doutera
        return None


def _landing_ref(repo: str, target: str) -> str:
    """La référence qui lit la cible d'atterrissage `target` dans `repo`.

    `refs/remotes/<distant>/<cible>` quand le clone la connaît, sinon `target`
    tel quel (dépôt sans distant, SHA) : jamais une branche locale qu'aucun
    `fetch` ne fait avancer quand sa version distante est lisible (#3068).
    La référence est rendue **complète** : le nom court `origin/main` se
    résout d'abord en `refs/tags/origin/main`, et une étiquette homonyme
    remplacerait la cible dans le diff de classe (B1 de la relecture cx2)."""
    try:
        remote = canon_mod.remote_branch(repo, target)
        if remote:
            full = "refs/remotes/" + remote
            if plan_git._commit(repo, full):
                return full
    except canon_mod.GitError:  # sonde illisible : le diff dira son erreur
        pass
    return target


def repo_for_freeze(db, cfg, item: dict, frozen: str, *, extra_paths=(),
                    overview: list | None = None) -> tuple[str, str]:
    """Le dépôt local qui contient le commit gelé `frozen`, et ce commit résolu.

    L'hôte qui lit un lot (alerte SLO, vue) n'est pas forcément celui de son
    auteur : `radar` ne connaît pas toujours le dépôt du projet. On essaie donc,
    dans l'ordre, le dossier de travail de l'assigné (registre), le dépôt du
    projet (`repository`), tout dossier de travail local du registre, les
    chemins `extra_paths` de l'appelant, puis le dossier courant. Le premier
    dont le dépôt résout `frozen` en commit gagne.

    Rend `(dépôt, commit complet)` — le SHA résolu, jamais le préfixe reçu
    (#2564) — ou `("", "")` si aucun dépôt ne le résout : l'appelant ne conclut
    pas (jamais de classe rassurante par défaut). `overview` réutilise les
    lignes de `registry.overview` déjà lues par l'appelant (une passe = une
    lecture pour N lots, #2564) ; `None` les relit. Lecture seule.

    #2851 : la sonde est locale (`plan_git.local_commit`). Un clone partiel
    d'un autre projet dont le commit manque lançait sinon un fetch paresseux
    (~10 s) avant d'échouer, et le balayage des dossiers du registre payait ce
    délai plusieurs fois par passe d'alertes."""
    from . import registry

    frozen = str(frozen or "").strip()
    if not frozen:
        return "", ""
    # #2786 : un gel qui ne nomme pas un commit hexadécimal (`HEAD`, une
    # branche) n'est jamais résolu dans le clone de l'hôte, qui lirait une
    # tête que personne n'a gelée (#2712) ; la lecture reste en doute.
    if not plan_git.commit_id(frozen):
        return "", ""
    candidates = []
    assignee = str((item or {}).get("assignee") or "").strip()
    try:
        if assignee:
            candidates.append((registry.get(db, assignee) or {}).get("cwd") or "")
    except Exception:  # noqa: BLE001 - registre illisible : les autres pistes
        pass
    try:
        candidates.append(repository(cfg, item) or "")
    except Exception:  # noqa: BLE001 - pas de réglage radar : les autres pistes
        pass
    try:
        rows = registry.overview(db) if overview is None else overview
        candidates.extend(str(row.get("cwd") or "") for row in rows)
    except Exception:  # noqa: BLE001 - registre illisible : les autres pistes
        pass
    candidates.extend(extra_paths or ())
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass
    seen = set()
    for path in candidates:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        try:
            root = canon_mod.git_toplevel(path)
        except Exception:  # noqa: BLE001 - pas un dépôt : piste suivante
            root = None
        if not root or root in seen:
            continue
        seen.add(root)
        try:
            commit = plan_git.local_commit(root, frozen)
        except Exception:  # noqa: BLE001 - commit illisible : piste suivante
            continue
        if commit:
            return root, commit
    return "", ""


def _doubtful_files(paths) -> list:
    """Les chemins d'un diff lu mais inclassable, tous sensibles (doute) : la
    politique manque, pas le diff, et les motifs de secours de la porte
    (`merge_queue.CRITICAL_SCOPE`) doivent encore y voir SQL, la sécurité ou la
    prod (revue #2394 : un doute à un avis ne tait pas un chemin critique)."""
    return [{"path": str(path), "class": SENSITIVE, "rule": None}
            for path in paths or () if str(path or "")]


def _light_reviews(canons) -> tuple[int, str]:
    """Le plancher d'avis d'un lot léger, au même commit que la politique.

    `reviews.light_reviews` du canon (#2642) est lu par le lecteur strict
    `mechanics`, jamais recopié ici : la porte de fusion et `paquet-candidats`
    lisent la classe, donc le même plancher que la désignation. Rend
    `(plancher, problème)` : 0 sans document mécanique (canon antérieur, ou
    canon en fichiers de travail sans commit exact — la transition bornée à la
    PR home qui le pose à 1), mais un document présent et illisible rend un
    problème que l'appelant traduit en doute, jamais en zéro rassurant.
    """
    from . import mechanics
    value = 0
    for canon in canons or ():
        for source in getattr(canon, "sources", ()) or ():
            if (getattr(source, "mode", "") != "git" or not source.repo
                    or not source.commit or getattr(source, "prefix", "")):
                continue
            try:
                value = max(value, mechanics.light_reviews_at(source.repo,
                                                              source.commit))
            except mechanics.MechanicsAbsent:
                continue
            except mechanics.MechanicsError as exc:
                return value, str(exc)
    return value, ""


def _review_flags(canons) -> tuple[bool, bool, str]:
    """Les drapeaux d'avis du canon compilé, au même commit que la classe (#2726).

    `reviews.exclude_author_family` et `reviews.distinct_families` sont lus par
    le lecteur strict `mechanics` : la porte de fusion et le train les
    appliquent, ils ne sont pas recopiés ici. Le plus strict l'emporte entre
    canons : une exclusion demandée par un canon s'applique, et une double
    famille n'est levée que si tous les documents la lèvent. Rend
    `(exclude, distinct, problème)` : sans document mécanique (canon
    antérieur), le repli est le comportement historique (pas d'exclusion de
    famille, deux familles distinctes) ; un document présent et illisible rend
    un problème que l'appelant traduit en doute, jamais en repli rassurant.
    """
    from . import mechanics
    exclude, distinct = False, None
    for canon in canons or ():
        for source in getattr(canon, "sources", ()) or ():
            if (getattr(source, "mode", "") != "git" or not source.repo
                    or not source.commit or getattr(source, "prefix", "")):
                continue
            try:
                declared_exclude, declared_distinct = mechanics.review_flags_at(
                    source.repo, source.commit)
            except mechanics.MechanicsAbsent:
                continue
            except mechanics.MechanicsError as exc:
                return True, True, str(exc)
            exclude = exclude or declared_exclude
            distinct = declared_distinct if distinct is None else distinct or declared_distinct
    return exclude, True if distinct is None else distinct, ""


def _train_reviews(federation, project: Optional[str], paths) -> tuple[int, str]:
    """Le nombre d'avis déclaré par chemin (`train_reviews`, PR85).

    Lu dans le **même document** que la politique de classe, donc au même
    commit : la désignation au gel, la porte de fusion et `paquet-candidats`
    lisent le même nombre (#2696, canon §7). Le maximum des règles applicables
    l'emporte ; un avis déclaré 2 sur un chemin léger ne reclasse pas le
    chemin, il exige le second avis.

    Un bloc absent (canon antérieur) ou un projet absent du bloc ne disent
    rien : 0, sans doute. Un bloc ou une règle illisible rend un problème que
    `class_of_paths` traduit en doute, jamais en zéro rassurant.
    """
    if not isinstance(federation, dict):
        return 0, ""
    policies = federation.get("review_policies")
    if not isinstance(policies, dict):
        return 0, ""
    block = policies.get("train_reviews")
    if block is None:
        return 0, ""
    if not isinstance(block, dict) \
            or block.get("schema") != "ameesh-train-reviews-policy/1":
        return 0, "review_policies.train_reviews: unknown schema"
    projects = block.get("projects")
    if not isinstance(projects, dict):
        return 0, "review_policies.train_reviews.projects: not a mapping"
    declaration = projects.get(project)
    if declaration is None:
        return 0, ""            # ce canon ne déclare pas les avis de ce projet
    rules = declaration.get("rules") if isinstance(declaration, dict) else None
    if not isinstance(rules, list):
        return 0, ("review_policies.train_reviews.projects.%s.rules: not a list"
                   % project)
    wanted = [str(path) for path in paths or () if str(path or "")]
    value = 0
    for index, rule in enumerate(rules):
        where = ("review_policies.train_reviews.projects.%s.rules[%d]"
                 % (project, index))
        if not isinstance(rule, dict) or set(rule) != {"paths", "reviews"}:
            return 0, "%s: expected {paths, reviews}" % where
        count = rule["reviews"]
        if type(count) is not int or count not in (1, 2):
            return 0, "%s.reviews: expected 1 or 2" % where
        patterns = rule["paths"]
        if (not isinstance(patterns, list) or not patterns
                or any(not isinstance(p, str) or not p.strip() for p in patterns)):
            return 0, "%s.paths: nonempty strings required" % where
        try:
            compiled = tuple(review_mod.compile_glob(pattern) for pattern in patterns)
        except re.error as exc:
            return 0, "%s.paths: invalid pattern (%s)" % (where, exc)
        if any(rx.fullmatch(path) for path in wanted for rx in compiled):
            value = max(value, count)
    return value, ""


def class_of_paths(canons, project: Optional[str], paths) -> dict:
    """La classe des `paths` pour `project`, lue dans tous les canons.

    Rend `{class, why, files, policy_ref, doubt, project, light_reviews,
    train_reviews, exclude_author_family, distinct_families}` ; une politique
    illisible, un projet que **aucun** canon
    ne déclare (sans repli déclaré) ou des chemins inclassables rendent
    `sensitive` avec `doubt` — jamais une classe rassurante par défaut. Un
    canon qui déclare d'autres projets sans repli ne dit rien de celui-ci : il
    est ignoré, pas compté comme un doute. `light_reviews` est le plancher
    d'avis de la classe légère (#2642), lu au même commit ; la classe reste
    `light`, le plancher ne reclasse pas. `train_reviews` est le plus grand
    nombre d'avis déclaré par chemin (`train_reviews`, #2696) : la porte et
    `paquet-candidats` lisent le même nombre que la désignation au gel.
    `exclude_author_family` et `distinct_families` sont les drapeaux compilés
    du canon (#2726) : la porte exclut la famille d'un auteur et la double
    famille au compte d'avis requis au lieu de supposer un repli.
    `project` permet à la porte de n'appliquer ses motifs de secours qu'au
    projet ameesh, dont ils décrivent les chemins (#2696, cas pilote Nexlink).
    """
    canons = tuple(canons or ())
    light_reviews, light_problem = _light_reviews(canons)
    exclude_author_family, distinct_families, flags_problem = _review_flags(canons)
    if light_problem:
        return {"class": SENSITIVE, "files": _doubtful_files(paths),
                "policy_ref": "", "doubt": True, "project": project or "",
                "light_reviews": light_reviews, "train_reviews": 0,
                "exclude_author_family": True, "distinct_families": True,
                "why": "review light floor unreadable: %s" % light_problem}
    if flags_problem:
        return {"class": SENSITIVE, "files": _doubtful_files(paths),
                "policy_ref": "", "doubt": True, "project": project or "",
                "light_reviews": light_reviews, "train_reviews": 0,
                "exclude_author_family": True, "distinct_families": True,
                "why": "review flags unreadable: %s" % flags_problem}
    worst, why, files, ref = LIGHT, "", [], ""
    critical_paths = set()
    files_by_path = {}
    covered = False
    train_reviews = 0
    for canon in canons or ():
        root = str(getattr(canon, "root", "") or "")
        federation = getattr(canon, "federation", None)
        policy, problems = review_mod.policy_from_federation(federation, project)
        errors = [p for p in problems if p[1] == "error"]
        if errors and all(p[0] == review_mod.UNKNOWN_PROJECT for p in errors):
            # Ce canon ne couvre pas ce projet : sa politique n'en parle pas.
            continue
        if errors:
            return {"class": SENSITIVE, "files": _doubtful_files(paths),
                    "policy_ref": _ref(canon), "doubt": True,
                    "project": project or "",
                    "light_reviews": light_reviews,
                    "train_reviews": train_reviews,
                    "exclude_author_family": exclude_author_family,
                    "distinct_families": distinct_families,
                    "why": "review policy unreadable (%s): %s"
                           % (root or "canon", errors[0][2])}
        covered = True
        try:
            result = review_mod.classify(policy, paths)
        except review_mod.ReviewError as exc:
            return {"class": SENSITIVE, "files": _doubtful_files(paths),
                    "policy_ref": _ref(canon), "doubt": True,
                    "project": project or "",
                    "light_reviews": light_reviews,
                    "train_reviews": train_reviews,
                    "exclude_author_family": exclude_author_family,
                    "distinct_families": distinct_families,
                    "why": "cannot classify the change: %s" % exc}
        declared, declared_problem = _train_reviews(
            federation, project, [entry["path"] for entry in result["files"]])
        if declared_problem:
            return {"class": SENSITIVE, "files": _doubtful_files(paths),
                    "policy_ref": _ref(canon), "doubt": True,
                    "project": project or "",
                    "light_reviews": light_reviews,
                    "train_reviews": train_reviews,
                    "exclude_author_family": exclude_author_family,
                    "distinct_families": distinct_families,
                    "why": "review train counts unreadable (%s): %s"
                           % (root or "canon", declared_problem)}
        train_reviews = max(train_reviews, declared)
        if not ref:
            ref = _ref(canon)
        critical_paths.update(entry["path"] for entry in result["files"]
                              if entry.get("critical") is True)
        for entry in result["files"]:
            previous = files_by_path.get(entry["path"])
            if previous is None or RANK[entry["class"]] > RANK[previous["class"]]:
                files_by_path[entry["path"]] = entry
        # #2374 : un diff entièrement léger garde ses chemins. Sans eux, la
        # porte des avis (et l'ouverture de la relecture après fusion) ne peut
        # pas constater un chemin critique que le canon a classé `light`.
        if not files or RANK[result["class"]] > RANK[worst]:
            worst, files = result["class"], result["files"]
            why = "policy of %s" % (root or "canon")
    if not covered:
        return {"class": SENSITIVE, "files": _doubtful_files(paths), "policy_ref": "",
                "doubt": True, "project": project or "", "light_reviews": light_reviews,
                "train_reviews": 0,
                "exclude_author_family": exclude_author_family,
                "distinct_families": distinct_families,
                "why": "project %r is not declared by any canon (no fallback policy)"
                       % (project,)}
    # Le maximum du lot ne suffit pas : une exception de la porte (Markdown
    # léger) doit lire la classe la plus haute de chaque fichier, tous canons.
    files = [files_by_path[entry["path"]] for entry in files]
    for entry in files:
        if entry["path"] in critical_paths:
            entry["critical"] = True
    return {"class": worst, "why": why, "files": files, "policy_ref": ref,
            "doubt": False, "project": project or "", "light_reviews": light_reviews,
            "train_reviews": train_reviews,
            "exclude_author_family": exclude_author_family,
            "distinct_families": distinct_families}


def of_lot(db, lot: int, *, cfg=None, item: Optional[dict] = None,
           repo: Optional[str] = None, base: Optional[str] = None,
           head: Optional[str] = None, milestones: Optional[list] = None,
           canons=None, base_sha: Optional[str] = None) -> dict:
    """La classe du lot `lot` ; ne lève jamais (tout doute rend `sensitive`).

    `item` évite une lecture de la base ; `repo`, `base` et `head` sont ceux
    que l'appelant a déjà résolus (la file de fusion connaît la tête gelée et
    le dépôt du clone). `base_sha` est la révision exacte de la cible quand
    l'appelant l'a déjà lue (la file de fusion à l'admission, #2318) : `base`
    reste son nom dans la trace, et le diff se lit depuis ce commit — une ref
    peut bouger entre la lecture et la classification. `milestones` et
    `canons` (liste ou lecteur différé) réutilisent les lectures groupées de
    l'appelant. Rend aussi `files`, `policy_ref`, `target`, `frozen_sha`,
    `why` et `doubt` (vrai quand la classe vient d'une lecture impossible, pas
    d'une politique), `light_reviews` (le plancher d'avis d'un lot léger,
    #2642), `train_reviews` avec `project` (le barème par chemin et le
    projet qui décide du repli de la porte, #2696) et les drapeaux compilés
    `exclude_author_family` / `distinct_families` (#2726) pour la trace.
    """
    out: dict = {"lot": int(lot), "class": SENSITIVE, "why": "", "files": [],
                 "policy_ref": "", "repo": repo or "", "target": base or "",
                 "base_sha": "", "frozen_sha": head or "", "doubt": True,
                 "project": "", "exclude_author_family": False,
                 "distinct_families": True}
    row = item if item is not None else work.get(db, lot)
    if row is None:
        out["why"] = "lot #%d not found" % lot
        return out
    out["project"] = str(row.get("app") or "")
    reason = release_reason(row)
    if reason:
        out.update({"class": RELEASE, "why": reason, "doubt": False})
        return out
    if not repo:
        repo = repository(cfg, row)
    out["repo"] = repo or ""
    if not repo:
        out["why"] = "no repository known for the lot"
        return out
    if not head:
        freeze = plan_git.latest_freeze(work.milestones(db, lot) if milestones is None
                                        else milestones)
        if not freeze or not str(freeze.get("sha") or "").strip():
            out["why"] = "lot #%d is not frozen" % lot
            return out
        frozen = str(freeze["sha"]).strip()
        # #2786 : jamais de ref symbolique résolue ici (le `HEAD` d'un autre
        # clone a classé/fermé le mauvais commit, #2712).
        if not plan_git.commit_id(frozen):
            out["why"] = ("the freeze %r is not a commit id (the last freeze must "
                          "name the full commit)" % (frozen[:24],))
            return out
        try:
            head = plan_git._commit(repo, frozen)
        except canon_mod.GitError as exc:
            out["why"] = "cannot read the freeze: %s" % exc
            return out
        if not head:
            out["why"] = "the freeze %s is not a commit of %s" % (frozen[:12], repo)
            return out
    out["frozen_sha"] = head
    # La cible résolue à l'atterrissage (celle du projet, ou `origin/HEAD`) peut
    # être `release/*` sans que la ligne du lot le dise : l'axe release se lit
    # sur elle aussi, normalisée (revue #544 r1). Mais quand l'appelant nomme
    # déjà la base (commit de cible frais, cible du paquet), elle seule fait
    # foi : relire `origin/HEAD` d'un clone pour la surclasser rendait
    # « release » un lot visant `main` (#2464, clone radar figé sur une release
    # périmée).
    landing = None if base else default_base(cfg, row, repo)
    if not base:
        base = landing
    out["target"] = base or ""
    out["base_sha"] = str(base or "")
    reason = release_reason(row, base)
    if reason:
        out.update({"class": RELEASE, "why": reason, "doubt": False})
        return out
    # #3068 : une cible que l'appelant n'a pas nommée (vue `work show`,
    # stagnation) se lit sur `origin/<cible>` avant la branche locale, comme la
    # file (#2207) : dans le clone partagé des agents, `main` local n'avance
    # pas, et le diff depuis lui rapportait au lot tout ce qui a été fusionné
    # depuis (#2955 léger affiché sensible, 60 fichiers au lieu de 3).
    revision = base_sha or (_landing_ref(repo, base) if landing else base)
    if not revision:
        out["why"] = "no target branch: freeze the lot or name its target"
        return out
    out["base_sha"] = str(revision)
    try:
        paths = canon_mod.git_range_paths(repo, revision, head)
    except canon_mod.GitError as exc:
        out["why"] = "cannot read the diff %s..%s: %s" % (revision, head[:12], exc)
        return out
    if not paths:
        out.update({"class": LIGHT, "why": "no file changed since %s" % revision,
                    "doubt": False})
        return out
    if canons is None:
        canons = work.configured_canons(cfg)
    elif callable(canons):
        canons = canons()
    if not canons:
        out["files"] = _doubtful_files(paths)
        out["why"] = "no readable canon: review policy unknown"
        return out
    result = class_of_paths(canons, row.get("app"), paths)
    out.update(result)
    out["why"] = "%s; %d file(s) since %s" % (result.get("why") or "policy",
                                              len(paths), revision)
    return out
