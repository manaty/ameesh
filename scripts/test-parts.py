#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Découpage de la suite de tests en parts parallèles (L116).

    scripts/test-parts.py split --parts N --part K --driver psql [--list F]
    scripts/test-parts.py run   --parts N --part K --driver psql [--list F]
                                [--output-durations F.json] [--since REF]
                                [--flow F.jsonl] [--label "psql 1/6"]
                                [--measure DIR] [--map carte.json.gz]
    scripts/test-parts.py run   --plan plan.json --part K --driver psql [--flow F.jsonl]
    scripts/test-parts.py plan  --out plan.json --parts "mesure-psycopg-1 …"
                                [--list-psql F] [--map carte.json.gz] [--summary F]
    scripts/test-parts.py durations --driver psql F.json [F.json…]

La répartition est **déterministe** et se fait **par module** de test
(`tests/test*.py`, le motif de `unittest discover`) : un module n'est jamais
coupé, ses `setUpClass` et son schéma jetable restent ensemble. Les modules
sont pesés par la durée mesurée de `tests/parts/durees.json` (par pilote),
puis placés du plus lourd au plus léger dans la part la moins chargée.
Un module absent de la table (nouveau) est pesé par son nombre de tests
(`def test_`) multiplié par la durée moyenne d'un test du pilote : c'est le
repli, signalé sur la sortie d'erreur pour qu'on rafraîchisse la table.

`--list` restreint la suite aux modules d'un fichier (un nom par ligne, `#`
pour les commentaires) : `tests/parts/pilote-psql.txt` sur une PR de lot (le
plan de repli le demande par `pilot_list` ; une PR de release garde la suite
complète, scripts/test-plan.py). La liste
est additive (L160a) : on écrit un nom par ligne, en ordre alphabétique et
sans doublon ; la lecture range et dédoublonne ce qu'une fusion `union`
(.gitattributes) produit, et refuse toujours un nom inconnu.

`run` exécute la part dans ce processus (`unittest`, verbeux) et mesure la
durée de chaque module ; `durations` fusionne ces mesures dans la table.
Le signal SIGINT est remis à son traitement par défaut avant les tests : lancé
en arrière-plan par un shell, il serait sinon ignoré, et hérité ignoré par
les sous-processus des tests (exécuteur, CLI) qui s'arrêtent sur SIGINT.

