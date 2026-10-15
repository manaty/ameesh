# SPDX-License-Identifier: AGPL-3.0-only
"""#1681 — l'incident de CI ouvert après un atterrissage sur la branche d'intégration.

Canon `processus/lots.md` §2 et §11 bis : quand la suite de la branche
d'intégration (`develop` pour Nexlink, `main` pour ameesh) échoue après un
atterrissage, l'outil ouvre lui-même le **lot de correction prioritaire** et
désigne l'auteur du lot fautif d'après la **carte d'impact** des tests en
échec, sans attendre qu'un agent le remarque.

Ce module porte la règle seule :

* `attribute` est **pure** — elle croise les tests en échec (leur module) avec
  les commits de l'atterrissage et leurs fichiers, **par les liens qualifiés**
  du port `test→fichiers` (`links_of` sur la carte mesurée `ameesh-carte/1`) :
  elle rend un lot candidat unique, plusieurs candidats, ou rien. Sans carte
  (`links=None`), elle rend `unknown` : rien n'est affirmé. Jamais d'accusation
  sans preuve — un candidat n'est pas une culpabilité ;
* `open_incident` écrit le lot, **une seule fois** par exécution (identité
  portée par `issue_ref = ci-incident:<repo>:<run_id>`, quel que soit l'état du
  lot), et **ne lève jamais** : un incident non écrit est rendu, pas propagé ;
* `consume` est le **consommateur de l'événement CI terminal** : il lit la
  source courante du veilleur (`ci.latest_concluded`) sur la branche
  d'intégration de chaque dépôt (`INTEGRATION`), ignore une tête remplacée, et
  ouvre l'incident. L'atterrissage lui est fourni par `lands(repo, head_sha)`
  (`[{sha, lot, author, summary, files}]`), par défaut `ci.landed_commits` (la
  comparaison GitHub du client du veilleur). C'est le contrat demandé au train
  #1467 : le manifeste persisté `integration_trains.plan` nomme les membres du
  paquet (`work_item_id`, `frozen_sha`), et la comparaison de chaque membre
  fournit ses fichiers — l'agrégat d'un paquet ne doit jamais accuser un lot du
  changement d'un autre.

Le relevé Git/API (commits de l'atterrissage) reste chez `ci` : ce module ne
parle ni à GitHub ni au réseau. Limite connue : deux passages **concurrents**
(deux hôtes dans la même fenêtre) peuvent ouvrir deux incidents pour la même
exécution, faute de verrou partagé sur le run ; les passages successifs, eux,
sont idempotents par `issue_ref`.
"""
from __future__ import annotations

import posixpath
from typing import Iterable, Optional

from . import storage, work

#: l'identité de l'incident d'une exécution, portée par `issue_ref`
PREFIX = "ci-incident:"
#: les modules qui ne sont pas des fichiers de test : un agrégat, une panne
#: d'installation — rien qui désigne un lot
PLACEHOLDERS = (
    "<suite>", "<snapshots>", "<snapshots en échec>", "<hors test>", "<module>",
    "<échec agrégé non rattaché>", "<échec du module>", "<erreurs non rattachées>",
)


def ref_of(repo: str, run_id: int) -> str:
    """La référence de l'incident d'une exécution (`<repo>:<run_id>`)."""
    return "%s%s:%s" % (PREFIX, (repo or "").strip(), int(run_id))


