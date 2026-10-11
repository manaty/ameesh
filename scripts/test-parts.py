#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Découpage de la suite de tests en parts parallèles (L116).

    scripts/test-parts.py repartir --parts N --part K --pilote psql [--liste F]
    scripts/test-parts.py lancer   --parts N --part K --pilote psql [--liste F]
                                   [--durees-sortie F.json] [--depuis REF]
                                   [--flux F.jsonl] [--etiquette "psql 1/6"]
                                   [--mesure DIR] [--carte carte.json.gz]
    scripts/test-parts.py lancer   --plan plan.json --part K --pilote psql [--flux F.jsonl]
    scripts/test-parts.py durees   --pilote psql F.json [F.json…]

La répartition est **déterministe** et se fait **par module** de test
(`tests/test*.py`, le motif de `unittest discover`) : un module n'est jamais
coupé, ses `setUpClass` et son schéma jetable restent ensemble. Les modules
sont pesés par la durée mesurée de `tests/parts/durees.json` (par pilote),
puis placés du plus lourd au plus léger dans la part la moins chargée.
Un module absent de la table (nouveau) est pesé par son nombre de tests
(`def test_`) multiplié par la durée moyenne d'un test du pilote : c'est le
repli, signalé sur la sortie d'erreur pour qu'on rafraîchisse la table.

`--liste` restreint la suite aux modules d'un fichier (un nom par ligne, `#`
pour les commentaires) : `tests/parts/pilote-psql.txt` sur une PR. La liste
est additive (L160a) : noms en ordre alphabétique et sans doublon, sinon
refusée.

`lancer` exécute la part dans ce processus (`unittest`, verbeux) et mesure la
durée de chaque module ; `durees` fusionne ces mesures dans la table.
Le signal SIGINT est remis à son traitement par défaut avant les tests : lancé
en arrière-plan par un shell, il serait sinon ignoré, et hérité ignoré par
les sous-processus des tests (exécuteur, CLI) qui s'arrêtent sur SIGINT.