L161b — **ordre simple** : avec `--since REF`, les fichiers changés entre
REF et HEAD (`git diff --name-only`) classent les modules de la part :
P0, le module de test lui-même a changé ; P1, son texte nomme un fichier
changé (module Python `ameesh.x`, chemin ou nom de fichier) ; P3, le reste.
Chaque part passe P0, puis P1 (les plus courts d'abord), puis le reste dans
l'ordre alphabétique. Rien n'est sauté, et le découpage en parts ne change
pas : seul l'ordre dans la part. C'est une heuristique textuelle, en
attendant la carte mesurée (L161e) ; diff illisible : ordre alphabétique.

L161b — **flux** : avec `--flow F.jsonl`, une ligne JSON par test terminé
(schéma `ameesh-test-event/1`, écrite et vidée aussitôt), plus une ligne
`debut` et une ligne `fin` par part. `scripts/ci-flow.py` en fait le résumé
du job ; L161c le suivra en direct pour le courrier de l'agent.

L161e — **mesure** : avec `--measure DIR`, la part passe sous mesure pour la
carte test → code (`scripts/map.py`) : couverture par test, contexte
`tests.<module>.<Classe>.<test>` (la classe seule pendant `setUpClass` et
`tearDownClass`, le module seul pendant son chargement), sous-processus
compris (`CARTE_CONTEXTE` hérité), fichiers lus et tables touchées.
Avec `--map F`, les durées des modules viennent de la carte ;
`tests/parts/durees.json` ne reste qu'en repli.

L161f — **plan**: with `--plan plan.json` (written once per CI run by
`scripts/test-plan.py plan`), the part takes its modules, their order and
their classes from the plan: every part reads the same split, computed once
from the map. `--parts`, `--list`, `--since` and `--map` are then
ignored; a plan without a module list for the part (fallback plan) gives
the L116 split. On a lot branch (L1211), the plan holds only the impacted
modules and the contract list: the part runs them, the rest of the suite
runs on main, `release/**` and the package integration branch; a release
pull request (same branches, source or target) keeps the full suite. With
`--flow`, the part writes an `impacted` event once its P0 and P1 modules are
done (`statut`: `vert` or `rouge`): the provisional green, when every part
says `vert`.

#1341 R1 — **plan de mesure**: `plan` lit la carte une seule fois et écrit le
découpage de toutes les parts attendues (`--parts` : les noms d'artefacts du
workflow, ex. `mesure-psycopg-1 …`) ; chaque part le lit ensuite
(`run --plan`). Avant, chaque part de `carte.yml` restaurait la carte et
recalculait son découpage : deux jobs pouvaient lire deux cartes différentes
(cache écrit entre-temps) et produire des parts disjointes — un module omis,
un autre doublé — toutes avec des `durees.json` non vides.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
TABLE = TESTS / "parts" / "durees.json"
#: durée par test quand la table n'a rien pour le pilote (repli du repli)
DEFAULT_PER_TEST = 0.5
#: schéma des lignes du flux (docs/design/etudes/l161-tests-par-impact.md)
FLOW_SCHEMA = "ameesh-test-event/1"
#: modules « pilote » psql (découpage de la mesure, #1341 R1)
PILOTE_PSQL = TESTS / "parts" / "pilote-psql.txt"
#: schéma d'un plan de tests (scripts/test-plan.py, L161f)
PLAN_SCHEMA = "ameesh-test-plan/1"
#: #1341 R1 — une part de mesure : `mesure-<pilote>-<n>` (PARTS_MESURE, carte.yml)
PART_MESURE = re.compile(r"mesure-(psycopg|psql)-(\d+)$")
#: lignes de trace gardées dans l'extrait d'un échec
EXCERPT_LINES = 40


def modules(tests: pathlib.Path = TESTS) -> list[str]:
    """Les modules que `unittest discover -s tests` trouverait (sans préfixe)."""
    return sorted(p.stem for p in tests.glob("test*.py"))


def read_list(chemin: str | os.PathLike, connus: list[str]) -> list[str]:
    noms = []
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        nom = ligne.split("#", 1)[0].strip()
        if nom:
            noms.append(nom)
    inconnus = sorted(set(noms) - set(connus))
    if inconnus:
        raise SystemExit(f"{chemin} : module(s) inconnu(s) : {', '.join(inconnus)}")
    # L160a : on écrit une entrée par ligne, à sa place alphabétique. #1517 :
    # la fusion `union` (.gitattributes) de deux ajouts au même endroit garde
    # les deux lignes dans l'ordre des côtés, ou deux fois la même : la
    # lecture range et dédoublonne au lieu de refuser.
    return sorted(set(noms))


def test_count(module: str, tests: pathlib.Path = TESTS) -> int:
    texte = (tests / f"{module}.py").read_text(encoding="utf-8")
    return max(1, len(re.findall(r"^\s*def test", texte, re.M)))


def map_durations(chemin: str | os.PathLike) -> dict:
    """La table des durées (même forme que durees.json) tirée d'une carte
    (L161e) ; vide si la carte est illisible."""
    import gzip
    try:
        with gzip.open(chemin, "rt", encoding="utf-8") as f:
            carte = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"carte {chemin} illisible ({exc}) : durées de {TABLE.relative_to(REPO)}",
              file=sys.stderr, flush=True)
        return {}
    table: dict = {}
    for module, infos in carte.get("modules", {}).items():
        # L148 stage 6 : la carte porte `durations` (anglais) et l'ancienne
        # `durees` pendant une release ; l'anglaise est lue en premier.
        durees = infos.get("durations")
        if durees is None:
            durees = infos.get("durees", {})
        for pilote, duree in durees.items():
            table.setdefault(pilote, {})[module] = duree
    return table


