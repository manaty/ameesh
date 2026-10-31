# SPDX-License-Identifier: AGPL-3.0-only
"""Le banc de rejeu comparatif (#3264 B, L15).

  ameesh replay bench --project P [--json]
        les trois lots du banc (léger, normal, sensible), leur classe, leur
        base gelée et leurs tests ; lecture seule ;
  ameesh replay run --project P --new-binary PATH [options]
        rejoue chaque lot du banc deux fois — version installée, version
        évaluée — à modèle, effort et plafond de tour égaux, puis mesure le
        coût, la durée, les tests du vrai lot et une relecture croisée à
        l'aveugle ; écrit un rapport JSON et son tableau ;
  ameesh replay report FILE [--json]
        relit un rapport et rend le tableau et la conclusion.

Le banc suit la spécification `docs/design/3264-veille-versions.md` §3 :

* **sélection** : les lots livrés du projet, du plus récent au plus ancien,
  un par classe (léger, normal, sensible) ; une classe absente est comblée par
  le lot livré suivant. Un lot sans base gelée prouvable, de classe release ou
  au diff douteux est écarté avec sa raison — jamais deviné.
* **base gelée** : l'intervalle relu d'un avis (`a..b`, b = tête gelée), sinon
  le commit de fusion qui a introduit la tête dans la cible
  (`merge-base(parent, tête)`), sinon la remontée par les sujets `#<lot>`, le
  parent en dernier recours. La source retenue est écrite dans le rapport.
* **isolation** : `git archive` de la base + `git init`, sans distant, sans
  historique ni branches ; l'environnement du harnais ne porte ni DSN, ni
  état, ni boîte ameesh, et le foyer dsh est une copie sans hooks. L'arbre de
  rejeu ne peut donc pas pousser, ni lire le fil, ni écrire une file.
* **mesures** : le flux du harnais donne l'usage par étape (`input`, cache,
  `output`) et le barème `cost.py` le prix ; la durée est celle du tour et le
  nombre de tours celui des invocations du harnais (étapes comptées à part).
* **qualité** : les tests du vrai lot (fichiers ajoutés ou modifiés par son
  diff, plus les modules voisins de la carte d'impact quand un plan de test
  sûr est disponible) sont injectés dans l'arbre rejoué puis exécutés ; une
  relecture croisée d'une **autre famille** note le diff de 0 à 10 et liste
  les défauts. Le relecteur ne voit que deux soumissions anonymes A et B :
  ni version, ni coût, ni durée, ni étiquette de dossier.
* **paramètre évalué** : un **harnais** (deux binaires, même modèle, même
  effort, même plafond) ou un **modèle** (même binaire, deux identifiants de
  modèle, même effort, même plafond). Le rapport nomme séparément le
  paramètre évalué (`evaluated`) et ceux qui restent constants (`constants`),
  pour qu'un lecteur ne suppose jamais « même modèle » quand c'est le modèle
  qui change.
* **conclusion** : meilleure (aucune mesure moins bonne, au moins une
  meilleure), équivalente (écarts dans le bruit : ±10 % coût et durée, ±1
  point de note) ou régression ; un rejeu qui échoue aux tests du vrai lot
  n'est jamais une adoption.
* **travail et reprise** : chaque évaluation crée son propre sous-dossier sous
  `--work-root` et n'efface que ce qu'elle a créé ; `--resume` compare
  l'identité complète du plan (binaires, modèles, effort, banc, relecteur,
  plafonds) et refuse un rapport produit dans d'autres conditions plutôt que
  de relabelliser ses mesures.

Le banc n'écrit aucune note ni aucun jalon : le rapport est un fichier, et
l'agent d'évaluation le cite dans la note du lot d'évaluation. Aucun nom de
client ni de projet n'est codé : le projet, les binaires et le plan de test
sont des paramètres.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from . import adapters
from . import config as config_mod
from . import cost as cost_mod
from . import db as db_mod
from . import harness_versions
from . import lot_class
from . import plan_git
from . import work
from .config import Config

#: schéma du rapport de rejeu (stable, consommé par les rapports et les tests)
REPORT_SCHEMA = "ameesh-replay-report/1"
#: schéma de l'identité du plan : ce qui doit être identique pour qu'un
#: rapport puisse être repris (`--resume`) sans relabelliser d'anciennes
#: mesures (relecture B/P2 de #4292).
PLAN_SCHEMA = "ameesh-replay-plan/1"
#: taille du banc et classes admises (spec §3.1)
BENCH_SIZE = 3
BENCH_CLASSES = ("light", "normal", "sensitive")
#: plafonds d'une évaluation (spec §3.3) : 3 lots × 2 versions, 6 relectures
MAX_RUNS = BENCH_SIZE * 2
MAX_REVIEWS = 6
#: nombre maximal de lots candidats relus pour trouver le banc
CANDIDATE_LIMIT = 300
#: remontée maximale de l'historique pour trouver la base d'un lot
BASE_WALK_MAX = 200
#: bruit admis par la conclusion (spec §3.4)
NOISE_COST = 0.10
NOISE_DURATION = 0.10
NOISE_SCORE = 1.0
#: durée par défaut d'un tour de rejeu (secondes) et plafonds des tests et de
#: la relecture. Ce sont des paramètres compilés (canon §7), surchargeables
#: par la ligne de commande.
TURN_CAP_S = 1800.0
TEST_CAP_S = 1800.0
REVIEW_CAP_S = 900.0
#: plafond de dépense d'une évaluation (USD) quand rien n'est déclaré
DEFAULT_BUDGET_USD = 25.0
#: bornes de taille des textes joints au rapport et au relecteur
PROMPT_BODY_MAX_CHARS = 40000
PROMPT_DIFF_MAX_CHARS = 120000
OUTPUT_TAIL_CHARS = 4000
#: bornes de temps des lectures git et des sondes de version
GIT_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 20.0
#: fichier d'environnement de l'exécuteur, pour la clé d'API du harnais
ENV_FILE = "~/.config/ameesh/env"
#: variables d'identité ou d'état d'ameesh retirées de l'environnement du
#: harnais rejoué : aucun accès à la base, à la boîte, ni au registre. Les
#: variables `DSH_*` de la session appelante partent aussi : un rejeu ne
#: reprend jamais la session du harnais qui le lance (`DSH_SESSION_ID`).
STRIPPED_PREFIXES = ("AMEESH_", "AGENT_MAIL_", "AGENT_MESH_", "DSH_")
#: variables de clé d'API par harnais (les autres passent par le descripteur)
#: seul le harnais payé au token exige une clé d'API : Claude et Codex lisent
#: leurs identifiants dans leur dossier de configuration (OIDC/plan).
KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY"}

_RANGE_RE = re.compile(r"\b([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})\b")
#: libellé français d'une conclusion (le JSON garde les identifiants anglais)
CONCLUSION_LABELS = {"better": "meilleure", "equal": "équivalente",
                     "regression": "régression", "incomplete": "non concluante"}
_TEST_RE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")


class ReplayError(RuntimeError):
    """Un rejeu qui ne peut pas être lancé, avec un message actionnable."""


# --------------------------------------------------------------------------
# petites lectures
# --------------------------------------------------------------------------

def _iso(ts: float | None = None) -> str:
    moment = time.time() if ts is None else float(ts)
    return datetime.datetime.fromtimestamp(
        moment, datetime.timezone.utc).isoformat(timespec="seconds")


def _expand(path: str | None) -> str:
    return os.path.abspath(os.path.expanduser(str(path or "")))


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n… (tronqué)"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _git(repo: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> str:
    """`git -C repo …` : la sortie, ou une ReplayError qui dit la première
    ligne d'erreur. Les lectures sont bornées et ne prennent aucun verrou."""
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True,
        timeout=timeout,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat",
                 LC_ALL="C", GIT_TERMINAL_PROMPT="0"))
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise ReplayError("git %s: %s" % (" ".join(args),
                                          detail[0] if detail else "échec"))
    return proc.stdout


