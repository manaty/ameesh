# SPDX-License-Identifier: AGPL-3.0-only
"""#3801 — nommer l'unité probable d'un paquet rouge, sans rien retirer.

Un paquet (`integration/**`) est composé d'unités : chaque commit de fusion de
première parente porte un trailer `ameesh-work: <lot>` et son second parent est
la tête composée. Quand sa CI est rouge, `ameesh ci status <paquet>` et le
courrier du composeur nommaient le test et le lot porteur, jamais l'unité du
paquet qui l'a probablement cassé. Le pilote et le composeur refaisaient le lien
à la main (10 à 40 min par rouge sur h-30, 26/10).

Ce module construit ce lien **à partir de preuves**, pour chaque test en
échec :

1. **fichier du test ou module qu'il couvre** — la carte mesurée
   (`ameesh-carte/1`, L161e) relie un module de test aux blocs de `src/` qu'il
   exécute ; l'unité qui modifie l'un de ces fichiers est nommée. Le fichier du
   test lui-même et les modules de `tests/` qu'il importe (support partagé,
   gabarit) comptent aussi : la carte ne mesure pas leurs imports ;
2. sinon **une liste attendue par le test** — `tests/parts/*`,
   `deploy/sql/role-*`, `src/ameesh/migrations/*` lus ou listés par le test ;
   une migration n'est retenue que si le test touche des tables (un test sans
   base ne désigne pas une migration) ;
3. sinon **non attribuable** : rien n'est inventé.

L'unité est nommée, jamais retirée : ce module ne modifie ni lot, ni branche,
ni CI. Un candidat n'est pas une preuve de culpabilité.

Le manifeste de composition est lu **à la tête du run** dans le dépôt
(`reviews/integration-<nom>.md`, #2426) : c'est le seul état qui décrit encore
une tête recomposée (les manifestes v1 à v4 de h-30). À défaut, la composition
enregistrée par `ameesh work packet record` (#2443) ne sert que si sa tête est
celle du run ; un enregistrement d'une autre composition n'est pas appliqué à
un ancien run. Les fichiers de chaque unité sont lus par `git diff <unit>^1
<unit>` dans un clone local ; sans clone, ils sont lus au commit GitHub
(`commits/<sha>`, champ `files`) quand un client API est fourni.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import posixpath
import re
import shutil
import tempfile
import time
from urllib.parse import quote

from . import packet_close, plan_git, storage, work

#: le schéma de la projection par run (aucune autorité, seulement des faits)
SCHEMA = "ameesh-packet-red/1"
#: la carte mesurée lue (L161e)
CARD_SCHEMA = "ameesh-carte/1"
#: au-delà, la carte est dite périmée (règle #3344) mais reste utilisable
CARD_STALE_S = 48 * 3600
#: au-delà, elle n'est plus lue du tout : le lien test→fichier n'est plus sûr
CARD_MAX_S = 7 * 24 * 3600
#: la liste des migrations, seule liste dont un test sans table ne se sert pas
MIGRATIONS_DIR = "src/ameesh/migrations"
#: le préfixe d'une branche porteuse de paquet
CARRIER_PREFIX = "integration/"
#: poids des preuves : le fichier du test et les listes passent avant une
#: couverture large ; un support partagé (`tests/support.py`) est une
#: dépendance de tous les tests de base, pas une preuve plus forte qu'un
#: module précis couvert par le test.
WEIGHTS = {"test": 6, "support": 2, "list": 4, "migration": 4, "covered": 2}
#: ordre de préférence quand un même fichier porte plusieurs preuves
_KIND_ORDER = ("test", "support", "list", "migration", "covered")
#: unités nommées au plus par test (le reste est compté, jamais caché)
MAX_UNITS = 3
#: caractères d'un dépôt conservés dans un nom de fichier de cache
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


# --------------------------------------------------------------------------
# chemins et petits lecteurs
# --------------------------------------------------------------------------

def _norm(path) -> str:
    """Un chemin de dépôt normalisé (séparateurs `/`, ni `./` ni `/` initial)."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _text(value) -> str:
    return str(value or "").strip()


def is_carrier(item) -> bool:
    """Le lot porte-t-il une branche `integration/**` ?"""
    return _text((item or {}).get("branch")).startswith(CARRIER_PREFIX)