def weight(mods: list[str], pilote: str, table: dict | None = None,
          tests: pathlib.Path = TESTS) -> tuple[dict[str, float], list[str]]:
    """Poids de chaque module, et la liste de ceux pesés par le repli."""
    if table is None:
        table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.get(pilote, {})
    connus = [m for m in mesures if m in set(mods)]
    total_tests = sum(test_count(m, tests) for m in connus)
    par_test = (sum(mesures[m] for m in connus) / total_tests) if total_tests else DEFAULT_PER_TEST
    resultat, repli = {}, []
    for m in mods:
        if m in mesures:
            resultat[m] = float(mesures[m])
        else:
            resultat[m] = test_count(m, tests) * par_test
            repli.append(m)
    return resultat, repli


def split(pesees: dict[str, float], parts: int) -> list[list[str]]:
    """Plus lourd d'abord, dans la part la moins chargée (à égalité : la
    première). Même entrée, même découpage, sur toutes les machines."""
    charges = [0.0] * parts
    contenu: list[list[str]] = [[] for _ in range(parts)]
    for m in sorted(pesees, key=lambda m: (-pesees[m], m)):
        i = min(range(parts), key=lambda i: (charges[i], i))
        charges[i] += pesees[m]
        contenu[i].append(m)
    return [sorted(c) for c in contenu]


def changed_files(depuis: str, repo: pathlib.Path = REPO) -> list[str] | None:
    """Fichiers changés entre `depuis` et HEAD, ou None si le diff est
    illisible (historique trop court, référence inconnue)."""
    try:
        sortie = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", depuis, "HEAD"], cwd=repo,
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ordre : diff depuis {depuis} illisible ({exc}) ; ordre alphabétique",
              file=sys.stderr, flush=True)
        return None
    return [n for n in sortie.splitlines() if n.strip()]


