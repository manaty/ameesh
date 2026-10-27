#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Découpage de la suite de tests en parts parallèles (L116).

    scripts/test-parts.py split --parts N --part K --driver psql [--list F]
    scripts/test-parts.py run   --parts N --part K --driver psql [--list F]
                                [--output-durations F.json] [--since REF]
                                [--flow F.jsonl] [--label "psql 1/6"]
                                [--measure DIR] [--map carte.json.gz]
    scripts/test-parts.py run   --plan plan.json --part K --driver psql [--flow F.jsonl]
                                [--parallel N]
    scripts/test-parts.py plan  --out plan.json --parts "mesure-psycopg-1 …"
                                [--list-psql F] [--map carte.json.gz] [--summary F]
    scripts/test-parts.py durations --driver psql F.json [F.json…]
                                [--flows F.jsonl…] [--min-test-s 5]

La répartition est **déterministe** et se fait **par module** de test
(`tests/test*.py`, le motif de `unittest discover`) : un module n'est jamais
coupé, ses `setUpClass` et son schéma jetable restent ensemble. Les modules
sont pesés par la durée mesurée de `tests/parts/durees.json` (par pilote),
puis placés du plus lourd au plus léger dans la part la moins chargée.
Un module absent de la table (nouveau) est pesé par son nombre de tests
(`def test_`) multiplié par la durée moyenne d'un test du pilote : c'est le
repli, signalé sur la sortie d'erreur pour qu'on rafraîchisse la table.

#3754 — **parallélisme dans le job** : `run --parallel N` découpe la part en
N processus au moins, chacun sur son schéma par défaut (`AMEESH_SCHEMA`), et
fusionne leur flux en un seul (`debut`, `impacted`, `fin` compris). La charge
mesurée de la part peut relever N jusqu'à 4 : avec 24 jobs sur 48 vCPU, une
part deux fois plus lourde prend deux fois plus de processus (cible ~450 s
par processus). Le découpage se fait par **unité insécable** : une classe de
test entière (son `setUpClass` reste dans un processus), sauf une classe de
plus de 5 min découpée en tests pour isoler un test lent ; les unités sont
pondérées par les durées par test de `durees.json` (`tests.<pilote>`) quand
elles existent. Les tests d'une classe partagent déjà un schéma unique par
classe dans `PgTestCase` : deux processus de la même base ne se voient pas.
La mesure (`--measure`) reste série : ses couvertures ne se fusionnent pas.

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

#2224 — avec `--plafond-min N` (le `timeout-minutes` du job de mesure), le plan
signale (`::warning::`) une part dont l'estimation × `SURCOUT_CHARGE` dépasse
le plafond : la carte source est mesurée dans un autre contexte et sous-estime
une part chargée (psycopg 3/6 annulée à 40m46, aucune carte publiée).