def manifest_name(branch: str) -> str:
    """`integration/<nom>` → `reviews/integration-<nom>.md` (#2426, #2682)."""
    tail = _text(branch)[len(CARRIER_PREFIX):]
    return "reviews/integration-%s.md" % tail.replace("/", "-")


def module_of(failure) -> str:
    """Le module de test d'un échec (`test_423_agent_aliases`), ou "".

    Le flux CI porte `module` ; les lignes antérieures à #3783 et les échecs de
    `setUpClass` ne le portent pas toujours : le nom du test le dit alors.
    """
    failure = failure or {}
    module = _text(failure.get("module"))
    if module:
        module = module.replace("\\", "/").rsplit("/", 1)[-1]
        if module.endswith(".py"):
            module = module[:-3]
        if module.startswith("tests."):
            module = module.split(".", 1)[1].split(".", 1)[0]
        return module
    test = _text(failure.get("test"))
    found = (re.search(r"\(tests\.(test_[A-Za-z0-9_]+)[.)]", test)
             or re.match(r"tests\.(test_[A-Za-z0-9_]+)(?:[.)]|$)", test))
    return found.group(1) if found else ""


def same_head(left: str, right: str) -> bool:
    """Deux têtes désignent-elles le même commit (préfixe admis) ?"""
    left, right = _text(left).lower(), _text(right).lower()
    if not left or not right:
        return False
    if left == right:
        return True
    short, full = (left, right) if len(left) < len(right) else (right, left)
    return len(short) >= 7 and full.startswith(short)


# --------------------------------------------------------------------------
# composition du paquet : manifeste de la tête, sinon enregistrement
# --------------------------------------------------------------------------

def _file_text(checkout: str, head: str, path: str, *, api=None, repo: str = "") -> str | None:
    """Le contenu d'un fichier à la tête : clone local, puis API GitHub."""
    if checkout and head and plan_git.local_commit(checkout, head):
        text = plan_git._out(checkout, "show", "%s:%s" % (head, path))
        if text is not None:
            return text
    if api is None or not repo or not head:
        return None
    try:
        data = api("repos/%s/contents/%s?ref=%s"
                   % (repo, quote(path, safe="/"), quote(head, safe=""))) or {}
    except Exception:  # noqa: BLE001 - un lecteur muet ne relie rien
        return None
    if _text(data.get("encoding")) != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return None


def _manifest_base(text: str) -> str:
    """La base de composition déclarée par un manifeste (« Base : main `sha` »).

    Le manifeste d'un paquet nomme la tête exacte de la cible dont il est
    parti ; #3954 en a besoin pour dire si la carte d'impact a été mesurée
    avant cette base. Un manifeste ancien ou sans ligne de base rend "" : la
    comparaison est alors inconnue, jamais supposée.
    """
    for line in (text or "").splitlines():
        if not line.strip().lower().startswith("base"):
            continue
        for token in re.findall(r"`([^`]+)`", line):
            if re.fullmatch(r"[0-9a-fA-F]{7,64}", token):
                return token.lower()
    return ""


def _members_from_manifest(checkout: str, branch: str, head: str, *,
                           api=None, repo: str = "") -> tuple:
    """Les unités du manifeste de paquet **à la tête du run**, ou (None, "").

    Le manifeste est versionné dans la branche : une tête recomposée garde son
    propre manifeste, même si la composition courante enregistrée est plus
    récente. Un manifeste sans table de composition n'établit rien. Rend
    aussi la base déclarée (#3954), "" quand elle n'est pas lisible.
    """
    if not branch or not head:
        return None, "", ""
    path = manifest_name(branch)
    text = _file_text(checkout, head, path, api=api, repo=repo)
    if text is None:
        # pas de repli sur un autre manifeste de la tête : `main` en porte des
        # dizaines (anciens paquets) et le premier lisible nommerait les lots
        # d'un autre paquet. Sans le manifeste conventionnel, la composition
        # enregistrée (`work packet`) à la tête exacte fait foi, sinon rien.
        return None, "", ""
    members = []
    for entry in packet_close.parse_manifest(text):
        if entry.get("withdrawn"):
            continue
        commits, sources = entry.get("commits") or [], entry.get("sources") or []
        if not commits:
            continue
        for lot in entry.get("lots") or ():
            members.append({"lot": int(lot), "unit_sha": commits[0],
                            "source_sha": sources[0] if sources else ""})
    return (members or None), path, _manifest_base(text)