def _norm(path: str) -> str:
    """Un chemin de dépôt normalisé : séparateurs `/`, sans `./` ni `/` initial."""
    text = (path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _module_key(path: str) -> str:
    """Le nom de module de test d'un chemin (`tests/test_x.py` -> `test_x`)."""
    name = posixpath.basename(_norm(path))
    stem, dot, _ = name.rpartition(".")
    return stem if dot else name


def links_of(carte: dict) -> dict:
    """Le port `liens test→fichiers`, lu dans une carte **mesurée**.

    Inverse une carte `ameesh-carte/1` (`scripts/map.py`) en
    `{module de test: {fichiers}}` : un fichier est relié au test qui l'a
    réellement exécuté (`blocs.<fichier>.<bloc>.modules`), lu
    (`donnees.<fichier>`), ou atteint par une migration qu'il touche
    (`migrations.<fichier>` -> `tables.<table>`, une migration sans table
    connue étant reliée à tous les tests — comme la carte).

    Aucun lien n'est déclaré ni deviné : c'est l'adaptateur du port commun
    demandé par cx3 (#1681), pas un second producteur de carte. Sans carte sur
    l'hôte, l'appelant passe `links=None` et `attribute` rend `unknown`.
    """
    liens: dict = {}

    def add(module: str, fichier: str) -> None:
        module, fichier = (module or "").strip(), _norm(fichier)
        if module and fichier:
            liens.setdefault(module, set()).add(fichier)

    for fichier, blocs in (carte.get("blocs") or {}).items():
        for bloc in (blocs or {}).values():
            for module in (bloc or {}).get("modules") or ():
                add(module, fichier)
    for fichier, modules in (carte.get("donnees") or {}).items():
        for module in modules or ():
            add(module, fichier)
    tables = carte.get("tables") or {}
    migrations = carte.get("migrations") or {}
    all_tests = set(liens)
    for fichier, created in migrations.items():
        touched = set(created or ())
        if not touched:
            modules = all_tests
        else:
            modules = set()
            for table in touched:
                modules.update(tables.get(table) or ())
        for module in modules:
            add(module, fichier)
    return liens


def _link_files(links, module: str):
    """Les fichiers que `links` relie au module de test, ou None sans port."""
    if links is None:
        return None
    if callable(links):
        try:
            files = links(module)
        except Exception:  # noqa: BLE001 - un port muet ne relie rien
            return set()
        return {_norm(f) for f in (files or ())}
    for key in (module, _norm(module), _module_key(module)):
        if key in links:
            return {_norm(f) for f in (links[key] or ())}
    return set()


def attributable(modules: Iterable[str]) -> list[str]:
    """Les modules réellement porteurs d'un fichier de test, sans doublon."""
    out: list[str] = []
    for module in modules or ():
        path = _norm(module)
        if not path or path in PLACEHOLDERS:
            continue
        if path not in out:
            out.append(path)
    return out


def _lot_of(commit: dict) -> Optional[int]:
    value = commit.get("lot")
    try:
        return int(value) if value not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


def attribute(modules: Iterable[str], commits: Iterable[dict], links=None) -> dict:
    """La carte d'impact : tests en échec × commits de l'atterrissage.

    `modules` : chemins des fichiers de test en échec (le champ `module` du
    flux CI). `commits` : `[{sha, lot, author, summary, files}]`, les commits
    de l'atterrissage, chacun avec ses fichiers modifiés. `links` : le port
    `test→fichiers` **qualifié** — soit `{module: fichiers}`, soit un appelable
    `module -> fichiers` ; `links_of(carte)` en construit un depuis la carte
    mesurée, et `None` signifie « pas de carte sur cet hôte ».

    Rend `{status, lot, author, candidates, modules}` où `status` vaut :

    * `unique` — un seul lot **candidat** (jamais une preuve de culpabilité) ;
    * `multiple` — plusieurs lots candidats : aucun n'est désigné ;
    * `unattributed` — des commits reliés, mais aucun lot identifiable ;
    * `none` — des liens qualifiés, mais aucun commit relié ;
    * `unknown` — **aucun lien qualifié disponible** (carte absente) : rien
      n'est affirmé, pas même un candidat ;
    * `unattributable` — aucun fichier de test exploitable (agrégat, panne).

    Un commit sans lot (`lot` absent) reste un candidat sans nom : il ne peut
    pas fonder une désignation, mais il interdit `unique`.
    """
    mods = attributable(modules)
    out = {"status": "unattributable", "lot": None, "author": "",
           "candidates": [], "modules": mods}
    if not mods:
        return out
    if links is None:
        out["status"] = "unknown"
        return out
    candidates: list[dict] = []
    for commit in commits or ():
        files = [_norm(f) for f in (commit.get("files") or ())]
        hits: list[dict] = []
        for module in mods:
            covered = _link_files(links, module) or set()
            for changed in files:
                if changed in covered:
                    hits.append({"module": module, "file": changed})
        if not hits:
            continue
        candidates.append({
            "sha": str(commit.get("sha") or ""),
            "lot": _lot_of(commit),
            "author": str(commit.get("author") or ""),
            "summary": " ".join(str(commit.get("summary") or "").split())[:160],
            "hits": hits[:20],
            "files": files[:40],
        })
    lots = {c["lot"] for c in candidates if c["lot"] is not None}
    unnamed = [c for c in candidates if c["lot"] is None]
    out["candidates"] = candidates
    if not candidates:
        out["status"] = "none"
        return out
    if not lots:
        out["status"] = "unattributed"
        return out
    if len(lots) == 1 and not unnamed:
        winner = next(c for c in candidates if c["lot"] is not None)
        out.update({"status": "unique", "lot": winner["lot"],
                    "author": winner.get("author") or ""})
        return out
    out["status"] = "multiple"
    return out


def title_of(*, branch: str, run_id: int, workflow: str = "", attribution: Optional[dict] = None) -> str:
    """Le titre de l'incident : l'exécution, et le lot candidat s'il est unique."""
    where = "%s%s" % (branch or "?", " (%s)" % workflow if workflow else "")
    out = "INCIDENT CI %s rouge — run %s" % (where, int(run_id))
    attribution = attribution or {}
    if attribution.get("status") == "unique" and attribution.get("lot"):
        out += " — lot #%s candidat" % attribution["lot"]
    return out


def body_of(*, repo: str, branch: str, run_id: int, head_sha: str = "", workflow: str = "",
            url: str = "", modules: Iterable[str] = (), tests: Iterable[str] = (),
            attribution: Optional[dict] = None, author: str = "") -> str:
    """Le corps de l'incident : l'exécution, les tests, l'attribution, la suite."""
    attribution = attribution or {}
    mods = list(modules or ())
    names = list(tests or ())
    lines = [
        "Incident de CI ouvert par l'outil après un atterrissage (canon "
        "`processus/lots.md` §2 et §11 bis).",
        "",
        "- Exécution : `%s` sur `%s`, workflow `%s`, tête `%s`."
        % (repo, branch or "?", workflow or "?", (head_sha or "?")[:12]),
        "- Lien : %s" % (url or "https://github.com/%s/actions/runs/%s" % (repo, run_id)),
        "- Statut de l'attribution : **%s**." % (attribution.get("status") or "?"),
    ]
    if attribution.get("status") == "unique" and attribution.get("lot"):
        lines.append("- Lot **candidat** : #%s (auteur %s), d'après les liens mesurés "
                     "test→fichier (`ameesh-carte/1`) — un candidat, pas une preuve de "
                     "culpabilité." % (attribution["lot"], author or "?"))
    else:
        lines.append("- Aucun lot n'est désigné : %s."
                     % {"multiple": "plusieurs lots candidats, à trancher par le pilote",
                        "unattributed": "des commits reliés, mais aucun lot identifiable",
                        "none": "des liens qualifiés, mais aucun commit relié",
                        "unknown": "carte d'impact absente : aucun lien test→fichier "
                                   "qualifié sur cet hôte, rien n'est affirmé",
                        "unattributable": "échec non rattaché à un fichier de test"}
                     .get(attribution.get("status"), "attribution incomplète"))
    if mods:
        lines += ["", "Fichiers de test en échec :"]
        lines += ["- `%s`" % m for m in mods[:20]]
    if names:
        lines += ["", "Tests en échec (extraits) :"]
        lines += ["- %s" % " ".join(str(t).split())[:160] for t in names[:20]]
    candidates = attribution.get("candidates") or []
    if candidates:
        lines += ["", "Candidats (commits de l'atterrissage reliés par la carte) :"]
        for candidate in candidates[:10]:
            lines.append("- `%s` lot %s %s — %s"
                         % ((candidate.get("sha") or "?")[:12],
                            "#%s" % candidate["lot"] if candidate.get("lot") else "(sans lot)",
                            candidate.get("author") or "?",
                            candidate.get("summary") or ""))
    lines += [
        "",
        "À faire :",
        "- lot de correction **prioritaire** (§2) : il passe avant tout nouveau travail ;",
        "- reproduire l'échec sur la tête d'atterrissage, corriger à la source, témoin rouge d'abord ;",
        "- le lot candidat reste un candidat : si l'attribution n'est pas `unique`, ou si "
        "elle est `unknown`, le pilote tranche ; aucun auteur n'est accusé sans preuve.",
        "",
        "Ouvert une seule fois par exécution (`%s`)." % ref_of(repo, run_id),
    ]
    return "\n".join(lines)


def _open(ops, ref: str) -> Optional[dict]:
    """L'incident déjà écrit pour `ref`, quel que soit son état."""
    for name in ("by_issue_ref", "open_by_ref"):
        reader = getattr(ops, name, None)
        if reader is None:
            continue
        try:
            rows = reader(ref) or []
        except Exception:  # noqa: BLE001 - magasin incomplet : on essaie l'autre
            continue
        if rows:
            return rows[0]
    return None


def open_incident(db, *, repo: str, branch: str, run_id: int, head_sha: str = "",
                  workflow: str = "", url: str = "", modules: Iterable[str] = (),
                  tests: Iterable[str] = (), commits: Iterable[dict] = (), app: str = "",
                  links=None, actor: str = work.SYSTEM_SENDER) -> dict:
    """Ouvre l'incident d'une exécution rouge d'atterrissage, s'il n'existe pas.

    Rend `{id, created, status, lot, author, assigned, why}` — ne lève jamais.
    `links` est le port `test→fichiers` qualifié passé à `attribute` : sans
    lui, le statut est `unknown` et aucun lot n'est désigné.

    Le lot est créé `bug` priorité 1, **sans assigné** (une création assignée
    refuserait un auteur non réveillable et perdrait l'incident), puis l'auteur
    du lot **candidat** (`unique`) est assigné si le registre l'accepte ;
    sinon l'incident reste dans la file de l'équipe et le corps porte le nom.
    """
    ref = ref_of(repo, run_id)
    out = {"id": None, "created": False, "status": "", "lot": None, "author": "",
           "assigned": False, "why": ""}
    attribution = attribute(modules, commits, links)
    out.update({"status": attribution["status"], "lot": attribution.get("lot"),
                "author": attribution.get("author") or ""})
    ops = storage.of(db).work
    existing = _open(ops, ref)
    if existing is not None:
        out.update({"id": int(existing["id"]),
                    "why": "incident #%d déjà ouvert (état %s)"
                           % (int(existing["id"]), existing.get("state") or "?")})
        return out
    author = ""
    if attribution["status"] == "unique":
        author = attribution.get("author") or ""
    try:
        created = work.add(
            db, title=title_of(branch=branch, run_id=run_id, workflow=workflow,
                               attribution=attribution),
            type="bug", priority=1, source=ref, app=app,
            body=body_of(repo=repo, branch=branch, run_id=run_id, head_sha=head_sha,
                         workflow=workflow, url=url, modules=attribution["modules"],
                         tests=tests, attribution=attribution, author=author),
            issue_ref=ref, actor=actor)
    except Exception as exc:  # noqa: BLE001 - l'incident ne fait jamais tomber le relevé
        existing = _open(ops, ref)
        if existing is not None:
            out.update({"id": int(existing["id"]),
                        "why": "incident #%d déjà ouvert (course perdue : %s)"
                               % (int(existing["id"]), type(exc).__name__)})
            return out
        out["why"] = "incident non ouvert : %s: %s" % (type(exc).__name__, str(exc)[:200])
        return out
    out.update({"id": int(created["id"]), "created": True, "why": "ouvert"})
    if author:
        try:
            work.assign(db, int(created["id"]), author, actor=actor)
            out["assigned"] = True
        except Exception as exc:  # noqa: BLE001 - l'incident reste dans la file
            out["why"] = "ouvert (auteur %s non assigné : %s)" % (author, type(exc).__name__)
    return out


#: la branche d'intégration de chaque dépôt et le projet correspondant (canon
#: §11 bis : la suite complète tourne sur la branche d'intégration ; Nexlink
#: `develop`, ameesh `main`). Un dépôt absent n'est pas surveillé ici.
INTEGRATION = {
    "manaty/ameesh": ("main", "ameesh"),
    "manaty/nexlink": ("develop", "nexlink"),
}


def consume(db, *, integration=None, lands=None, links=None,
            actor: str = work.SYSTEM_SENDER) -> list[dict]:
    """#1681 : ouvre les incidents des exécutions **terminales rouges** de la
    branche d'intégration, une seule fois par exécution.

    L'entrée est la source courante du veilleur (`ci.latest_concluded`, le
    relevé de `ameesh notify`) : par dépôt, branche et workflow, l'exécution
    conclue la plus récente sur la dernière tête. Une tête remplacée
    (`ci.head_replaced`) est ignorée — un verdict périmé n'ouvre rien.

    `lands(repo, head_sha)` rend les commits de l'atterrissage
    (`[{sha, lot, author, summary, files}]`) : par défaut `ci.landed_commits`
    (comparaison GitHub par le client du veilleur). `integration` remplace la
    table `INTEGRATION` (`{repo: (branche, projet)}`), pour un hôte qui déclare
    d'autres dépôts. `links` est le port `test→fichiers` qualifié
    (`links_of(carte)`) : sans carte sur l'hôte, l'attribution est `unknown` et
    aucun lot n'est désigné.

    Rend une ligne par exécution examinée, ne lève jamais.
    """
    st = storage.of(db)
    table = dict(integration or INTEGRATION)
    if lands is None:
        from . import ci
        lands = ci.landed_commits
    out: list[dict] = []
    for repo, spec in table.items():
        branch, app = spec if isinstance(spec, tuple) else (spec, "")
        try:
            rows = [r for r in st.ci.latest_concluded([branch]) if r.get("repo") == repo]
        except Exception as exc:  # noqa: BLE001 - un dépôt illisible n'arrête pas les autres
            out.append({"repo": repo, "branch": branch, "run_id": None, "id": None,
                        "created": False, "why": "relevé illisible : %s" % type(exc).__name__})
            continue
        for row in rows:
            if row.get("status") != "failure":
                continue
            run_id, head_sha = int(row["run_id"]), str(row.get("head_sha") or "")
            line = {"repo": repo, "branch": branch, "app": app, "run_id": run_id, "id": None,
                    "created": False, "status": "", "lot": None, "author": "",
                    "assigned": False, "why": ""}
            try:
                if st.ci.head_replaced(repo, branch, head_sha, run_id):
                    line["why"] = "tête remplacée : verdict périmé, aucun incident"
                    out.append(line)
                    continue
            except Exception:  # noqa: BLE001 - garde indisponible : le relevé courant fait foi
                pass
            try:
                failures = st.ci.failures(repo, run_id)
            except Exception:  # noqa: BLE001 - sans échecs relevés, l'incident reste dû
                failures = []
            commits: list[dict] = []
            try:
                commits = list(lands(repo, head_sha))
            except Exception as exc:  # noqa: BLE001 - sans preuve, aucune accusation
                line["why"] = "atterrissage illisible : %s" % type(exc).__name__
            result = open_incident(
                db, repo=repo, branch=branch, run_id=run_id, head_sha=head_sha,
                workflow=str(row.get("workflow") or ""), url=str(row.get("url") or ""),
                modules=[f.get("module") for f in failures],
                tests=[f.get("test") for f in failures],
                commits=commits, app=app, links=links, actor=actor)
            line.update(result)
            if not line["why"]:
                line["why"] = "ouvert"
            out.append(line)
    return out