#3604 — `--test-timeout N` (défaut 600 s, 0 désactive) borne chaque test de la
part mesurée : un test bloqué est nommé dans la carte comme erreur et la suite
continue, au lieu de laisser la part consumer le plafond de 60 min (psycopg
2/8, run 40177589953 : 3 087 s sans sortie après `test_503_agent_host`).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
TABLE = TESTS / "parts" / "durees.json"
#: durée par test quand la table n'a rien pour le pilote (repli du repli)
DEFAULT_PER_TEST = 0.5
#: #1476 : poids plancher d'un module mesuré. La table arrondit au dixième et
#: ne compte ni l'import ni setUpClass : 73 modules psql et 67 psycopg y pèsent
#: 0,0 s. À poids nul, `split` les donnait tous à la même part (la première à
#: égalité), qui portait alors seule leur coût réel de démarrage.
MIN_WEIGHT = 1.0
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
#: #2224 — surcoût de charge d'une part de mesure : run 39015105210, psycopg
#: 3/6 estimée 1 588,7 s, 2 316 s de tests réels (×1,46) ; une part dont
#: l'estimation × ce facteur dépasse le plafond du job est signalée par le plan
SURCOUT_CHARGE = 1.5
#: #3604 — borne par test sous mesure. Le run 40177589953 (main 7d3e776e) a
#: perdu psycopg 2/8 après 3 087 s sans sortie : un test bloqué a consommé le
#: plafond de 60 min et annulé la part, donc la carte. La borne nomme le test
#: (erreur notée dans la carte) et rend la main aux tests suivants ; 0 la
#: désactive.
TEST_TIMEOUT = 600
#: #3754 — une classe dont la durée mesurée dépasse ce seuil est découpée en
#: tests (sinon elle reste insécable) : une classe de 11 min dans une part de
#: 13 min n'aiderait pas deux processus. Le test lent d'une classe (503 s
#: mesurées dans test_l108_executor_server) est ainsi isolé, les autres tests
#: de la classe tournent en parallèle.
UNIT_SPLIT_S = 300.0
#: #3754 — charge visée par processus d'une part parallèle. Le pool de 24
#: runners offre 48 vCPU (16 par hôte, 8 jobs) ; à ~450 s de tests par
#: processus, la somme des besoins des 24 parts tient dans 48 processus (le
#: `--parallel` demandé est un plancher, `PARALLEL_MAX` un plafond). Le run
#: 40384366825 (main f444955b, 26/10) pèse 12 034 s psql + ~8 500 s psycopg :
#: 46 processus à 450 s, soit un mur théorique de ~7,5 min avant mise en place.
PARALLEL_TARGET_S = 450.0
PARALLEL_MAX = 4
#: #3945 — le plafond de l'hôte. Le calcul ci-dessus visait 48 processus ;
#: le run 40470243300 (main ad042a0c, 27/10) en a lancé 73 (25, 24 et 24 par
#: hôte : charge totale ~28 400 s, arrondis, plancher 2 et plafond 4), plus
#: les 8 Postgres de chaque hôte, sur 16 vCPU par hôte : main a été rouge cinq
#: runs de suite sur un test de timing différent à chaque fois. Chaque job
#: prend donc au plus sa part des vCPU de l'hôte, `vCPU // runners de
#: l'hôte` (2 sur 16 vCPU et 8 runners), même au-dessus du `--parallel`
#: demandé ; sans la variable (poste, fourche), pas de plafond.
JOBS_PER_HOST_ENV = "AMEESH_CI_JOBS_PER_HOST"
#: #3754 — un processus de la part parallèle : il exécute un sous-ensemble de
#: tests (plan dérivé) et ne refait pas de fan-out. La variable évite la
#: récursion du découpage.
PARALLEL_ENV = "AMEESH_TEST_PARALLEL_WORKER"


class TestTimeout(Exception):
    """Un test a dépassé la borne de la part mesurée (#3604)."""