def _members_from_record(db, package_id: int, head: str) -> tuple:
    """La composition enregistrée, seulement si elle décrit la tête du run."""
    try:
        rows = storage.of(db).work.packet_members(package_ids=[int(package_id)])
    except Exception as exc:  # noqa: BLE001 - un magasin incomplet ne conclut pas
        return None, ("composition enregistrée illisible : %s"
                      % type(exc).__name__), ""
    if not rows:
        return None, "aucune composition enregistrée", ""
    recorded = _text(rows[0].get("head_sha"))
    if recorded and not same_head(recorded, head):
        return None, ("composition enregistrée pour %s, pas pour la tête %s"
                      % (recorded[:12], _text(head)[:12])), ""
    members = [{"lot": int(row["work_item_id"]),
                "unit_sha": _text(row.get("unit_sha")),
                "source_sha": _text(row.get("source_sha")),
                "frozen_sha": _text(row.get("frozen_sha")),
                "recorded_ts": row.get("recorded_ts")}
               for row in rows if row.get("unit_sha")]
    return members or None, ("composition enregistrée" if members
                             else "composition enregistrée sans unité"), \
        (_text(rows[0].get("base_sha")) if members else "")


def composition(db, item, head: str, *, checkout: str = "", api=None,
                repo: str = "") -> dict:
    """Les unités de la composition qui a produit `head` (manifeste, sinon
    enregistrement), avec la preuve de leur origine et la base (#3954)."""
    branch = _text((item or {}).get("branch"))
    members, path, base = _members_from_manifest(checkout, branch, head,
                                                 api=api, repo=repo)
    if members:
        return {"members": members, "source": "manifest", "path": path,
                "base": base,
                "why": "manifeste de la tête %s" % _text(head)[:12]}
    members, why, recorded_base = _members_from_record(
        db, int(item.get("id") or 0), head)
    if members:
        return {"members": members, "source": "record", "path": "",
                "base": recorded_base or base, "why": why}
    if path:
        return {"members": [], "source": "none", "path": path,
                "base": base,
                "why": "manifeste %s sans composition" % path}
    return {"members": [], "source": "none", "path": "", "base": "",
            "why": "manifeste de paquet absent à la tête %s ; %s"
                   % (_text(head)[:12], why)}


# --------------------------------------------------------------------------
# fichiers des unités
# --------------------------------------------------------------------------

def _commit_files(checkout: str, sha: str) -> list:
    """Les fichiers d'un commit de composition, lus à son premier parent."""
    if not sha or not plan_git.local_commit(checkout, sha):
        return []
    raw = plan_git._out(checkout, "diff", "--name-only", "-z", "--no-renames",
                        sha + "^1", sha)
    if raw is None:
        return []
    return [_norm(name) for name in raw.split("\0") if name]


def _api_commit_files(api, repo: str, sha: str) -> list:
    """Les fichiers d'un commit lus à l'API GitHub (`files`), sans clone."""
    if api is None or not repo or not sha:
        return []
    try:
        entry = api("repos/%s/commits/%s" % (repo, sha)) or {}
    except Exception:  # noqa: BLE001 - une lecture muette ne désigne rien
        return []
    return [_norm(f.get("filename")) for f in (entry.get("files") or ())
            if f.get("filename")]


def unit_files(members, *, checkout: str = "", api=None, repo: str = "") -> list:
    """Les unités avec les fichiers qu'elles modifient, et leur provenance.

    Un commit de composition est lu par `git diff <unit>^1 <unit>` (le diff du
    commit de fusion contre la composition précédente, résolutions comprises).
    Sans clone — ou si la tête n'y est pas — le détail du commit GitHub fournit
    `files` quand `api` est donné. Fichiers vides : l'unité reste nommable par
    son manifeste, mais ne peut pas être reliée ; l'appelant le dit.
    """
    out = []
    for member in members or ():
        sha = _text(member.get("unit_sha"))
        files = _commit_files(checkout, sha) if checkout else []
        origin = "git" if files else ""
        if not files:
            files = _api_commit_files(api, repo, sha)
            origin = "github" if files else ""
        out.append(dict(member, files=files, files_from=origin))
    return out


# --------------------------------------------------------------------------
# carte d'impact : test → fichiers, listes attendues
# --------------------------------------------------------------------------