def ambiguous_names(repo: pathlib.Path = REPO) -> set[str]:
    """Noms de fichier portés par plusieurs fichiers suivis (README.md…) :
    un test qui les nomme ne désigne aucun d'eux en particulier."""
    try:
        suivis = subprocess.run(["git", "ls-files"], cwd=repo, check=True,
                                capture_output=True, text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return set()
    comptes = collections.Counter(pathlib.PurePosixPath(f).name for f in suivis)
    return {nom for nom, n in comptes.items() if n > 1}


def _patterns(chemin: str, ambigus: set[str] = frozenset()) -> list[re.Pattern]:
    """Ce qui, dans le texte d'un module de test, désigne le fichier changé :
    son chemin, son nom s'il est seul à le porter, son module Python."""
    p = pathlib.PurePosixPath(chemin)
    motifs = [re.compile(re.escape(chemin))]
    if p.name not in ambigus and p.name != "__init__.py" and "." in p.name:
        motifs.append(re.compile(r"(?<![\w.-])" + re.escape(p.name) + r"(?![\w-])"))
    if p.suffix == ".py" and p.parts[0] == "src" and p.name != "__init__.py":
        pointe = ".".join(p.with_suffix("").parts[1:])
        parent, _, nom = pointe.rpartition(".")
        motifs.append(re.compile(r"\b" + re.escape(pointe) + r"\b"))
        if parent:
            motifs.append(re.compile(
                r"from\s+" + re.escape(parent) + r"\s+import\s+(?:\([^)]*|[^\n]*)\b"
                + re.escape(nom) + r"\b"))
    return motifs


def classify(mods: list[str], changes: list[str], tests: pathlib.Path = TESTS,
            ambigus: set[str] = frozenset()) -> dict[str, tuple[str, str]]:
    """Classe et raison de chaque module : P0 (module changé), P1 (nomme un
    fichier changé), P3 (le reste, raison vide)."""
    prefixe = tests.relative_to(REPO).as_posix() + "/" if tests.is_relative_to(REPO) else "tests/"
    motifs = [(c, _patterns(c, ambigus)) for c in changes]
    classes = {}
    for m in mods:
        propre = f"{prefixe}{m}.py"
        if propre in changes:
            classes[m] = ("P0", propre)
            continue
        texte = (tests / f"{m}.py").read_text(encoding="utf-8")
        raison = next((c for c, ms in motifs if c != propre and any(x.search(texte) for x in ms)), "")
        classes[m] = ("P1", raison) if raison else ("P3", "")
    return classes


def order(part: list[str], pesees: dict[str, float],
             classes: dict[str, tuple[str, str]]) -> list[str]:
    """P0 puis P1, les plus courts d'abord ; le reste dans l'ordre alphabétique."""
    def key(m):
        classe = classes.get(m, ("P3", ""))[0]
        return (classe, pesees.get(m, 0.0) if classe != "P3" else 0.0, m)
    return sorted(part, key=key)


def read_plan(chemin: str | os.PathLike, pilote: str, part: int) -> dict | None:
    """L161f: the entry of part `part` for `pilote` in a plan, or None when
    the plan has no module list for it (fallback plan)."""
    plan = json.loads(pathlib.Path(chemin).read_text(encoding="utf-8"))
    for entree in plan.get("parts", {}).get(pilote, []):
        if entree.get("part") == part:
            if entree.get("modules") is None:
                return None
            return {**entree, "classes": {m: tuple(c) for m, c in plan.get("classes", {}).items()}}
    raise SystemExit(f"{chemin} : pas de part {part} pour {pilote}")


def selection(args) -> tuple[list[str], float, list[str], dict[str, float]]:
    if not 1 <= args.part <= args.parts:
        raise SystemExit(f"part {args.part} hors de 1..{args.parts}")
    mods = modules()
    if args.list:
        mods = read_list(args.list, mods)
    table = None
    if args.map:
        table = map_durations(args.map)
        if not table.get(args.driver):
            table = None
    pesees, repli = weight(mods, args.driver, table)
    part = split(pesees, args.parts)[args.part - 1]
    return part, sum(pesees[m] for m in part), [m for m in repli if m in part], pesees


def parts_du_plan(noms: list[str]) -> dict[str, int]:
    """#1341 R1 — les parts attendues (`mesure-<pilote>-<n>`, la liste
    `PARTS_MESURE` du workflow) → le nombre de parts de chaque pilote.

    Un nom inconnu ou une numérotation non contiguë est refusé : un plan
    incomplet doit échouer, jamais laisser des parts que personne ne lit."""
    par_pilote: dict[str, set[int]] = collections.defaultdict(set)
    for nom in noms:
        m = PART_MESURE.fullmatch(nom)
        if m is None:
            raise SystemExit(f"part inconnue : {nom!r} (attendu mesure-<pilote>-<n>)")
        par_pilote[m.group(1)].add(int(m.group(2)))
    sortie = {}
    for pilote, parts in sorted(par_pilote.items()):
        if sorted(parts) != list(range(1, max(parts) + 1)):
            raise SystemExit(f"parts {pilote} non contiguës : {sorted(parts)}")
        sortie[pilote] = max(parts)
    return sortie


def plan(args) -> int:
    """#1341 R1 — le plan de la mesure, calculé une seule fois par run.

    La carte est lue ici, une fois ; le découpage pondéré (A, `--map`) est
    figé dans le plan, que toutes les parts lisent ensuite (`run --plan`).
    Chaque part restaurait auparavant la carte et recalculait son découpage :
    deux jobs pouvaient lire deux cartes différentes et produire des parts
    disjointes (module omis ou doublé), toutes avec des `durees.json` non
    vides — la garde D publiait alors une carte incomplète avec une matrice
    verte (témoin K1/K2 de la R1)."""
    mods = modules()
    table = map_durations(args.map) if args.map else None
    parts = parts_du_plan(args.parts.replace(",", " ").split())
    sortie: dict = {"schema": PLAN_SCHEMA, "kind": "mesure", "parts": {}, "classes": {},
                    "poids": {}}
    for pilote, n in sorted(parts.items()):
        sel = read_list(args.list_psql, mods) if pilote == "psql" else mods
        table_pilote = table
        if table_pilote is not None and not table_pilote.get(pilote):
            table_pilote = None  # la carte ne porte pas ce pilote : table statique
        poids, repli = weight(sel, pilote, table_pilote)
        sortie["parts"][pilote] = [
            {"part": i, "of": n, "modules": part, "label": f"{pilote} {i}/{n}",
             "estimate_s": round(sum(poids[m] for m in part), 1),
             "pilot_list": pilote == "psql"}
            for i, part in enumerate(split(poids, n), 1)]
        sortie["poids"][pilote] = {
            "modules": len(sel), "repli": len(repli),
            "source": "carte" if (table or {}).get(pilote) else "statique"}
    pathlib.Path(args.out).write_text(json.dumps(sortie, indent=1, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
    lignes = ["## plan de mesure"]
    for pilote, n in sorted(parts.items()):
        infos = sortie["poids"][pilote]
        charges = [entree["estimate_s"] for entree in sortie["parts"][pilote]]
        lignes.append(f"* {pilote} : {infos['modules']} module(s) en {n} parts "
                      f"(poids {infos['source']}, {infos['repli']} en repli), "
                      f"max ~{max(charges):.0f} s")
    texte = "\n".join(lignes) + "\n"
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(texte)
    print(texte, end="")
    return 0


def _module_of(test) -> str:
    module = type(test).__module__
    if module.startswith("unittest"):
        # _ErrorHolder : « setUpClass (tests.test_x.C) », « setUpModule (tests.test_x) » ;
        # _FailedTest (import en échec) : sa méthode porte le nom du module.
        description = getattr(test, "description", None)
        if description is None:
            module = getattr(test, "_testMethodName", "?")
        else:
            methode, _, cible = description.rpartition(" (")
            module = cible.rstrip(")")
            if methode.endswith("Class"):  # module.Classe, qualname avec points possible
                while "." in module and module not in sys.modules:
                    module = module.rsplit(".", 1)[0]
    return module.rsplit(".", 1)[-1]


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


class Flow:
    """Le fichier JSON lignes du flux : une ligne complète par événement,
    vidée aussitôt (un lecteur qui suit le fichier ne voit jamais de ligne
    coupée en deux écritures)."""

    def __init__(self, chemin: str | os.PathLike, context: dict):
        self._f = open(chemin, "w", encoding="utf-8")
        self.context = context

    def write(self, type_: str, **champs) -> None:
        ligne = {"v": 1, "schema": FLOW_SCHEMA, "type": type_, **self.context, **champs,
                 "ts": _timestamp()}
        self._f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def ci_context(etiquette: str) -> dict:
    """Où tourne la part : dépôt, commit poussé (`AMEESH_CI_TETE`, la tête
    de la PR) et commit testé, numéro d'exécution GitHub, s'ils sont connus."""
    env = os.environ
    execution = env.get("GITHUB_RUN_ID", "")
    return {"depot": env.get("GITHUB_REPOSITORY", ""),
            "tete": env.get("AMEESH_CI_TETE") or env.get("GITHUB_SHA", ""),
            "fusion": env.get("GITHUB_SHA", ""),
            "execution": int(execution) if execution.isdigit() else None,
            "part": etiquette}


#: du moins grave au plus grave : un test garde le pire de ses issues
#: (sous-tests compris)
_SEVERITY = ("succes", "saute", "echec_attendu", "succes_inattendu", "echec", "erreur")


class _Result(unittest.TextTestResult):
    """Chronomètre par module : le temps écoulé depuis la fin du test précédent
    est compté au module du test qui se termine (setUpClass compris).
    Avec un flux (L161b), une ligne par test à sa fin."""

    durations: collections.Counter
    flow: Flow | None = None
    classes: dict = {}
    measure = None  # L161e : _Mesure de la part, ou None
    #: L161f: P0 and P1 modules of the part, for the provisional green
    #: (None: no `impacted` event, the part does not run from a plan)
    impactes: tuple | None = None

    def startTestRun(self):
        self.durations = collections.Counter()
        self.comptes = collections.Counter()
        self._depuis = time.monotonic()
        self._courant = None
        self._impactes_restants = set(self.impactes or ())
        self._impactes_rouges: set[str] = set()
        self._impactes_dit = False
        super().startTestRun()

    def _report_impacted(self) -> None:
        """L161f: one `impacted` event, once no P0 or P1 module is left."""
        if self._impactes_dit or self.flow is None or self.impactes is None:
            return
        self._impactes_dit = True
        self.flow.write("impacted", statut="rouge" if self._impactes_rouges else "vert",
                         modules=list(self.impactes), rouges=sorted(self._impactes_rouges))

    def stopTestRun(self):
        self._report_impacted()
        super().stopTestRun()

    def startTest(self, test):
        module = _module_of(test)
        if module not in self._impactes_restants:
            # impacted modules run first: the first other module closes them
            self._impactes_restants.clear()
        if not self._impactes_restants:
            self._report_impacted()
        if self.measure is not None:
            self.measure.context(test.id())
        super().startTest(test)
        self._courant, self._debut = test, time.monotonic()
        self._statut, self._extrait = "succes", ""

    def _record(self, test, statut: str, err=None) -> None:
        extrait = ""
        if err is not None:
            lignes = self._exc_info_to_string(err, test).rstrip().splitlines()
            extrait = "\n".join(lignes[-EXCERPT_LINES:])
        if test is not self._courant:
            # erreur hors d'un test : setUpClass, setUpModule, tearDownClass
            self._emit(test, statut, 0.0, extrait)
            return
        if _SEVERITY.index(statut) >= _SEVERITY.index(self._statut):
            self._statut, self._extrait = statut, extrait or self._extrait

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "erreur", err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "echec", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "saute")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "echec_attendu")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "succes_inattendu")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            echec = issubclass(err[0], test.failureException)
            self._record(test, "echec" if echec else "erreur", err)

    def _emit(self, test, statut: str, duree: float, extrait: str) -> None:
        self.comptes[statut] += 1
        if self.flow is None:
            return
        module = _module_of(test)
        if statut in ("echec", "erreur") and module in (self.impactes or ()):
            self._impactes_rouges.add(module)
        nom = test.id()
        classe, raison = self.classes.get(module, ("P3", ""))
        ligne = {"module": module, "test": nom, "classe": classe, "raison": raison,
                 "statut": statut, "duree_s": round(duree, 3)}
        if extrait:
            ligne["extrait"] = extrait
        if statut in ("echec", "erreur") and not nom.startswith(("setUp", "tearDown")):
            ligne["rejouer"] = f"python -m unittest {nom}"
        self.flow.write("test", **ligne)

    def stopTest(self, test):
        super().stopTest(test)
        maintenant = time.monotonic()
        self.durations[_module_of(test)] += maintenant - self._depuis
        self._depuis = maintenant
        if test is self._courant:
            self._emit(test, self._statut, maintenant - self._debut, self._extrait)
            self._courant = None