def armer_borne(secondes: float):
    """Pose la borne SIGALRM d'un test mesuré ; renvoie l'ancien gestionnaire.

    `_Result` arme et désarme le minuteur par test ; le gestionnaire lève une
    exception que unittest enregistre comme erreur du test, sans interrompre
    les tests suivants.
    """
    if not hasattr(signal, "SIGALRM"):
        return None

    def _sonner(signum, frame):
        raise TestTimeout("test au-delà de %g s sous mesure (#3604)" % secondes)

    return signal.signal(signal.SIGALRM, _sonner)


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
    """Poids de chaque module, et la liste de ceux pesés par le repli.

    #1476 : la carte ne mesure pas tous les modules sous chaque pilote (le
    psql complet n'est pas sous mesure). La table statique des durées comble
    ces trous ; la carte reste prioritaire quand elle a une durée. Sans cela,
    un module non mesuré était pesé par son nombre de tests — un module
    d'intégration lourd (des dizaines de secondes par test) passait pour léger
    et déséquilibrait une part.
    """
    if table is None:
        table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    else:
        base = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
        for driver, mesures in table.items():
            base.setdefault(driver, {}).update(mesures)
        table = base
    mesures = table.get(pilote, {})
    connus = [m for m in mesures if m in set(mods)]
    total_tests = sum(test_count(m, tests) for m in connus)
    par_test = (sum(mesures[m] for m in connus) / total_tests) if total_tests else DEFAULT_PER_TEST
    resultat, repli = {}, []
    for m in mods:
        if m in mesures:
            resultat[m] = max(float(mesures[m]), MIN_WEIGHT)
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
        for entree in sortie["parts"][pilote] if args.plafond_min else ():
            if entree["estimate_s"] * SURCOUT_CHARGE > args.plafond_min * 60:
                print(f"::warning::mesure {entree['label']} estimée "
                      f"{entree['estimate_s']:.0f} s : ×{SURCOUT_CHARGE} sous charge, "
                      f"au-delà du plafond de {args.plafond_min} min", flush=True)
                lignes.append(f"  * ⚠ {entree['label']} : ~{entree['estimate_s']:.0f} s, "
                              f"marge ×{SURCOUT_CHARGE} au-delà du plafond "
                              f"{args.plafond_min} min")
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
        self._verrou = threading.Lock()

    def write(self, type_: str, **champs) -> None:
        ligne = {"v": 1, "schema": FLOW_SCHEMA, "type": type_, **self.context, **champs,
                 "ts": _timestamp()}
        with self._verrou:
            self._f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
            self._f.flush()

    def write_raw(self, ligne: str) -> None:
        """#3754 : recopie une ligne déjà sérialisée d'un processus de la part
        parallèle (même contexte et même horodatage que son émission)."""
        with self._verrou:
            self._f.write(ligne.rstrip("\n") + "\n")
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
    #: #3604 : borne par test (secondes) ; None/0 : pas de borne
    test_timeout = None
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
        if self.test_timeout:
            signal.setitimer(signal.ITIMER_REAL, float(self.test_timeout))

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
        if self.test_timeout:
            signal.setitimer(signal.ITIMER_REAL, 0)
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


def _load(part: list[str], measure: _Measure | None,
          tests: list[str] | None = None) -> unittest.TestSuite:
    if tests is not None:
        # #3754 : le processus parallèle reçoit ses unités (classes ou
        # fonctions) déjà découpées ; le chargement ne dépend pas des modules.
        return unittest.TestLoader().loadTestsFromNames(tests)
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
                  impactes: tuple | None, test_timeout: float | None = None) -> type:
    """La classe de résultat de la part : chaque clé porte le nom de
    l'attribut que lit `_Result` (`measure`, `flow`, `classes`, `impactes`,
    `test_timeout`)."""
    return type("_ResultatPart", (_Result,),
                {"flow": flow, "classes": classes, "measure": measure,
                 "impactes": impactes, "test_timeout": test_timeout})


def _iter_tests(suite):
    """Tous les cas de test d'une suite, récursivement."""
    for element in suite:
        if isinstance(element, unittest.TestSuite):
            yield from _iter_tests(element)
        else:
            yield element


def _module_of_test_id(test_id: str) -> str:
    """Le module d'un identifiant `tests.<module>[.<Classe>].<test>`."""
    morceaux = test_id.split(".")
    if len(morceaux) >= 3 and morceaux[0] == "tests":
        return morceaux[1]
    return morceaux[-1]


