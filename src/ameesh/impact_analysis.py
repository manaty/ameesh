# SPDX-License-Identifier: AGPL-3.0-only
"""#4818 (manaty/ameesh#3643) : l'analyse d'impact d'un lot avant dispatch.

Une passe rapide, en lecture seule et déterministe (aucun modèle), estime la
**surface** d'un lot — les fichiers ou modules qu'il touchera probablement —
avec une confiance par chemin :

* `high` : un chemin cité par le titre ou le corps (`intake_neighbors.
  cited_paths`), résolu dans le clone (un nom seul devient son chemin s'il
  est unique) ; un chemin cité absent du dépôt est gardé (fichier à créer) ;
* `medium` : un identifiant cité (`snake_case`, `CamelCase`, `module.fonction`,
  commande `sync-branches`) que `git grep` trouve dans au plus
  `MAX_FILES_PER_IDENTIFIER` fichiers hors tests — sinon dans ses seules
  définitions (`def`, `class`, `function`…) ;
* `weak` : un mot du titre qui nomme un fichier (`footprint` →
  `work_footprints.py`). Ces chemins sont journalisés, jamais écrits dans
  l'empreinte : une devinette ne retient aucun lot.

L'empreinte du lot est l'union des chemins `high` et `medium`. Sans aucun
(rien de cité, pas de clone, budget épuisé, git illisible), l'analyse est
**inconnue** : la politique du dépôt (`repository_footprint_policies`) décide —
`repository` pose le marqueur `.` (dépôt entier, chevauche tout), `open`
(défaut sans politique posée) ne pose rien (règle d'avant #4818).

Le budget est borné deux fois : une échéance (`budget_s`) partagée par tous
les appels git, et un nombre d'appels (`max_git_calls`). Une analyse rend
toujours sa ligne de journal (`note`), qu'elle aboutisse ou non.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

BUDGET_S = 3.0
MAX_GIT_CALLS = 16
MAX_IDENTIFIERS = 8
MAX_FILES_PER_IDENTIFIER = 3
MAX_WEAK = 5
MIN_WEAK_WORD = 6

#: le marqueur d'empreinte inconnue : le dépôt entier
WHOLE_REPOSITORY = "."

_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.-][A-Za-z_][A-Za-z0-9_]*)*")
_SNAKE_RE = re.compile(r"(?<![\w./-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w/])")
_CAMEL_RE = re.compile(r"[a-z][A-Z]|[A-Z][a-z]+[A-Z]")
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|\.(test|spec)\.[^/]+$")


class _Budget:
    def __init__(self, budget_s: float, max_calls: int, clock=time.monotonic):
        from .deadline import Deadline
        self.deadline = Deadline(budget_s, clock=clock)
        self.calls = 0
        self.max_calls = int(max_calls)
        self.exhausted = False

    def take(self) -> bool:
        if self.calls >= self.max_calls or self.deadline.expired():
            self.exhausted = True
            return False
        self.calls += 1
        return True


def identifiers(text: str) -> list[str]:
    """Les identifiants cités par `text`, dans l'ordre, sans doublon : les
    jetons de code entre accents graves qui ont la forme d'un identifiant
    (`a_b`, `aB`, `AbCd`, `mod.fonction` → `fonction`, `sync-branches`), et
    les `snake_case` du texte courant. Un chemin de fichier n'en est pas un
    (il est déjà cité)."""
    from . import intake_neighbors
    cited = set()
    for c in intake_neighbors.cited_paths(text):
        name = c["git_path"].rsplit("/", 1)[-1]
        cited |= {c["git_path"], name, name.rsplit(".", 1)[0]}
    out: list[str] = []

    def keep(token: str) -> None:
        token = token.strip("._-")
        # le nom d'un fichier cité n'est pas un identifiant : il est déjà cité
        if len(token) < 4 or token in out or token in cited:
            return
        out.append(token)

    for span in _BACKTICK_RE.findall(str(text or "")):
        if "/" in span or any(span.endswith("." + ext) for ext in ("py", "md", "ts", "sql")):
            continue
        for token in _TOKEN_RE.findall(span):
            if "." in token:
                token = token.rsplit(".", 1)[-1]
            if "_" in token or _CAMEL_RE.search(token):
                keep(token)
            elif "-" in token and len(token) >= 6:
                keep(token)
    for token in _SNAKE_RE.findall(str(text or "")):
        keep(token)
    return out[:MAX_IDENTIFIERS]


def _is_test(path: str) -> bool:
    return bool(_TEST_RE.search(path))


def _lines(proc, ref: str) -> list[str]:
    if proc is None or proc.returncode != 0:
        return []
    prefix = ref + ":"
    out = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        out.append(line[len(prefix):] if line.startswith(prefix) else line)
    return out


def _grep(git, repo: str, ref: str, budget: _Budget, *args: str) -> Optional[list[str]]:
    """`git grep -l … ref --`, ou None si le budget est épuisé."""
    if not budget.take():
        return None
    try:
        proc = git(repo, "grep", "-l", "-I", *args, ref, "--",
                   timeout=max(0.1, budget.deadline.remaining()))
    except Exception:  # noqa: BLE001 - lecture au mieux, l'échec se dit
        return []
    return _lines(proc, ref)


def _tree(git, repo: str, ref: str, budget: _Budget, trees: dict) -> Optional[list[str]]:
    """Les fichiers de `ref`, lus une fois par analyse ; None si le budget
    est épuisé avant la lecture."""
    if ref not in trees:
        if not budget.take():
            return None
        try:
            listing = git(repo, "ls-tree", "-r", "--name-only", ref,
                          timeout=max(0.1, budget.deadline.remaining()))
            trees[ref] = _lines(listing, ref)
        except Exception:  # noqa: BLE001 - arbre illisible : rien à résoudre
            trees[ref] = []
    return trees[ref]


def _resolve_cited(git, repo: str, ref: str, path: str, budget: _Budget,
                   trees: dict) -> Optional[str]:
    """Le chemin réel d'un fichier cité à `ref`, ou None — la règle de
    `intake_neighbors._resolve_git_path` (chemin exact, sinon l'unique
    fichier de ce nom, la casse en dernier recours), mais chaque appel git
    passe par le budget partagé et le temps qui lui reste (B1 de cx1)."""
    if not budget.take():
        return None
    try:
        proc = git(repo, "rev-parse", "--verify", "--quiet", "%s:%s" % (ref, path),
                   timeout=max(0.1, budget.deadline.remaining()))
    except Exception:  # noqa: BLE001 - dépôt illisible
        return None
    if proc is not None and proc.returncode == 0:
        return path
    if "/" in path:
        return None
    files = _tree(git, repo, ref, budget, trees)
    if not files:
        return None
    basename = path.rsplit("/", 1)[-1]
    exact = [f for f in files if f.rsplit("/", 1)[-1] == basename]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    folded = [f for f in files if f.rsplit("/", 1)[-1].casefold() == basename.casefold()]
    return folded[0] if len(folded) == 1 else None


def analyze(text: str, *, repo: Optional[str], ref: str = "", title: str = "",
            budget_s: float = BUDGET_S, max_git_calls: int = MAX_GIT_CALLS,
            git: Optional[Callable] = None, clock=time.monotonic) -> dict:
    """La surface estimée de `text` dans `repo` à `ref` (lecture seule).

    Rend `{paths: [{path, confidence, why}], weak: [...], status, reason,
    elapsed_ms, git_calls}` ; `status` vaut `known` si au moins un chemin
    `high`/`medium` est trouvé, sinon `unknown` (avec `reason`)."""
    from . import intake_neighbors, work_footprints
    started = clock()
    budget = _Budget(budget_s, max_git_calls, clock=clock)
    if git is None:
        def git(repo_, *args, timeout=None):
            from . import canon
            return canon._git(repo_, *args, timeout=timeout or canon.GIT_TIMEOUT,
                              env_extra=canon.NO_LAZY_FETCH)
    found: dict[str, dict] = {}

    def add(path: str, confidence: str, why: str) -> None:
        clean = work_footprints.normalize(path)
        if clean and clean not in found:
            found[clean] = {"path": clean, "confidence": confidence, "why": why}

    usable = bool(repo and ref)
    trees: dict = {}
    for cited in intake_neighbors.cited_paths(text):
        path = cited["git_path"]
        if usable:
            path = _resolve_cited(git, repo, ref, path, budget, trees) or path
        add(path, "high", "cited")
    weak: list[dict] = []
    if usable:
        for ident in identifiers(text):
            hits = _grep(git, repo, ref, budget, "-F", "-w", "-e", ident)
            if hits is None:
                break
            hits = [h for h in hits if not _is_test(h)]
            if len(hits) > MAX_FILES_PER_IDENTIFIER:
                escaped = re.escape(ident)
                hits = _grep(git, repo, ref, budget, "-E", "-e",
                             r"(def|class|function|const|let|var|interface|type|struct|fn)"
                             r"[[:space:]]+" + escaped + r"([^A-Za-z0-9_]|$)")
                if hits is None:
                    break
                hits = [h for h in hits if not _is_test(h)]
            if 0 < len(hits) <= MAX_FILES_PER_IDENTIFIER:
                for hit in hits:
                    add(hit, "medium", "identifier %s" % ident)
        words = sorted(w for w in intake_neighbors.significant_words(title or "")
                       if len(w) >= MIN_WEAK_WORD)
        if words:
            _tree(git, repo, ref, budget, trees)
        files = [f for f in trees.get(ref) or () if not _is_test(f)]
        for word in words:
            hits = [f for f in files
                    if word in intake_neighbors._fold(f.rsplit("/", 1)[-1])]
            if 0 < len(hits) <= MAX_FILES_PER_IDENTIFIER:
                for hit in hits:
                    if hit not in found and all(w["path"] != hit for w in weak):
                        weak.append({"path": hit, "confidence": "weak",
                                     "why": "title word %s" % word})
            if len(weak) >= MAX_WEAK:
                break
    paths = sorted(found.values(), key=lambda p: p["path"])
    if paths:
        status, reason = "known", None
    elif not usable:
        status, reason = "unknown", "no local clone of the repository and no cited path"
    elif budget.exhausted:
        status, reason = "unknown", "budget exhausted (%d git call(s))" % budget.calls
    else:
        status, reason = "unknown", "no cited path, no identifier found in the repository"
    return {"status": status, "reason": reason, "paths": paths, "weak": weak[:MAX_WEAK],
            "elapsed_ms": int(round((clock() - started) * 1000)),
            "git_calls": budget.calls}


def note(analysis: dict) -> str:
    """La ligne de journal d'une estimation (une par analyse)."""
    timing = "%d ms, %d git call(s)" % (analysis.get("elapsed_ms") or 0,
                                       analysis.get("git_calls") or 0)
    weak = analysis.get("weak") or ()
    tail = ("; weak (not held): %s" % ", ".join(w["path"] for w in weak)) if weak else ""
    if analysis.get("status") == "known":
        groups = []
        for level in ("high", "medium"):
            items = [p["path"] for p in analysis.get("paths") or ()
                     if p.get("confidence") == level]
            if items:
                groups.append("%s: %s" % (level, ", ".join(items)))
        return "Impact analysis: %d path(s) (%s) in %s%s" % (
            len(analysis.get("paths") or ()), "; ".join(groups), timing, tail)
    policy = analysis.get("policy") or "open"
    outcome = ("unknown footprint held as the whole repository (policy repository)"
               if policy == "repository"
               else "unknown footprint, nothing held (policy open)")
    return "Impact analysis: %s — %s, in %s%s" % (
        analysis.get("reason") or "no path found", outcome, timing, tail)
