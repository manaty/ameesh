#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3931 — attribuer un test ciblé rouge de composition à une unité (même
lecture que `packet_red`, #3801).

    scripts/paquet-attribution.py --repo <worktree> [--slug owner/repo] \\
        [--state-dir DIR] < demande.json

La demande est lue sur l'entrée standard :

    {"units": [{"lot": 1, "unit_sha": "<sha>"}],
     "failures": [{"test": "tests.test_cluster.T.t", "module": "test_cluster"}]}

La réponse porte la projection `packet_red.attribute_tests` telle quelle :
pour chaque échec, l'unité désignée et ses preuves (fichier du test, support
importé, liste attendue, migration, module couvert), ou `unattributable`.
Aucune décision de retrait ici : le script ne modifie ni lot, ni branche, ni
base. `scripts/paquet-auto.py` applique le seuil de preuve forte et retire ;
sans attribution sûre, il garde son échec actuel. #4217 : un rouge de module
**voisin** ne passe plus par cette attribution — paquet-auto le situe en
rejouant le test sur les préfixes de l'empilement.

La carte d'impact est lue **localement** (clone ou cache de l'hôte, sans
téléchargement) : un tic de composition ne dépend pas du réseau. Sans carte,
seules les preuves directes (fichier du test, support importé) sont nommées,
et le reste est `unattributable`. Aucune donnée de la base n'est lue.

#4025 — `--select` rend, pour chaque unité, les modules de test **voisins**
que paquet-auto joue en plus des modules propres de l'unité :

    {"schema": "ameesh-paquet-selection-request/1", "base": "<sha>",
     "budget": 180, "driver": "psycopg",
     "units": [{"lot": 1, "unit_sha": "<sha>"}]}

* `support` : les `tests/test_*.py` qui importent un module `tests/*.py`
  modifié par l'unité (un niveau, même lecture que `imported_test_files`) ;
  #4814 : le moins cher d'abord, dans `budget` secondes par unité, avant la
  carte — un support partagé (`tests/support.py`, ~545 modules) faisait de
  la série la suite entière, jamais finie dans sa borne ;
* `carte` : les modules que la carte d'impact locale relie aux **blocs**
  `src/` que l'unité change (empreintes de `scripts/map.py`, un bloc nouveau
  lu à son bloc englobant) ; #7195 : le budget se partage entre les blocs
  changés, les plus rares d'abord (`<module>` en dernier), chacun proposant
  son test le moins cher à tour de rôle, pour qu'un bloc très couvert ne
  consomme pas seul la borne ; `dropped` compte les modules non retenus ;
  `coverage` nomme les fichiers `src/` hors carte et les blocs non mesurés,
  pour qu'une carte antérieure ne fasse pas croire à une sélection complète.

#7195 (B1) : les fichiers et les blocs d'une unité se lisent depuis le même
point de départ. Un commit de composition (`paquet-compose.py`) est une
fusion et garde son premier parent ; une unité de plusieurs commits ordinaires
se lit depuis `merge-base(base, unit_sha)`, sinon seul son dernier commit
serait vu. Un fichier changé dont le découpage ne rend aucun bloc retombe sur
`<module>`, et la couverture le dit si même ce bloc manque.

Une carte absente ou antérieure à `base` est dite dans `card`, sans bloquer.

#4271 — la durée d'un module est celle de `tests/parts/durees.json` de l'arbre
lu, **pour le pilote** (`driver`, `psycopg` par défaut) avec lequel paquet-auto
joue les tests : la carte ne mesure que psycopg, et sous psql la suite est
environ six fois plus lente (h-07 du 28/10 : 1134 s de carte pour 13 unités,
20 à 30 min réelles chacune). La durée de la carte ne sert qu'à défaut, et
seulement pour psycopg avec une carte à la base ; sinon `UNKNOWN_DURATION_S`.