def _load_card(path: str):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            card = json.load(stream)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(card, dict) or card.get("schema") != CARD_SCHEMA:
        return None
    return card


def _card_age(card: dict, path: str, *, now: float) -> float | None:
    stamp = _text(card.get("genere"))
    if stamp:
        try:
            import datetime as _dt
            parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return max(0.0, now - parsed.timestamp())
        except (TypeError, ValueError):
            pass
    try:
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return None


def _state_cache(cfg, slug: str) -> str:
    state = _text(getattr(cfg, "state_dir", ""))
    if not state or not slug:
        return ""
    return os.path.join(state, "packet-red", "carte-%s.json.gz" % _SAFE.sub("-", slug))


def _copy_atomic(source: str, target: str) -> None:
    folder = os.path.dirname(target)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="carte.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream, open(source, "rb") as origin:
            shutil.copyfileobj(origin, stream)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def carte_for(cfg, slug: str, *, repo: str = "", fetch=None, now: float | None = None,
              download: bool = True):
    """La carte mesurée du dépôt, avec sa provenance, ou (None, provenance).

    Ordre : carte du clone (`carte/carte.json.gz`), puis cache de l'hôte
    (`<state>/packet-red/`), puis téléchargement de l'artefact `carte` de la
    dernière exécution verte (`prevalidation_calls.map_for`). Une carte de plus
    de 48 h est dite périmée ; au-delà de 7 jours elle n'est plus lue. Le
    téléchargement est borné (#2665) et un échec rend `absent`, jamais une
    exception.
    """
    now = time.time() if now is None else float(now)
    slug = _text(slug)
    candidates = []
    if repo:
        candidates.append(("local", os.path.join(repo, "carte", "carte.json.gz")))
    cache = _state_cache(cfg, slug)
    if cache:
        candidates.append(("cache", cache))
    for origin, path in candidates:
        if not path or not os.path.isfile(path):
            continue
        card = _load_card(path)
        if card is None:
            continue
        age = _card_age(card, path, now=now)
        if age is not None and age > CARD_MAX_S:
            continue
        prov = {"status": "périmée" if age is not None and age > CARD_STALE_S
                else "lue", "origin": origin, "commit": _text(card.get("commit")),
                "genere": _text(card.get("genere")),
                "age_s": None if age is None else int(age)}
        return card, prov
    if not download:
        return None, {"status": "absente", "origin": "", "commit": "", "age_s": None,
                      "why": "aucune carte locale"}
    if fetch is None:
        try:
            from . import prevalidation_calls
            from .deadline import Deadline
            fetch = lambda: prevalidation_calls.map_for(  # noqa: E731
                slug, repo=repo, deadline=Deadline(30))
        except Exception as exc:  # noqa: BLE001 - le lecteur absent n'est pas fatal
            return None, {"status": "absente", "origin": "", "commit": "",
                          "age_s": None, "why": "lecteur de carte absent : %s"
                                                 % type(exc).__name__}
    try:
        found = fetch()
    except Exception as exc:  # noqa: BLE001 - pas de carte, pas d'exception
        return None, {"status": "absente", "origin": "", "commit": "",
                      "age_s": None, "why": "téléchargement impossible : %s"
                                             % type(exc).__name__}
    path, scratch = (found if isinstance(found, tuple) else (found, ""))
    try:
        if not path or not os.path.isfile(path):
            return None, {"status": "absente", "origin": "", "commit": "",
                          "age_s": None, "why": "artefact carte introuvable"}
        card = _load_card(path)
        if card is None:
            return None, {"status": "absente", "origin": "", "commit": "",
                          "age_s": None, "why": "artefact carte illisible"}
        age = _card_age(card, path, now=now)
        if age is not None and age > CARD_MAX_S:
            return None, {"status": "absente", "origin": "",
                          "commit": _text(card.get("commit")),
                          "age_s": int(age),
                          "why": "artefact carte trop vieux (> 7 jours)"}
        if cache:
            try:
                _copy_atomic(path, cache)
            except OSError:
                pass
        return card, {"status": "périmée" if age is not None and age > CARD_STALE_S
                      else "lue", "origin": "téléchargée",
                      "commit": _text(card.get("commit")),
                      "genere": _text(card.get("genere")),
                      "age_s": None if age is None else int(age)}
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def card_index(card) -> dict:
    """L'index inverse de la carte : module → fichiers, dossiers, tables.

    `blocs.<fichier>.<bloc>.modules` donne le code exécuté, `donnees` les
    fichiers lus, `dossiers` les dossiers listés, `tables` les tables
    touchées. Une carte mal formée rend des ensembles vides, jamais une
    exception.
    """
    out = {"blocs": {}, "reads": {}, "dirs": {}, "tables": {}}
    if not isinstance(card, dict):
        return out
    for field, key in (("donnees", "reads"), ("dossiers", "dirs"), ("tables", "tables")):
        values = card.get(field)
        if not isinstance(values, dict):
            continue
        for path, modules in values.items():
            if not isinstance(modules, (list, tuple, set)):
                continue
            for module in modules:
                out[key].setdefault(_text(module), set()).add(_norm(path))
    blocks_by_file = card.get("blocs")
    if not isinstance(blocks_by_file, dict):
        return out
    for path, blocks in blocks_by_file.items():
        if not isinstance(blocks, dict):
            continue
        for block in blocks.values():
            if not isinstance(block, dict):
                continue
            for module in block.get("modules") or ():
                out["blocs"].setdefault(_text(module), set()).add(_norm(path))
    return out


