#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Carte test → code mesurée (L161e).

    scripts/map.py install
    scripts/test-parts.py run … --measure DIR      (une part sous mesure)
    scripts/map.py merge --output carte.json.gz [--previous F] DIR [DIR…]
    scripts/map.py show carte.json.gz [CHEMIN | table:NOM | test_module …]
    scripts/map.py blocks src/ameesh/x.py

Le lien entre un module de test et le code n'est jamais déclaré : il est
**mesuré** en passant la suite sous mesure (docs/design/etudes/
l161-tests-par-impact.md, section 1) :

1. **code Python exécuté, par test, sous-processus compris** : coverage.py,
   un contexte par test dans le processus de la suite, et dans chaque
   sous-processus un contexte statique lu dans `CARTE_CONTEXTE` ;
   `install` pose le fichier `.pth` qui démarre la mesure dans les
   sous-processus (scripts/map_measure.py) ;
2. **fichiers de données lus et dossiers listés**, par test : hook d'audit ;
3. **tables touchées**, par classe : `pg_stat_user_tables` du schéma de la
   classe, relevé par `tests/support.py` avant de le supprimer.

**Grain : le bloc.** Chaque fichier de `src/` est découpé en blocs :
fonctions et méthodes (leur corps), classes (leur corps hors méthodes),
et `<module>` (le reste, dont les lignes `def` et `class`, exécutées à
l'import). L'empreinte d'un bloc est celle de son arbre syntaxique, sans
numéros de ligne : une PR change un bloc si son empreinte diffère. Un module
de test qui exécute une méthode dépend aussi de sa classe et du `<module>`
de son fichier (il les a importés).

**La carte** (`ameesh-carte/1`, JSON compressé, clés triées) :

    commit, genere, outils     commit mesuré, date, versions (Python, coverage)
    measure, mesure            parts, processus mesurés (dont sans contexte),
                               artefacts de mesure illisibles (jamais comptés
                               comme mesure valide), sous-processus Python
                               lancés sans la mesure, surcoût (L148 stage 6 :
                               les deux graphies, l'anglaise d'abord, une
                               release)
    modules.<m>                durations et durees (mêmes valeurs), échecs sous
                               mesure (14 jours)
    blocs.<fichier>.<bloc>     empreinte, lignes [début, fin], modules
    donnees.<fichier>          modules qui l'ont lu
    dossiers.<dossier>         modules qui l'ont listé
    tables.<table>             modules qui l'ont touchée
    migrations.<fichier>       tables créées ou modifiées par la migration
                               (liste vide : inconnues, à traiter comme
                               « toutes »)
    sans_test                  blocs qu'aucun test n'exécute (fichier::bloc)

Les noms de modules sont ceux de `tests/` sans préfixe (`test_cluster`).
La carte n'est jamais bloquante : un échec sous mesure est noté, pas reporté.
"""
from __future__ import annotations

import argparse
import ast
import collections
import copy
import datetime
import glob
import gzip
import hashlib
import json
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import sys
import sysconfig

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src"
MIGRATIONS = SRC / "ameesh" / "migrations"
TABLE_DURATIONS = REPO / "tests" / "parts" / "durees.json"
SCHEMA = "ameesh-carte/1"
#: nom du fichier .pth posé dans le site-packages de l'interpréteur
PTH = "ameesh_carte.pth"
#: les échecs sous mesure sont gardés ce nombre de jours
FAILURE_DAYS = 14
MODULE = "<module>"


# -- installation ---------------------------------------------------------

def write_rc(dossier: str) -> str:
    """Configuration coverage de la mesure : chaque processus écrit son
    propre fichier de données, contexte statique lu dans l'environnement."""
    rc = os.path.join(dossier, "coveragerc")
    os.makedirs(os.path.join(dossier, "cov"), exist_ok=True)
    pathlib.Path(rc).write_text(
        "[run]\n"
        f"data_file = {os.path.join(dossier, 'cov', '.coverage')}\n"
        "parallel = true\n"
        f"source = {SRC / 'ameesh'}\n"
        "context = ${CARTE_CONTEXTE-}\n", encoding="utf-8")
    return rc


def _site_packages() -> pathlib.Path:
    return pathlib.Path(sysconfig.get_paths()["purelib"])


def pth_installed() -> bool:
    return (_site_packages() / PTH).exists()


def install(_args=None) -> int:
    sys.path.insert(0, str(REPO / "scripts"))
    import map_measure
    try:
        import coverage  # noqa: F401
    except ImportError:
        raise SystemExit("coverage absent : pip install 'coverage>=7.10'")
    cible = _site_packages() / PTH
    cible.write_text(map_measure.pth_line(), encoding="utf-8")
    print(f"{cible} : mesure des sous-processus quand CARTE_MESURE est posé")
    return 0


# -- blocs ----------------------------------------------------------------

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


class _Stub(ast.NodeTransformer):
    """Remplace le corps des fonctions et des classes par `pass` : ce qui
    reste est exécuté à l'import (en-têtes, décorateurs, valeurs par défaut)."""

    def visit_FunctionDef(self, noeud):
        noeud.body = [ast.Pass()]
        return noeud

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


def _fingerprint(noeud: ast.AST) -> str:
    return hashlib.sha1(ast.dump(noeud).encode("utf-8")).hexdigest()[:16]


def blocks(source: str) -> tuple[dict[str, dict], list[str]]:
    """Les blocs d'un fichier Python ({nom: {empreinte, lignes}}) et, pour
    chaque numéro de ligne, le bloc qui la porte (index 0 inutilisé)."""
    arbre = ast.parse(source)
    n = len(source.splitlines())
    proprietaire = [MODULE] * (n + 2)
    infos: dict[str, dict] = {
        MODULE: {"empreinte": _fingerprint(_Stub().generic_visit(copy.deepcopy(arbre))),
                 "lignes": [1, max(n, 1)]}}

    def unique(nom: str) -> str:
        if nom not in infos:
            return nom
        i = 2
        while f"{nom}#{i}" in infos:
            i += 1
        return f"{nom}#{i}"

    def walk(corps: list, prefixe: str) -> None:
        for noeud in corps:
            if isinstance(noeud, _DEFS + (ast.ClassDef,)):
                nom = unique(prefixe + noeud.name)
                debut, fin = noeud.body[0].lineno, noeud.end_lineno
                if isinstance(noeud, ast.ClassDef):
                    classe = copy.deepcopy(noeud)
                    classe.body = [_Stub().visit(x) for x in classe.body]
                    empreinte = _fingerprint(classe)
                else:
                    empreinte = _fingerprint(noeud)
                infos[nom] = {"empreinte": empreinte, "lignes": [debut, fin]}
                for ligne in range(debut, fin + 1):
                    proprietaire[ligne] = nom
                if isinstance(noeud, ast.ClassDef):
                    walk(noeud.body, nom + ".")
            else:  # if, try, with… au niveau du module ou de la classe
                for champ in ("body", "orelse", "finalbody"):
                    sous = getattr(noeud, champ, None)
                    if isinstance(sous, list):
                        walk(sous, prefixe)
                for groupe in getattr(noeud, "handlers", []) + getattr(noeud, "cases", []):
                    walk(groupe.body, prefixe)

    walk(arbre.body, "")
    return infos, proprietaire


def _ancestors(nom: str) -> list[str]:
    """`A.B.f` → `A.B`, `A` (blocs englobants, s'ils existent)."""
    morceaux = nom.split("#")[0].split(".")
    return [".".join(morceaux[:i]) for i in range(len(morceaux) - 1, 0, -1)]


# -- fusion ---------------------------------------------------------------

def context_module(contexte: str) -> str | None:
    """`tests.test_x.Classe.test_y` → `test_x` ; contexte vide → None."""
    morceaux = contexte.split("|")[-1].split(".")
    if len(morceaux) >= 2 and morceaux[0] == "tests" and morceaux[1].startswith("test"):
        return morceaux[1]
    return None


def relative_path(chemin: str) -> str | None:
    """Chemin mesuré (absolu, sur le runner) → `src/ameesh/…`."""
    chemin = chemin.replace(os.sep, "/")
    i = chemin.rfind("/src/")
    return chemin[i + 1:] if i >= 0 else None


_RE_TABLES = [re.compile(m, re.I) for m in (
    r"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)",
    r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.\"]+)",
    r"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"[\w\"]*\s*on\s+(?:only\s+)?([\w.\"]+)",
    r"\bcreate\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\s+[\w\"]+[^;]*?\bon\s+([\w.\"]+)",
    r"\bgrant\s+[^;]*?\bon\s+(?:table\s+)?([\w.\", ]+?)\s+to\b",
    r"\binsert\s+into\s+([\w.\"]+)",
    r"\bupdate\s+([\w.\"]+)\s+set\b",
    r"\bdelete\s+from\s+([\w.\"]+)",
    r"\bdrop\s+table\s+(?:if\s+exists\s+)?([\w.\"]+)",
)]


def migration_tables(sql: str) -> list[str]:
    """Tables qu'une migration crée, modifie ou dont elle change les droits."""
    sql = re.sub(r"--[^\n]*", "", sql)
    tables = set()
    for motif in _RE_TABLES:
        for trouve in motif.findall(sql):
            for nom in trouve.split(","):
                nom = nom.strip().strip('"').split(".")[-1].strip('"').lower()
                if nom and nom not in ("all", "tables", "schema", "sequence", "function"):
                    tables.add(nom)
    return sorted(tables)


def _json_lines(motif: str):
    for chemin in sorted(glob.glob(motif)):
        with open(chemin, encoding="utf-8", errors="replace") as fh:
            for ligne in fh:
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    yield json.loads(ligne)
                except ValueError:
                    continue  # ligne coupée (processus tué en écrivant)


def _coverage(dossiers: list[str]) -> tuple[dict, int, int, list[dict]]:
    """{fichier relatif: {ligne: {modules}}}, le nombre de processus mesurés,
    celui des processus sans contexte (environnement remplacé par le test :
    leurs lignes ne sont attribuées à aucun module) et les artefacts de mesure
    illisibles (part annulée en cours d'écriture) : signalés, jamais comptés
    comme mesure valide ni attribués à un module."""
    lignes: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    processus = sans_contexte = 0
    incomplets: list[dict] = []
    for dossier in dossiers:
        for fichier in sorted(glob.glob(os.path.join(dossier, "cov", ".coverage*"))):
            from coverage import CoverageData, CoverageException
            donnees = CoverageData(basename=fichier)
            try:
                donnees.read()
            except (CoverageException, sqlite3.DatabaseError, OSError) as exc:
                incomplets.append({
                    "fichier": os.path.relpath(fichier, dossier),
                    "erreur": f"{type(exc).__name__}: {exc}",
                })
                continue
            processus += 1
            contextes = donnees.measured_contexts()
            if contextes and not any(context_module(c) for c in contextes):
                sans_contexte += 1
            for mesure in donnees.measured_files():
                relatif = relative_path(mesure)
                if relatif is None:
                    continue
                for ligne, contextes in donnees.contexts_by_lineno(mesure).items():
                    modules = {m for m in map(context_module, contextes) if m}
                    if modules:
                        lignes[relatif][ligne] |= modules
    return lignes, processus, sans_contexte, incomplets


def _version_coverage() -> str:
    try:
        import coverage
    except ImportError:
        return ""
    return coverage.__version__


def _per_module(ensembles: dict) -> dict:
    return {k: sorted(v) for k, v in sorted(ensembles.items()) if v}


def _module_out(infos: dict) -> dict:
    """L148 stage 6 : `durations` (anglais) et l'ancienne `durees`, une release."""
    return {"durations": infos["durations"], "durees": infos["durations"],
            "echecs": infos["echecs"]}


def merge(dossiers: list[str], commit: str, precedente: dict | None = None,
              maintenant: datetime.datetime | None = None,
              table_durees: dict | None = None) -> dict:
    """Les mesures des parts → la carte."""
    maintenant = maintenant or datetime.datetime.now(datetime.timezone.utc)
    lignes, processus, sans_contexte, incomplets = _coverage(dossiers)

    # blocs de tout src/, exécutés ou non
    carte_blocs: dict[str, dict] = {}
    for fichier in sorted(SRC.rglob("*.py")):
        relatif = fichier.relative_to(REPO).as_posix()
        if "__pycache__" in relatif:
            continue
        try:
            infos, proprietaire = blocks(fichier.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        modules: dict[str, set] = collections.defaultdict(set)
        for ligne, mods in lignes.get(relatif, {}).items():
            nom = proprietaire[ligne] if ligne < len(proprietaire) else MODULE
            modules[nom] |= mods
        for nom, mods in list(modules.items()):
            for ancetre in _ancestors(nom) + [MODULE]:
                if ancetre in infos:
                    modules[ancetre] |= mods
        carte_blocs[relatif] = {nom: {**info, "modules": sorted(modules.get(nom, ()))}
                                for nom, info in sorted(infos.items())}
    sans_test = [f"{f}::{b}" for f, bs in carte_blocs.items()
                 for b, info in bs.items() if not info["modules"] and b != MODULE]

    # fichiers lus, dossiers listés, sous-processus sans mesure
    donnees, dossiers_lus = collections.defaultdict(set), collections.defaultdict(set)
    sans_mesure: dict = collections.defaultdict(set)
    for dossier in dossiers:
        for fait in _json_lines(os.path.join(dossier, "lectures", "*.jsonl")):
            module = context_module(fait.get("c", ""))
            if module is None:
                continue
            if "f" in fait:
                donnees[fait["f"]].add(module)
            elif "d" in fait:
                dossiers_lus[fait["d"]].add(module)
            elif "sans_mesure" in fait:
                sans_mesure[module].add(fait["sans_mesure"])

    # tables touchées, par classe
    tables = collections.defaultdict(set)
    for dossier in dossiers:
        for fait in _json_lines(os.path.join(dossier, "tables", "*.jsonl")):
            module = context_module(fait.get("classe", ""))
            if module:
                for table in fait.get("tables", {}):
                    tables[table].add(module)

    migrations = {}
    for sql in sorted(MIGRATIONS.glob("*.sql")):
        migrations[sql.relative_to(REPO).as_posix()] = migration_tables(
            sql.read_text(encoding="utf-8"))

    # modules : durées par pilote, échecs sous mesure (14 jours)
    # L148 stage 6 : la carte porte les deux graphies (`durations` et l'ancienne
    # `durees`) pendant une release ; les lecteurs préfèrent l'anglaise.
    modules: dict = collections.defaultdict(lambda: {"durations": {}, "echecs": []})
    for dossier in dossiers:
        for chemin in sorted(glob.glob(os.path.join(dossier, "durees*.json"))):
            for pilote, par_module in json.loads(pathlib.Path(chemin).read_text()).items():
                for module, duree in par_module.items():
                    modules[module]["durations"][pilote] = duree
    limite = (maintenant - datetime.timedelta(days=FAILURE_DAYS)).isoformat()
    for module, infos in ((precedente or {}).get("modules") or {}).items():
        anciens = [e for e in infos.get("echecs", []) if e.get("ts", "") >= limite]
        if anciens:
            modules[module]["echecs"].extend(anciens)
    for dossier in dossiers:
        for fait in _json_lines(os.path.join(dossier, "flux*.jsonl")):
            if fait.get("type") == "test" and fait.get("statut") in ("echec", "erreur"):
                modules[fait["module"]]["echecs"].append(
                    {"ts": fait.get("ts", maintenant.isoformat()), "commit": commit,
                     "test": fait.get("test", "")})
    vus = {m for ms in donnees.values() for m in ms} | {m for ms in tables.values() for m in ms}
    vus |= {m for f in carte_blocs.values() for b in f.values() for m in b["modules"]}
    for module in vus:
        modules[module]  # présent même sans durée ni échec
    for infos in modules.values():
        infos["echecs"].sort(key=lambda e: (e.get("ts", ""), e.get("test", "")))

    # surcoût : durées sous mesure face à la table de référence
    if table_durees is None and TABLE_DURATIONS.exists():
        table_durees = json.loads(TABLE_DURATIONS.read_text(encoding="utf-8"))
    surcout = {}
    for pilote, reference in (table_durees or {}).items():
        communs = [m for m in reference if pilote in modules.get(m, {}).get("durations", {})]
        base = sum(reference[m] for m in communs)
        if base > 0:
            surcout[pilote] = round(sum(modules[m]["durations"][pilote] for m in communs) / base, 2)

    # L148 stage 6 : les deux graphies pendant une release, l'anglaise d'abord.
    measure = {"parts": len(dossiers), "processus": processus,
               "processus_sans_contexte": sans_contexte,
               "artefacts_incomplets": incomplets,
               "sous_processus_sans_mesure": _per_module(sans_mesure),
               "surcout": surcout}
    return {
        "schema": SCHEMA,
        "commit": commit,
        "genere": maintenant.isoformat(timespec="seconds"),
        "outils": {"python": platform.python_version(), "coverage": _version_coverage()},
        "measure": measure,
        "mesure": measure,
        "modules": {m: _module_out(modules[m]) for m in sorted(modules)},
        "blocs": carte_blocs,
        "donnees": _per_module(donnees),
        "dossiers": _per_module(dossiers_lus),
        "tables": _per_module(tables),
        "migrations": migrations,
        "sans_test": sans_test,
    }


def write(carte: dict, chemin: str) -> None:
    texte = json.dumps(carte, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    with gzip.open(chemin, "wt", encoding="utf-8") as f:
        f.write(texte)


def read(chemin: str) -> dict:
    with gzip.open(chemin, "rt", encoding="utf-8") as f:
        carte = json.load(f)
    if carte.get("schema") != SCHEMA:
        raise ValueError(f"{chemin} : schéma {carte.get('schema')!r}, attendu {SCHEMA}")
    return carte


def summary(carte: dict) -> str:
    """Résumé Markdown (résumé du job de la carte)."""
    blocs_ = [b for bs in carte["blocs"].values() for b in bs]
    mesure = carte.get("measure", carte.get("mesure", {}))
    lignes = [
        f"### Carte test → code ({carte['commit'][:12]})", "",
        f"* {len(carte['modules'])} modules de test, {len(carte['blocs'])} fichiers de code, "
        f"{len(blocs_)} blocs, dont {len(carte['sans_test'])} sans test",
        f"* {len(carte['donnees'])} fichiers de données lus, {len(carte['dossiers'])} dossiers "
        f"listés, {len(carte['tables'])} tables touchées",
        f"* {mesure['processus']} processus mesurés en {mesure['parts']} parts, dont "
        f"{mesure['processus_sans_contexte']} sans contexte de test",
    ]
    if mesure.get("artefacts_incomplets"):
        lignes.append("* artefacts de mesure illisibles, jamais comptés comme mesure valide : "
                      + ", ".join(f"{i['fichier']} ({i['erreur']})"
                                  for i in mesure["artefacts_incomplets"]))
    if mesure["surcout"]:
        lignes.append("* surcoût de la mesure : " + ", ".join(
            f"{p} ×{r}" for p, r in sorted(mesure["surcout"].items())))
    if mesure["sous_processus_sans_mesure"]:
        lignes.append("* sous-processus Python lancés sans la mesure (environnement remplacé) : "
                      + ", ".join(sorted(mesure["sous_processus_sans_mesure"])))
    echecs = sorted(m for m, i in carte["modules"].items()
                    if any(e.get("commit") == carte["commit"] for e in i["echecs"]))
    if echecs:
        lignes.append("* en échec sous mesure (noté, jamais bloquant) : " + ", ".join(echecs))
    return "\n".join(lignes) + "\n"


def show(carte: dict, cles: list[str]) -> str:
    if not cles:
        return summary(carte)
    sortie = []
    for cle in cles:
        if cle.startswith("table:"):
            sortie.append(f"{cle} : {', '.join(carte['tables'].get(cle[6:], [])) or 'aucun'}")
        elif cle in carte["blocs"]:
            for nom, info in carte["blocs"][cle].items():
                sortie.append(f"{cle}::{nom} [{info['lignes'][0]}-{info['lignes'][1]}] : "
                              f"{', '.join(info['modules']) or 'aucun test'}")
        elif cle in carte["modules"]:
            code = sorted(f"{f}::{b}" for f, bs in carte["blocs"].items()
                          for b, i in bs.items() if b != MODULE and cle in i["modules"])
            lus = sorted(f for f, ms in carte["donnees"].items() if cle in ms)
            tab = sorted(t for t, ms in carte["tables"].items() if cle in ms)
            sortie.append(f"{cle} : {len(code)} blocs, {len(lus)} fichiers lus, tables : "
                          f"{', '.join(tab) or 'aucune'}")
            sortie.extend(f"  lit {f}" for f in lus)
        else:
            lecteurs = carte["donnees"].get(cle) or carte["dossiers"].get(cle.rstrip("/"))
            sortie.append(f"{cle} : {', '.join(lecteurs or []) or 'inconnu de la carte'}")
    return "\n".join(sortie) + "\n"


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    sous.add_parser("install", help="pose le .pth qui mesure les sous-processus")
    p = sous.add_parser("merge", help="mesures des parts → carte.json.gz")
    p.add_argument("--output", required=True)
    p.add_argument("--previous", help="carte précédente (historique des échecs)")
    p.add_argument("--commit", help="commit mesuré (défaut : HEAD)")
    p.add_argument("--summary", help="écrit aussi le résumé Markdown dans ce fichier")
    p.add_argument("directories", nargs="+")
    p = sous.add_parser("show", help="lire une carte")
    p.add_argument("map")
    p.add_argument("keys", nargs="*", help="fichier, module de test ou table:NOM")
    p = sous.add_parser("blocks", help="blocs et empreintes d'un fichier Python")
    p.add_argument("file")
    args = ap.parse_args(argv)
    if args.commande == "install":
        return install(args)
    if args.commande == "merge":
        precedente = None
        if args.previous and os.path.exists(args.previous):
            try:
                precedente = read(args.previous)
            except (OSError, ValueError) as exc:
                print(f"carte précédente ignorée : {exc}", file=sys.stderr)
        carte = merge(args.directories, args.commit or _commit(), precedente)
        write(carte, args.output)
        texte = summary(carte)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(texte)
        print(texte, end="")
        return 0
    if args.commande == "show":
        print(show(read(args.map), args.keys), end="")
        return 0
    infos, _ = blocks(pathlib.Path(args.file).read_text(encoding="utf-8"))
    print(json.dumps(infos, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