class _Measure:
    """L161e : la part sous mesure. Couverture de ce processus avec un
    contexte par test, posé aussi dans `CARTE_CONTEXTE` pour les
    sous-processus (qui démarrent la leur par le fichier `.pth`), et relevé
    des fichiers lus (scripts/map_measure.py)."""

    def __init__(self, dossier: str):
        sys.path.insert(0, str(REPO / "scripts"))
        import map  # scripts/map.py
        import map_measure
        import coverage
        self.dossier = os.path.abspath(dossier)
        os.makedirs(self.dossier, exist_ok=True)
        rc = map.write_rc(self.dossier)
        os.environ["COVERAGE_PROCESS_START"] = rc
        os.environ[map_measure.ENV_MEASURE_DIR] = self.dossier
        os.environ[map_measure.ENV_MEASURE_CONTEXT] = ""
        if not map.pth_installed():
            print("mesure : fichier .pth absent (scripts/map.py install) ; "
                  "les sous-processus ne seront pas mesurés", file=sys.stderr, flush=True)
        self.cov = coverage.Coverage(config_file=rc)
        self.cov.start()
        map_measure.start(couverture=False)
        self._env = map_measure.ENV_MEASURE_CONTEXT

    def context(self, nom: str) -> None:
        os.environ[self._env] = nom
        self.cov.switch_context(nom)

    def stop(self) -> None:
        self.cov.stop()
        self.cov.save()