def _listed(dirs, prefix: str) -> bool:
    """Le test liste-t-il `prefix` (ou un de ses sous-dossiers) ?"""
    for folder in dirs:
        if folder == prefix or folder.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _expected_evidence(path: str, reads: set, dirs: set, tables: set):
    """La preuve « liste attendue » d'un fichier d'unité, ou None."""
    if path.startswith("tests/parts/") and (path in reads or _listed(dirs, "tests/parts")):
        return {"kind": "list", "file": path}
    if path.startswith("deploy/sql/role-") \
            and (path in reads or _listed(dirs, "deploy/sql")):
        return {"kind": "list", "file": path}
    if path.startswith(MIGRATIONS_DIR + "/") and tables \
            and (path in reads or _listed(dirs, MIGRATIONS_DIR)):
        return {"kind": "migration", "file": path}
    return None


# --------------------------------------------------------------------------
# imports de test : support partagé que la carte ne mesure pas
# --------------------------------------------------------------------------

_FROM_REL = re.compile(r"(?m)^\s*from\s+(\.+)([\w.]*)\s+import\s+([^\n(]+)")
_FROM_TESTS = re.compile(r"(?m)^\s*from\s+tests\.([\w.]+)\s+import\b")
_FROM_TESTS_PKG = re.compile(r"(?m)^\s*from\s+tests\s+import\s+([^\n(]+)")
_IMPORT_TESTS = re.compile(r"(?m)^\s*import\s+tests\.([\w.]+)")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def imported_test_files(text: str) -> set:
    """Les modules `tests/*.py` importés par un fichier de test.

    Un support partagé (`tests/support.py`, `tests/gabarit.py`) n'est pas un
    module de `src/` : la carte L161e ne le relie pas. Un test qui l'importe
    dépend pourtant de son contenu ; l'unité qui le modifie est donc un
    candidat, avec cette preuve nommée.
    """
    out: set[str] = set()

    def add(name: str) -> None:
        name = _text(name).split(".")[0]
        if _NAME.match(name):
            out.add("tests/%s.py" % name)

    for match in _FROM_REL.finditer(text or ""):
        dots, module, names = match.groups()
        if module:
            add(module)
        elif dots:
            for name in names.split(","):
                add(name.strip().split(" as ")[0].strip())
    for match in _FROM_TESTS.finditer(text or ""):
        add(match.group(1))
    for match in _IMPORT_TESTS.finditer(text or ""):
        add(match.group(1))
    for match in _FROM_TESTS_PKG.finditer(text or ""):
        for name in match.group(1).split(","):
            add(name.strip().split(" as ")[0].strip())
    return out


def _source_text(checkout: str, head: str, module: str, *, api=None, repo: str = "") -> str:
    """Le source du test à la tête : clone local d'abord, API ensuite."""
    if not module:
        return ""
    return _file_text(checkout, head, "tests/%s.py" % module,
                      api=api, repo=repo) or ""


# --------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------

def _evidence_rank(evidence: list) -> tuple:
    """Le rang d'un candidat : preuve la plus forte, puis nombre de preuves."""
    best = max((WEIGHTS.get(item["kind"], 0) for item in evidence), default=0)
    return best, sum(WEIGHTS.get(item["kind"], 0) for item in evidence), len(evidence)