def _unites_de(part: list[str]) -> dict[str, list[str]]:
    """#3754 : les unités insécables d'une part, pour ses processus parallèles.

    Une unité est une **classe** de test (tous ses tests restent dans le même
    processus : son `setUpClass` et l'état de classe ne sont pas partagés), ou
    une fonction de test seule. Découper par module ne suffisait pas : un
    module peut porter un test de plusieurs minutes (`test_l108_executor_server`,
    10 min à lui seul) et resterait le chemin critique d'une part. Les schémas
    de test étant uniques par classe, deux processus ne se voient pas.
    """
    suite = unittest.TestLoader().loadTestsFromNames([f"tests.{m}" for m in part])
    unites: dict[str, list[str]] = {}
    for test in _iter_tests(suite):
        test_id = test.id()
        morceaux = test_id.split(".")
        cle = ".".join(morceaux[:3]) if len(morceaux) >= 4 else test_id
        unites.setdefault(cle, []).append(test_id)
    return unites


def _poids_par_test(driver: str) -> dict[str, float]:
    """Les durées par test de `durees.json` (`tests.<pilote>`), ou {}."""
    if not TABLE.exists():
        return {}
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    par_test = (table.get("tests") or {}).get(driver) if isinstance(
        table.get("tests"), dict) else None
    return par_test if isinstance(par_test, dict) else {}


def _pesees_unites(unites: dict[str, list[str]], driver: str,
                   part: list[str]) -> dict[str, float]:
    """Poids de chaque unité : durée mesurée du test quand la table la porte
    (`tests.<pilote>` de `durees.json`), sinon part du module au prorata de ses
    tests. Somme les tests d'une classe pour qu'un test lent pèse son coût."""
    par_test = _poids_par_test(driver)
    poids_modules, _ = weight(part, driver)
    comptes: collections.Counter = collections.Counter()
    for test_ids in unites.values():
        for test_id in test_ids:
            comptes[_module_of_test_id(test_id)] += 1
    pesees: dict[str, float] = {}
    for unite, test_ids in unites.items():
        total = 0.0
        for test_id in test_ids:
            mesure = par_test.get(test_id)
            if isinstance(mesure, (int, float)) and not isinstance(mesure, bool) and mesure > 0:
                total += float(mesure)
            else:
                module = _module_of_test_id(test_id)
                n = max(comptes[module], 1)
                total += poids_modules.get(module, MIN_WEIGHT * n) / n
        pesees[unite] = max(total, MIN_WEIGHT)
    return pesees


