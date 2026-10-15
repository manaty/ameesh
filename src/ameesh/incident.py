# SPDX-License-Identifier: AGPL-3.0-only
"""#1681 — l'incident de CI ouvert après un atterrissage sur la branche d'intégration.

Canon `processus/lots.md` §2 et §11 bis : quand la suite de la branche
d'intégration (`develop` pour Nexlink, `main` pour ameesh) échoue après un
atterrissage, l'outil ouvre lui-même le **lot de correction prioritaire** et
désigne l'auteur du lot fautif d'après la **carte d'impact** des tests en
échec, sans attendre qu'un agent le remarque.

Ce module porte la règle seule :

* `attribute` est **pure** — elle croise les tests en échec (leur module, le
  chemin du fichier de test) avec les commits candidats de l'atterrissage et
  leurs fichiers, et rend soit un lot unique (l'accusation), soit plusieurs
  candidats, soit rien. Jamais d'accusation sans preuve : un doute rend
  `multiple` ou `none`, pas un nom ;
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
#: les fichiers d'infrastructure de test d'un paquet : leur changement peut
#: faire échouer n'importe quelle suite du même paquet
INFRA = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "tsconfig.json", "jest.config.js", "jest.config.cjs", "jest.config.mjs",
    "jest.config.ts", "jest.setup.js", "jest.setup.ts",
    "vitest.config.ts", "vitest.config.mts", "vitest.config.js",
)
#: les fichiers de la racine qui gouvernent tous les paquets
ROOT_INFRA = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
              "tsconfig.json", "tsconfig.base.json")
INFRA_PREFIXES = ("scripts/ci/", ".github/workflows/")
INFRA_PARTS = ("/__mocks__/", "/test-utils/", "/setupTests.")


def ref_of(repo: str, run_id: int) -> str:
    """La référence de l'incident d'une exécution (`<repo>:<run_id>`)."""
    return "%s%s:%s" % (PREFIX, (repo or "").strip(), int(run_id))


def _norm(path: str) -> str:
    """Un chemin de dépôt normalisé : séparateurs `/`, sans `./` ni `/` initial."""
    text = (path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _stem(path: str) -> str:
    """Le nom de fichier sans son extension de test (`.spec`, `.test`)."""
    name = posixpath.basename(_norm(path))
    for suffix in (".spec.tsx", ".spec.ts", ".spec.js", ".spec.mjs",
                   ".test.tsx", ".test.ts", ".test.js", ".test.mjs"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    stem, dot, _ = name.rpartition(".")
    return stem if dot else name


def _infra(changed: str, module: str) -> bool:
    """Le fichier change-t-il l'infrastructure de test du paquet de `module` ?

    Un `package.json` de `mobile/` ne peut pas expliquer l'échec d'un test de
    `backend/` : les fichiers de paquet ne sont reliés qu'au même premier
    segment de chemin. Les lockfiles et configurations de la racine, eux,
    gouvernent tous les paquets ; `scripts/ci/**` et les workflows aussi.
    """
    path = _norm(changed)
    if not path:
        return False
    base = posixpath.basename(path)
    same_package = path.split("/")[0] == _norm(module).split("/")[0]
    if path.startswith(INFRA_PREFIXES):
        return True
    if "/" not in path and base in ROOT_INFRA:
        return True
    if not same_package:
        return False
    if base in INFRA:
        return True
    return any(part in "/" + path for part in INFRA_PARTS)


def related(module: str, changed: str) -> bool:
    """Vrai si le changement de `changed` peut expliquer l'échec de `module`.

    Règle volontairement **étroite** : aucun graphe d'imports ici. Sont
    reliés le fichier de test lui-même, un fichier de son répertoire ou d'un
    sous-répertoire, la source du même nom (paire `x.spec.ts` / `x.ts`), et
    l'infrastructure de test de son paquet. Un lien plus lointain (import
    indirect) rend `none` et laisse le pilote trancher, plutôt qu'une
    accusation fausse.
    """
    module, changed = _norm(module), _norm(changed)
    if not module or not changed:
        return False
    if module == changed:
        return True
    if _infra(changed, module):
        return True
    if _stem(module) == _stem(changed):
        return True
    directory = posixpath.dirname(module)
    other = posixpath.dirname(changed)
    if directory == other:
        return True
    return bool(directory) and other.startswith(directory + "/")


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


def attribute(modules: Iterable[str], commits: Iterable[dict]) -> dict:
    """La carte d'impact : tests en échec × commits candidats -> lot fautif.

    `modules` : chemins des fichiers de test en échec (le champ `module` du
    flux CI). `commits` : `[{sha, lot, author, summary, files}]`, les commits
    de l'atterrissage, chacun avec ses fichiers modifiés.

    Rend `{status, lot, author, candidates, modules}` où `status` vaut :

    * `unique` — un seul lot désigné (l'accusation, avec ses preuves) ;
    * `multiple` — plusieurs lots candidats : aucun n'est accusé ;
    * `unattributed` — des commits reliés, mais aucun lot identifiable ;
    * `none` — des fichiers de test en échec, mais aucun commit relié ;
    * `unattributable` — aucun fichier de test exploitable (agrégat, panne).

    Un commit sans lot (`lot` absent) reste un candidat sans nom : il ne peut
    pas fonder une accusation, mais il interdit `unique`.
    """
    mods = attributable(modules)
    candidates: list[dict] = []
    for commit in commits or ():
        files = [_norm(f) for f in (commit.get("files") or ())]
        hits: list[dict] = []
        for module in mods:
            for changed in files:
                if related(module, changed):
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
    out = {"status": "unattributable", "lot": None, "author": "",
           "candidates": candidates, "modules": mods}
    if not mods:
        return out
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
    """Le titre de l'incident : l'exécution, et le lot désigné s'il est unique."""
    where = "%s%s" % (branch or "?", " (%s)" % workflow if workflow else "")
    out = "INCIDENT CI %s rouge — run %s" % (where, int(run_id))
    attribution = attribution or {}
    if attribution.get("status") == "unique" and attribution.get("lot"):
        out += " — lot #%s désigné" % attribution["lot"]
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
        lines.append("- Lot fautif désigné : #%s (auteur %s), par la carte d'impact "
                     "des tests en échec." % (attribution["lot"], author or "?"))
    else:
        lines.append("- Aucun lot n'est accusé : %s."
                     % {"multiple": "plusieurs candidats",
                        "unattributed": "des commits reliés, mais aucun lot identifiable",
                        "none": "aucun commit relié",
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
        lines += ["", "Candidats (commits de l'atterrissage reliés) :"]
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
        "- si l'attribution n'est pas unique, le pilote tranche : la carte d'impact est "
        "dans ce corps, aucun auteur n'est accusé sans preuve.",
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
                  actor: str = work.SYSTEM_SENDER) -> dict:
    """Ouvre l'incident d'une exécution rouge d'atterrissage, s'il n'existe pas.

    Rend `{id, created, status, lot, author, assigned, why}` — ne lève jamais.
    Le lot est créé `bug` priorité 1, **sans assigné** (une création assignée
    refuserait un auteur non réveillable et perdrait l'incident), puis
    l'auteur désigné par une attribution `unique` est assigné si le registre
    l'accepte ; sinon l'incident reste dans la file de l'équipe et le corps
    porte le nom.
    """
    ref = ref_of(repo, run_id)
    out = {"id": None, "created": False, "status": "", "lot": None, "author": "",
           "assigned": False, "why": ""}
    attribution = attribute(modules, commits)
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


def consume(db, *, integration=None, lands=None, actor: str = work.SYSTEM_SENDER) -> list[dict]:
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
    d'autres dépôts.

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
                commits=commits, app=app, actor=actor)
            line.update(result)
            if not line["why"]:
                line["why"] = "ouvert"
            out.append(line)
    return out