def _dedupe(evidence: list) -> list:
    """Un même fichier ne porte qu'une preuve, la plus forte."""
    best: dict[str, dict] = {}
    for item in evidence:
        path = item.get("file") or ""
        if not path:
            continue
        previous = best.get(path)
        if previous is None or _KIND_ORDER.index(item["kind"]) \
                < _KIND_ORDER.index(previous["kind"]):
            best[path] = item
    return sorted(best.values(),
                  key=lambda item: (_KIND_ORDER.index(item["kind"]), item["file"]))


def attribute_tests(failures, units, index: dict, *, sources=None) -> list:
    """La projection « test → unités probables », preuve comprise.

    `failures` : les échecs du run (`test`, `part`, `module`). `units` : les
    unités de la composition, chacune avec `files`. `index` : `card_index`.
    `sources(module)` rend le source du test (imports) quand il est lisible.
    Aucune unité : chaque test est `unattributable`, sans invention.
    """
    sources = sources or (lambda module: "")
    out = []
    for failure in failures or ():
        module = module_of(failure)
        test_file = "tests/%s.py" % module if module else ""
        imports = imported_test_files(sources(module)) if module else set()
        candidates = []
        for unit in units or ():
            files = unit.get("files") or ()
            evidence: list[dict] = []
            for path in files:
                if test_file and path == test_file:
                    evidence.append({"kind": "test", "file": path})
                elif path in imports:
                    evidence.append({"kind": "support", "file": path})
            for path in files:
                expected = _expected_evidence(
                    path, index["reads"].get(module, set()),
                    index["dirs"].get(module, set()), index["tables"].get(module, set()))
                if expected:
                    evidence.append(expected)
            for path in sorted(set(files) & index["blocs"].get(module, set())):
                evidence.append({"kind": "covered", "file": path})
            evidence = _dedupe(evidence)
            if not evidence:
                continue
            best, score, count = _evidence_rank(evidence)
            candidates.append({"lot": int(unit["lot"]),
                               "unit_sha": _text(unit.get("unit_sha")),
                               "evidence": evidence,
                               "best": best, "score": score, "count": count})
        # une preuve forte (fichier du test, liste attendue, migration) rend
        # la couverture large inutile : on ne noie pas le composeur sous les
        # modules qu'un test d'intégration exécute par ailleurs.
        strong = [candidate for candidate in candidates if candidate["best"] >= 4]
        if strong:
            candidates = strong
        candidates.sort(key=lambda row: (-row["best"], -row["score"], -row["count"],
                                         row["lot"]))
        shown = candidates[:MAX_UNITS]
        entry = {"test": _text(failure.get("test")), "part": _text(failure.get("part")),
                 "module": module,
                 "status": "probable" if shown else "unattributable",
                 "units": shown,
                 "more": max(0, len(candidates) - len(shown))}
        out.append(entry)
    return out


def _unit_label(unit: dict) -> str:
    details = []
    for item in unit.get("evidence") or ():
        kind, path = item.get("kind"), item.get("file") or ""
        if kind == "test":
            details.append("fichier du test %s" % path)
        elif kind == "support":
            details.append("support %s" % path)
        elif kind == "list":
            details.append("liste %s" % path)
        elif kind == "migration":
            details.append("migration %s" % posixpath.basename(path))
        elif kind == "covered":
            details.append("module couvert %s" % path)
    suffix = " ; ".join(details[:3])
    if len(details) > 3:
        suffix += " ; …"
    return "#%s (%s)" % (unit.get("lot"), suffix) if suffix else "#%s" % unit.get("lot")


def evidence_line(entry) -> str:
    """La ligne lisible d'une attribution de test, ou ""."""
    if not entry:
        return ""
    if entry.get("status") != "probable" or not entry.get("units"):
        return "non attribuable"
    text = " ; ".join(_unit_label(unit) for unit in entry["units"])
    if entry.get("more"):
        text += " ; … et %d autre(s)" % entry["more"]
    return text


def _card_ancestor(checkout: str, head: str, commit: str):
    """La carte est-elle un ancêtre de la tête du run (True/False/None) ?"""
    if not checkout or not head or not commit:
        return None
    if not plan_git.local_commit(checkout, head) \
            or not plan_git.local_commit(checkout, commit):
        return None
    try:
        done = plan_git._git(checkout, "merge-base", "--is-ancestor", commit, head)
    except Exception:  # noqa: BLE001 - une provenance illisible n'est pas une preuve
        return None
    return done.returncode == 0