def parallel_count(charge: float, minimum: int = 1) -> int:
    """#3754 : nombre de processus d'une part, à charge visée constante.

    `minimum` est le plancher demandé (`--parallel`) ; la charge mesurée de la
    part peut le relever jusqu'à `PARALLEL_MAX`, pour que les parts lourdes
    n'attendent pas derrière les légères sur un pool de 48 vCPU.
    """
    besoin = int(charge // PARALLEL_TARGET_S) \
        + (1 if charge % PARALLEL_TARGET_S > 0 else 0)
    return max(1, minimum, min(PARALLEL_MAX, max(1, besoin)))


def host_cpus() -> int:
    """#3945 : les vCPU que ce processus peut occuper (affinité, sinon l'hôte)."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def host_cap() -> int | None:
    """#3945 : le plafond de processus d'une part, `vCPU // runners de l'hôte`.

    `None` sans `AMEESH_CI_JOBS_PER_HOST` (ou valeur illisible ou nulle) :
    le poste d'un développeur et une fourche gardent le seul `PARALLEL_MAX`.
    """
    try:
        jobs = int(os.environ.get(JOBS_PER_HOST_ENV, "").strip() or 0)
    except ValueError:
        return None
    if jobs <= 0:
        return None
    return max(1, host_cpus() // jobs)


def _charge_systeme() -> str:
    """#3945 : la charge moyenne sur 1 min de l'hôte, pour mesurer sa saturation."""
    try:
        return f"{os.getloadavg()[0]:.1f}"
    except (AttributeError, OSError):
        return "?"


def _affiner_unites(unites: dict[str, list[str]], pesees: dict[str, float],
                    par_test: dict[str, float]) -> tuple[dict[str, list[str]], dict[str, float]]:
    """#3754 : découpe une classe trop lourde en tests, avec leur durée mesurée.

    Une classe de plus de `UNIT_SPLIT_S` resterait seule dans un processus et
    fixerait le mur de la part ; ses tests, eux, sont indépendants (chaque
    `setUp` remet le schéma à zéro). Les tests sans durée connue reçoivent la
    moyenne de leur classe, pour ne pas surcharger un processus.
    """
    nouveaux: dict[str, list[str]] = {}
    nouvelles: dict[str, float] = {}
    for unite, test_ids in unites.items():
        if pesees.get(unite, 0.0) <= UNIT_SPLIT_S or len(test_ids) <= 1:
            nouveaux[unite] = test_ids
            nouvelles[unite] = max(pesees.get(unite, MIN_WEIGHT), MIN_WEIGHT)
            continue
        connus = {t: float(par_test[t]) for t in test_ids
                  if isinstance(par_test.get(t), (int, float))
                  and not isinstance(par_test.get(t), bool) and par_test[t] > 0}
        inconnus = [t for t in test_ids if t not in connus]
        residuel = max(pesees[unite] - sum(connus.values()), 0.0)
        moyenne = residuel / len(inconnus) if inconnus else 0.0
        for test_id in test_ids:
            poids = connus.get(test_id, moyenne)
            nouveaux[test_id] = [test_id]
            nouvelles[test_id] = max(poids, MIN_WEIGHT)
    return nouveaux, nouvelles


def _suivre_flux(chemin: str, flow: Flow, evenements: list, verrou: threading.Lock,
                 fini: threading.Event) -> None:
    """#3754 : recopie en direct les lignes `test` d'un processus parallèle
    dans le flux de la part. Les lignes `debut`/`fin`/`impacted` de chaque
    processus restent locales : le parent écrit les siennes une seule fois."""
    fichier = None
    tampon = ""

    def traiter(ligne: str) -> None:
        nonlocal tampon
        tampon += ligne
        while "\n" in tampon:
            complete, tampon = tampon.split("\n", 1)
            if not complete.strip():
                continue
            try:
                evenement = json.loads(complete)
            except json.JSONDecodeError:
                continue
            if evenement.get("type") != "test":
                continue
            flow.write_raw(complete)
            with verrou:
                evenements.append(evenement)

    while True:
        if fichier is None:
            try:
                fichier = open(chemin, encoding="utf-8")
            except FileNotFoundError:
                if fini.is_set():
                    return
                time.sleep(0.1)
                continue
        ligne = fichier.readline()
        if ligne:
            traiter(ligne)
            continue
        if fini.is_set():
            traiter(fichier.read())
            if tampon.strip():  # ligne coupée : illisible, jamais comptée
                try:
                    evenement = json.loads(tampon)
                except json.JSONDecodeError:
                    evenement = None
                if isinstance(evenement, dict) and evenement.get("type") == "test":
                    flow.write_raw(tampon)
                    with verrou:
                        evenements.append(evenement)
            return
        time.sleep(0.2)


def run_parallel(args) -> int | None:
    """#3754 — exécute la part en N processus `run`, chacun sur son schéma.

    Le découpage se fait par unités (classes de test), pondérées par les
    durées mesurées de `durees.json` (`tests.<pilote>`) : un test lent ne
    retient plus sa classe entière dans le chemin critique. Chaque processus
    reçoit un plan dérivé (mêmes métadonnées, `tests` = ses identifiants) et
    un schéma par défaut unique ; le flux des tests est fusionné en direct
    dans celui de la part, avec un seul `debut`, un seul `impacted` et un seul
    `fin`. `None` rend la main à l'exécution série (pas de plan, ou mesure).
    """
    if not args.plan or args.measure:
        return None
    entree = read_plan(args.plan, args.driver, args.part)
    if entree is None or not entree.get("modules"):
        return None
    plan = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
    part = list(entree["modules"])
    args.parts = entree.get("of") or args.parts
    classes = {m: tuple(c) for m, c in plan.get("classes", {}).items()}
    etiquette = args.label or entree.get("label") or f"{args.driver} {args.part}/{args.parts}"
    os.chdir(REPO)
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    try:
        unites = _unites_de(part)
    except Exception as exc:  # un module qui n'importe pas : découpage par module
        print(f"découpage parallèle par module ({exc})", file=sys.stderr, flush=True)
        unites = {m: [f"tests.{m}"] for m in part}
    pesees = _pesees_unites(unites, args.driver, part)
    unites, pesees = _affiner_unites(unites, pesees, _poids_par_test(args.driver))
    charge = sum(pesees.values())
    processus = min(parallel_count(charge, max(1, args.parallel)), max(1, len(pesees)))
    plafond = host_cap()
    if plafond is not None and processus > plafond:
        print(f"#3945 : {processus} processus ramenés à {plafond} ({host_cpus()} vCPU, "
              f"{os.environ.get(JOBS_PER_HOST_ENV)} runners par hôte)", file=sys.stderr, flush=True)
        processus = plafond
    shards = [s for s in split(pesees, processus) if s]
    if len(shards) <= 1:  # une seule unité : rien à paralléliser
        return None
    base_tmp = os.environ.get("RUNNER_TEMP") or None
    tmp = tempfile.mkdtemp(prefix="ameesh-parts-", dir=base_tmp)
    flow = Flow(args.flow, ci_context(etiquette)) if args.flow else None
    if flow:
        flow.write("debut", pilote=args.driver, modules=part,
                   classes={m: classes.get(m, ("P3", ""))[0] for m in part},
                   estimee_s=round(sum(pesees.values()), 1))
    evenements: list = []
    verrou = threading.Lock()
    fini = threading.Event()
    procs: list[subprocess.Popen] = []
    fils: list[threading.Thread] = []
    debut = time.monotonic()
    charge_systeme_debut = _charge_systeme()

    def arreter(signum, _frame):
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        fini.set()
        raise SystemExit(128 + signum)

    anciens = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    signal.signal(signal.SIGTERM, arreter)
    signal.signal(signal.SIGINT, arreter)
    try:
        for i, shard in enumerate(shards, 1):
            tests = [test_id for unite in shard for test_id in unites[unite]]
            modules = sorted({_module_of_test_id(t) for t in tests})
            worker = json.loads(json.dumps(plan))
            trouve = False
            for candidat in worker.get("parts", {}).get(args.driver, []):
                if candidat.get("part") == args.part:
                    candidat["tests"] = tests
                    candidat["modules"] = modules
                    candidat["estimate_s"] = round(sum(pesees[u] for u in shard), 1)
                    trouve = True
            if not trouve:
                raise SystemExit(f"plan {args.plan} : part {args.part} introuvable")
            worker_plan = os.path.join(tmp, f"plan-{i}.json")
            pathlib.Path(worker_plan).write_text(
                json.dumps(worker, ensure_ascii=False), encoding="utf-8")
            commande = [sys.executable, str(pathlib.Path(__file__).resolve()), "run",
                        "--plan", worker_plan, "--part", str(args.part),
                        "--driver", args.driver]
            worker_flow = os.path.join(tmp, f"flux-{i}.jsonl")
            if flow:
                commande += ["--flow", worker_flow]
            worker_durees = os.path.join(tmp, f"durees-{i}.json")
            if args.output_durations:
                commande += ["--output-durations", worker_durees]
            env = dict(os.environ)
            env[PARALLEL_ENV] = "1"
            env["AMEESH_SCHEMA"] = "t_par_%d_%d_%d" % (args.part, i, os.getpid())
            procs.append(subprocess.Popen(commande, cwd=REPO, env=env))
            if flow:
                fil = threading.Thread(target=_suivre_flux,
                                       args=(worker_flow, flow, evenements, verrou, fini),
                                       daemon=True)
                fil.start()
                fils.append(fil)
        codes = [proc.wait() for proc in procs]
        fini.set()
        for fil in fils:
            fil.join(timeout=10)
        with verrou:
            recus = list(evenements)
        if flow:
            comptes = collections.Counter(e.get("statut") for e in recus)
            impactes = [m for m in part if classes.get(m, ("P3", ""))[0] in ("P0", "P1")]
            rouges = sorted({e.get("module") for e in recus
                             if e.get("module") in impactes
                             and e.get("statut") in ("echec", "erreur")})
            flow.write("impacted", statut="rouge" if rouges else "vert",
                       modules=impactes, rouges=rouges)
            echec = any(code != 0 for code in codes) \
                or bool(comptes.get("echec") or comptes.get("erreur"))
            flow.write("fin", statut="echec" if echec else "succes",
                       comptes=dict(comptes), duree_s=round(time.monotonic() - debut, 1))
            flow.close()
        if args.output_durations:
            fusion: dict = {}
            for i in range(1, len(shards) + 1):
                chemin = os.path.join(tmp, f"durees-{i}.json")
                if not os.path.exists(chemin):
                    continue
                for driver, valeurs in json.loads(
                        pathlib.Path(chemin).read_text(encoding="utf-8")).items():
                    fusion.setdefault(driver, {}).update(valeurs)
            pathlib.Path(args.output_durations).write_text(
                json.dumps({d: dict(sorted(v.items())) for d, v in fusion.items()},
                           indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"part {args.part}/{args.parts} ({args.driver}) : {len(shards)} processus, "
              f"charge ~{charge:.0f} s, {len(recus)} test(s), "
              f"{time.monotonic() - debut:.0f} s, hôte {host_cpus()} vCPU, "
              f"charge système {charge_systeme_debut} → {_charge_systeme()}", flush=True)
        return 0 if all(code == 0 for code in codes) else 1
    finally:
        fini.set()
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for fil in fils:
            fil.join(timeout=5)
        for sig, ancien in anciens.items():
            signal.signal(sig, ancien)
        shutil.rmtree(tmp, ignore_errors=True)


def run(args) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if getattr(args, "parallel", 1) > 1 and not os.environ.get(PARALLEL_ENV):
        resultat = run_parallel(args)
        if resultat is not None:
            return resultat
        print(f"parallélisme {args.parallel} non applicable : exécution série",
              file=sys.stderr, flush=True)
    entree = read_plan(args.plan, args.driver, args.part) if args.plan else None
    if args.plan and entree is None:  # fallback plan: the L116 split
        print(f"plan {args.plan} : pas de modules pour {args.driver} {args.part} ; "
              "découpage L116", file=sys.stderr, flush=True)
        args.parts = args.parts or json.loads(pathlib.Path(args.plan).read_text(
            encoding="utf-8"))["parts"][args.driver][0]["of"]
        if args.driver == "psql" and not args.list and _driver_plan(args.plan):
            args.list = str(TESTS / "parts" / "pilote-psql.txt")
    tests_du_plan = None
    if entree is not None:
        part, estimee, repli = entree["modules"], entree.get("estimate_s", 0.0), []
        tests_du_plan = entree.get("tests")
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
    suite = _load(part, measure, tests_du_plan)
    impactes = None if entree is None else tuple(
        m for m in part if classes.get(m, ("P3", ""))[0] in ("P0", "P1"))
    borne = args.test_timeout if measure is not None and hasattr(signal, "SIGALRM") else None
    resultat_cls = _result_class(flow, classes, measure, impactes, borne)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=resultat_cls)
    ancien_gestionnaire = armer_borne(borne) if borne else None
    debut, resultat = time.monotonic(), None
    try:
        resultat = runner.run(suite)
    finally:
        if borne:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if ancien_gestionnaire is not None:
                signal.signal(signal.SIGALRM, ancien_gestionnaire)
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
    if getattr(args, "flows", None):
        # #3754 : les flux d'une ci-full portent la durée de chaque test et,
        # sommées, celle de chaque module ; seuls les tests assez lents pour
        # peser sur un découpage sont gardés dans `tests.<pilote>`. La table du
        # pilote est remplacée, jamais complétée : rejouer les mêmes flux (ou
        # les compléter) ne doit pas additionner deux fois leurs durées.
        table.setdefault("tests", {})
        par_test: dict = {}
        par_module: collections.Counter = collections.Counter()
        for f in args.flows:
            for ligne in pathlib.Path(f).read_text(encoding="utf-8").splitlines():
                try:
                    evenement = json.loads(ligne)
                except json.JSONDecodeError:
                    continue
                if evenement.get("type") != "test" or evenement.get("module") not in connus:
                    continue
                test_id = evenement.get("test")
                duree = evenement.get("duree_s")
                if not test_id or not isinstance(duree, (int, float)) \
                        or isinstance(duree, bool):
                    continue
                par_test[test_id] = round(par_test.get(test_id, 0.0) + float(duree), 1)
                par_module[evenement["module"]] += float(duree)
        mesures.update({m: round(t, 1) for m, t in par_module.items()})
        seuil = float(getattr(args, "min_test_s", 5.0) or 0.0)
        par_test = {t: d for t, d in sorted(par_test.items()) if d >= seuil}
        if par_test:
            table["tests"][args.driver] = par_test
        else:
            table["tests"].pop(args.driver, None)
        # Seul un relevé de flux est une mesure de ci-full : la fraîcheur
        # (`_generated`, lue par test-plan.py) ne marque pas une fusion de
        # fichiers partiels (`--files`, scripts/test-parallele.sh).
        table["_generated"] = datetime.datetime.now(datetime.timezone.utc) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
    table[args.driver] = {m: t for m, t in sorted(mesures.items()) if m in connus}
    TABLE.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    try:
        etiquette_table = TABLE.relative_to(REPO).as_posix()
    except ValueError:
        etiquette_table = str(TABLE)
    print(f"{etiquette_table} : {len(table[args.driver])} module(s) pour {args.driver}")
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
            p.add_argument("--test-timeout", type=float, default=TEST_TIMEOUT,
                           help="borne chaque test sous mesure (secondes, défaut %(default)s) ; "
                           "0 la désactive (#3604)")
            p.add_argument("--parallel", type=int, default=1,
                           help="#3754 : N processus de test au moins dans la part, "
                           "chacun son schéma (1 = série, défaut) ; la charge mesurée "
                           "peut en lancer jusqu'à 4 ; nécessite --plan")
        p.add_argument("--map", help="carte (carte.json.gz) : durées des modules, "
                       "tests/parts/durees.json en repli (L161e)")
        if nom == "run":
            p.add_argument("--plan", help="plan.json (scripts/test-plan.py, L161f): "
                           "modules, order and classes of the part")
    p = sous.add_parser("durations")
    p.add_argument("--driver", choices=("psql", "psycopg"), required=True)
    p.add_argument("files", nargs="*")
    p.add_argument("--flows", nargs="+",
                   help="#3754 : flux JSONL d'une ci-full, pour les durées par test")
    p.add_argument("--min-test-s", type=float, default=5.0,
                   help="durée minimale d'un test conservé dans tests.<pilote> (défaut 5 s)")
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
    p.add_argument("--plafond-min", type=int,
                   help="timeout-minutes du job de mesure : signale une part trop "
                   "proche du plafond (#2224)")
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
    if args.commande == "durations" and not args.files and not args.flows:
        ap.error("durations: au moins un fichier de durées ou un flux est requis")
    return run(args) if args.commande == "run" else durations(args)


if __name__ == "__main__":
    sys.exit(main())