def _measured_suite(measure: _Measure) -> type:
    """Suite unittest qui pose le contexte de la classe pendant ses
    `setUpClass` et `tearDownClass` (hors de tout test)."""
    class _Suite(unittest.TestSuite):
        def _handleClassSetUp(self, test, result):
            if test.__class__ != getattr(result, "_previousTestClass", None):
                measure.context(f"{test.__class__.__module__}.{test.__class__.__qualname__}")
            super()._handleClassSetUp(test, result)

        def _tearDownPreviousClass(self, test, result):
            precedente = getattr(result, "_previousTestClass", None)
            if precedente is not None and precedente != test.__class__:
                measure.context(f"{precedente.__module__}.{precedente.__qualname__}")
            super()._tearDownPreviousClass(test, result)
    return _Suite


def _load(part: list[str], measure: _Measure | None) -> unittest.TestSuite:
    if measure is None:
        return unittest.TestLoader().loadTestsFromNames([f"tests.{m}" for m in part])
    chargeur = unittest.TestLoader()
    chargeur.suiteClass = _measured_suite(measure)
    suites = []
    for m in part:  # ce qu'importe un module de test lui est compté
        measure.context(f"tests.{m}")
        suites.append(chargeur.loadTestsFromName(f"tests.{m}"))
    return chargeur.suiteClass(suites)


