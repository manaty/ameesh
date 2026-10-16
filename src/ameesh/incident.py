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
* `diagnosis_of` lit le **rapport factuel commun** `ameesh-train-diagnosis/1`
  (#1467) quand il vise la tête exacte : il en rend les faits (paquet, jobs
  rouges avec leur tentative, environnement, attribution) et les manques
  (`missing`), sans en tirer d'exemption, de retrait ni d'accusation. Un lot
  « prouvé » sans reçu d'exécution locale (`environment.receipts`) retombe en
  candidat : jamais « prouvé » sans reçu. Le module ne dépend pas de #1467 à
  l'exécution, le rapport lui est fourni ;
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
  changement d'un autre. La carte d'impact elle aussi se qualifie par ligne :
  `links_for(repo, head_sha)` (#1467) rend la carte du dépôt et de la tête
  exacts, `None` laissant `unknown` — une carte globale, valable pour tous les
  dépôts et toutes les têtes, attribuerait le lot d'un autre paquet.

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
#: le schéma du rapport factuel commun produit par le train (#1467, #1616) —
#: lu comme une donnée, sans dépendre de son module à l'exécution
DIAGNOSIS_SCHEMA = "ameesh-train-diagnosis/1"
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


def _dicts(value) -> list:
    """Les entrées dict d'une liste, sans rien inventer."""
    return [entry for entry in value or () if isinstance(entry, dict)]


def _texts(value) -> list:
    """Les phrases non vides d'une liste, espaces normalisés."""
    out: list[str] = []
    for entry in value or ():
        text = " ".join(str(entry).split())
        if text:
            out.append(text)
    return out


def _declared(entry: dict) -> str:
    """Un candidat déclaré, décrit par sa seule identité lisible."""
    lot = entry.get("lot", entry.get("work_item_id", entry.get("id")))
    if lot not in (None, ""):
        return "#%s" % lot
    return str(entry.get("sha") or entry.get("file") or "?")[:12]


def diagnosis_of(report, *, head_sha: str = "") -> Optional[dict]:
    """Le rapport factuel commun `ameesh-train-diagnosis/1` (#1467), lu comme donnée.

    N'accepte que le schéma connu et une **tête attendue non vide** égale à
    `packet.head` : un rapport d'une autre tête, d'un autre schéma
    ou illisible rend `None`, et l'appelant garde son propre relevé.

    Le rapport n'autorise rien par lui-même : ni exemption d'environnement, ni
    retrait de lot, ni accusation. Ce lecteur en rend les faits (paquet, jobs
    rouges avec leur tentative, environnement, attribution) et les manques
    (`missing`). Un lot d'`attribution.proven_lots` sans **reçu** d'exécution
    locale (`environment.receipts`) n'est pas cru prouvé : il retombe en
    candidat et le manque est ajouté — jamais « prouvé » sans reçu (#1467).
    """
    if not isinstance(report, dict) or report.get("schema") != DIAGNOSIS_SCHEMA:
        return None
    packet = report.get("packet") if isinstance(report.get("packet"), dict) else {}
    head = str(packet.get("head") or "")
    if not head_sha or head != str(head_sha):
        return None
    environment = report.get("environment") \
        if isinstance(report.get("environment"), dict) else {}
    attribution = report.get("attribution") \
        if isinstance(report.get("attribution"), dict) else {}
    # Une liste illisible n'est pas une liste vide ni un reçu : rejeter le
    # rapport entier conserve le relevé local sans inventer de fait.
    dict_lists = [report.get("ci"), environment.get("receipts"),
                  attribution.get("candidates"), attribution.get("proven_lots")]
    if any(not isinstance(value, list) or
           any(not isinstance(entry, dict) for entry in value) for value in dict_lists):
        return None
    if any(not isinstance(run.get("failed_jobs"), list) or
           any(not isinstance(job, dict) for job in run["failed_jobs"])
           for run in report["ci"]):
        return None
    if not isinstance(report.get("missing"), list) or \
            any(not isinstance(phrase, str) for phrase in report["missing"]):
        return None
    receipts = [receipt for receipt in environment["receipts"] if receipt]
    candidates = _dicts(attribution.get("candidates"))
    proven = _dicts(attribution.get("proven_lots"))
    missing = _texts(report.get("missing"))
    attribution_status = str(attribution.get("status") or "unknown")
    if proven and not receipts:
        candidates = candidates + proven
        proven = []
        attribution_status = "unknown"
        missing.append("reçu d'exécution local absent : les lots cités prouvés "
                       "retombent en candidats")
    runs = [{"workflow": str(run.get("workflow") or ""),
             "run": run.get("run"), "attempt": run.get("attempt"),
             "failed_jobs": [{"id": job.get("id"), "name": str(job.get("name") or "")}
                             for job in _dicts(run.get("failed_jobs"))]}
            for run in _dicts(report.get("ci"))]
    return {
        "schema": DIAGNOSIS_SCHEMA,
        "packet": {field: str(packet.get(field) or "")
                   for field in ("source", "target", "base", "head", "tree")},
        "environment": {"status": str(environment.get("status") or "unknown"),
                        "receipts": len(receipts)},
        "attribution": {"status": attribution_status,
                        "candidates": candidates, "proven_lots": proven},
        "ci": runs,
        "missing": missing,
    }


def title_of(*, branch: str, run_id: int, workflow: str = "", attribution: Optional[dict] = None) -> str:
    """Le titre de l'incident : l'exécution, et le lot candidat s'il est unique."""
    where = "%s%s" % (branch or "?", " (%s)" % workflow if workflow else "")
    out = "INCIDENT CI %s rouge — run %s" % (where, int(run_id))
    attribution = attribution or {}
    if attribution.get("status") == "unique" and attribution.get("lot"):
        out += " — lot #%s candidat" % attribution["lot"]
    return out


def _diagnosis_lines(diagnosis: dict) -> list[str]:
    """Les faits du rapport commun, sans rien en tirer de plus."""
    packet = diagnosis.get("packet") or {}
    environment = diagnosis.get("environment") or {}
    attribution = diagnosis.get("attribution") or {}
    lines = [
        "",
        "Diagnostic factuel (`%s`) : paquet `%s` → `%s`, base `%s`, tête `%s`, "
        "arbre `%s`."
        % (diagnosis.get("schema") or DIAGNOSIS_SCHEMA,
           (packet.get("source") or "?")[:60], (packet.get("target") or "?")[:60],
           (packet.get("base") or "?")[:12], (packet.get("head") or "?")[:12],
           (packet.get("tree") or "?")[:12]),
        "- Environnement : **%s** (%d reçu(s) d'exécution locale)."
        % (environment.get("status") or "unknown", int(environment.get("receipts") or 0)),
        "- Attribution : **%s** — ce rapport ne désigne aucun lot."
        % (attribution.get("status") or "unknown"),
    ]
    for run in diagnosis.get("ci") or ():
        jobs = ["%s%s" % (job.get("name") or "?",
                          " (#%s)" % job["id"] if job.get("id") else "")
                for job in run.get("failed_jobs") or ()]
        lines.append("- Job(s) rouge(s) : workflow `%s`, run `%s`, tentative `%s` — %s."
                     % (run.get("workflow") or "?", run.get("run") or "?",
                        run.get("attempt") or "?", ", ".join(jobs) or "aucun job nommé"))
    declared = attribution.get("candidates") or []
    if declared:
        lines.append("- Candidats **déclarés** par le rapport (à trancher, jamais une "
                     "désignation) : %s." % ", ".join(_declared(c) for c in declared[:10]))
    missing = diagnosis.get("missing") or []
    if missing:
        lines.append("- Manques déclarés par le rapport :")
        lines += ["  - %s" % phrase for phrase in missing[:10]]
    return lines


def body_of(*, repo: str, branch: str, run_id: int, head_sha: str = "", workflow: str = "",
            url: str = "", modules: Iterable[str] = (), tests: Iterable[str] = (),
            attribution: Optional[dict] = None, author: str = "",
            diagnosis: Optional[dict] = None) -> str:
    """Le corps de l'incident : l'exécution, les tests, l'attribution, le
    diagnostic factuel commun s'il est fourni, la suite."""
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
    if diagnosis:
        lines += _diagnosis_lines(diagnosis)
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


def _lot_author(db, lot) -> str:
    """L'agent assigné au lot candidat, ou "" (lot introuvable, sans assigné,
    magasin illisible) : sans nom sûr, l'incident reste dans la file."""
    try:
        row = work.get(db, int(lot)) or {}
    except Exception:  # noqa: BLE001 - sans nom sûr, personne n'est désigné
        return ""
    return str(row.get("assignee") or "")


def open_incident(db, *, repo: str, branch: str, run_id: int, head_sha: str = "",
                  workflow: str = "", url: str = "", modules: Iterable[str] = (),
                  tests: Iterable[str] = (), commits: Iterable[dict] = (), app: str = "",
                  links=None, diagnosis: Optional[dict] = None,
                  actor: str = work.SYSTEM_SENDER) -> dict:
    """Ouvre l'incident d'une exécution rouge d'atterrissage, s'il n'existe pas.

    Rend `{id, created, status, lot, author, assigned, why}` — ne lève jamais.
    `links` est le port `test→fichiers` qualifié passé à `attribute` : sans
    lui, le statut est `unknown` et aucun lot n'est désigné. `diagnosis` est le
    rapport commun lu par `diagnosis_of` : il est versé au corps comme fait,
    sans désigner de lot.

    Le lot est créé `bug` priorité 1, **sans assigné** (une création assignée
    refuserait un auteur non réveillable et perdrait l'incident), puis l'auteur
    du lot **candidat** (`unique`) est assigné si le registre l'accepte ;
    sinon l'incident reste dans la file de l'équipe et le corps porte le nom.
    L'auteur est l'agent assigné au lot candidat, jamais l'auteur Git du
    commit : un commit de fusion peut porter le compte de l'owner ou du
    composeur, pas celui de l'agent qui a fait le lot.
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
        author = _lot_author(db, attribution.get("lot"))
        out["author"] = author
    try:
        created = work.add(
            db, title=title_of(branch=branch, run_id=run_id, workflow=workflow,
                               attribution=attribution),
            type="bug", priority=1, source=ref, app=app,
            body=body_of(repo=repo, branch=branch, run_id=run_id, head_sha=head_sha,
                         workflow=workflow, url=url, modules=attribution["modules"],
                         tests=tests, attribution=attribution, author=author,
                         diagnosis=diagnosis),
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


def _qualified_card(links_for, repo: str, head_sha: str):
    """La carte qualifiée sur (dépôt, tête) rendue par le port `links_for`.

    Rend `(carte, verdict)`. Le port est appelé **par ligne**, sur la tête
    observée : `None`, une exception, ou une tête non observée signifie « pas de
    carte qualifiée » — le statut reste `unknown`, jamais de repli sur une carte
    globale, qui attribuerait le lot d'un autre dépôt ou d'une autre tête.
    """
    if not head_sha:
        return None, "absente (sans tête observée)"
    try:
        card = links_for(repo, head_sha)
    except Exception as exc:  # noqa: BLE001 - un lecteur muet ne qualifie rien
        return None, "illisible : %s" % type(exc).__name__
    if card is None:
        return None, "absente"
    return card, "qualifiée (dépôt + tête)"


def consume(db, *, integration=None, lands=None, links=None, links_for=None,
            diagnoses=None, actor: str = work.SYSTEM_SENDER) -> list[dict]:
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

    `links_for(repo, head_sha)` est le port **contextualisé** : appelé par ligne
    avant l'ouverture, il rend la carte `test→fichiers` du dépôt et de la tête
    exacts, ou `None`. Quand il est fourni, il qualifie chaque ligne et `links`
    ne sert plus ; `None`, une exception ou une tête non observée laissent le
    statut `unknown` — jamais de repli sur une carte globale, qui attribuerait
    le lot d'un autre dépôt ou d'une autre tête. Sans `links_for`, `links` garde
    son sens (carte globale, rétrocompatible). La lecture est rendue par ligne
    dans `links` (`qualifiée (dépôt + tête)`, `absente`, `illisible : …`).

    `diagnoses(repo, head_sha)` rend le rapport factuel commun
    (`ameesh-train-diagnosis/1`, #1467) du paquet atterri, ou None. Il n'est lu
    que s'il vise la **tête exacte** (`diagnosis_of`) et ne sert que de fait
    versé au corps : il n'en découle aucune exemption d'environnement, aucun
    retrait ni aucune accusation. Sans lui, le relevé local fait foi.

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
                    "assigned": False, "why": "", "diagnosis": "", "links": ""}
            try:
                if st.ci.head_replaced(repo, branch, head_sha, run_id):
                    line["why"] = "tête remplacée : verdict périmé, aucun incident"
                    out.append(line)
                    continue
            except Exception:  # noqa: BLE001 - garde indisponible : le relevé courant fait foi
                pass
            existing = _open(st.work, ref_of(repo, run_id))
            if existing is not None:
                # déjà porté : ni relevé GitHub ni rapport à chaque passage
                line.update({"id": int(existing["id"]),
                             "why": "incident #%d déjà ouvert (état %s)"
                                    % (int(existing["id"]), existing.get("state") or "?")})
                out.append(line)
                continue
            try:
                failures = st.ci.failures(repo, run_id)
            except Exception:  # noqa: BLE001 - sans échecs relevés, l'incident reste dû
                failures = []
            commits: list[dict] = []
            try:
                commits = list(lands(repo, head_sha))
            except Exception as exc:  # noqa: BLE001 - sans preuve, aucune accusation
                line["why"] = "atterrissage illisible : %s" % type(exc).__name__
            diagnosis = None
            if diagnoses is not None and head_sha:
                try:
                    report = diagnoses(repo, head_sha)
                    diagnosis = diagnosis_of(report, head_sha=head_sha)
                except Exception as exc:  # noqa: BLE001 - un lecteur muet n'empêche pas l'incident
                    diagnosis = None
                    line["diagnosis"] = "illisible : %s" % type(exc).__name__
                else:
                    line["diagnosis"] = ("lu (tête exacte)" if diagnosis
                                         else ("ignoré : schéma ou tête étrangers"
                                               if report is not None else "absent"))
            else:
                line["diagnosis"] = "absent"
            if links_for is not None:
                # carte qualifiée sur le dépôt et la tête exacts : None reste
                # unknown, jamais de repli sur une carte globale
                card, line["links"] = _qualified_card(links_for, repo, head_sha)
            else:
                card, line["links"] = links, ("globale" if links is not None else "absente")
            result = open_incident(
                db, repo=repo, branch=branch, run_id=run_id, head_sha=head_sha,
                workflow=str(row.get("workflow") or ""), url=str(row.get("url") or ""),
                modules=[f.get("module") for f in failures],
                tests=[f.get("test") for f in failures],
                commits=commits, app=app, links=card, diagnosis=diagnosis, actor=actor)
            earlier = line["why"]
            line.update(result)
            if earlier:
                line["why"] = "%s ; %s" % (line["why"] or "ouvert", earlier)
            elif not line["why"]:
                line["why"] = "ouvert"
            out.append(line)
    return out