def _card_before_base(checkout: str, commit: str, base: str):
    """La carte a-t-elle été mesurée avant la base du paquet (True/False/None) ?"""
    if not checkout or not commit or not base:
        return None
    if same_head(commit, base):
        return False
    if not plan_git.local_commit(checkout, commit) \
            or not plan_git.local_commit(checkout, base):
        return None
    try:
        if plan_git._git(checkout, "merge-base", "--is-ancestor",
                         commit, base).returncode == 0:
            return True
        if plan_git._git(checkout, "merge-base", "--is-ancestor",
                         base, commit).returncode == 0:
            return False
    except Exception:  # noqa: BLE001 - une provenance illisible n'est pas une preuve
        return None
    return None


def note_of(attribution) -> str:
    """La provenance courte du relevé (manifeste, carte), pour le courrier."""
    attribution = attribution or {}
    manifest = attribution.get("manifest") or {}
    card = attribution.get("card") or {}
    parts = []
    if manifest.get("units"):
        parts.append("manifeste %d unité(s)" % int(manifest["units"]))
    if card.get("commit"):
        age = card.get("age_s")
        when = ""
        if age is not None:
            when = " (%dh)" % max(1, int(age) // 3600) if age >= 3600 \
                else " (%d min)" % max(1, int(age) // 60)
        suffix = ""
        if card.get("status") == "périmée":
            suffix += ", périmée"
        if card.get("ancestor") is False:
            suffix += ", non ancêtre de la tête"
        if card.get("older_than_base"):
            #: #3954 : la carte précède la base du paquet ; ses liens test →
            #: fichier peuvent dater d'avant les changements déjà dans la
            #: base. Le verdict le dit avant de nommer une unité, pour qu'une
            #: attribution de h-37 (#3923/#3885/#3920 pour un rouge de #3925)
            #: ne se lise pas comme une preuve.
            suffix += (", antérieure à la base du paquet %s — unités indicatives"
                       % (_text(card.get("base"))[:12] or "?"))
        parts.append("carte d'impact %s%s%s"
                     % (card["commit"][:12], when, suffix))
    if not parts:
        why = _text(attribution.get("why"))
        if why:
            return ("Unités probables : non attribuable (%s). Aucun retrait "
                    "automatique." % why)
        return ""
    if not card.get("commit"):
        parts.append("carte d'impact absente : seules les preuves directes sont nommées")
    return ("Paquet : %s. Unités probables nommées seulement, aucun retrait "
            "automatique." % " ; ".join(parts))


def checkout_for(db, item, cfg=None) -> str:
    """Un clone local du dépôt du lot (`radar.projects`, sinon le cwd)."""
    from . import migrations
    try:
        slug = work.lot_repository(item, cfg)
    except Exception:  # noqa: BLE001 - sans dépôt, l'appelant dégrade
        return ""
    candidates = []
    try:
        found = migrations.checkout_for(slug, cfg)
        if found:
            candidates.append(found)
    except Exception:  # noqa: BLE001 - une configuration illisible ne conclut pas
        pass
    candidates.append(os.getcwd())
    for where in candidates:
        try:
            return work._checkout(slug, where, cfg)
        except Exception:  # noqa: BLE001 - un clone illisible n'est pas une preuve
            continue
    return ""


def _attribute_run(db, item, run, failures, *, cfg=None, api=None, checkout: str = "",
                   card=None, card_prov=None, fetch=None, card_known: bool = False,
                   shared: dict | None = None) -> dict:
    """Le relevé complet d'un run rouge de paquet, ou un statut qui dit pourquoi.

    Ne lève jamais : une lecture impossible est un statut (`no-manifest`,
    `no-files`, `no-card`), pour que le courrier et `ci status` restent
    utilisables. Aucun retrait, aucune écriture.
    """
    shared = shared if shared is not None else {}
    out = {"schema": SCHEMA, "package": int((item or {}).get("id") or 0),
           "branch": _text((item or {}).get("branch")),
           "head": _text((run or {}).get("head_sha")), "status": "ok",
           "manifest": {}, "card": card_prov or {}, "tests": [],
           "unattributable": 0, "why": ""}
    if not is_carrier(item):
        out["status"] = "not-carrier"
        return out
    if not failures:
        out["status"] = "no-failure"
        return out
    head = out["head"]
    if not head:
        out["status"] = "no-head"
        out["why"] = "tête du run absente"
        return out
    try:
        slug = work.lot_repository(item, cfg)
    except Exception:  # noqa: BLE001 - le manifeste local peut rester lisible
        slug = ""
    path = checkout or _text(shared.get("checkout")) or checkout_for(db, item, cfg)
    shared["checkout"] = path
    if card is None and not card_known and shared.get("card") is not None:
        card, card_prov, card_known = shared["card"], shared.get("prov"), True
    elif card is None and not card_known and shared.get("card_tried"):
        card_prov, card_known = shared.get("prov"), True
    comp = composition(db, item, head, checkout=path, api=api, repo=slug)
    out["manifest"] = {"source": comp["source"], "path": comp["path"],
                       "units": len(comp["members"])}
    if not comp["members"]:
        out["status"] = "no-manifest"
        out["why"] = comp["why"]
        return out
    units = unit_files(comp["members"], checkout=path, api=api, repo=slug)
    out["manifest"]["files_from"] = sorted({unit["files_from"] for unit in units
                                            if unit["files_from"]})
    if not any(unit.get("files") for unit in units):
        out["status"] = "no-files"
        out["why"] = ("fichiers des unités illisibles : ni clone local à la tête, "
                      "ni détail de commit GitHub")
        out["tests"] = attribute_tests(failures, units, card_index(card),
                                       sources=lambda module: "")
        out["unattributable"] = len(out["tests"])
        return out
    if card is None and not card_known:
        card, card_prov = carte_for(cfg, slug, repo=path, fetch=fetch)
    shared["card"], shared["prov"] = card, card_prov
    shared["card_tried"] = True
    out["card"] = dict(card_prov or {})
    if card is not None and not out["card"].get("commit"):
        out["card"]["commit"] = _text(card.get("commit"))
    if card is not None and out["card"].get("commit"):
        out["card"]["ancestor"] = _card_ancestor(
            path, head, _text(out["card"].get("commit")))
        base = _text(comp.get("base"))
        if base:
            out["card"]["base"] = base
            out["card"]["older_than_base"] = _card_before_base(
                path, _text(out["card"]["commit"]), base)
    index = card_index(card)
    texts: dict[str, str] = {}

    def source(module: str) -> str:
        if module not in texts:
            texts[module] = _source_text(path, head, module, api=api, repo=slug)
        return texts[module]

    out["tests"] = attribute_tests(failures, units, index, sources=source)
    out["unattributable"] = sum(1 for entry in out["tests"]
                                if entry["status"] != "probable")
    if card is None:
        out["status"] = "no-card"
        out["why"] = (out["card"] or {}).get("why") or "carte d'impact absente"
    return out


def attribute_run(db, item, run, failures, *, cfg=None, api=None, checkout: str = "",
                  card=None, card_prov=None, fetch=None, card_known: bool = False,
                  shared: dict | None = None) -> dict:
    """Le relevé, jamais propagé : une lecture adverse rend un statut `error`.

    `ci status` et le courrier ne doivent jamais tomber sur une carte, un
    manifeste ou un import illisible ; le détail est dans `why`, et aucun lot
    n'est retiré ni désigné par cette erreur.
    """
    try:
        return _attribute_run(
            db, item, run, failures, cfg=cfg, api=api, checkout=checkout,
            card=card, card_prov=card_prov, fetch=fetch, card_known=card_known,
            shared=shared)
    except Exception as exc:  # noqa: BLE001 - le courrier et le statut restent dus
        return {"schema": SCHEMA, "package": int((item or {}).get("id") or 0),
                "branch": _text((item or {}).get("branch")),
                "head": _text((run or {}).get("head_sha")), "status": "error",
                "manifest": {}, "card": {}, "tests": [], "unattributable": 0,
                "why": "relevé impossible : %s: %s" % (type(exc).__name__,
                                                       str(exc)[:200])}


def tests_by_name(attribution) -> dict:
    """{nom du test: entrée} pour les textes de CI."""
    return {entry["test"]: entry for entry in (attribution or {}).get("tests") or ()
            if entry.get("test")}