Codes de retour : 0 relevé produit (même sans carte), 2 entrée invalide,
1 lecture impossible (le relevé n'est pas une attribution).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ameesh import packet_red  # noqa: E402

SCHEMA = "ameesh-paquet-attribution/1"
REQUEST_SCHEMA = "ameesh-paquet-attribution-request/1"
SELECTION_SCHEMA = "ameesh-paquet-selection/1"
SELECTION_REQUEST_SCHEMA = "ameesh-paquet-selection-request/1"
TEST_FILE_RE = re.compile(r"tests/(test_[A-Za-z0-9_]+)\.py")
#: la durée prêtée à un module que la carte ne mesure pas
UNKNOWN_DURATION_S = 60.0
MAP_SCRIPT = "scripts/map.py"
#: #4271 : les durées mesurées de la suite, par pilote (`test-parts.py durations`)
DURATIONS_REL = "tests/parts/durees.json"
DRIVERS = ("psycopg", "psql")
SLUG_RE = re.compile(r"(?:github\.com|gitlab\.com)[:/]([^/]+/[^/]+?)(?:\.git)?/?$")
DEFAULT_STATE = Path.home() / ".local" / "state" / "ameesh"


class InputError(RuntimeError):
    """La demande ou les arguments ne permettent pas de lire la composition."""


def _text(value) -> str:
    return str(value or "").strip()


def repository_slug(repo: Path) -> str:
    """`owner/repo` du remote `origin`, ou "" (la carte n'est alors pas lue)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    found = SLUG_RE.search(_text(proc.stdout))
    return found.group(1) if found else ""


def head_of(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{commit}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return _text(proc.stdout) if proc.returncode == 0 else ""


def members(payload) -> list[dict]:
    """Les unités de la demande, contrôlées (`lot` positif, `unit_sha` hex)."""
    if not isinstance(payload, dict):
        raise InputError("la demande doit être un objet JSON")
    units = payload.get("units")
    if not isinstance(units, list):
        raise InputError("la demande doit porter une liste `units`")
    out: list[dict] = []
    for entry in units:
        if not isinstance(entry, dict):
            raise InputError("unité illisible : %r" % (entry,))
        try:
            lot = int(entry["lot"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError("unité sans lot valide : %r" % (entry,)) from exc
        sha = _text(entry.get("unit_sha"))
        if lot <= 0 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise InputError("unité invalide (lot %r, sha %r)" % (lot, sha))
        out.append({"lot": lot, "unit_sha": sha})
    return out


def failures(payload) -> list[dict]:
    """Les échecs de la demande, au format attendu par `packet_red`."""
    raw = payload.get("failures")
    if not isinstance(raw, list):
        raise InputError("la demande doit porter une liste `failures`")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("échec illisible : %r" % (entry,))
        test = _text(entry.get("test"))
        module = _text(entry.get("module"))
        if not test and not module:
            raise InputError("échec sans test ni module : %r" % (entry,))
        out.append({"test": test, "module": module})
    return out


def read_card(repo: Path, slug: str, state_dir: Path):
    """La carte du clone ou du cache de l'hôte, sans réseau (`download=False`)."""
    cfg = types.SimpleNamespace(state_dir=str(state_dir))
    return packet_red.carte_for(
        cfg, slug, repo=str(repo), download=False)


def attribute(repo: Path, unit_rows: list[dict], rows: list[dict],
              slug: str, state_dir: Path) -> dict:
    """Le relevé complet, ou un statut qui dit pourquoi il ne conclut pas."""
    head = head_of(repo)
    units = packet_red.unit_files(unit_rows, checkout=str(repo))
    card, card_prov = read_card(repo, slug, state_dir)

    def source(module: str) -> str:
        return packet_red._source_text(str(repo), head, module, repo="")

    tests = packet_red.attribute_tests(
        rows, units, packet_red.card_index(card), sources=source)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "repo": str(repo),
        "head": head,
        "slug": slug,
        "card": card_prov,
        "units": units,
        "tests": tests,
        "unattributable": sum(1 for entry in tests
                              if entry.get("status") != "probable"),
        "why": "",
    }