L161b — **ordre simple** : avec `--depuis REF`, les fichiers changés entre
REF et HEAD (`git diff --name-only`) classent les modules de la part :
P0, le module de test lui-même a changé ; P1, son texte nomme un fichier
changé (module Python `ameesh.x`, chemin ou nom de fichier) ; P3, le reste.
Chaque part passe P0, puis P1 (les plus courts d'abord), puis le reste dans
l'ordre alphabétique. Rien n'est sauté, et le découpage en parts ne change
pas : seul l'ordre dans la part. C'est une heuristique textuelle, en
attendant la carte mesurée (L161e) ; diff illisible : ordre alphabétique.

L161b — **flux** : avec `--flux F.jsonl`, une ligne JSON par test terminé
(schéma `ameesh-test-event/1`, écrite et vidée aussitôt), plus une ligne
`debut` et une ligne `fin` par part. `scripts/ci-flux.py` en fait le résumé
du job ; L161c le suivra en direct pour le courrier de l'agent.

L161e — **mesure** : avec `--mesure DIR`, la part passe sous mesure pour la
carte test → code (`scripts/carte.py`) : couverture par test, contexte
`tests.<module>.<Classe>.<test>` (la classe seule pendant `setUpClass` et
`tearDownClass`, le module seul pendant son chargement), sous-processus
compris (`CARTE_CONTEXTE` hérité), fichiers lus et tables touchées.
Avec `--carte F`, les durées des modules viennent de la carte ;
`tests/parts/durees.json` ne reste qu'en repli.

L161f — **plan**: with `--plan plan.json` (written once per CI run by
`scripts/test-plan.py plan`), the part takes its modules, their order and
their classes from the plan: every part reads the same split, computed once
from the map. `--parts`, `--liste`, `--depuis` and `--carte` are then
ignored; a plan without a module list for the part (fallback plan) gives
the L116 split. With `--flux`, the part writes an `impacted` event once its
P0 and P1 modules are done (`statut`: `vert` or `rouge`): the provisional
green, when every part says `vert`.
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
DEFAUT_PAR_TEST = 0.5
#: schéma des lignes du flux (docs/design/etudes/l161-tests-par-impact.md)
SCHEMA_FLUX = "ameesh-test-event/1"
#: lignes de trace gardées dans l'extrait d'un échec
EXTRAIT_LIGNES = 40


def modules(tests: pathlib.Path = TESTS) -> list[str]:
    """Les modules que `unittest discover -s tests` trouverait (sans préfixe)."""
    return sorted(p.stem for p in tests.glob("test*.py"))


def lire_liste(chemin: str | os.PathLike, connus: list[str]) -> list[str]:
    noms = []
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        nom = ligne.split("#", 1)[0].strip()
        if nom:
            noms.append(nom)
    inconnus = sorted(set(noms) - set(connus))
    if inconnus:
        raise SystemExit(f"{chemin} : module(s) inconnu(s) : {', '.join(inconnus)}")
    # L160a: kept sorted and unique so that two additions never touch the same line
    if noms != sorted(set(noms)):
        raise SystemExit(f"{chemin} : liste non rangée (ordre alphabétique, sans doublon) ; "
                         f"attendu : {', '.join(sorted(set(noms)))}")
    return noms


def nombre_de_tests(module: str, tests: pathlib.Path = TESTS) -> int:
    texte = (tests / f"{module}.py").read_text(encoding="utf-8")
    return max(1, len(re.findall(r"^\s*def test", texte, re.M)))


def durees_de_la_carte(chemin: str | os.PathLike) -> dict:
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
        for pilote, duree in infos.get("durees", {}).items():
            table.setdefault(pilote, {})[module] = duree
    return table


def poids(mods: list[str], pilote: str, table: dict | None = None,
          tests: pathlib.Path = TESTS) -> tuple[dict[str, float], list[str]]:
    """Poids de chaque module, et la liste de ceux pesés par le repli."""
    if table is None:
        table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.get(pilote, {})
    connus = [m for m in mesures if m in set(mods)]
    total_tests = sum(nombre_de_tests(m, tests) for m in connus)
    par_test = (sum(mesures[m] for m in connus) / total_tests) if total_tests else DEFAUT_PAR_TEST
    resultat, repli = {}, []
    for m in mods:
        if m in mesures:
            resultat[m] = float(mesures[m])
        else:
            resultat[m] = nombre_de_tests(m, tests) * par_test
            repli.append(m)
    return resultat, repli


def repartir(pesees: dict[str, float], parts: int) -> list[list[str]]:
    """Plus lourd d'abord, dans la part la moins chargée (à égalité : la
    première). Même entrée, même découpage, sur toutes les machines."""
    charges = [0.0] * parts
    contenu: list[list[str]] = [[] for _ in range(parts)]
    for m in sorted(pesees, key=lambda m: (-pesees[m], m)):
        i = min(range(parts), key=lambda i: (charges[i], i))
        charges[i] += pesees[m]
        contenu[i].append(m)
    return [sorted(c) for c in contenu]


def fichiers_changes(depuis: str, repo: pathlib.Path = REPO) -> list[str] | None:
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


def noms_ambigus(repo: pathlib.Path = REPO) -> set[str]:
    """Noms de fichier portés par plusieurs fichiers suivis (README.md…) :
    un test qui les nomme ne désigne aucun d'eux en particulier."""
    try:
        suivis = subprocess.run(["git", "ls-files"], cwd=repo, check=True,
                                capture_output=True, text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return set()
    comptes = collections.Counter(pathlib.PurePosixPath(f).name for f in suivis)
    return {nom for nom, n in comptes.items() if n > 1}


def _motifs(chemin: str, ambigus: set[str] = frozenset()) -> list[re.Pattern]:
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


def classer(mods: list[str], changes: list[str], tests: pathlib.Path = TESTS,
            ambigus: set[str] = frozenset()) -> dict[str, tuple[str, str]]:
    """Classe et raison de chaque module : P0 (module changé), P1 (nomme un
    fichier changé), P3 (le reste, raison vide)."""
    prefixe = tests.relative_to(REPO).as_posix() + "/" if tests.is_relative_to(REPO) else "tests/"
    motifs = [(c, _motifs(c, ambigus)) for c in changes]
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


def ordonner(part: list[str], pesees: dict[str, float],
             classes: dict[str, tuple[str, str]]) -> list[str]:
    """P0 puis P1, les plus courts d'abord ; le reste dans l'ordre alphabétique."""
    def cle(m):
        classe = classes.get(m, ("P3", ""))[0]
        return (classe, pesees.get(m, 0.0) if classe != "P3" else 0.0, m)
    return sorted(part, key=cle)


def lire_plan(chemin: str | os.PathLike, pilote: str, part: int) -> dict | None:
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
    if args.liste:
        mods = lire_liste(args.liste, mods)
    table = None
    if getattr(args, "carte", None):
        table = durees_de_la_carte(args.carte)
        if not table.get(args.pilote):
            table = None
    pesees, repli = poids(mods, args.pilote, table)
    part = repartir(pesees, args.parts)[args.part - 1]
    return part, sum(pesees[m] for m in part), [m for m in repli if m in part], pesees


def _module_de(test) -> str:
    module = type(test).__module__
    if module.startswith("unittest"):  # _ErrorHolder (setUpClass en échec)
        module = getattr(test, "description", "?").split("(")[-1].split(".")[0]
    return module.rsplit(".", 1)[-1]


def _horodatage() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


class Flux:
    """Le fichier JSON lignes du flux : une ligne complète par événement,
    vidée aussitôt (un lecteur qui suit le fichier ne voit jamais de ligne
    coupée en deux écritures)."""

    def __init__(self, chemin: str | os.PathLike, contexte: dict):
        self._f = open(chemin, "w", encoding="utf-8")
        self.contexte = contexte

    def ecrire(self, type_: str, **champs) -> None:
        ligne = {"v": 1, "schema": SCHEMA_FLUX, "type": type_, **self.contexte, **champs,
                 "ts": _horodatage()}
        self._f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        self._f.flush()

    def fermer(self) -> None:
        self._f.close()


def contexte_ci(etiquette: str) -> dict:
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
_GRAVITE = ("succes", "saute", "echec_attendu", "succes_inattendu", "echec", "erreur")


class _Resultat(unittest.TextTestResult):
    """Chronomètre par module : le temps écoulé depuis la fin du test précédent
    est compté au module du test qui se termine (setUpClass compris).
    Avec un flux (L161b), une ligne par test à sa fin."""

    durees: collections.Counter
    flux: Flux | None = None
    classes: dict = {}
    mesure = None  # L161e : _Mesure de la part, ou None
    #: L161f: P0 and P1 modules of the part, for the provisional green
    #: (None: no `impacted` event, the part does not run from a plan)
    impactes: tuple | None = None

    def startTestRun(self):
        self.durees = collections.Counter()
        self.comptes = collections.Counter()
        self._depuis = time.monotonic()
        self._courant = None
        self._impactes_restants = set(self.impactes or ())
        self._impactes_rouges: set[str] = set()
        self._impactes_dit = False
        super().startTestRun()

    def _dire_impactes(self) -> None:
        """L161f: one `impacted` event, once no P0 or P1 module is left."""
        if self._impactes_dit or self.flux is None or self.impactes is None:
            return
        self._impactes_dit = True
        self.flux.ecrire("impacted", statut="rouge" if self._impactes_rouges else "vert",
                         modules=list(self.impactes), rouges=sorted(self._impactes_rouges))

    def stopTestRun(self):
        self._dire_impactes()
        super().stopTestRun()

    def startTest(self, test):
        module = _module_de(test)
        if module not in self._impactes_restants:
            # impacted modules run first: the first other module closes them
            self._impactes_restants.clear()
        if not self._impactes_restants:
            self._dire_impactes()
        if self.mesure is not None:
            self.mesure.contexte(test.id())
        super().startTest(test)
        self._courant, self._debut = test, time.monotonic()
        self._statut, self._extrait = "succes", ""

    def _noter(self, test, statut: str, err=None) -> None:
        extrait = ""
        if err is not None:
            lignes = self._exc_info_to_string(err, test).rstrip().splitlines()
            extrait = "\n".join(lignes[-EXTRAIT_LIGNES:])
        if test is not self._courant:
            # erreur hors d'un test : setUpClass, setUpModule, tearDownClass
            self._emettre(test, statut, 0.0, extrait)
            return
        if _GRAVITE.index(statut) >= _GRAVITE.index(self._statut):
            self._statut, self._extrait = statut, extrait or self._extrait

    def addError(self, test, err):
        super().addError(test, err)
        self._noter(test, "erreur", err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._noter(test, "echec", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._noter(test, "saute")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._noter(test, "echec_attendu")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._noter(test, "succes_inattendu")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            echec = issubclass(err[0], test.failureException)
            self._noter(test, "echec" if echec else "erreur", err)

    def _emettre(self, test, statut: str, duree: float, extrait: str) -> None:
        self.comptes[statut] += 1
        if self.flux is None:
            return
        module = _module_de(test)
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
        self.flux.ecrire("test", **ligne)

    def stopTest(self, test):
        super().stopTest(test)
        maintenant = time.monotonic()
        self.durees[_module_de(test)] += maintenant - self._depuis
        self._depuis = maintenant
        if test is self._courant:
            self._emettre(test, self._statut, maintenant - self._debut, self._extrait)
            self._courant = None


class _Mesure:
    """L161e : la part sous mesure. Couverture de ce processus avec un
    contexte par test, posé aussi dans `CARTE_CONTEXTE` pour les
    sous-processus (qui démarrent la leur par le fichier `.pth`), et relevé
    des fichiers lus (scripts/carte_mesure.py)."""

    def __init__(self, dossier: str):
        sys.path.insert(0, str(REPO / "scripts"))
        import carte  # scripts/carte.py
        import carte_mesure
        import coverage
        self.dossier = os.path.abspath(dossier)
        os.makedirs(self.dossier, exist_ok=True)
        rc = carte.ecrire_rc(self.dossier)
        os.environ["COVERAGE_PROCESS_START"] = rc
        os.environ[carte_mesure.ENV_DOSSIER] = self.dossier
        os.environ[carte_mesure.ENV_CONTEXTE] = ""
        if not carte.pth_installe():
            print("mesure : fichier .pth absent (scripts/carte.py installer) ; "
                  "les sous-processus ne seront pas mesurés", file=sys.stderr, flush=True)
        self.cov = coverage.Coverage(config_file=rc)
        self.cov.start()
        carte_mesure.demarrer(couverture=False)
        self._env = carte_mesure.ENV_CONTEXTE

    def contexte(self, nom: str) -> None:
        os.environ[self._env] = nom
        self.cov.switch_context(nom)

    def arreter(self) -> None:
        self.cov.stop()
        self.cov.save()


def _suite_mesuree(mesure: _Mesure) -> type:
    """Suite unittest qui pose le contexte de la classe pendant ses
    `setUpClass` et `tearDownClass` (hors de tout test)."""
    class _Suite(unittest.TestSuite):
        def _handleClassSetUp(self, test, result):
            if test.__class__ != getattr(result, "_previousTestClass", None):
                mesure.contexte(f"{test.__class__.__module__}.{test.__class__.__qualname__}")
            super()._handleClassSetUp(test, result)

        def _tearDownPreviousClass(self, test, result):
            precedente = getattr(result, "_previousTestClass", None)
            if precedente is not None and precedente != test.__class__:
                mesure.contexte(f"{precedente.__module__}.{precedente.__qualname__}")
            super()._tearDownPreviousClass(test, result)
    return _Suite


def _charger(part: list[str], mesure: _Mesure | None) -> unittest.TestSuite:
    if mesure is None:
        return unittest.TestLoader().loadTestsFromNames([f"tests.{m}" for m in part])
    chargeur = unittest.TestLoader()
    chargeur.suiteClass = _suite_mesuree(mesure)
    suites = []
    for m in part:  # ce qu'importe un module de test lui est compté
        mesure.contexte(f"tests.{m}")
        suites.append(chargeur.loadTestsFromName(f"tests.{m}"))
    return chargeur.suiteClass(suites)


def lancer(args) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    entree = lire_plan(args.plan, args.pilote, args.part) if args.plan else None
    if args.plan and entree is None:  # fallback plan: the L116 split
        print(f"plan {args.plan} : pas de modules pour {args.pilote} {args.part} ; "
              "découpage L116", file=sys.stderr, flush=True)
        args.parts = args.parts or json.loads(pathlib.Path(args.plan).read_text(
            encoding="utf-8"))["parts"][args.pilote][0]["of"]
        if args.pilote == "psql" and not args.liste and _plan_pilote(args.plan):
            args.liste = str(TESTS / "parts" / "pilote-psql.txt")
    if entree is not None:
        part, estimee, repli = entree["modules"], entree.get("estimate_s", 0.0), []
        args.parts = entree["of"]
        args.etiquette = args.etiquette or entree.get("label")
    else:
        part, estimee, repli, pesees = selection(args)
    print(f"part {args.part}/{args.parts} ({args.pilote}) : {len(part)} module(s), "
          f"~{estimee:.0f} s estimées", flush=True)
    if repli:
        print(f"  pesés par le nombre de tests (absents de {TABLE.relative_to(REPO)}) : "
              f"{', '.join(repli)}", file=sys.stderr, flush=True)
    classes: dict[str, tuple[str, str]] = {}
    changes = fichiers_changes(args.depuis) if args.depuis and entree is None else None
    if entree is not None:
        classes = {m: entree["classes"].get(m, ("P3", "")) for m in part}
        touches = [m for m in part if classes[m][0] != "P3"]
        print(f"  d'abord, selon le plan : {', '.join(touches) or 'aucun'}", flush=True)
    elif changes is not None:
        classes = classer(part, changes, ambigus=noms_ambigus())
        part = ordonner(part, pesees, classes)
        touches = [m for m in part if classes[m][0] != "P3"]
        print(f"  d'abord, touchés par les {len(changes)} fichier(s) changé(s) depuis "
              f"{args.depuis} : {', '.join(touches) or 'aucun'}", flush=True)
    etiquette = args.etiquette or f"{args.pilote} {args.part}/{args.parts}"
    flux = Flux(args.flux, contexte_ci(etiquette)) if args.flux else None
    if flux:
        flux.ecrire("debut", pilote=args.pilote, modules=part,
                    classes={m: classes.get(m, ("P3", ""))[0] for m in part},
                    estimee_s=round(estimee, 1))
    os.chdir(REPO)
    mesure = _Mesure(args.mesure) if args.mesure else None
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    suite = _charger(part, mesure)
    impactes = None if entree is None else tuple(
        m for m in part if classes.get(m, ("P3", ""))[0] in ("P0", "P1"))
    resultat_cls = type("_ResultatPart", (_Resultat,),
                        {"flux": flux, "classes": classes, "mesure": mesure,
                         "impactes": impactes})
    runner = unittest.TextTestRunner(verbosity=2, resultclass=resultat_cls)
    debut, resultat = time.monotonic(), None
    try:
        resultat = runner.run(suite)
    finally:
        if mesure is not None:
            mesure.arreter()
        if flux:
            if resultat is None:  # interrompu (SIGINT, délai du job)
                statut, comptes = "interrompu", {}
            else:
                statut = "succes" if resultat.wasSuccessful() else "echec"
                comptes = dict(resultat.comptes)
            flux.ecrire("fin", statut=statut, comptes=comptes,
                        duree_s=round(time.monotonic() - debut, 1))
            flux.fermer()
    durees = {m: round(t, 1) for m, t in sorted(resultat.durees.items())}
    if args.durees_sortie:
        pathlib.Path(args.durees_sortie).write_text(
            json.dumps({args.pilote: durees}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lents = sorted(durees.items(), key=lambda kv: -kv[1])[:5]
    print("modules les plus lents : " + ", ".join(f"{m} {t:.0f} s" for m, t in lents), flush=True)
    return 0 if resultat.wasSuccessful() else 1


def _plan_pilote(chemin: str) -> bool:
    plan = json.loads(pathlib.Path(chemin).read_text(encoding="utf-8"))
    return any(p.get("pilot_list") for p in plan.get("parts", {}).get("psql", []))


def durees(args) -> int:
    table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.setdefault(args.pilote, {})
    for f in args.fichiers:
        mesures.update(json.loads(pathlib.Path(f).read_text(encoding="utf-8")).get(args.pilote, {}))
    connus = set(modules())
    table[args.pilote] = {m: t for m, t in sorted(mesures.items()) if m in connus}
    TABLE.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    print(f"{TABLE.relative_to(REPO)} : {len(table[args.pilote])} module(s) pour {args.pilote}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    for nom in ("repartir", "lancer"):
        p = sous.add_parser(nom)
        p.add_argument("--parts", type=int, required=(nom == "repartir"))
        p.add_argument("--part", type=int, required=True)
        p.add_argument("--pilote", choices=("psql", "psycopg"), required=True)
        p.add_argument("--liste", help="fichier de modules (ex. tests/parts/pilote-psql.txt)")
        if nom == "lancer":
            p.add_argument("--durees-sortie", help="écrit les durées mesurées (JSON)")
            p.add_argument("--depuis", help="référence git : les modules touchés par les "
                           "fichiers changés depuis passent d'abord (ex. HEAD^1)")
            p.add_argument("--flux", help="écrit un événement JSON par test (JSON lignes)")
            p.add_argument("--etiquette", help="nom de la part dans le flux (ex. « psql 1/6 »)")
            p.add_argument("--mesure", help="dossier de mesure pour la carte test → code "
                           "(L161e, scripts/carte.py)")
        p.add_argument("--carte", help="carte (carte.json.gz) : durées des modules, "
                       "tests/parts/durees.json en repli (L161e)")
        if nom == "lancer":
            p.add_argument("--plan", help="plan.json (scripts/test-plan.py, L161f): "
                           "modules, order and classes of the part")
    p = sous.add_parser("durees")
    p.add_argument("--pilote", choices=("psql", "psycopg"), required=True)
    p.add_argument("fichiers", nargs="+")
    args = ap.parse_args(argv)
    if args.commande == "lancer" and not args.parts and not args.plan:
        ap.error("lancer : --parts ou --plan requis")
    if args.commande == "repartir":
        part, estimee, _, _ = selection(args)
        print("\n".join(f"tests.{m}" for m in part))
        print(f"~{estimee:.0f} s estimées", file=sys.stderr)
        return 0
    return lancer(args) if args.commande == "lancer" else durees(args)


if __name__ == "__main__":
    sys.exit(main())