def _git_ok(repo: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """Comme `_git`, mais rend `(code, sortie, erreur)` sans lever."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True,
            timeout=timeout,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat",
                     LC_ALL="C", GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", type(exc).__name__
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _env_file(path: str | None = None) -> dict:
    """Les `KEY=VALUE` d'un `EnvironmentFile` systemd, absente : vide.

    Même lecture que `role_timers._env_file` : commentaires et lignes vides
    ignorés, une paire de guillemets retirée. Les valeurs ne sont jamais
    affichées ni journalisées."""
    values: dict = {}
    try:
        with open(_expand(path or ENV_FILE), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _safe_rel(path: str) -> str:
    """Un chemin de dépôt sûr : relatif, sans `..` ni composant vide."""
    text = str(path or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or text.startswith("-"):
        raise ReplayError("chemin de dépôt refusé: %r" % (path,))
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReplayError("chemin de dépôt refusé: %r" % (path,))
    return text


def is_test_path(path: str) -> bool:
    """Le chemin appartient-il aux tests du lot (à injecter) ?

    Convention du dépôt : `tests/**` (Python), `*.test.*`/`*.spec.*` (JS/TS),
    `e2e/**`. Un fichier de données sous `tests/` est injecté aussi : les
    tests du vrai lot peuvent en dépendre."""
    rel = str(path or "")
    if rel.startswith("tests/") or "/tests/" in rel:
        return True
    if rel.startswith("e2e/") or "/e2e/" in rel:
        return True
    return bool(_TEST_RE.search(rel.rsplit("/", 1)[-1]))


def python_module(path: str) -> str | None:
    """`tests/test_x.py` → `test_x` (nom de module unittest), sinon None."""
    rel = str(path or "")
    if not rel.startswith("tests/") or not rel.endswith(".py"):
        return None
    base = rel.rsplit("/", 1)[-1]
    if not (base.startswith("test_") or base.endswith("_test.py")):
        return None
    name = rel[len("tests/"):-len(".py")].replace("/", ".")
    return name if re.fullmatch(r"[A-Za-z0-9_.]+", name) else None


# --------------------------------------------------------------------------
# sélection du banc
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class BenchLot:
    """Un lot du banc : sa base gelée, ses tests et sa classe."""

    lot: int
    title: str
    klass: str
    app: str
    repo: str
    target: str
    base: str
    frozen: str
    body: str
    files: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    base_source: str = ""
    merged_ts: float = 0.0

    def as_dict(self) -> dict:
        return {"lot": self.lot, "title": self.title, "class": self.klass,
                "app": self.app, "repo": self.repo, "target": self.target,
                "base": self.base, "frozen": self.frozen, "base_source": self.base_source,
                "files": list(self.files), "tests": list(self.tests)}


def _merged_ts(marks) -> float:
    values = [float(m.get("at_ts") or 0.0) for m in marks or ()
              if m.get("kind") == "merged" and not m.get("annulled")]
    return max(values) if values else 0.0


def _freeze_sha(marks) -> str:
    freeze = plan_git.latest_freeze(list(marks or ()))
    return str((freeze or {}).get("sha") or "").strip()


def _target_name(cfg, row: dict, repo: str) -> str:
    try:
        target = lot_class.default_base(cfg, row, repo)
    except Exception:  # noqa: BLE001 — un canon illisible ne devine pas une cible
        target = None
    return str(target or "main")


def _ancestor(repo: str, left: str, right: str) -> bool:
    code, _out, _err = _git_ok(repo, "merge-base", "--is-ancestor", left, right)
    return code == 0


def base_of(repo: str, lot: int, frozen: str, marks, *,
            target: str = "main") -> tuple[str, str]:
    """La base gelée d'un lot et la source de la lecture.

    Ordre des preuves, de la plus forte à la plus faible :

    1. le commit de fusion qui a introduit la tête dans la cible :
       `merge-base(parent du merge, tête)` — c'est le point de fourche réel de
       la branche, et il ne contient ni la solution ni les lots suivants ;
    2. un intervalle `a..b` écrit dans un avis, avec `b` = tête gelée. Il sert
       de repli quand la tête a été rebasée ou fusionnée sans commit de
       fusion ; une plage de delta (dernier correctif seul) est alors un
       risque, que la source `reviewed-range` dit dans le rapport ;
    3. la remontée des sujets `#<lot>` sur les premiers parents ;
    4. le parent direct de la tête (lot d'un seul commit).

    La plage relue passe après la fusion parce qu'un avis de correction cite
    souvent `dernier correctif..tête`, pas `base..tête` : la prendre d'abord
    rejouerait le lot à moitié résolu (#4171). Aucune preuve : ReplayError ;
    le lot est écarté du banc, jamais deviné."""
    head = str(frozen or "").strip().lower()
    if not plan_git.commit_id(head):
        raise ReplayError("gel absent ou non hexadécimal: %r" % (frozen,))
    code, out, _err = _git_ok(repo, "rev-parse", "--verify", "--quiet",
                              "%s^{commit}" % head)
    if code != 0 or not out.strip():
        raise ReplayError("le gel %s n'est pas un commit du dépôt" % head[:12])
    head = out.strip()

    # 1. commit de fusion qui a introduit la tête dans la cible. La cible
    # publiée d'abord : dans un clone partagé, `main` local n'avance pas et
    # son merge-base ramènerait la base à un ancêtre arbitraire (#2955).
    candidates: list[str] = []
    for name in ("origin/%s" % target if target else "", target,
                 "origin/main", "main"):
        if name and name not in candidates:
            candidates.append(name)
    resolved: list[tuple[str, str]] = []
    for candidate in candidates:
        code, tip, _err = _git_ok(repo, "rev-parse", "--verify", "--quiet",
                                  "%s^{commit}" % candidate)
        if code == 0 and tip.strip():
            resolved.append((candidate, tip.strip()))
    containing = [(name, tip) for name, tip in resolved if _ancestor(repo, head, tip)]
    for _name, tip in containing:
        code, listed, _err = _git_ok(repo, "rev-list", "--ancestry-path", "--merges",
                                     "%s..%s" % (head, tip))
        merges = [line.strip() for line in listed.splitlines() if line.strip()]
        if merges:
            # `rev-list` rend du plus récent au plus ancien : la fusion la
            # plus proche de la tête est la dernière
            merge = merges[-1]
            code, parents, _err = _git_ok(repo, "rev-list", "--parents", "-n", "1", merge)
            fields = parents.split()
            if code == 0 and len(fields) >= 2:
                code, merged, _err = _git_ok(repo, "merge-base", fields[1], head)
                if code == 0 and merged.strip() and merged.strip() != head:
                    return merged.strip(), "merge-introduction"
    if not containing:
        # pas encore fusionné : la base est l'ancêtre commun, s'il est
        # distinct de la tête (sinon la tête est la cible elle-même)
        for _name, tip in resolved:
            code, merged, _err = _git_ok(repo, "merge-base", tip, head)
            if code == 0 and merged.strip() and merged.strip() != head:
                return merged.strip(), "merge-base"

    # 2. intervalle relu, de la source la plus récente à la plus ancienne
    for mark in sorted(marks or (), key=lambda m: (float(m.get("at_ts") or 0.0),
                                                    int(m.get("id") or 0)),
                       reverse=True):
        if mark.get("kind") not in ("verdict", "frozen"):
            continue
        for before, after in _RANGE_RE.findall(mark.get("note") or ""):
            if after != head and not head.startswith(after):
                continue
            # les notes citent souvent une plage abrégée (12 hex) : la base
            # n'est rendue qu'une fois résolue en commit complet et unique
            code, resolved_range, _err = _git_ok(repo, "rev-parse", "--verify",
                                                 "--quiet", "%s^{commit}" % before)
            if code == 0 and resolved_range.strip():
                return resolved_range.strip(), "reviewed-range"

    # 3. sujets `#<lot>` : le premier commit qui ne porte plus le lot est la base
    current = head
    for _step in range(BASE_WALK_MAX):
        code, subject, _err = _git_ok(repo, "log", "-1", "--format=%s", current)
        if code != 0:
            break
        code, parent_line, _err = _git_ok(repo, "rev-list", "--parents", "-n", "1",
                                          current)
        fields = parent_line.split()
        parents = fields[1:]
        if re.search(r"#%d\b" % int(lot), subject or ""):
            if not parents:
                break
            current = parents[0]
            continue
        if len(parents) > 1 and (subject or "").startswith("Merge "):
            # fusion de la cible dans la branche du lot : la base est la cible
            # fusionnée (premier parent), on continue la remontée
            current = parents[0]
            continue
        if current != head:
            return current, "subject-walk"
        break

    # 4. parent direct
    code, parent_line, _err = _git_ok(repo, "rev-list", "--parents", "-n", "1", head)
    fields = parent_line.split()
    if code == 0 and len(fields) == 2:
        return fields[1], "parent"
    raise ReplayError("base du lot #%s introuvable depuis le gel %s"
                      % (lot, head[:12]))


def build_lot(db, cfg: Config, row: dict, *, repo: str | None = None,
              marks: list | None = None) -> BenchLot:
    """Le lot du banc, ou ReplayError avec la raison de son écarte."""
    lot = int(row["id"])
    marks = work.milestones(db, lot, limit=None) if marks is None else marks
    frozen_raw = _freeze_sha(marks)
    if not frozen_raw:
        raise ReplayError("pas de gel")
    if not repo:
        try:
            repo = lot_class.repository(cfg, row)
        except Exception:  # noqa: BLE001 — dépôt inconnu : lot écarté
            repo = None
    if not repo or not os.path.isdir(repo):
        raise ReplayError("dépôt local inconnu")
    target = _target_name(cfg, row, repo)
    base, source = base_of(repo, lot, frozen_raw, marks, target=target)
    try:
        info = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=repo, base=target,
                                head=frozen_raw, milestones=marks, base_sha=base)
    except Exception as exc:  # noqa: BLE001 — une classe illisible écarte le lot
        raise ReplayError("classe illisible: %s" % exc) from None
    klass = str(info.get("class") or "")
    if klass not in BENCH_CLASSES:
        raise ReplayError("classe %s hors banc" % (klass or "?"))
    if info.get("doubt"):
        raise ReplayError("classe douteuse: %s" % (info.get("why") or "?"))
    files: list[str] = []
    for entry in info.get("files") or ():
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            files.append(str(path))
    files_tuple = tuple(sorted(set(files)))
    tests = tuple(path for path in files_tuple if is_test_path(path))
    return BenchLot(
        lot=lot, title=str(row.get("title") or ""), klass=klass,
        app=str(row.get("app") or ""), repo=_expand(repo), target=target,
        base=base, frozen=str(info.get("frozen_sha") or frozen_raw), body=str(row.get("body") or ""),
        files=files_tuple, tests=tests, base_source=source,
        merged_ts=_merged_ts(marks))


def pick_bench(candidates: list[BenchLot], limit: int = BENCH_SIZE) -> list[BenchLot]:
    """Un lot par classe, du plus récent au plus ancien ; les classes absentes
    sont comblées par les lots suivants (spec §3.1)."""
    chosen: list[BenchLot] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.klass in seen:
            continue
        chosen.append(candidate)
        seen.add(candidate.klass)
        if len(chosen) >= limit:
            return chosen
    for candidate in candidates:
        if candidate in chosen:
            continue
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def _project_repo(cfg: Config, project: str) -> str:
    """Le clone de code déclaré du projet (`radar.projects`), ou ''.

    Le banc ne rejoue que les lots du dépôt du projet : les lots du canon
    (`app` = projet mais PR dans le dépôt du canon) ne sont pas du code et
    n'ont pas la même base ni les mêmes tests."""
    if not project:
        return ""
    try:
        from . import radar
        settings = radar.settings_of(cfg).projects.get(project)
    except Exception:  # noqa: BLE001 — réglages illisibles : pas de filtre
        return ""
    path = str(getattr(settings, "repo", "") or "")
    return _expand(path) if path else ""


def discover(db, cfg: Config, project: str, *, limit: int = CANDIDATE_LIMIT,
             repo: str | None = None) -> tuple[list[BenchLot], list[str]]:
    """Les trois lots du banc et les raisons des lots écartés.

    Les lots sont lus dans l'état `merged` (le livré d'ameesh), triés par
    jalon de fusion, puis filtrés par projet, dépôt de code, gel et classe."""
    declared = _expand(repo) if repo else _project_repo(cfg, project)
    # Le tri par jalon de fusion, pas par `updated_at` : un vieux lot dont un
    # avis a été ajouté hier ne doit pas prendre la place d'un lot livré avant
    # lui. Une seule lecture, bornée, sans verrou.
    rows = db.query(
        "SELECT w.id, w.title, w.body, w.branch, w.branch_target, w.type,"
        "       w.issue_ref, w.app, w.state,"
        "       (SELECT max(m.at) FROM work_item_milestones m"
        "         WHERE m.work_item_id = w.id AND m.kind = 'merged') AS merged_at"
        "  FROM work_items w"
        f" WHERE coalesce(to_jsonb(w)->>'legacy_state', w.state) = 'merged' AND w.app = %s"
        " ORDER BY merged_at DESC NULLS LAST, w.id DESC LIMIT %s",
        (project, int(limit)))
    candidates: list[BenchLot] = []
    rejected: list[str] = []
    for row in rows:
        if row.get("merged_at") is None:
            continue
        branch = str(row.get("branch") or "")
        if branch.startswith("integration/") or branch.startswith("canon/"):
            rejected.append("#%s: %s (hors banc des lots)"
                            % (row["id"], "paquet" if branch.startswith("integration/")
                               else "canon"))
            continue
        try:
            lot_repo = lot_class.repository(cfg, row)
        except Exception:  # noqa: BLE001 — dépôt inconnu : lot écarté
            lot_repo = None
        if declared and lot_repo \
                and os.path.realpath(lot_repo) != os.path.realpath(declared):
            rejected.append("#%s: dépôt %s hors du dépôt du projet"
                            % (row["id"], lot_repo))
            continue
        if declared and not lot_repo:
            rejected.append("#%s: dépôt du lot inconnu" % row["id"])
            continue
        marks = work.milestones(db, int(row["id"]), limit=None)
        try:
            candidates.append(build_lot(db, cfg, row, repo=lot_repo or repo,
                                        marks=marks))
        except ReplayError as exc:
            rejected.append("#%s: %s" % (row["id"], exc))
            continue
        # Trois classes distinctes : le banc est trouvé, inutile de classer
        # les lots plus anciens. Une classe absente fait lire la suite.
        if len({candidate.klass for candidate in candidates}) >= BENCH_SIZE:
            break
    return pick_bench(candidates), rejected


# --------------------------------------------------------------------------
# arbre isolé, brief, environnement
# --------------------------------------------------------------------------

def strip_hook_inserts(text: str) -> str:
    """Retire d'un profil dsh les blocs `- insert:` qui posent des hooks.

    Le profil `agent` de dsh charge `agent-mail` par ce bloc ; un rejeu ne doit
    ni lire ni écrire la boîte. Un bloc qui ne parle pas de hook est gardé
    (le modèle par défaut, par exemple)."""
    lines = (text or "").splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    kept: list[str] = []
    for block in blocks:
        body = "\n".join(block).lower()
        if "hook" in body or "agent-mail" in body:
            continue
        kept.extend(block)
    return "\n".join(kept).rstrip() + "\n"


def isolated_dsh_home(source: str, destination: str) -> dict:
    """Une copie du foyer dsh sans hooks ni sessions, pour un rejeu.

    Les identifiants (clé d'API) ne sont pas dans le foyer : ils viennent de
    l'environnement. Le foyer copié porte le profil `agent` et ses caches de
    modèle, jamais les sessions réelles."""
    source = _expand(source)
    if not os.path.isdir(source):
        raise ReplayError("foyer dsh introuvable: %s" % source)
    os.makedirs(destination, exist_ok=True)
    for name in ("profiles", "llm-deepseek"):
        origin = os.path.join(source, name)
        if os.path.isdir(origin):
            shutil.copytree(origin, os.path.join(destination, name), dirs_exist_ok=True)
    anon = os.path.join(source, ".anonymous-user-id")
    if os.path.isfile(anon):
        shutil.copy2(anon, os.path.join(destination, ".anonymous-user-id"))
    profile = os.path.join(destination, "profiles", "agent", "cordis.yml")
    if not os.path.isfile(profile):
        raise ReplayError("profil dsh `agent` absent de %s" % source)
    with open(profile, encoding="utf-8") as fh:
        text = fh.read()
    cleaned = strip_hook_inserts(text)
    if cleaned == text and "hook" in text.lower():
        raise ReplayError("profil dsh: bloc de hooks non retiré (%s)" % profile)
    with open(profile, "w", encoding="utf-8") as fh:
        fh.write(cleaned)
    with open(os.path.join(destination, "hooks.json"), "w", encoding="utf-8") as fh:
        fh.write('{"hooks": {}}\n')
    return {"source": source, "destination": destination,
            "hooks": "stripped", "sessions": "excluded"}


def materialize(repo: str, base: str, destination: str) -> dict:
    """L'arbre d'un rejeu : `git archive base` + `git init`, sans distant.

    Aucun objet de la solution n'entre dans l'arbre : l'archive ne porte que
    le contenu de la base, et le dépôt créé n'a ni branches ni historique
    au-delà de son commit de base."""
    destination = _expand(destination)
    if os.path.isdir(destination) and os.listdir(destination):
        raise ReplayError("arbre de rejeu déjà peuplé: %s" % destination)
    os.makedirs(destination, exist_ok=True)
    proc = subprocess.Popen(
        ["git", "-C", repo, "archive", "--format=tar", base],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            try:
                archive.extractall(destination, filter="data")  # Python 3.12+
            except TypeError:  # pragma: no cover — Python < 3.12
                archive.extractall(destination)
    except (OSError, tarfile.TarError) as exc:
        proc.kill()
        raise ReplayError("archive de %s impossible: %s" % (base[:12], exc)) from None
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    error = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    if proc.wait() != 0:
        raise ReplayError("git archive %s: %s" % (base[:12], error or "échec"))
    if proc.stderr is not None:
        proc.stderr.close()
    _git(destination, "init", "-q")
    _git_ok(destination, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(destination, "config", "user.name", "ameesh replay")
    _git(destination, "config", "user.email", "replay@ameesh.invalid")
    _git(destination, "add", "-A")
    _git(destination, "commit", "-q", "-m", "replay: base %s" % base[:12])
    remotes = [line for line in _git(destination, "remote").splitlines() if line]
    if remotes:
        raise ReplayError("arbre de rejeu avec un distant: %s" % ", ".join(remotes))
    commits = len([line for line in _git(destination, "rev-list", "HEAD").splitlines()
                   if line])
    if commits != 1:
        raise ReplayError("arbre de rejeu avec %s commits (un seul attendu)" % commits)
    return {"tree": destination, "base": base,
            "base_commit": _git(destination, "rev-parse", "HEAD").strip(),
            "remote": None, "commits": commits,
            "heads": _git(destination, "branch", "--format=%(refname:short)").split()}


def _git_blob(repo: str, frozen: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", repo, "show", "%s:%s" % (frozen, path)],
        capture_output=True, timeout=GIT_TIMEOUT_S,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat",
                 LC_ALL="C", GIT_TERMINAL_PROMPT="0"))
    if proc.returncode != 0:
        raise ReplayError("lecture de %s@%s impossible" % (path, frozen[:12]))
    return proc.stdout


def _git_mode(repo: str, frozen: str, path: str) -> str:
    code, out, _err = _git_ok(repo, "ls-tree", frozen, "--", path)
    fields = out.split()
    return fields[0] if code == 0 and fields else "100644"


def inject_tests(repo: str, frozen: str, tree: str, paths) -> list[str]:
    """Injecte dans l'arbre rejoué les fichiers de tests du vrai lot.

    Le rejeu est jugé par les tests livrés : les fichiers du lot remplacent
    ceux que l'agent a pu écrire. Un chemin supprimé par le lot est ignoré
    (il n'existe plus dans son arbre gelé)."""
    written: list[str] = []
    for path in paths:
        rel = _safe_rel(path)
        try:
            blob = _git_blob(repo, frozen, rel)
        except ReplayError:
            continue
        target = os.path.join(tree, rel)
        if not os.path.abspath(target).startswith(os.path.abspath(tree) + os.sep):
            raise ReplayError("chemin hors de l'arbre: %r" % rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)
        mode = _git_mode(repo, frozen, rel)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)
        written.append(rel)
    return written


def replay_prompt(lot: BenchLot) -> str:
    """La consigne du rejeu : le corps d'origine et la règle d'isolement.

    Ni diff, ni fil, ni courrier, ni note de gel : le brief est reconstruit à
    partir du corps d'origine, sans aucune trace de la solution (l'ancien
    rejeu contaminé du 04/10 avait lu le diff de référence dans le worktree)."""
    body = _clip(lot.body.strip(), PROMPT_BODY_MAX_CHARS)
    return "\n".join([
        "# Rejeu d'un lot livré, sur sa base gelée", "",
        "Tu rejoues un lot déjà livré, à partir de sa base gelée, dans un dépôt "
        "isolé. Tu ne dois ni pousser, ni envoyer de courrier, ni écrire dans une "
        "file ou un tableau : ce dépôt n'a aucun distant et aucun accès au mesh. "
        "N'essaie pas de retrouver la solution d'origine ; travaille uniquement "
        "dans le dossier courant.", "",
        "## Lot #%d — %s" % (lot.lot, lot.title),
        "- projet : %s" % lot.app,
        "", "## Corps d'origine", body, "",
        "## Consigne", "",
        "Implémente ce lot dans le dépôt courant. Ajoute ou modifie les tests "
        "nécessaires et exécute-les. Tu peux committer ; les modifications non "
        "committées sont lues aussi. Termine par un résumé court de ce que tu "
        "as fait et de l'état des tests.", "",
    ])


# --------------------------------------------------------------------------
# harnais : environnement, commande, exécution
# --------------------------------------------------------------------------

def harness_env(harness: str, *, env_file: str | None = None,
                source: dict | None = None) -> dict:
    """L'environnement d'un harnais rejoué, sans identité ni état ameesh.

    La clé d'API du harnais vient de l'environnement de l'appelant, sinon du
    fichier 0600 de l'hôte (`~/.config/ameesh/env`) — jamais écrite ni
    journalisée. Aucune variable `AMEESH_*`, `AGENT_MAIL_*` ou `AGENT_MESH_*`
    ne passe : le harnais ne peut pas joindre la base, le registre ou la boîte.
    Les dossiers de configuration (identifiants du harnais) restent ceux de
    l'appelant ; le rejeu dsh les remplace par une copie isolée."""
    env = {key: value for key, value in (source or os.environ).items()
           if not key.startswith(STRIPPED_PREFIXES)}
    key = KEY_ENV.get(harness)
    if key and not env.get(key):
        value = _env_file(env_file).get(key)
        if value:
            env[key] = value
    if key and not env.get(key):
        raise ReplayError("clé du harnais %s absente: %s (fichier %s)"
                          % (harness, key, _expand(env_file or ENV_FILE)))
    env.setdefault("HOME", os.path.expanduser("~"))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["NO_COLOR"] = "1"
    return env


def isolate_env(env: dict, directory: str) -> dict:
    """Neutralise les lectures et écritures ameesh d'un harnais (boîte v0,
    configuration, état) dans un dossier jetable. La base reste injoignable."""
    conf = os.path.join(directory, "v0conf")
    state = os.path.join(directory, "v0state")
    home = os.path.join(directory, "home")
    for path in (conf, state, home):
        os.makedirs(path, exist_ok=True)
    env = dict(env)
    env["AGENT_MAIL_CONFIG"] = conf
    env["AGENT_MAIL_STATE"] = state
    env["AMEESH_CONFIG"] = os.path.join(directory, "config.json")
    env["AMEESH_STATE"] = os.path.join(directory, "state")
    env["AMEESH_DSN"] = "postgresql://invalid@127.0.0.1:1/invalid"
    os.makedirs(env["AMEESH_STATE"], exist_ok=True)
    with open(env["AMEESH_CONFIG"], "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    return env


def isolated_test_env(env: dict, directory: str) -> dict:
    """L'environnement des tests du lot rejoué, sans identité de l'appelant.

    Les tests sont du code produit par le rejeu : ils ne doivent voir ni la
    base, ni la boîte, ni l'état ameesh réels (`isolate_env`), ni le foyer
    d'un harnais, ni la clé d'un fournisseur. `HOME` est privé pour que les
    lectures par défaut ne retrouvent pas les fichiers réels (B1/P1 de
    #4292, avis cx2/cx3)."""
    out = isolate_env(env, directory)
    out["HOME"] = os.path.join(directory, "home")
    for name in list(out):
        if name.endswith("_API_KEY") or name in (
                "DSH_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            out.pop(name, None)
    return out


def probe_version(adapter) -> str | None:
    """La version du binaire d'un adaptateur (`--version`), ou None.

    Le binaire est lancé exactement comme pour un tour : interpréteur résolu
    et `PATH` du descripteur compris, sans réseau."""
    command = [adapter.binary, "--version"]
    env = dict(os.environ)
    resolution = getattr(adapter, "resolution", None)
    if resolution is not None:
        if resolution.interpreter_path:
            command = [resolution.interpreter_path, adapter.binary, "--version"]
        if resolution.path_prepend:
            env["PATH"] = os.pathsep.join(list(resolution.path_prepend)
                                          + [env.get("PATH", "")])
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first = next((line.strip() for line in (proc.stdout or proc.stderr).splitlines()
                  if line.strip()), "")
    return harness_versions.parse_version(first)


def _kill_group(proc: subprocess.Popen) -> None:
    """Tue le groupe du harnais, jamais seulement le parent."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_harness(adapter, prompt: str, *, run_dir: str, cwd: str, env: dict,
                harness: str, model: str, effort: str, timeout: float,
                prices: dict) -> dict:
    """Un tour de harnais dans l'arbre courant de `run_dir`.

    Le flux est écrit dans un journal (pas retenu en mémoire), relu ensuite
    étape par étape : usage, coût, durée, sortie finale et erreurs. Un
    dépassement du plafond tue le groupe et rend `timed_out`."""
    os.makedirs(run_dir, exist_ok=True)
    prompt_path = os.path.join(run_dir, "prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    patch = os.path.join(run_dir, "model.patch.yml")
    argv = adapter.command(prompt, model=model or None, effort=effort or None,
                           patch=patch)
    stdout_path = os.path.join(run_dir, "harness.jsonl")
    stderr_path = os.path.join(run_dir, "harness.stderr")
    started_wall = time.time()
    started = time.monotonic()
    timed_out = False
    with open(stdout_path, "w", encoding="utf-8") as stdout, \
            open(stderr_path, "w", encoding="utf-8") as stderr:
        from .platform.child_environment import harness as harness_environment
        proc = subprocess.Popen(argv, cwd=cwd, env=harness_environment(env, harness),
                                stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, text=True,
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
    duration = time.monotonic() - started
    ended_wall = time.time()
    usage = {"input": 0, "cache": 0, "output": 0}
    steps = 0
    final = ""
    error = ""
    harness_cost = None
    session = ""
    try:
        with open(stdout_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    parsed = adapter.parse(line)
                except Exception:  # noqa: BLE001 — une ligne illisible n'arrête pas
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("session"):
                    session = str(parsed["session"])
                if parsed.get("final") is not None:
                    final = str(parsed["final"])
                    if isinstance(parsed.get("cost"), (int, float)):
                        harness_cost = float(parsed["cost"])
                if parsed.get("error"):
                    error = str(parsed["error"])
                if isinstance(parsed.get("usage"), dict):
                    tokens = adapters.usage_tokens(parsed["usage"])
                    usage["input"] += tokens[0]
                    usage["cache"] += tokens[1]
                    usage["output"] += tokens[2]
                    steps += 1
    except OSError as exc:
        error = error or ("journal illisible: %s" % exc)
    stderr_tail = ""
    try:
        with open(stderr_path, encoding="utf-8", errors="replace") as fh:
            stderr_tail = fh.read()[-OUTPUT_TAIL_CHARS:]
    except OSError:
        pass
    if proc.returncode not in (0, None) and not error:
        error = "sortie %s" % proc.returncode
    triplet = (cost_mod.period_price(prices, model or None, started_wall, ended_wall)
               or cost_mod.price_of(prices, harness, model or None))
    amount = (usage["input"] * triplet[0] + usage["cache"] * triplet[1]
              + usage["output"] * triplet[2]) / 1_000_000.0
    patch_text = ""
    if os.path.exists(patch):
        with open(patch, encoding="utf-8", errors="replace") as fh:
            patch_text = fh.read()
    return {
        "duration_s": round(duration, 3), "turns": 1, "steps": steps,
        "input_tokens": usage["input"], "cache_tokens": usage["cache"],
        "output_tokens": usage["output"], "cost_usd": round(amount, 6),
        "harness_cost_usd": harness_cost, "exit": proc.returncode,
        "timed_out": timed_out, "error": error, "final": _clip(final, OUTPUT_TAIL_CHARS),
        "session": session, "stderr_tail": stderr_tail,
        "settings": {"model": model, "effort": effort,
                     "patch_sha256": _sha256_text(patch_text)} if patch_text else {},
        "log": stdout_path, "prompt": prompt_path,
    }


def measure_diff(tree: str, base: str) -> dict:
    """Le diff du rejeu depuis sa base, indexé (les commits de l'agent
    compris), avec le nombre de fichiers et de lignes."""
    _git(tree, "add", "-A")
    code, numstat, _err = _git_ok(tree, "diff", "--cached", "--numstat", base)
    files = 0
    insertions = 0
    deletions = 0
    paths: list[str] = []
    if code == 0:
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            paths.append(parts[2])
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
    code, text, _err = _git_ok(tree, "diff", "--cached", "--no-color", base)
    full = text if code == 0 else ""
    stat = ""
    code, stat_text, _err = _git_ok(tree, "diff", "--cached", "--stat", base)
    if code == 0:
        stat = stat_text.strip()
    return {"files": files, "insertions": insertions, "deletions": deletions,
            "paths": paths, "stat": _clip(stat, OUTPUT_TAIL_CHARS * 2),
            "text": _clip(full, PROMPT_DIFF_MAX_CHARS),
            "truncated": len(full) > PROMPT_DIFF_MAX_CHARS}


def _run_capture(argv, *, cwd: str, env: dict, timeout: float) -> dict:
    """Exécute une commande bornée, tue son groupe au dépassement, rend la
    sortie et la durée. Jamais d'exception de lancement non transformée."""
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except (OSError, ValueError) as exc:
        return {"exit": None, "output": "", "duration_s": 0.0, "timed_out": False,
                "error": "lancement impossible: %s" % exc}
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        output, _ = proc.communicate()
    return {"exit": proc.returncode, "output": output or "",
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": timed_out, "error": ""}


_TEST_SUMMARY_RE = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
_TEST_FAILED_RE = re.compile(r"^FAILED \(([^)]*)\)", re.MULTILINE)
_TEST_OK_RE = re.compile(r"^OK(?: \(([^)]*)\))?", re.MULTILINE)


def parse_unittest_summary(output: str) -> dict:
    """Les compteurs d'un résumé unittest (`Ran`, `OK`, `FAILED`)."""
    ran = _TEST_SUMMARY_RE.search(output or "")
    failed = _TEST_FAILED_RE.search(output or "")
    ok = _TEST_OK_RE.search(output or "")
    counts = {"ran": int(ran.group(1)) if ran else 0, "failures": 0, "errors": 0,
              "skipped": 0}
    if failed:
        for part in failed.group(1).split(","):
            key, _, value = part.strip().partition("=")
            if key in counts and value.isdigit():
                counts[key] = int(value)
    elif ok and ok.group(1):
        for part in ok.group(1).split(","):
            key, _, value = part.strip().partition("=")
            if key in counts and value.isdigit():
                counts[key] = int(value)
    return counts


def run_tests(tree: str, modules: list[str], *, files=(), command: str = "",
              timeout: float = TEST_CAP_S, env: dict | None = None) -> dict:
    """Exécute les tests du vrai lot dans l'arbre rejoué.

    `modules` : les noms de modules unittest (Python) retenus par la
    sélection. `files` : les chemins **réels** des tests injectés, Python et
    non Python (`web/a.test.ts`, `e2e/**`), tels qu'écrits dans l'arbre ; une
    commande déclarée reçoit `{modules}` et `{files}` déjà substitués. Sans
    commande, la convention Python/unittest du dépôt est utilisée
    (`tests.<module>` sur `PYTHONPATH=src`) ; un lot sans module Python mais
    avec une commande explicite et des fichiers (JS/TS) est exécuté, pas
    déclaré `no-tests`. Sans commande utilisable, le résultat reste non
    concluant (`no-tests`, `passed=None`)."""
    existing = [name for name in modules
                if os.path.isfile(os.path.join(tree, "tests",
                                               name.replace(".", "/") + ".py"))]
    present = [rel for rel in files
               if os.path.isfile(os.path.join(tree, str(rel)))]
    if not existing and not (command and present):
        return {"status": "no-tests", "passed": None, "exit": None,
                "duration_s": 0.0, "modules": [], "files": [], "counts": {},
                "output_tail": "", "command": command or "python -m unittest"}
    run_env = dict(env or os.environ)
    source = os.path.join(tree, "src")
    run_env["PYTHONPATH"] = os.pathsep.join(
        [source, tree] + ([run_env["PYTHONPATH"]] if run_env.get("PYTHONPATH") else []))
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if command:
        # #4292 B/P2 : les chemins injectés, jamais une reconstruction
        # `tests/<module>.py` qui élimine les fichiers JS/TS.
        shown = present or ["tests/%s.py" % name.replace(".", "/")
                            for name in existing]
        argv = []
        for part in shlex.split(command):
            # Un emplacement de liste fournit un argument par élément,
            # même quand un chemin contient des espaces.
            if part == "{files}":
                argv.extend(str(path) for path in shown)
            elif part == "{modules}":
                argv.extend(existing)
            else:
                argv.append(part.format(modules=" ".join(existing),
                                        files=" ".join(shown)))
    else:
        argv = [sys.executable, "-m", "unittest"] + ["tests.%s" % name
                                                    for name in existing]
    result = _run_capture(argv, cwd=tree, env=run_env, timeout=timeout)
    output = result["output"]
    counts = parse_unittest_summary(output)
    passed = None if result["timed_out"] else (result["exit"] == 0)
    return {"status": "ran", "passed": passed, "exit": result["exit"],
            "duration_s": result["duration_s"], "modules": existing,
            "files": present, "counts": counts,
            "timed_out": result["timed_out"],
            "output_tail": _clip(output[-OUTPUT_TAIL_CHARS:], OUTPUT_TAIL_CHARS),
            "command": " ".join(argv)}


# --------------------------------------------------------------------------
# relecture croisée à l'aveugle
# --------------------------------------------------------------------------

def review_prompt(lot: BenchLot, blind: str, run: dict) -> str:
    """La consigne du relecteur : la soumission anonyme `blind` seulement.

    Ni version, ni binaire, ni coût, ni durée, ni nom de dossier : le
    relecteur ne peut pas savoir laquelle des deux versions a produit ce
    diff. Le contenu du lot et les tests sont communs aux deux soumissions."""
    diff = (run.get("diff") or {}).get("text") or ""
    tests = run.get("tests") or {}
    counts = tests.get("counts") or {}
    return "\n".join([
        "# Relecture à l'aveugle d'un rejeu", "",
        "Tu relis la soumission %s d'un rejeu de lot, sans savoir quelle version "
        "du harnais l'a produite. Ne devine pas : juge le diff, les tests et la "
        "conformité au corps du lot." % blind, "",
        "## Lot rejoué", "#%d — %s" % (lot.lot, lot.title), "",
        lot.body.strip(), "",
        "## Tests du vrai lot (injectés après le rejeu)", "- commande : %s"
        % (tests.get("command") or "?"),
        "- résultat : %s ; %s test(s), %s échec(s), %s erreur(s)"
        % ("vert" if tests.get("passed") else "rouge ou non concluant",
           counts.get("ran", 0), counts.get("failures", 0), counts.get("errors", 0)),
        "", "Fin de la sortie des tests :", "```",
        _clip(tests.get("output_tail") or "(vide)", OUTPUT_TAIL_CHARS), "```",
        "", "## Diff de la soumission", "```diff", diff or "(diff vide)", "```", "",
        "## Réponse attendue", "",
        "Réponds UNIQUEMENT par un objet JSON :",
        '{"score": <entier 0-10>, "defects": [{"severity": "blocking|major|minor", '
        '"where": "fichier:ligne", "summary": "..."}], "summary": "..."}',
        "La note mesure la qualité de la solution (0 = inutilisable, 10 = "
        "livrable sans réserve). Les défauts doivent être vérifiables.",
    ])


def parse_review(text: str) -> dict:
    """La note et les défauts d'une réponse de relecteur.

    Le dernier objet JSON valide est retenu (le modèle peut entourer sa
    réponse de texte ou de clôtures Markdown). Une réponse illisible rend
    `score=None` et `error`, jamais une note inventée."""
    candidates = re.findall(r"\{.*\}", text or "", re.DOTALL)
    for chunk in reversed(candidates):
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        score = data.get("score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
        if score is not None:
            score = max(0.0, min(10.0, score))
        defects = []
        for defect in data.get("defects") or []:
            if isinstance(defect, dict):
                defects.append({"severity": str(defect.get("severity") or ""),
                                "where": str(defect.get("where") or ""),
                                "summary": str(defect.get("summary") or "")})
            elif defect:
                defects.append({"severity": "", "where": "", "summary": str(defect)})
        return {"score": score, "defects": defects,
                "summary": str(data.get("summary") or ""), "error": ""}
    return {"score": None, "defects": [], "summary": "",
            "error": "réponse de relecture illisible"}


def run_review(adapter, prompt: str, *, run_dir: str, cwd: str | None = None,
               env: dict, harness: str, model: str, effort: str, timeout: float,
               prices: dict) -> dict:
    """Lance un relecteur et rend sa note, ses défauts et son coût.

    Le relecteur est un harnais d'une autre famille ; il reçoit la soumission
    anonyme (aucune étiquette de version) et un dossier neutre."""
    os.makedirs(run_dir, exist_ok=True)
    prompt_path = os.path.join(run_dir, "prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    patch = os.path.join(run_dir, "model.patch.yml")
    argv = adapter.command(prompt, model=model or None, effort=effort or None,
                           patch=patch)
    stdout_path = os.path.join(run_dir, "review.jsonl")
    stderr_path = os.path.join(run_dir, "review.stderr")
    started_wall = time.time()
    started = time.monotonic()
    timed_out = False
    with open(stdout_path, "w", encoding="utf-8") as stdout, \
            open(stderr_path, "w", encoding="utf-8") as stderr:
        from .platform.child_environment import harness as harness_environment
        proc = subprocess.Popen(argv, cwd=cwd or run_dir,
                                env=harness_environment(env, harness), stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, text=True,
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
    duration = time.monotonic() - started
    ended_wall = time.time()
    usage = {"input": 0, "cache": 0, "output": 0}
    final = ""
    error = ""
    try:
        with open(stdout_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    parsed = adapter.parse(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("final") is not None:
                    final = str(parsed["final"])
                if parsed.get("error"):
                    error = str(parsed["error"])
                if isinstance(parsed.get("usage"), dict):
                    tokens = adapters.usage_tokens(parsed["usage"])
                    usage["input"] += tokens[0]
                    usage["cache"] += tokens[1]
                    usage["output"] += tokens[2]
    except OSError as exc:
        error = error or ("journal illisible: %s" % exc)
    if proc.returncode not in (0, None) and not error:
        error = "sortie %s" % proc.returncode
    triplet = (cost_mod.period_price(prices, model or None, started_wall, ended_wall)
               or cost_mod.price_of(prices, harness, model or None))
    amount = (usage["input"] * triplet[0] + usage["cache"] * triplet[1]
              + usage["output"] * triplet[2]) / 1_000_000.0
    parsed_review = parse_review(final)
    if timed_out:
        parsed_review["error"] = parsed_review.get("error") or "délai dépassé"
    return {**parsed_review, "duration_s": round(duration, 3),
            "input_tokens": usage["input"], "cache_tokens": usage["cache"],
            "output_tokens": usage["output"], "cost_usd": round(amount, 6),
            "exit": proc.returncode, "timed_out": timed_out,
            "error": parsed_review.get("error") or error,
            "prompt": prompt_path, "log": stdout_path}


# --------------------------------------------------------------------------
# comparaison et conclusion
# --------------------------------------------------------------------------

def run_valid(result: dict | None) -> bool:
    """Une mesure de rejeu valide : le harnais a terminé normalement.

    Un tour sorti en erreur, interrompu ou sans code de sortie n'est pas une
    mesure comparable : il ne compte ni dans la qualité ni dans une
    conclusion (relecture B2/P2 de #4292)."""
    return (bool(result) and result.get("exit") == 0
            and not result.get("timed_out") and not result.get("error")
            and not result.get("pending"))


def review_valid(review: dict | None) -> bool:
    """Une relecture valide : note présente et relecteur terminé normalement.

    Une note lue malgré un `error`, un délai dépassé ou une sortie non nulle
    ne vaut pas qualité (relecture B2/P2 de #4292)."""
    return (bool(review) and review.get("score") is not None
            and review.get("exit") == 0 and not review.get("timed_out")
            and not review.get("error"))


def compare_runs(old: dict, new: dict) -> dict:
    """Le verdict d'un lot : `better`, `equal`, `regression` ou `failed`.

    Le bruit est celui de la spec §3.4 : ±10 % sur le coût et la durée, ±1
    point sur la note. Un test du vrai lot rouge fait toujours régresser la
    qualité ; deux rejeux rouges ne sont pas comparables (`failed`)."""
    worse: list[str] = []
    better: list[str] = []
    deltas: dict = {}

    old_cost = float(old.get("cost_usd") or 0.0)
    new_cost = float(new.get("cost_usd") or 0.0)
    if old_cost > 0:
        delta = (new_cost - old_cost) / old_cost
        deltas["cost_ratio"] = round(delta, 4)
        delta = round(delta, 9)
        if delta > NOISE_COST + 1e-9:
            worse.append("coût")
        elif delta < -NOISE_COST - 1e-9:
            better.append("coût")

    old_duration = float(old.get("duration_s") or 0.0)
    new_duration = float(new.get("duration_s") or 0.0)
    if old_duration > 0:
        delta = (new_duration - old_duration) / old_duration
        deltas["duration_ratio"] = round(delta, 4)
        delta = round(delta, 9)
        if delta > NOISE_DURATION + 1e-9:
            worse.append("durée")
        elif delta < -NOISE_DURATION - 1e-9:
            better.append("durée")

    old_tests = (old.get("tests") or {}).get("passed")
    new_tests = (new.get("tests") or {}).get("passed")
    if old_tests is not None and new_tests is not None:
        if old_tests and not new_tests:
            worse.append("tests")
        elif new_tests and not old_tests:
            better.append("tests")
        elif old_tests is False and new_tests is False:
            worse.append("tests")

    old_score = (old.get("review") or {}).get("score")
    new_score = (new.get("review") or {}).get("score")
    if old_score is not None and new_score is not None:
        delta = float(new_score) - float(old_score)
        deltas["score_delta"] = round(delta, 2)
        if delta < -NOISE_SCORE:
            worse.append("note")
        elif delta > NOISE_SCORE:
            better.append("note")

    both_failed = old_tests is False and new_tests is False
    if both_failed:
        verdict = "failed"
    elif worse:
        verdict = "regression"
    elif better:
        verdict = "better"
    else:
        verdict = "equal"
    return {"verdict": verdict, "worse": worse, "better": better, "deltas": deltas}


def conclusion(lots: list[dict]) -> str:
    """La conclusion de l'évaluation, du plus grave au plus favorable.

    Une qualité incomplète (relecture absente, tests non concluants) ne se
    compare pas : la conclusion reste `incomplete`, jamais une adoption."""
    if any(lot.get("quality") == "incomplete" for lot in lots):
        return "incomplete"
    verdicts = [lot.get("verdict") for lot in lots if lot.get("verdict")]
    if not verdicts:
        return "incomplete"
    if any(verdict in ("regression", "failed") for verdict in verdicts):
        return "regression"
    if any(verdict == "better" for verdict in verdicts):
        return "better"
    if all(verdict == "equal" for verdict in verdicts):
        return "equal"
    return "incomplete"


# --------------------------------------------------------------------------
# plan et exécution
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Plan:
    """Tout ce qui est décidé avant le premier tour : versions, banc, garde."""

    project: str
    harness: str
    model: str
    effort: str
    old_binary: str
    old_version: str
    new_binary: str
    new_version: str
    lots: list[BenchLot]
    work_root: str
    eval_id: str
    report_path: str
    #: paramètre évalué : `harness` (deux binaires, modèle constant) ou
    #: `model` (même binaire, deux identifiants de modèle). Le rapport nomme
    #: le paramètre évalué et ceux qui restent constants (spec §3.1, cx3).
    evaluated: str = "harness"
    old_model: str = ""
    new_model: str = ""
    old_adapter: object = None
    new_adapter: object = None
    reviewer: str = ""
    reviewer_model: str = ""
    reviewer_effort: str = ""
    reviewer_adapter: object = None
    reviewer_version: str = ""
    turn_cap_s: float = TURN_CAP_S
    test_cap_s: float = TEST_CAP_S
    review_cap_s: float = REVIEW_CAP_S
    test_command: str = ""
    map_path: str = ""
    dsh_home: str = dataclasses.field(
        default_factory=lambda: os.environ.get("DSH_HOME") or "~/.dsh")
    env_file: str = ENV_FILE
    prices: dict = dataclasses.field(
        default_factory=lambda: cost_mod.load_prices(os.environ.get("AMEESH_PRICES")))
    max_runs: int = MAX_RUNS
    max_reviews: int = MAX_REVIEWS
    budget_usd: float = DEFAULT_BUDGET_USD
    keep: bool = False
    #: vrai si `build_plan` a créé `work_root` lui-même : seul ce dossier,
    #: privé à l'évaluation, peut être retiré en fin d'exécution.
    created_root: bool = False
    resume: dict | None = None


def build_plan(cfg: Config, db, *, project: str, new_binary: str = "",
               old_binary: str = "", harness: str = "deepseek", model: str = "",
               effort: str = "", lots=(), reviewer: str = "",
               reviewer_model: str = "", reviewer_effort: str = "",
               turn_cap_s: float = TURN_CAP_S, test_cap_s: float = TEST_CAP_S,
               review_cap_s: float = REVIEW_CAP_S, test_command: str = "",
               map_path: str = "", work_root: str = "", report_path: str = "",
               dsh_home: str = "", env_file: str = ENV_FILE,
               max_runs: int = MAX_RUNS, max_reviews: int = MAX_REVIEWS,
               budget_usd: float = DEFAULT_BUDGET_USD, keep: bool = False,
               resume: dict | None = None, evaluated: str = "harness",
               old_model: str = "", new_model: str = "") -> Plan:
    """Résout les versions, le banc et le relecteur ; ne dépense aucun token.

    `evaluated` dit le paramètre comparé : `harness` (deux binaires, même
    modèle et même effort) ou `model` (même binaire, deux modèles, même
    effort). Les paramètres non évalués sont écrits `constants` dans le
    rapport ; aucun n'est supposé constant par le lecteur."""
    if evaluated not in ("harness", "model"):
        raise ReplayError("--evaluate doit être `harness` ou `model`")
    if max_runs > MAX_RUNS or max_reviews > MAX_REVIEWS:
        raise ReplayError("plafond d'évaluation dépassé: au plus %d rejeux et %d "
                          "relectures (spec §3.3)" % (MAX_RUNS, MAX_REVIEWS))
    if evaluated == "harness":
        if not new_binary:
            raise ReplayError("--new-binary est requis pour évaluer un harnais")
        try:
            old_adapter = adapters.adapter_for(harness, old_binary or None)
            new_adapter = adapters.adapter_for(harness, new_binary)
        except adapters.HarnessMissing as exc:
            raise ReplayError(str(exc)) from None
        old_version = probe_version(old_adapter) or "?"
        new_version = probe_version(new_adapter) or "?"
        if old_version == new_version and old_version != "?":
            raise ReplayError("les deux binaires annoncent la même version (%s): "
                              "aucune comparaison possible" % old_version)
        old_model = new_model = model
    else:
        if not old_model or not new_model:
            raise ReplayError("évaluation de modèle: --old-model et --new-model "
                              "sont requis")
        if old_model == new_model:
            raise ReplayError("évaluation de modèle: les deux modèles sont identiques "
                              "(%s)" % old_model)
        try:
            adapter = adapters.adapter_for(harness, old_binary or None)
        except adapters.HarnessMissing as exc:
            raise ReplayError(str(exc)) from None
        if new_binary and _expand(new_binary) != _expand(adapter.binary):
            raise ReplayError("évaluation de modèle: le même binaire doit servir les "
                              "deux versions (reçu %s et %s)"
                              % (adapter.binary, new_binary))
        old_adapter = new_adapter = adapter
        old_version = new_version = probe_version(adapter) or "?"
        model = ""
    if lots:
        rows = []
        for ident in lots:
            row = work.get(db, int(ident))
            if row is None:
                raise ReplayError("lot #%s inconnu" % ident)
            rows.append(row)
        bench = [build_lot(db, cfg, row) for row in rows]
        if len(bench) > BENCH_SIZE:
            raise ReplayError("le banc fait au plus %d lots" % BENCH_SIZE)
    else:
        bench, _rejected = discover(db, cfg, project)
        if len(bench) < BENCH_SIZE:
            raise ReplayError("banc incomplet: %d lot(s) prouvable(s) sur %d pour %s"
                              % (len(bench), BENCH_SIZE, project))
    reviewer_adapter = None
    reviewer_version = ""
    if reviewer:
        if reviewer == harness:
            raise ReplayError("le relecteur doit être d'une autre famille que le "
                              "harnais évalué (%s)" % harness)
        try:
            reviewer_adapter = adapters.adapter_for(reviewer)
        except adapters.HarnessMissing as exc:
            raise ReplayError(str(exc)) from None
        reviewer_version = probe_version(reviewer_adapter) or "?"
    eval_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    # `--work-root` est un parent : chaque évaluation reçoit son propre
    # sous-dossier, créé exclusivement ici. Un dossier déjà peuplé n'est donc
    # jamais adopté, et le nettoyage ne peut pas l'effacer (relecture B/P1).
    parent = _expand(work_root) if work_root else tempfile.gettempdir()
    root = os.path.join(parent, "ameesh-replay-%s" % eval_id)
    report = _expand(report_path) if report_path else os.path.join(root, "report.json")
    plan = Plan(
        project=project, harness=harness, model=model, effort=effort,
        evaluated=evaluated, old_model=old_model, new_model=new_model,
        old_binary=old_adapter.binary, old_version=old_version,
        new_binary=new_adapter.binary, new_version=new_version,
        old_adapter=old_adapter, new_adapter=new_adapter, lots=bench,
        work_root=root, eval_id=eval_id, report_path=report,
        reviewer=reviewer, reviewer_model=reviewer_model,
        reviewer_effort=reviewer_effort, reviewer_adapter=reviewer_adapter,
        reviewer_version=reviewer_version, turn_cap_s=turn_cap_s,
        test_cap_s=test_cap_s, review_cap_s=review_cap_s,
        test_command=test_command, map_path=map_path,
        dsh_home=_expand(dsh_home) if dsh_home else _expand(
            os.environ.get("DSH_HOME") or "~/.dsh"),
        env_file=env_file,
        prices=cost_mod.load_prices(os.environ.get("AMEESH_PRICES")),
        max_runs=max_runs, max_reviews=max_reviews,
        budget_usd=budget_usd, keep=keep, resume=resume)
    # Une reprise ne relabellise jamais des mesures d'un autre plan : le
    # rapport porte l'identité complète, vérifiée avant toute réutilisation
    # (relecture B/P2 de #4292). La vérification précède la création du
    # dossier privé pour ne pas en laisser un vide sur un refus.
    if resume:
        _validate_resume(plan, resume)
    try:
        os.makedirs(parent, exist_ok=True)
        os.makedirs(root)
    except FileExistsError:
        raise ReplayError("dossier de travail déjà présent: %s" % root) from None
    except OSError as exc:
        raise ReplayError("dossier de travail impossible: %s" % exc) from None
    plan.created_root = True
    return plan


def _digest_file(path: str | None) -> str | None:
    """Empreinte d'un fichier du plan ; illisible, son chemin (pour refuser)."""
    if not path:
        return None
    target = _expand(path)
    try:
        digest = hashlib.sha256()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "illisible:%s" % target


def _digest_json(value) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   default=str))


def plan_identity(plan: Plan) -> dict:
    """L'identité mesurable d'un plan, telle qu'elle est figée dans le rapport.

    Tout ce qui, changé, rendrait une ancienne mesure non comparable sous les
    nouveaux libellés : versions et binaires, modèle, effort, plafonds,
    commande de test, carte d'impact, barème, relecteur et banc (base et tête
    gelées, tests). Les gardes de dépense (`budget_usd`, `max_runs`,
    `max_reviews`) n'en font pas partie : les changer ne relabellise rien."""
    identity = {
        "schema": PLAN_SCHEMA,
        "project": plan.project,
        "harness": plan.harness,
        "evaluated": plan.evaluated,
        "model": plan.model,
        "effort": plan.effort,
        "old_binary": plan.old_binary,
        "old_version": plan.old_version,
        "old_model": plan.old_model,
        "new_binary": plan.new_binary,
        "new_version": plan.new_version,
        "new_model": plan.new_model,
        "turn_cap_s": plan.turn_cap_s,
        "test_cap_s": plan.test_cap_s,
        "review_cap_s": plan.review_cap_s,
        "test_command": plan.test_command,
        "map_sha256": _digest_file(plan.map_path),
        "prices_sha256": _digest_json(plan.prices),
        "dsh_home": plan.dsh_home if plan.harness == "deepseek" else None,
        "lots": [{"lot": lot.lot, "class": lot.klass, "app": lot.app,
                  "target": lot.target, "base": lot.base, "frozen": lot.frozen,
                  "tests": list(lot.tests), "files": list(lot.files)}
                 for lot in plan.lots],
    }
    identity["reviewer"] = (
        {"harness": plan.reviewer, "version": plan.reviewer_version,
         "model": plan.reviewer_model, "effort": plan.reviewer_effort}
        if plan.reviewer else None)
    return identity


def _identity_value(value) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "…"


def _identity_differences(previous, current, prefix: str = "") -> list[str]:
    """Les chemins qui diffèrent entre deux identités, pour un refus lisible."""
    if isinstance(previous, dict) and isinstance(current, dict):
        differences = []
        for key in sorted(set(previous) | set(current)):
            name = "%s.%s" % (prefix, key) if prefix else key
            if key not in previous:
                differences.append("%s: absent → %s"
                                   % (name, _identity_value(current[key])))
            elif key not in current:
                differences.append("%s: %s → absent"
                                   % (name, _identity_value(previous[key])))
            else:
                differences.extend(_identity_differences(
                    previous[key], current[key], name))
        return differences
    if isinstance(previous, list) and isinstance(current, list):
        differences = []
        if len(previous) != len(current):
            differences.append("%s: %d → %d élément(s)"
                               % (prefix, len(previous), len(current)))
        for index, (one, two) in enumerate(zip(previous, current)):
            differences.extend(_identity_differences(
                one, two, "%s[%d]" % (prefix, index)))
        return differences
    if previous != current:
        return ["%s: %s → %s" % (prefix, _identity_value(previous),
                                 _identity_value(current))]
    return []


def _validate_resume(plan: Plan, previous: dict | None) -> None:
    """Refuse une reprise dont le plan a changé (relecture B/P2 de #4292).

    L'identité stockée dans le rapport est comparée en entier ; un rapport
    antérieur au schéma d'identité est refusé plutôt que repris à l'aveugle."""
    stored = (previous or {}).get("plan") if isinstance(previous, dict) else None
    if not isinstance(stored, dict) or stored.get("schema") != PLAN_SCHEMA:
        raise ReplayError(
            "reprise refusée: le rapport %s n'a pas d'identité de plan (%s); "
            "rejouer sans --resume"
            % ((previous or {}).get("eval_id") if isinstance(previous, dict) else "?",
               stored.get("schema") if isinstance(stored, dict) else "absente"))
    differences = _identity_differences(stored, plan_identity(plan))
    if differences:
        shown = "; ".join(differences[:6])
        if len(differences) > 6:
            shown += "; … (%d différence(s))" % len(differences)
        raise ReplayError("reprise refusée: le plan du rapport diffère (%s); "
                          "rejouer sans --resume" % shown)


def impact_modules(repo: str, base: str, frozen: str, map_path: str,
                   *, timeout: float = 300.0) -> tuple[list[str], str]:
    """Les modules voisins d'un plan de test sûr, ou `([], raison)`.

    Le plan L161f est un outil du dépôt (`scripts/test-plan.py`). Il n'est
    utilisé que sur une sélection **impactée sûre** : un repli sur la suite
    complète, une carte absente ou un plan illisible ne rendent aucun voisin
    (le rejeu ne noie pas la mesure dans 800 modules)."""
    if not map_path:
        return [], "pas de carte d'impact déclarée"
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "..", "scripts", "test-plan.py")
    script = os.path.abspath(script)
    if not os.path.isfile(script):
        return [], "scripts/test-plan.py absent de l'installation"
    if not os.path.isfile(_expand(map_path)):
        return [], "carte d'impact illisible: %s" % map_path
    out = os.path.join(tempfile.gettempdir(), "ameesh-replay-plan-%s.json"
                       % uuid.uuid4().hex[:8])
    try:
        proc = subprocess.run(
            [sys.executable, script, "plan", "--out", out, "--map", _expand(map_path),
             "--mode", "lot", "--base", base, "--ref", frozen],
            cwd=repo, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return [], "plan de test en échec: %s" % (
                (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""])[0]
        with open(out, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [], "plan de test illisible: %s" % exc
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    selection = plan.get("selection") or {}
    if selection.get("mode") != "impacted":
        return [], "sélection non sûre: %s" % (selection.get("why") or "?")
    classes = plan.get("classes") or {}
    return sorted(name for name, value in classes.items()
                  if isinstance(value, (list, tuple)) and value
                  and value[0] == "P1"), ""


def _run_one(plan: Plan, lot: BenchLot, version: str, blind: str, run_dir: str,
             *, run_agent, test_runner) -> dict:
    """Prépare un arbre, rejoue une version, mesure le diff et lance les tests.

    En évaluation de harnais, le binaire change et le modèle est constant ; en
    évaluation de modèle, le binaire est le même et le modèle change. Le
    rapport nomme le paramètre évalué, jamais un `model` supposé constant."""
    if plan.evaluated == "model":
        adapter = plan.old_adapter
        model = plan.old_model if version == "old" else plan.new_model
        label = model
    else:
        adapter = plan.old_adapter if version == "old" else plan.new_adapter
        model = plan.model
        label = plan.old_version if version == "old" else plan.new_version
    tree = os.path.join(run_dir, "tree")
    meta = os.path.join(run_dir, "meta")
    os.makedirs(meta, exist_ok=True)
    isolation = materialize(lot.repo, lot.base, tree)
    prompt = replay_prompt(lot)
    env = harness_env(plan.harness, env_file=plan.env_file)
    isolation_dir = os.path.join(run_dir, "isolation")
    env = isolate_env(env, isolation_dir)
    # Les tests du lot tournent dans le même environnement isolé que le
    # harnais : jamais le DSN, l'état ni la boîte réels de l'appelant
    # (relecture B1/P1 de #4292). Foyers de harnais et clés des fournisseurs
    # sont retirés aussi : un test n'a aucune raison de les joindre.
    test_env = isolated_test_env(env, isolation_dir)
    env.update(adapter.env())
    use_dsh_home = os.path.join(run_dir, "dsh-home")
    home_info = None
    if plan.harness == "deepseek":
        home_info = isolated_dsh_home(plan.dsh_home, use_dsh_home)
        env["DSH_HOME"] = use_dsh_home
    result = run_agent(adapter, prompt, run_dir=meta, cwd=tree, env=env,
                       harness=plan.harness, model=model, effort=plan.effort,
                       timeout=plan.turn_cap_s, prices=plan.prices)
    result["version"] = version
    result["label"] = label
    result["model"] = model
    result["blind"] = blind
    result["tree"] = tree
    result["isolation"] = {**isolation, "dsh_home": home_info}
    result["diff"] = measure_diff(tree, isolation["base_commit"])
    modules = [name for name in (python_module(path) for path in lot.tests) if name]
    extra, why = impact_modules(lot.repo, lot.base, lot.frozen, plan.map_path)
    modules += [name for name in extra if name not in modules]
    result["neighbors"] = {"modules": extra, "detail": why}
    # Les tests du vrai lot remplacent ceux de l'agent avant l'exécution : le
    # rejeu est jugé par la référence, pas par ses propres témoins. Les
    # chemins injectés sont transmis entiers (#4292 B/P2) : un lot JS/TS n'a
    # pas de module unittest, et `{files}` doit nommer ses vrais fichiers.
    injected = inject_tests(lot.repo, lot.frozen, tree, lot.tests)
    result["tests"] = {"injected": injected}
    result["tests"].update(test_runner(tree, modules, files=injected,
                                       command=plan.test_command,
                                       timeout=plan.test_cap_s, env=test_env))
    return result


def _review_one(plan: Plan, lot: BenchLot, result: dict, blind: str,
                run_dir: str, *, review_agent) -> dict:
    """Une relecture aveugle de la soumission `blind`."""
    adapter = plan.reviewer_adapter
    env = harness_env(plan.reviewer, env_file=plan.env_file)
    isolation_dir = os.path.join(run_dir, "isolation")
    env = isolate_env(env, isolation_dir)
    env.update(adapter.env())
    home_info = None
    if plan.reviewer == "deepseek":
        # Même foyer privé que le rejeu principal : profil sans hooks,
        # sessions exclues, HOME privé. Un relecteur ne voit ni le profil
        # réel ni ses hooks (B1/P1 de #4292, avis cx2/cx3).
        review_home = os.path.join(run_dir, "dsh-home")
        home_info = isolated_dsh_home(plan.dsh_home, review_home)
        env["DSH_HOME"] = review_home
        env["HOME"] = os.path.join(isolation_dir, "home")
    prompt = review_prompt(lot, blind, result)
    review = review_agent(adapter, prompt, run_dir=run_dir, env=env,
                          harness=plan.reviewer, model=plan.reviewer_model,
                          effort=plan.reviewer_effort, timeout=plan.review_cap_s,
                          prices=plan.prices)
    review["reviewer"] = plan.reviewer
    review["reviewer_version"] = plan.reviewer_version
    review["reviewer_model"] = plan.reviewer_model
    review["reviewer_effort"] = plan.reviewer_effort
    review["isolation"] = {"dsh_home": home_info,
                           "home": ("isolated" if plan.reviewer == "deepseek"
                                    else "harness-credentials")}
    return review


def _expected_measure(plan: Plan, version: str) -> tuple[str, str]:
    """Le `(libellé, modèle)` qu'un rejeu de cette version doit porter."""
    if plan.evaluated == "model":
        model = plan.old_model if version == "old" else plan.new_model
        return model, model
    label = plan.old_version if version == "old" else plan.new_version
    return label, plan.model


def _reuse_run(report: dict | None, plan: Plan, lot: BenchLot,
               version: str) -> dict | None:
    """Un rejeu reprenable, seulement s'il porte l'identité de *ce* plan.

    Le lot et la version ne suffisent pas : un binaire, un modèle, un effort
    ou une base changés rendraient l'ancienne mesure non comparable sous les
    nouveaux libellés (relecture B/P2 de #4292). Le champ aveugle est exclu :
    il est retiré à chaque exécution."""
    label, model = _expected_measure(plan, version)
    for entry in (report or {}).get("lots") or []:
        if int(entry.get("lot") or 0) != int(lot.lot):
            continue
        run = (entry.get("runs") or {}).get(version)
        if not run_valid(run):
            continue
        if run.get("version") != version or run.get("label") != label:
            continue
        if run.get("model") != model:
            continue
        settings = run.get("settings") or {}
        if settings and (settings.get("model") != model
                         or settings.get("effort") != plan.effort):
            continue
        if (run.get("isolation") or {}).get("base") != lot.base:
            continue
        return run
    return None


def _reuse_review(report: dict | None, plan: Plan, lot: BenchLot,
                  version: str) -> dict | None:
    """Une relecture reprenable, seulement sous le même relecteur et la même
    version de relecteur ; une relecture d'une autre famille ne se relabellise
    pas."""
    for entry in (report or {}).get("lots") or []:
        if int(entry.get("lot") or 0) != int(lot.lot):
            continue
        review = (entry.get("reviews") or {}).get(version)
        if not review_valid(review):
            continue
        if review.get("reviewer") != plan.reviewer:
            continue
        if review.get("reviewer_version") != plan.reviewer_version:
            continue
        if ("reviewer_model" in review
                and review.get("reviewer_model") != plan.reviewer_model):
            continue
        if ("reviewer_effort" in review
                and review.get("reviewer_effort") != plan.reviewer_effort):
            continue
        return review
    return None


def execute(plan: Plan, *, run_agent=run_harness, test_runner=run_tests,
            review_agent=run_review, progress=None) -> dict:
    """Exécute le plan et écrit le rapport après chaque lot.

    `run_agent`, `test_runner` et `review_agent` sont injectables : les tests
    remplacent les appels au harnais sans réseau ni dépense. La garde du
    budget et des plafonds arrête proprement l'évaluation : le rapport partiel
    est écrit et dit ce qui manque."""
    say = progress or (lambda message: None)
    previous = plan.resume or None
    # Aucune reprise ne relabellise les mesures d'un autre plan : l'identité
    # complète est vérifiée avant de réutiliser quoi que ce soit (B/P2).
    if previous is not None:
        _validate_resume(plan, previous)
    # Le paramètre évalué et les paramètres tenus constants sont nommés par
    # le rapport, jamais déduits d'un `model` supposé partagé (spec §3.1,
    # point cx3) : on peut évaluer un harnais à modèle égal, ou un modèle à
    # harnais égal.
    if plan.evaluated == "model":
        old_label, new_label = plan.old_model, plan.new_model
        held_constant = ["harness", "effort", "turn_cap_s"]
    else:
        old_label, new_label = plan.old_version, plan.new_version
        held_constant = ["model", "effort", "turn_cap_s"]
    evaluated = {
        "kind": plan.evaluated,
        "old": {"label": old_label, "model": plan.old_model,
                "binary": plan.old_binary, "version": plan.old_version},
        "new": {"label": new_label, "model": plan.new_model,
                "binary": plan.new_binary, "version": plan.new_version},
    }
    report = {
        "schema": REPORT_SCHEMA, "generated": _iso(), "eval_id": plan.eval_id,
        "project": plan.project, "harness": plan.harness,
        "report_path": plan.report_path,
        "evaluated": evaluated,
        "model": plan.model or None, "effort": plan.effort,
        "turn_cap_s": plan.turn_cap_s, "test_cap_s": plan.test_cap_s,
        "review_cap_s": plan.review_cap_s, "work_root": plan.work_root,
        "versions": {"old": {"binary": plan.old_binary, "version": plan.old_version},
                     "new": {"binary": plan.new_binary, "version": plan.new_version}},
        "models": {"old": plan.old_model, "new": plan.new_model},
        "constants": {"harness": plan.harness,
                      "model": plan.model if plan.evaluated == "harness" else None,
                      "effort": plan.effort, "turn_cap_s": plan.turn_cap_s},
        "reviewer": ({"harness": plan.reviewer, "version": plan.reviewer_version,
                      "model": plan.reviewer_model, "effort": plan.reviewer_effort}
                     if plan.reviewer else None),
        # Identité figée du plan : la reprise la compare en entier avant de
        # réutiliser un rejeu ou une relecture (B/P2 de #4292).
        "plan": plan_identity(plan),
        "resume": ({"from_eval_id": previous.get("eval_id"),
                    "from_generated": previous.get("generated")}
                   if previous is not None else None),
        "lots": [], "conclusion": "incomplete",
        "controls": {
            "evaluated": plan.evaluated,
            "held_constant": held_constant,
            "effort": plan.effort or "(descripteur)",
            "turn_cap_s": plan.turn_cap_s,
            "blind_review": bool(plan.reviewer),
            "isolation": "git archive + git init, sans distant ; environ AMEESH_* "
                         "retiré, boîte et état ameesh dans un dossier jetable, "
                         "hooks dsh retirés",
        },
        "budget": {"cap_usd": plan.budget_usd, "spent_usd": 0.0,
                   "runs": 0, "reviews": 0, "max_runs": plan.max_runs,
                   "max_reviews": plan.max_reviews, "stopped": ""},
        "notes": [],
    }
    runs = 0
    reviews = 0
    spent = 0.0
    if previous is not None:
        # Reprendre une évaluation, c'est continuer la même dépense : les
        # gardes (plafonds, budget) portent sur le total et non sur la seule
        # invocation. Le rapport tient déjà ces compteurs.
        prior = previous.get("budget") or {}
        try:
            runs = int(prior.get("runs") or 0)
            reviews = int(prior.get("reviews") or 0)
            spent = float(prior.get("spent_usd") or 0.0)
        except (TypeError, ValueError):
            runs, reviews, spent = 0, 0, 0.0
    #: seuls les dossiers créés ici sont retirés : un dossier préexistant
    #: n'est jamais adopté ni effacé (relecture B/P1 de #4292).
    created: list[str] = []

    def stop(reason: str) -> None:
        report["budget"]["stopped"] = reason
        report["notes"].append(reason)

    def write() -> None:
        report["generated"] = _iso()
        report["budget"]["spent_usd"] = round(spent, 6)
        report["budget"]["runs"] = runs
        report["budget"]["reviews"] = reviews
        report["controls"]["no_remote"] = bool(runs) and all(
            ((run.get("isolation") or {}).get("remote") is None)
            for entry in report["lots"] for run in (entry.get("runs") or {}).values())
        settings = [run.get("settings") or {} for entry in report["lots"]
                    for run in (entry.get("runs") or {}).values()]
        hashes = [value.get("patch_sha256") for value in settings
                  if value.get("patch_sha256")]
        if plan.evaluated == "harness" and hashes:
            report["controls"]["same_model_effort"] = len(set(hashes)) == 1
        if plan.evaluated == "model":
            report["controls"]["same_binary"] = plan.old_binary == plan.new_binary
        report["conclusion"] = conclusion(report["lots"])
        os.makedirs(os.path.dirname(plan.report_path) or ".", exist_ok=True)
        temporary = plan.report_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=False)
            fh.write("\n")
        os.replace(temporary, plan.report_path)

    for lot in plan.lots:
        entry = {"lot": lot.lot, "title": lot.title, "class": lot.klass,
                 "base": lot.base, "frozen": lot.frozen,
                 "base_source": lot.base_source, "files": list(lot.files),
                 "tests": list(lot.tests), "runs": {}, "reviews": {},
                 "blind": {}, "verdict": "incomplete", "errors": []}
        # A/B tirés par lot : l'ordre ne révèle jamais la version, et le
        # dossier de chaque soumission porte le nom aveugle
        names = ["A", "B"]
        random.Random("%s:%s" % (plan.eval_id, lot.lot)).shuffle(names)
        entry["blind"] = {"old": names[0], "new": names[1]}
        report["lots"].append(entry)
        replayed: dict[str, dict] = {}
        for version in ("old", "new"):
            blind = entry["blind"][version]
            reused = _reuse_run(previous, plan, lot, version)
            if reused is not None:
                reused = dict(reused)
                reused["blind"] = blind
                entry["runs"][version] = reused
                replayed[version] = reused
                say("#%s %s: rejeu repris du rapport" % (lot.lot, version))
                continue
            if runs >= plan.max_runs:
                stop("plafond de rejeux atteint (%d)" % plan.max_runs)
                break
            if spent >= plan.budget_usd:
                stop("budget épuisé (%.2f $ sur %.2f $)" % (spent, plan.budget_usd))
                break
            say("#%s %s: arbre isolé et tour (%s)…" % (lot.lot, version, blind))
            run_dir = os.path.join(plan.work_root, "lot-%s-%s" % (lot.lot, blind))
            _create_eval_dir(run_dir, created)
            try:
                result = _run_one(plan, lot, version, blind, run_dir,
                                  run_agent=run_agent, test_runner=test_runner)
            except ReplayError as exc:
                result = {"version": version, "blind": blind, "error": str(exc),
                          "exit": None, "pending": True, "tests": {},
                          "diff": {}, "cost_usd": 0.0}
            entry["runs"][version] = result
            replayed[version] = result
            runs += 1
            spent += float(result.get("cost_usd") or 0.0)
            if not run_valid(result):
                entry["errors"].append(
                    "#%s %s: rejeu non concluant (%s)"
                    % (lot.lot, version,
                       result.get("error") or "sortie %s" % result.get("exit")))
            write()
        for version in ("old", "new"):
            if version not in replayed:
                continue
            if not run_valid(replayed[version]):
                # Un rejeu en erreur ne se relit pas : la relecture ne
                # fabrique pas une qualité sur une mesure absente (B2/P2).
                entry["reviews"][version] = {
                    "score": None, "defects": [], "summary": "",
                    "error": "rejeu non concluant: pas de relecture",
                    "cost_usd": 0.0, "exit": None, "timed_out": False}
                entry["errors"].append(
                    "#%s %s: relecture non lancée (rejeu non concluant)"
                    % (lot.lot, version))
                continue
            if reviews >= plan.max_reviews:
                stop("plafond de relectures atteint (%d)" % plan.max_reviews)
                break
            if spent >= plan.budget_usd:
                stop("budget épuisé (%.2f $ sur %.2f $)" % (spent, plan.budget_usd))
                break
            if not plan.reviewer_adapter:
                entry["reviews"]["missing"] = "aucun relecteur déclaré"
                break
            reused = _reuse_review(previous, plan, lot, version)
            if reused is not None:
                entry["reviews"][version] = dict(reused)
                replayed[version]["review"] = dict(reused)
                say("#%s %s: relecture reprise du rapport" % (lot.lot, version))
                continue
            blind = entry["blind"][version]
            say("#%s %s: relecture aveugle (%s)…" % (lot.lot, version, blind))
            run_dir = os.path.join(plan.work_root, "lot-%s-%s-review" % (lot.lot, blind))
            _create_eval_dir(run_dir, created)
            try:
                review = _review_one(plan, lot, replayed[version], blind, run_dir,
                                     review_agent=review_agent)
            except ReplayError as exc:
                review = {"score": None, "defects": [], "summary": "",
                          "error": str(exc), "cost_usd": 0.0}
            entry["reviews"][version] = review
            if review_valid(review):
                replayed[version]["review"] = review
            else:
                entry["errors"].append(
                    "#%s %s: relecture non concluante (%s)"
                    % (lot.lot, version,
                       review.get("error") or "sortie %s" % review.get("exit")))
                replayed[version]["review"] = {
                    "score": None, "defects": [], "summary": "",
                    "error": review.get("error") or "relecture non concluante"}
            reviews += 1
            spent += float(review.get("cost_usd") or 0.0)
            write()
        old_run = replayed.get("old")
        new_run = replayed.get("new")
        old_review = entry["reviews"].get("old")
        new_review = entry["reviews"].get("new")
        if (run_valid(old_run) and run_valid(new_run)
                and review_valid(old_review) and review_valid(new_review)):
            comparison = compare_runs(old_run, new_run)
            entry["verdict"] = comparison["verdict"]
            entry["deltas"] = comparison["deltas"]
            entry["reasons"] = {"worse": comparison["worse"],
                                "better": comparison["better"]}
        else:
            # Mesure ou relecture invalide : la conclusion du lot reste non
            # concluante, jamais « égale » par défaut (B2/P2 de #4292).
            entry["verdict"] = "incomplete"
        tests = [((entry["runs"].get(version) or {}).get("tests") or {}).get("passed")
                 for version in ("old", "new")]
        entry["quality"] = (
            "complete" if run_valid(old_run) and run_valid(new_run)
            and review_valid(old_review) and review_valid(new_review)
            and all(value is not None for value in tests) else "incomplete")
        write()
    write()
    if not plan.keep:
        _cleanup(plan, created, report, keep=(plan.report_path,))
    write()
    return report


def _create_eval_dir(path: str, created: list[str]) -> None:
    """Crée un dossier de rejeu neuf et l'inscrit au manifeste de nettoyage.

    Un dossier existant est refusé, jamais adopté : le nettoyage ne peut
    effacer que ce que cette évaluation a créé (relecture B/P1 de #4292)."""
    try:
        os.makedirs(path)
    except FileExistsError:
        raise ReplayError("dossier de rejeu déjà présent: %s" % path) from None
    except OSError as exc:
        raise ReplayError("dossier de rejeu impossible: %s" % exc) from None
    created.append(path)


def _is_under(path: str, child: str) -> bool:
    try:
        return os.path.commonpath([path, child]) == path
    except ValueError:
        return False


def _cleanup(plan: Plan, created: list[str], report: dict, *, keep=()) -> None:
    """Retire uniquement les dossiers de rejeu créés par cette évaluation.

    Aucun `os.listdir` de la racine : un manifeste strict des chemins créés
    (relecture B/P1 de #4292). Le rapport et tout fichier préexistant sont
    intacts ; la racine privée n'est retirée que si elle est vide et que
    `build_plan` l'a créée."""
    root = os.path.abspath(plan.work_root)
    kept = [os.path.abspath(path) for path in keep if path]
    for path in sorted(set(created), key=len, reverse=True):
        target = os.path.abspath(path)
        if target == root or not _is_under(root, target):
            continue
        if any(kept_path == target or _is_under(target, kept_path)
               for kept_path in kept):
            continue
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.unlink(target)
        except OSError:
            continue
    if plan.created_root:
        try:
            os.rmdir(root)
        except OSError:
            pass
    report.setdefault("notes", []).append(
        "arbres de rejeu retirés (--keep pour les garder)")


# --------------------------------------------------------------------------
# rapport
# --------------------------------------------------------------------------

def _fmt_usd(value) -> str:
    try:
        return "%.4f $" % float(value)
    except (TypeError, ValueError):
        return "?"


def _fmt_duration(value) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "?"
    return "%dm%02ds" % (int(seconds) // 60, int(seconds) % 60)


def _score(run: dict) -> str:
    score = (run.get("review") or {}).get("score")
    return "?" if score is None else ("%g" % score)


def report_markdown(report: dict) -> str:
    """Le tableau et la conclusion d'un rapport, en Markdown."""
    versions = report.get("versions") or {}
    evaluated = report.get("evaluated") or {}
    constants = report.get("constants") or {}
    kind = evaluated.get("kind") or "harness"
    old = evaluated.get("old") or {}
    new = evaluated.get("new") or {}
    old_label = old.get("label") or (versions.get("old") or {}).get("version") or "ancienne"
    new_label = new.get("label") or (versions.get("new") or {}).get("version") or "nouvelle"
    held = "harnais" if kind == "model" else "modèle"
    held_value = (constants.get("harness") if kind == "model"
                  else constants.get("model") or report.get("model")) or "(défaut)"
    if kind == "model":
        title = "# Rejeu comparatif — %s — modèles %s → %s" % (
            report.get("project") or "?", old_label, new_label)
    else:
        title = "# Rejeu comparatif — %s — %s %s → %s" % (
            report.get("project") or "?", report.get("harness") or "?",
            old_label, new_label)
    lines = [
        title, "",
        "%s `%s` (constant), effort `%s`, plafond de tour %ss ; %d lot(s)." % (
            held, held_value, report.get("effort") or "(défaut)",
            report.get("turn_cap_s"), len(report.get("lots") or [])), "",
    ]
    for entry in report.get("lots") or []:
        runs = entry.get("runs") or {}
        old_run = runs.get("old") or {}
        new_run = runs.get("new") or {}
        lines += ["## Lot #%s — %s (%s)" % (entry.get("lot"), entry.get("title"),
                                            entry.get("class")), "",
                  "| Mesure | %s | %s |" % (old_label, new_label),
                  "|---|---|---|",
                  "| Coût | %s | %s |" % (_fmt_usd(old_run.get("cost_usd")),
                                          _fmt_usd(new_run.get("cost_usd"))),
                  "| Durée | %s | %s |" % (_fmt_duration(old_run.get("duration_s")),
                                           _fmt_duration(new_run.get("duration_s"))),
                  "| Tours / étapes | %s / %s | %s / %s |" % (
                      old_run.get("turns", "?"), old_run.get("steps", "?"),
                      new_run.get("turns", "?"), new_run.get("steps", "?")),
                  "| Tests du lot | %s | %s |" % (
                      _tests_label(old_run), _tests_label(new_run)),
                  "| Note aveugle | %s | %s |" % (_score(old_run), _score(new_run)),
                  "| Défauts | %s | %s |" % (
                      len((old_run.get("review") or {}).get("defects") or []),
                      len((new_run.get("review") or {}).get("defects") or [])),
                  "",
                  "Verdict : **%s**%s." % (entry.get("verdict") or "?",
                                          _reasons(entry)), ""]
        if entry.get("errors"):
            lines += ["Erreurs :", ""] + ["- %s" % error
                                          for error in entry["errors"]] + [""]
    lines += ["## Conclusion", "",
              "**%s** — bruit admis : ±%d %% coût et durée, ±%g point de note." % (
                  report.get("conclusion") or "?",
                  int(NOISE_COST * 100), NOISE_SCORE), ""]
    if report.get("budget"):
        budget = report["budget"]
        lines += ["Dépense mesurée : %s (plafond %s), %s rejeu(x), %s relecture(s)." % (
            _fmt_usd(budget.get("spent_usd")), _fmt_usd(budget.get("cap_usd")),
            budget.get("runs"), budget.get("reviews")), ""]
    if report.get("notes"):
        lines += ["Notes :", ""] + ["- %s" % note for note in report["notes"]] + [""]
    return "\n".join(lines)


def _tests_label(run: dict) -> str:
    tests = run.get("tests") or {}
    if tests.get("status") == "no-tests":
        return "aucun"
    passed = tests.get("passed")
    if passed is None:
        return "non concluant"
    return "vert" if passed else "rouge"


def _reasons(entry: dict) -> str:
    deltas = entry.get("deltas") or {}
    parts = []
    if "cost_ratio" in deltas:
        parts.append("coût %+.0f %%" % (deltas["cost_ratio"] * 100))
    if "duration_ratio" in deltas:
        parts.append("durée %+.0f %%" % (deltas["duration_ratio"] * 100))
    if "score_delta" in deltas:
        parts.append("note %+g" % deltas["score_delta"])
    return (" (" + ", ".join(parts) + ")") if parts else ""


def report_sha256(path: str | None) -> str:
    """L'empreinte du fichier de rapport, telle que la note de B la porte."""
    if not path:
        return ""
    try:
        with open(_expand(path), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def note_text(report: dict) -> str:
    """La note de rejeu à poser sur le lot d'évaluation.

    Elle nomme le fichier ET son empreinte (#4312) : C lit le fichier après
    la note, et une empreinte absente ou différente refuse le rapport (le
    fichier a changé entre la note et la lecture)."""
    evaluated = report.get("evaluated") or {}
    kind = evaluated.get("kind") or "harness"
    old = evaluated.get("old") or {}
    new = evaluated.get("new") or {}
    versions = report.get("versions") or {}
    old_label = old.get("label") or (versions.get("old") or {}).get("version") or "?"
    new_label = new.get("label") or (versions.get("new") or {}).get("version") or "?"
    what = ("%s, modèles %s → %s" % (report.get("harness") or "?", old_label, new_label)
            if kind == "model" else
            "%s %s → %s" % (report.get("harness") or "?", old_label, new_label))
    bench = ", ".join("#%s (%s)" % (entry.get("lot"), entry.get("class"))
                      for entry in report.get("lots") or [])
    path = report.get("report_path") or report.get("work_root") or "?"
    lines = [
        "Rejeu comparatif %s : %s." % (
            what, CONCLUSION_LABELS.get(report.get("conclusion") or "", "?")),
        "Banc : %s ; coût, durée, tests du lot et note aveugle dans le rapport." % bench,
        "Rapport : %s" % path,
    ]
    digest = report_sha256(path)
    if digest:
        lines.append("Empreinte : %s" % digest)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ligne de commande
# --------------------------------------------------------------------------

def _add_bench_parser(sub) -> None:
    parser = sub.add_parser("bench", help="les trois lots du banc (lecture seule)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo", default="", help="dépôt local (sinon celui du projet)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_cmd_bench)


def _add_run_parser(sub) -> None:
    parser = sub.add_parser("run", help="rejouer le banc avec deux versions")
    parser.add_argument("--project", required=True)
    parser.add_argument("--harness", default="deepseek")
    parser.add_argument("--evaluate", choices=("harness", "model"), default="harness",
                        help="paramètre évalué : harnais (deux binaires) ou modèle "
                             "(même binaire, deux modèles)")
    parser.add_argument("--old-binary", default="",
                        help="binaire installé (défaut : celui que résout le descripteur)")
    parser.add_argument("--new-binary", default="",
                        help="binaire évalué (requis en évaluation de harnais)")
    parser.add_argument("--model", default="",
                        help="modèle maintenu constant (évaluation de harnais)")
    parser.add_argument("--old-model", default="",
                        help="modèle installé (évaluation de modèle)")
    parser.add_argument("--new-model", default="",
                        help="modèle évalué (évaluation de modèle)")
    parser.add_argument("--effort", default="", help="effort, identique aux deux versions")
    parser.add_argument("--turn-cap", type=float, default=TURN_CAP_S)
    parser.add_argument("--test-cap", type=float, default=TEST_CAP_S)
    parser.add_argument("--review-cap", type=float, default=REVIEW_CAP_S)
    parser.add_argument("--test-command", default="",
                        help="commande de test ({modules} et {files} substitués)")
    parser.add_argument("--map", default="", help="carte d'impact (carte.json.gz)")
    parser.add_argument("--lots", default="",
                        help="lots du banc, séparés par des virgules (défaut : sélection)")
    parser.add_argument("--reviewer", default="",
                        help="harnais relecteur, d'une autre famille")
    parser.add_argument("--reviewer-model", default="")
    parser.add_argument("--reviewer-effort", default="")
    parser.add_argument("--work-root", default="",
                        help="dossier parent du dossier privé de l'évaluation "
                             "(défaut : dossier temporaire)")
    parser.add_argument("--report", default="")
    parser.add_argument("--dsh-home", default="", help="foyer dsh source (copié sans hooks)")
    parser.add_argument("--env-file", default=ENV_FILE,
                        help="fichier d'environnement de l'hôte (clé d'API)")
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS)
    parser.add_argument("--max-reviews", type=int, default=MAX_REVIEWS)
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--resume", action="store_true",
                        help="reprendre le rapport existant (rejeux et relectures "
                             "faits) si le plan est identique ; sinon refus")
    parser.add_argument("--keep", action="store_true", help="garder les arbres de rejeu")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_cmd_run)


def _add_report_parser(sub) -> None:
    parser = sub.add_parser("report", help="relire un rapport de rejeu")
    parser.add_argument("file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--note", action="store_true", help="la note de rejeu (chemin et empreinte du rapport)")
    parser.set_defaults(func=_cmd_report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh replay",
        description="Banc de rejeu comparatif (#3264 B) : trois lots livrés, "
                    "deux versions, mêmes modèle, effort et plafond.")
    sub = parser.add_subparsers(dest="command")
    _add_bench_parser(sub)
    _add_run_parser(sub)
    _add_report_parser(sub)
    return parser


def _load_report(path: str) -> dict:
    try:
        with open(_expand(path), encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReplayError("rapport illisible: %s" % exc) from None
    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
        raise ReplayError("schéma de rapport inconnu: %r"
                          % (report.get("schema") if isinstance(report, dict) else None))
    return report


def _cmd_bench(cfg: Config, args) -> int:
    db = db_mod.connect(cfg)
    try:
        bench, rejected = discover(db, cfg, args.project, repo=args.repo or None)
    finally:
        db.close()
    payload = {"project": args.project, "lots": [lot.as_dict() for lot in bench],
               "rejected": rejected}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    for lot in bench:
        print("#%-6s %-9s base %s  %s" % (lot.lot, lot.klass, lot.base[:12],
                                          lot.title[:70]))
    for reason in rejected:
        print("écarté %s" % reason, file=sys.stderr)
    return 0


def _cmd_run(cfg: Config, args) -> int:
    db = db_mod.connect(cfg)
    try:
        resume = None
        if args.resume and args.report and os.path.isfile(_expand(args.report)):
            resume = _load_report(args.report)
        lots = tuple(int(part) for part in args.lots.split(",") if part.strip()) \
            if args.lots else ()
        plan = build_plan(
            cfg, db, project=args.project, new_binary=args.new_binary,
            old_binary=args.old_binary, harness=args.harness, model=args.model,
            evaluated=args.evaluate, old_model=args.old_model,
            new_model=args.new_model, effort=args.effort, lots=lots,
            reviewer=args.reviewer,
            reviewer_model=args.reviewer_model, reviewer_effort=args.reviewer_effort,
            turn_cap_s=args.turn_cap, test_cap_s=args.test_cap,
            review_cap_s=args.review_cap, test_command=args.test_command,
            map_path=args.map, work_root=args.work_root, report_path=args.report,
            dsh_home=args.dsh_home, env_file=args.env_file, max_runs=args.max_runs,
            max_reviews=args.max_reviews, budget_usd=args.budget_usd,
            keep=args.keep, resume=resume)
    finally:
        db.close()
    report = execute(plan, progress=lambda text: print(text, file=sys.stderr))
    report["report_path"] = plan.report_path
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json
          else report_markdown(report))
    print("\nRapport : %s" % plan.report_path, file=sys.stderr)
    return 0 if report.get("conclusion") != "incomplete" else 3


def _cmd_report(cfg: Config, args) -> int:
    report = _load_report(args.file)
    # le fichier est la source de l'empreinte de la note (#4312)
    report["report_path"] = _expand(args.file)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    elif args.note:
        print(note_text(report))
    else:
        print(report_markdown(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if getattr(args, "resume", False) and not getattr(args, "report", ""):
        print("ameesh replay: --resume demande --report FILE", file=sys.stderr)
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except ReplayError as exc:
        print("ameesh replay: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.Unavailable, db_mod.DbError) as exc:
        print("ameesh replay: base injoignable: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