def _card_against_base(repo: Path, prov: dict, base: str) -> dict:
    """La provenance de la carte, avec sa place par rapport à la base."""
    prov = dict(prov or {})
    commit = _text(prov.get("commit"))
    if not commit or not base:
        prov["base"] = "inconnue"
        return prov
    if commit == base or base.startswith(commit) or commit.startswith(base):
        prov["base"] = "à jour"
        return prov
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, base],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        code = proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        code = None
    prov["base"] = {0: "antérieure", 1: "hors historique"}.get(code, "illisible")
    return prov


def _show(repo: Path, ref: str, path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", "%s:%s" % (ref, path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


@functools.lru_cache(maxsize=1)
def _map_blocks():
    """`blocks` de `scripts/map.py` (le découpage de la carte), ou None."""
    path = ROOT / MAP_SCRIPT
    if not path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("paquet_carte_map", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - sans découpage, le fichier entier
        return None
    return getattr(module, "blocks", None)


def _merge_base(repo: Path, base: str, sha: str) -> str:
    """#7195 : `merge-base(base, sha)`, le point de départ de l'unité, ou ""."""
    if not base or not sha:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", base, sha],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return _text(proc.stdout) if proc.returncode == 0 else ""


def _parents(repo: Path, sha: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", sha],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return _text(proc.stdout).split()[1:]


def unit_left(repo: Path, base: str, sha: str) -> str:
    """#7195 : la gauche du diff des blocs d'une unité.

    Un commit de composition (`paquet-compose.py`) est une fusion : son
    premier parent est la composition précédente, donc `sha^1..sha` est
    exactement le delta de l'unité. Un commit d'unité ordinaire (branche de
    lot) peut porter plusieurs commits : la gauche est alors
    `merge-base(base, sha)`, le même point de départ que ses fichiers.
    """
    parents = _parents(repo, sha)
    if len(parents) >= 2:
        return parents[0]
    return _merge_base(repo, base, sha) or (parents[0] if parents else sha + "^1")


def diff_files(repo: Path, left: str, sha: str) -> set[str]:
    """#7195 : les fichiers changés entre `left` et `sha`."""
    if not left or not sha:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "-z",
             "--no-renames", left, sha],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {_text(name) for name in proc.stdout.split("\0") if _text(name)}


def changed_blocks(repo: Path, sha: str, path: str, blocks,
                   left: str = "") -> set | None:
    """Les blocs de `path` que l'unité change (nom de la carte), ou None
    quand le découpage est illisible (l'appelant prend le fichier entier).

    `left` est le point de départ (`unit_left`) ; à défaut `sha^1`, le geste
    antérieur des commits de composition.
    """
    if blocks is None:
        return None
    start = left or sha + "^1"
    old, new = _show(repo, start, path), _show(repo, sha, path)
    try:
        before = blocks(old)[0] if old is not None else {}
        after = blocks(new)[0] if new is not None else {}
    except (SyntaxError, ValueError):
        return None
    return {name for name in set(before) | set(after)
            if (before.get(name) or {}).get("empreinte")
            != (after.get(name) or {}).get("empreinte")}


def read_durations(repo: Path, driver: str) -> dict[str, float]:
    """{module: secondes} de `tests/parts/durees.json` pour `driver`, ou {}."""
    try:
        data = json.loads((repo / DURATIONS_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    table = data.get(driver) if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    return {str(module): float(value) for module, value in table.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0}


def _module_price(card, module: str, durations: dict | None = None,
                  card_durations: bool = True) -> tuple[float, bool]:
    """#4271 : `(durée, mesurée)` pour le pilote.

    La durée mesurée de `tests/parts/durees.json`, puis celle de la carte si
    elle vaut pour ce pilote et cette base, sinon `UNKNOWN_DURATION_S` et
    `False` : le forfait n'est pas une mesure et ne doit pas primer un test
    dont la durée est connue (#7195).
    """
    if durations and module in durations:
        return durations[module], True
    if not card_durations:
        return UNKNOWN_DURATION_S, False
    entry = ((card or {}).get("modules") or {}).get(module) or {}
    table = entry.get("durations") or entry.get("durees") or {}
    try:
        if "psycopg" in table:
            return float(table["psycopg"]), True
        if table:
            return float(max(table.values())), True
    except (TypeError, ValueError):
        pass
    return UNKNOWN_DURATION_S, False


def _module_seconds(card, module: str, durations: dict | None = None,
                    card_durations: bool = True) -> float:
    """#4271 : la durée prêtée à un module (voir `_module_price`)."""
    return _module_price(card, module, durations, card_durations)[0]


def _leaf_blocks(names) -> list[str]:
    """#7195 : les blocs changés les plus précis.

    `scripts/map.py` rend le bloc changé et ses parents (`<module>`,
    `Classe`, `methode`) : garder les parents ferait couvrir un module pour
    un changement voisin et noierait la sélection. Un parent n'est retenu que
    si aucun de ses enfants n'a changé ; `<module>` seul quand rien de plus
    précis n'est mesuré.
    """
    names = {str(name) for name in names or () if str(name)}
    precise = {name for name in names if name != "<module>"}
    leaves = [name for name in precise
              if not any(other != name and other.startswith(name + ".")
                         for other in precise)]
    if leaves:
        return sorted(leaves)
    return ["<module>"] if "<module>" in names else []


def _resolve_entry(by_block: dict, name: str) -> tuple[str, dict | None]:
    """L'entrée de carte du bloc changé, ou de son parent le plus proche.

    Un bloc neuf n'est pas mesuré : on remonte `Classe.methode` → `Classe`,
    puis `<module>` ; l'appelant dit que le bloc précis reste hors carte
    (#7195) mais garde les tests du parent, plutôt que rien.
    """
    parts = name.split("#")[0].split(".")
    while parts:
        key = ".".join(parts)
        entry = by_block.get(key)
        if isinstance(entry, dict):
            return key, entry
        parts = parts[:-1]
    entry = by_block.get("<module>")
    return ("<module>", entry) if isinstance(entry, dict) else ("", None)


def card_candidates(repo: Path, card, sha: str,
                    files, left: str = "") -> tuple[dict, dict]:
    """Les modules voisins d'une unité, et ce que la carte ne couvre pas.

    Rend `({module: {"blocks": [...], "files": [...]}}, couverture)` : chaque
    module est rattaché aux blocs changés qu'il couvre, pour que `select`
    partage le budget entre blocs (#7195). `couverture` nomme les `src/*.py`
    changés absents de la carte (`unknown_paths`) et les blocs qu'elle ne
    mesure pas (`unmapped_blocks`) : une carte antérieure ne peut pas faire
    croire à une sélection complète. `left` est le point de départ des blocs
    (`unit_left`) : une unité de plusieurs commits se lit depuis la base.
    """
    coverage = {"unknown_paths": [], "unmapped_blocks": []}
    raw = (card or {}).get("blocs")
    if not isinstance(raw, dict):
        raw = {}
    blocks = _map_blocks()
    out: dict[str, dict] = {}
    for path in sorted(files):
        if not (path.startswith("src/") and path.endswith(".py")):
            continue
        by_block = raw.get(path)
        if not isinstance(by_block, dict):
            coverage["unknown_paths"].append(path)
            continue
        names = changed_blocks(repo, sha, path, blocks, left)
        if names is None:
            names = set(by_block)
        if not names:
            # #7195 : le fichier change mais le découpage ne rend aucun bloc.
            # Le module entier reste le seul recours ; si `<module>` manque
            # aussi, la couverture le dira au lieu de se taire.
            names = {"<module>"}
        for name in _leaf_blocks(names):
            resolved, entry = _resolve_entry(by_block, name)
            if not isinstance(entry, dict):
                coverage["unmapped_blocks"].append("%s:%s" % (path, name))
                continue
            if resolved != name:
                # le bloc précis n'est pas mesuré : les tests du parent sont
                # pris, et la carte est dite incomplète (#7195)
                coverage["unmapped_blocks"].append("%s:%s" % (path, name))
            for module in entry.get("modules") or ():
                module = _text(module)
                if module.startswith("tests."):
                    module = module[len("tests."):]
                row = out.setdefault(module, {"blocks": set(), "files": set()})
                row["blocks"].add(name)
                row["files"].add(path)
    return ({module: {"blocks": sorted(row["blocks"]),
                      "files": sorted(row["files"])}
             for module, row in out.items()}, coverage)


def select(repo: Path, unit_rows: list[dict], budget: float, base: str,
           slug: str, state_dir: Path, driver: str = "psycopg") -> dict:
    """Les modules voisins de chaque unité (support, carte), avec leur raison."""
    units = packet_red.unit_files(unit_rows, checkout=str(repo))
    tests_dir = repo / "tests"
    imports: dict[str, set] = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        imports[path.stem] = packet_red.imported_test_files(text)
    card, card_prov = read_card(repo, slug, state_dir)
    card_prov = _card_against_base(repo, card_prov, base)
    durations = read_durations(repo, driver)
    # la carte mesure psycopg, à son commit : ni psql, ni une base plus récente
    card_durations = driver == "psycopg" and card_prov.get("base") == "à jour"
    priced = {"driver": driver, "table": DURATIONS_REL if durations else "",
              "measured": len(durations), "card": card_durations}
    out_units = []
    for unit in units:
        sha = _text(unit.get("unit_sha"))
        # #7195 : le point de départ des fichiers et des blocs est le même.
        # Une unité de plusieurs commits se lit depuis `merge-base(base, sha)`,
        # pas depuis son seul dernier commit.
        left = unit_left(repo, base, sha)
        files = set(unit.get("files") or ()) | diff_files(repo, left, sha)
        own = {match.group(1) for match in map(TEST_FILE_RE.fullmatch, files)
               if match and (repo / match.group(0)).is_file()}
        touched_tests = {path for path in files
                         if path.startswith("tests/") and path.endswith(".py")}
        added: list[dict] = []
        taken = set(own)
        supports = []
        for module in sorted(imports):
            if module in taken:
                continue
            hit = sorted(imports[module] & touched_tests)
            if hit:
                supports.append((_module_seconds(card, module, durations,
                                                 card_durations), module, hit))
                taken.add(module)
        # #4814 : un support partagé (`tests/support.py`, importé par ~545
        # modules) ne fait plus de la série de l'unité la suite entière
        supports.sort()
        spent, supported = 0.0, 0
        for seconds, module, hit in supports:
            if spent + seconds > budget:
                continue
            spent += seconds
            supported += 1
            added.append({"module": "tests." + module, "reason": "support",
                          "files": hit, "seconds": round(seconds, 1)})
        dropped = len(supports) - supported
        found, coverage = (card_candidates(repo, card, sha, files, left)
                           if card is not None else ({}, {}))
        candidates: dict[str, dict] = {}
        for module, row in found.items():
            if module in taken or not re.fullmatch(r"test_[A-Za-z0-9_]+", module) \
                    or not (tests_dir / (module + ".py")).is_file():
                continue
            seconds, measured = _module_price(card, module, durations,
                                              card_durations)
            candidates[module] = {"seconds": seconds, "measured": measured,
                                  "blocks": row["blocks"], "files": row["files"]}
        # #7195 : le budget se partage entre les blocs changés, pas seulement
        # par ordre de durée. Un bloc rare (peu de tests le couvrent) passe
        # avant un bloc très couvert ; `<module>` passe en dernier. Chaque
        # bloc propose son test le moins cher à tour de rôle : un test ciblé
        # d'un bloc rare n'est plus doublé par des tests d'un bloc voisin.
        # Les durées mesurées passent avant le forfait `UNKNOWN_DURATION_S` :
        # un forfait ne doit pas chasser un test ciblé mesuré plus court.
        by_block: dict[str, list[str]] = {}
        for module, row in candidates.items():
            for block in row["blocks"]:
                by_block.setdefault(block, []).append(module)
        order = sorted(by_block, key=lambda block: (block == "<module>",
                                                    len(by_block[block]), block))
        queues = []
        for block in order:
            measured = [name for name in by_block[block]
                        if candidates[name]["measured"]]
            unknown = [name for name in by_block[block]
                       if not candidates[name]["measured"]]
            measured.sort(key=lambda name: (candidates[name]["seconds"], name))
            unknown.sort(key=lambda name: (candidates[name]["seconds"], name))
            queues.append((measured, unknown))
        selected: set[str] = set()

        def offer(module: str) -> None:
            nonlocal spent
            if module in taken or module in selected:
                return
            seconds = candidates[module]["seconds"]
            if spent + seconds > budget:
                return
            spent += seconds
            selected.add(module)
            added.append({"module": "tests." + module, "reason": "carte",
                          "files": candidates[module]["files"],
                          "seconds": round(seconds, 1)})

        def drain(known: bool) -> None:
            pending = [entry for entry in queues
                       if (entry[0] if known else entry[1])]
            while pending:
                still = []
                for measured, unknown in pending:
                    queue = measured if known else unknown
                    while queue and queue[0] in selected:
                        queue.pop(0)
                    if not queue:
                        continue
                    still.append((measured, unknown))
                    offer(queue.pop(0))
                pending = still

        drain(True)
        drain(False)
        dropped += len(candidates) - len(selected)
        out_units.append({"lot": int(unit["lot"]),
                          "unit_sha": _text(unit.get("unit_sha")),
                          "added": added, "dropped": dropped,
                          "coverage": coverage,
                          "seconds": round(spent, 1)})
    return {"schema": SELECTION_SCHEMA, "status": "ok", "repo": str(repo),
            "slug": slug, "budget": budget, "card": card_prov,
            "durations": priced, "units": out_units, "why": ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Attribue un test ciblé rouge de composition (#3931).")
    parser.add_argument("--repo", default=".",
                        help="worktree composé à lire (défaut : .)")
    parser.add_argument("--slug", default="",
                        help="dépôt owner/repo de la carte (défaut : remote origin)")
    parser.add_argument("--state-dir", default=os.environ.get("AGENT_MESH_STATE")
                        or str(DEFAULT_STATE),
                        help="état ameesh, pour le cache de la carte")
    parser.add_argument("--select", action="store_true",
                        help="rend les modules voisins des unités (#4025)")
    args = parser.parse_args(argv)
    schema = SELECTION_SCHEMA if args.select else SCHEMA
    repo = Path(args.repo).resolve()
    try:
        try:
            payload = json.load(sys.stdin)
        except ValueError as exc:
            raise InputError("demande JSON illisible : %s" % exc) from exc
        if not isinstance(payload, dict):
            raise InputError("la demande doit être un objet JSON")
        expected = SELECTION_REQUEST_SCHEMA if args.select else REQUEST_SCHEMA
        if payload.get("schema") not in (None, expected):
            raise InputError("schéma de demande inconnu : %r"
                             % payload.get("schema"))
        unit_rows = members(payload)
        slug = _text(args.slug) or repository_slug(repo)
        if args.select:
            try:
                budget = float(payload.get("budget", 0))
            except (TypeError, ValueError) as exc:
                raise InputError("budget illisible : %r"
                                 % payload.get("budget")) from exc
            driver = _text(payload.get("driver")) or "psycopg"
            if driver not in DRIVERS:
                raise InputError("pilote inconnu : %r" % driver)
            out = select(repo, unit_rows, budget, _text(payload.get("base")),
                         slug, Path(args.state_dir), driver=driver)
        else:
            rows = failures(payload)
            out = attribute(repo, unit_rows, rows, slug, Path(args.state_dir))
    except InputError as exc:
        print(json.dumps({"schema": schema, "status": "invalid",
                          "tests": [], "why": str(exc)}, ensure_ascii=False))
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - le tic garde son échec actuel
        print(json.dumps({"schema": schema, "status": "error",
                          "tests": [], "why": "%s: %s"
                          % (type(exc).__name__, str(exc)[:300])},
                         ensure_ascii=False))
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