def _result_class(flow: Flow | None, classes: dict, measure: _Measure | None,
                  impactes: tuple | None) -> type:
    """La classe de résultat de la part : chaque clé porte le nom de
    l'attribut que lit `_Result` (`measure`, `flow`, `classes`, `impactes`)."""
    return type("_ResultatPart", (_Result,),
                {"flow": flow, "classes": classes, "measure": measure,
                 "impactes": impactes})


def run(args) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    entree = read_plan(args.plan, args.driver, args.part) if args.plan else None
    if args.plan and entree is None:  # fallback plan: the L116 split
        print(f"plan {args.plan} : pas de modules pour {args.driver} {args.part} ; "
              "découpage L116", file=sys.stderr, flush=True)
        args.parts = args.parts or json.loads(pathlib.Path(args.plan).read_text(
            encoding="utf-8"))["parts"][args.driver][0]["of"]
        if args.driver == "psql" and not args.list and _driver_plan(args.plan):
            args.list = str(TESTS / "parts" / "pilote-psql.txt")
    if entree is not None:
        part, estimee, repli = entree["modules"], entree.get("estimate_s", 0.0), []
        args.parts = entree["of"]
        args.label = args.label or entree.get("label")
    else:
        part, estimee, repli, pesees = selection(args)
    print(f"part {args.part}/{args.parts} ({args.driver}) : {len(part)} module(s), "
          f"~{estimee:.0f} s estimées", flush=True)
    if repli:
        print(f"  pesés par le nombre de tests (absents de {TABLE.relative_to(REPO)}) : "
              f"{', '.join(repli)}", file=sys.stderr, flush=True)
    classes: dict[str, tuple[str, str]] = {}
    changes = changed_files(args.since) if args.since and entree is None else None
    if entree is not None:
        classes = {m: entree["classes"].get(m, ("P3", "")) for m in part}
        touches = [m for m in part if classes[m][0] != "P3"]
        print(f"  d'abord, selon le plan : {', '.join(touches) or 'aucun'}", flush=True)
    elif changes is not None:
        classes = classify(part, changes, ambigus=ambiguous_names())
        part = order(part, pesees, classes)
        touches = [m for m in part if classes[m][0] != "P3"]
        print(f"  d'abord, touchés par les {len(changes)} fichier(s) changé(s) depuis "
              f"{args.since} : {', '.join(touches) or 'aucun'}", flush=True)
    etiquette = args.label or f"{args.driver} {args.part}/{args.parts}"
    flow = Flow(args.flow, ci_context(etiquette)) if args.flow else None
    if flow:
        flow.write("debut", pilote=args.driver, modules=part,
                    classes={m: classes.get(m, ("P3", ""))[0] for m in part},
                    estimee_s=round(estimee, 1))
    os.chdir(REPO)
    measure = _Measure(args.measure) if args.measure else None
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    suite = _load(part, measure)
    impactes = None if entree is None else tuple(
        m for m in part if classes.get(m, ("P3", ""))[0] in ("P0", "P1"))
    resultat_cls = _result_class(flow, classes, measure, impactes)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=resultat_cls)
    debut, resultat = time.monotonic(), None
    try:
        resultat = runner.run(suite)
    finally:
        if measure is not None:
            measure.stop()
        if flow:
            if resultat is None:  # interrompu (SIGINT, délai du job)
                statut, comptes = "interrompu", {}
            else:
                statut = "succes" if resultat.wasSuccessful() else "echec"
                comptes = dict(resultat.comptes)
            flow.write("fin", statut=statut, comptes=comptes,
                        duree_s=round(time.monotonic() - debut, 1))
            flow.close()
    durations = {m: round(t, 1) for m, t in sorted(resultat.durations.items())}
    if args.output_durations:
        pathlib.Path(args.output_durations).write_text(
            json.dumps({args.driver: durations}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lents = sorted(durations.items(), key=lambda kv: -kv[1])[:5]
    print("modules les plus lents : " + ", ".join(f"{m} {t:.0f} s" for m, t in lents), flush=True)
    return 0 if resultat.wasSuccessful() else 1


def _driver_plan(chemin: str) -> bool:
    plan = json.loads(pathlib.Path(chemin).read_text(encoding="utf-8"))
    return any(p.get("pilot_list") for p in plan.get("parts", {}).get("psql", []))


def durations(args) -> int:
    table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.setdefault(args.driver, {})
    for f in args.files:
        mesures.update(json.loads(pathlib.Path(f).read_text(encoding="utf-8")).get(args.driver, {}))
    connus = set(modules())
    table[args.driver] = {m: t for m, t in sorted(mesures.items()) if m in connus}
    TABLE.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    print(f"{TABLE.relative_to(REPO)} : {len(table[args.driver])} module(s) pour {args.driver}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    for nom in ("split", "run"):
        p = sous.add_parser(nom)
        p.add_argument("--parts", type=int, required=(nom == "split"))
        p.add_argument("--part", type=int, required=True)
        p.add_argument("--driver", choices=("psql", "psycopg"), required=True)
        p.add_argument("--list", help="fichier de modules (ex. tests/parts/pilote-psql.txt)")
        if nom == "run":
            p.add_argument("--output-durations", help="écrit les durées mesurées (JSON)")
            p.add_argument("--since", help="référence git : les modules touchés par les "
                           "fichiers changés depuis passent d'abord (ex. HEAD^1)")
            p.add_argument("--flow", help="écrit un événement JSON par test (JSON lignes)")
            p.add_argument("--label", help="nom de la part dans le flux (ex. « psql 1/6 »)")
            p.add_argument("--measure", help="dossier de mesure pour la carte test → code "
                           "(L161e, scripts/map.py)")
        p.add_argument("--map", help="carte (carte.json.gz) : durées des modules, "
                       "tests/parts/durees.json en repli (L161e)")
        if nom == "run":
            p.add_argument("--plan", help="plan.json (scripts/test-plan.py, L161f): "
                           "modules, order and classes of the part")
    p = sous.add_parser("durations")
    p.add_argument("--driver", choices=("psql", "psycopg"), required=True)
    p.add_argument("files", nargs="+")
    # #1341 R1 — le plan de la mesure, lu une fois par run (job `plan-mesure`) :
    # `run --plan` le consomme dans chaque part.
    p = sous.add_parser("plan", help="découpage de toutes les parts d'une mesure, "
                         "calculé une seule fois par run (#1341 R1)")
    p.add_argument("--out", required=True, help="écrit le plan (JSON)")
    p.add_argument("--parts", required=True,
                   help="parts attendues (noms d'artefacts, ex. « mesure-psycopg-1 … »)")
    p.add_argument("--list-psql", default=str(PILOTE_PSQL),
                   help="modules « pilote » psql (défaut tests/parts/pilote-psql.txt)")
    p.add_argument("--map", help="carte (carte.json.gz) : durées sous mesure ; "
                   "tests/parts/durees.json en repli")
    p.add_argument("--summary", help="écrit aussi le résumé Markdown dans ce fichier")
    args = ap.parse_args(argv)
    if args.commande == "run" and not args.parts and not args.plan:
        ap.error("run: --parts or --plan required")
    if args.commande == "plan":
        return plan(args)
    if args.commande == "split":
        part, estimee, _, _ = selection(args)
        print("\n".join(f"tests.{m}" for m in part))
        print(f"~{estimee:.0f} s estimées", file=sys.stderr)
        return 0
    return run(args) if args.commande == "run" else durations(args)


if __name__ == "__main__":
    sys.exit(main())
