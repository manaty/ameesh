# SPDX-License-Identifier: AGPL-3.0-only
"""Fusion d'un lot constatée par le CONTENU (lot L29), dans un clone git local.

    ameesh work sync-merges --git-dir D [--target origin/main] [--repo owner/repo]
                            [--dry-run] [--json]

Le nom d'un lot change (branche renommée entre gel et fusion), la fusion se
fait par rebase, squash ou cherry-pick sans commit de fusion reconnaissable :
le nom ne prouve rien. Seul le **dernier gel** d'un lot fait autorité (le
jalon `frozen` le plus récent de L10, avec son commit) : un gel antérieur
déjà fusionné ne prouve rien sur le travail courant. Pour chaque lot ouvert
qui a un tel gel, la fusion est constatée, dans cet ordre :

1. **ancêtre** : le commit du dernier gel est ancêtre de la branche cible ;
2. **patch-id exact** (rebase, cherry-pick) : chaque commit du gel absent
   de la cible (`cible..gel`, hors fusions) a son `git patch-id --verbatim`
   parmi ceux des commits de la cible depuis leur base commune ; ou
   (**squash**) le diff entier du gel (base..gel) a le patch-id d'un commit
   de la cible. `--verbatim` ne normalise AUCUN espace (`--stable` ignore les
   espaces, même dans une chaîne : `"a b"` et `"ab"` y sont égaux) ; seuls
   les numéros de ligne des en-têtes de bloc sont ignorés. Un git sans
   `--verbatim` (< 2.40) ne prouve que par l'ancêtre : faux négatif plutôt
   que faux positif ;
3. à défaut, si `--repo` est donné, **la PR liée est fusionnée**
   (`plan_github.sync_github`, lecture seule de GitHub).

La fermeture est conditionnée en base au gel examiné : si un nouveau gel est
déclaré entre l'examen et l'écriture, rien n'est fermé (`refrozen`).

Lecture seule du dépôt (`git merge-base`, `rev-list`, `log -p`, `patch-id`) ;
aucun `fetch` : la cible est lue telle que le clone la connaît. Un lot
absorbé par un autre ou remplacé se clôt par `ameesh work close <id>
--superseded-by <id>` ; jamais de réouverture d'un lot fermé.
"""
from __future__ import annotations

import re

from . import storage
from . import work as work_mod
from .canon import GitError, _git

DEFAULT_TARGET = "origin/main"
#: commits de la cible examinés au plus depuis la base commune (borne de coût)
MAX_TARGET_COMMITS = 5000
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class MergeProbeError(RuntimeError):
    """Dépôt ou cible illisible : message actionnable."""


class _NoVerbatim(Exception):
    """`git patch-id --verbatim` indisponible : aucune preuve par le contenu."""


#: le patch-id exact : aucun espace normalisé (voir le docstring du module)
PATCH_ID = ("patch-id", "--verbatim")


def _patch_id_run(repo: str, data: bytes):
    proc = _git(repo, *PATCH_ID, stdin=data)
    if proc.returncode != 0:
        raise _NoVerbatim(proc.stderr.decode("utf-8", "replace").strip())
    return proc


def _out(repo: str, *args: str) -> str | None:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _commit(repo: str, rev: str) -> str | None:
    return _out(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")


def _patch_ids(repo: str, *log_args: str) -> dict[str, str]:
    """{patch-id: commit} des commits non-fusions de `git log -p <log_args>`."""
    log = _git(repo, "log", "-p", "--no-merges", "--no-color", "--format=commit %H",
               *log_args)
    if log.returncode != 0:
        raise MergeProbeError("git log %s: %s" % (" ".join(log_args),
                                                   log.stderr.decode("utf-8", "replace").strip()))
    if not log.stdout.strip():
        return {}
    return _parse_patch_ids(_patch_id_run(repo, log.stdout))


def _parse_patch_ids(proc) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.setdefault(parts[0], parts[1])
    return out


def _diff_patch_id(repo: str, base: str, head: str) -> str | None:
    diff = _git(repo, "diff", "--no-color", base, head)
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    ids = _parse_patch_ids(_patch_id_run(
        repo, b"commit " + head.encode() + b"\n" + diff.stdout))
    return next(iter(ids), None)


def probe(repo: str, sha: str, target: str) -> dict:
    """Le commit `sha` est-il entré dans `target` ? Rend `{merged, how, ref, detail}`.

    `how` : ancestor | patch-id | squash ; `ref` : le commit de la cible qui
    porte le contenu (le gel lui-même s'il est ancêtre)."""
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    if not _SHA_RE.match(sha or ""):
        return {"merged": False, "how": None, "ref": None, "detail": "unreadable commit"}
    frozen = _commit(repo, sha)
    if frozen is None:
        return {"merged": False, "how": None, "ref": None,
                "detail": "commit %s not in the repository" % sha[:12]}
    if _git(repo, "merge-base", "--is-ancestor", frozen, head).returncode == 0:
        return {"merged": True, "how": "ancestor", "ref": frozen,
                "detail": "%s is an ancestor of %s" % (frozen[:12], target)}
    base = _out(repo, "merge-base", frozen, head)
    if not base:
        return {"merged": False, "how": None, "ref": None, "detail": "no common base"}
    try:
        return _by_patch_id(repo, frozen, head, base, target)
    except _NoVerbatim as exc:
        return {"merged": False, "how": None, "ref": None,
                "detail": "git patch-id --verbatim unavailable (git >= 2.40 required: %s): "
                          "only ancestry proves the merge" % (exc or "refused")}


def _by_patch_id(repo: str, frozen: str, head: str, base: str, target: str) -> dict:
    on_target = _patch_ids(repo, "--max-count=%d" % MAX_TARGET_COMMITS, "%s..%s" % (base, head))
    mine = _patch_ids(repo, "%s..%s" % (head, frozen))
    if mine and all(pid in on_target for pid in mine):
        # `git log` rend les commits du plus récent au plus ancien : le premier
        # patch-id est celui du dernier commit du gel
        last = on_target[next(iter(mine))]
        return {"merged": True, "how": "patch-id", "ref": last,
                "detail": "%d frozen commit(s) found by patch-id in %s"
                          % (len(mine), target)}
    squash = _diff_patch_id(repo, base, frozen)
    if squash and squash in on_target:
        return {"merged": True, "how": "squash", "ref": on_target[squash],
                "detail": "frozen diff found as one commit of %s (squash)" % target}
    missing = sum(1 for pid in mine if pid not in on_target)
    return {"merged": False, "how": None, "ref": None,
            "detail": "%d/%d frozen commit(s) missing from %s" % (missing, len(mine), target)}


def latest_freeze(rows: list[dict]) -> dict | None:
    """Le dernier gel d'un lot (jalon `frozen` le plus récent), ou None.

    Lui seul fait autorité : s'il n'a pas de commit, rien n'est prouvable."""
    freezes = [r for r in rows if r.get("kind") == "frozen"]
    if not freezes:
        return None
    return max(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))


def sync_merges(db, repo_dir: str, *, target: str = DEFAULT_TARGET, dry_run: bool = False,
                limit: int = 1000) -> dict:
    """Ferme les lots dont le contenu gelé est entré dans `target`."""
    try:
        if _commit(repo_dir, target) is None:
            raise MergeProbeError("target %r not found in %s" % (target, repo_dir))
    except GitError as exc:
        raise MergeProbeError(str(exc)) from exc
    st = storage.of(db)
    rows = [r for r in st.work.items(state=None, assignee=None, limit=limit)
            if r["state"] not in work_mod.MERGED_STATES and r["state"] != "closed"]
    declared: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    results: list[dict] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        freeze = latest_freeze(declared.get(int(row["id"]), []))
        sha = ((freeze or {}).get("sha") or "").strip()
        entry = {"work_item": int(row["id"]), "package": row.get("package_id"),
                 "state": row["state"], "result": "no-commit", "how": None, "ref": None,
                 "detail": ("last freeze has no commit (milestone frozen --sha)" if freeze
                            else "no freeze declared (milestone frozen --sha)")}
        if sha:
            try:
                found = probe(repo_dir, sha, target)
            except GitError as exc:
                raise MergeProbeError(str(exc)) from exc
            entry.update(how=found["how"], ref=found["ref"], detail=found["detail"],
                         sha=sha, result="open")
        if entry["how"]:
            if dry_run:
                entry["result"] = "would-merge"
            else:
                done = work_mod.close_merged(
                    db, int(row["id"]), sha=entry["ref"] or "", actor="git:%s" % target,
                    source="content (%s)" % entry["how"], frozen_id=int(freeze["id"]))
                entry["result"] = done["result"]
                entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
        results.append(entry)
    return {"git_dir": repo_dir, "target": target, "dry_run": bool(dry_run),
            "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged")}


# --------------------------------------------------------------------------
# L118 : fusion d'une BRANCHE constatée, avec ou sans PR
# --------------------------------------------------------------------------
#
# Une équipe qui fusionne directement sur sa branche cible ne passe ni par
# une PR (`sync-github`) ni par un gel déclaré (`sync-merges`) : ses lots
# restaient ouverts après la fusion. Un lot peut porter sa branche
# (`work add|assign --branch`, ou déduite d'un message `mail send --lot` qui
# cite une branche `agent/…`) ; le relevé périodique de l'exécuteur
# (`sync_branches`) la cherche dans le dépôt de l'assigné — son dossier de
# travail au registre, posé par `policy.work_dirs`/`work_roots` du canon
# (L35) : tous les worktrees d'un dépôt partagent ses références.
#
# Une branche tout juste créée depuis sa cible en est déjà « ancêtre » : ce
# n'est pas une fusion. La fusion est constatée, dans cet ordre :
#
# 1. la pointe de la branche est ancêtre de la cible SANS être sur sa chaîne
#    de premiers parents (un commit de fusion l'y a fait entrer) ;
# 2. la pointe est sur cette chaîne (avance rapide, ou branche sans travail) :
#    le dernier commit vu EN AVANCE par un relevé précédent (`branch_head`)
#    est entré dans la cible (ancêtre, patch-id ou squash : `probe`) ;
# 3. branche en avance : son contenu est entré par patch-id ou squash
#    (`probe`) ;
# 4. branche supprimée : le dernier commit vu (`probe`), sinon un commit de
#    fusion de la cible dont le message cite la branche (nom entier).
#
# Correctif du 2026-10-11 : une équipe fusionne aussi en local, sans PR, des
# lots SANS branche. Le relevé examine désormais tout lot ouvert assigné à un
# agent de l'hôte, avec ou sans branche : à défaut de preuve par la branche,
# un commit de FUSION de la cible, postérieur à la création du lot, le
# désigne par une ligne `ameesh-work: <id>` (n'importe où dans le message,
# même forme que dans le corps d'une PR) ou, seulement si le dépôt l'active
# (`git config ameesh.lotRef hash`), par `#<id>` dans son TITRE. Ailleurs
# « (#45) » est un numéro de PR GitHub : il fermerait le mauvais lot.
#
# Lecture seule du dépôt, aucun `fetch`. La cible : celle du lot, sinon
# `git config ameesh.target` du dépôt (une valeur introuvable est une
# erreur, jamais un repli silencieux sur main), sinon `origin/HEAD`, sinon
# main.

#: une branche citée dans un message (`agent/…`)
CITED_BRANCH_RE = re.compile(r"(?<![\w./-])(agent/[A-Za-z0-9._/-]*[A-Za-z0-9_-])")
#: nom de référence admis pour une branche ou une cible
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: caractères qui prolongent un nom de branche (citation à nom entier)
_REF_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
#: commits de fusion lus au plus pour une citation
MAX_MERGE_MENTIONS = 200
#: lots examinés au plus par relevé
MAX_BRANCH_LOTS = 1000
#: #312 r1 : corps de PR lus au plus par relevé (`gh`, 60 s chacun au pire)
MAX_PR_BODY_READS = 20
#: #312 r1 : après un échec de `gh` sur une PR, plus de lecture de son corps
#: pendant ce délai, doublé à chaque échec suivant (plafond), oublié après
#: une lecture réussie ; et au plus un échec par dépôt et par relevé
PR_BODY_BACKOFF_S = 60.0
PR_BODY_BACKOFF_MAX_S = 3600.0
#: (dépôt, PR) → (pas avant, délai courant) ; état du processus (relevés successifs)
_pr_body_backoff: dict = {}
#: #312 r2 : (dépôt, PR) → corps déjà lu d'une PR fusionnée (il ne change
#: plus) : relu sans appel ni lecture comptée, les relevés suivants lisent
#: les PR suivantes ; état du processus, borné
_pr_body_cache: dict = {}
PR_BODY_CACHE_MAX = 5000
#: clé de configuration git du dépôt qui nomme la cible par défaut
TARGET_GIT_KEY = "ameesh.target"
DEFAULT_TARGETS = ("origin/main", "main", "origin/master", "master")
#: une ligne `ameesh-work: <id>` désigne un lot précis (message d'un commit de
#: fusion, ou corps d'une PR : `plan_github`) ; le mot `final` après le numéro
#: déclare cette PR comme la dernière étape du lot (#759)
WORK_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?([1-9][0-9]{0,17})"
    r"(?:[ \t]+(final))?[ \t]*$")
#: L118b : une ligne `ameesh-work-partial: <id>` (corps d'une PR, message d'un
#: commit de fusion) : la PR ne livre qu'une partie du lot, qui reste ouvert
#: (lots parapluie, #139)
PARTIAL_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work-partial[ \t]*:[ \t]*#?([1-9][0-9]{0,17})[ \t]*$")
#: L118b : le titre d'un commit de fusion d'une branche `lot/<id>-…` : celui de
#: GitHub (« Merge pull request #N from owner/lot/<id>-… », `pr` : N) ou
#: d'une fusion locale (« Merge branch 'lot/<id>-…' [into …] »)
LOT_BRANCH_MERGE_RE = re.compile(
    r"^Merge (?:pull request #(?P<pr>[1-9][0-9]{0,17}) from [A-Za-z0-9_.-]+/"
    r"|(?:remote-tracking )?branch '(?:origin/)?)"
    r"(?P<branch>lot/(?P<lot>[1-9][0-9]{0,17})(?:-[A-Za-z0-9._/-]*)?)'?"
    r"(?: into [^\s]+)?[ \t]*$")
#: `#<id>` dans le titre d'un commit de fusion (« Merge #93 … », « … (#91) ») ;
#: ni `PR#9`, ni `depot#9`, ni `##9`, ni `#9a`
HASH_REF_RE = re.compile(r"(?<![\w/#&])#([1-9][0-9]{0,17})(?!\w)")
#: clé de configuration git du dépôt : comment ses commits de fusion citent un
#: lot en plus de `ameesh-work:` — `hash` : `#<id>` dans le titre
LOT_REF_GIT_KEY = "ameesh.lotRef"
LOT_REF_HASH = "hash"
#: commits de fusion lus au plus par dépôt et par relevé (les plus récents)
MAX_MERGE_SCAN = 500
#: écart d'horloge admis (s) entre la base (création du lot) et le poste qui
#: a fait le commit de fusion (date à la seconde)
CLOCK_SLACK_S = 120.0


def check_ref(name: str, what: str = "branch") -> str:
    """Un nom de branche (ou de cible) admissible, sinon ValueError."""
    text = (name or "").strip()
    if not _REF_RE.match(text) or ".." in text or "//" in text or text.endswith(
            ("/", ".", ".lock")) or "@{" in text:
        raise ValueError("invalid %s: %r (letters, digits, . _ / -)" % (what, name))
    return text


def cited_branch(text: str) -> str | None:
    """La branche `agent/…` citée par un message, si elle est UNIQUE (deux
    branches citées : on ne devine pas)."""
    found = {m.rstrip(".") for m in CITED_BRANCH_RE.findall(text or "")}
    found = {b for b in found if b != "agent/"}
    return next(iter(found)) if len(found) == 1 else None


def default_target(repo: str) -> str | None:
    """La cible par défaut du dépôt : `git config ameesh.target`, sinon la
    branche par défaut du dépôt distant (`origin/HEAD`), sinon main/master.

    Une cible configurée mais introuvable (faute de frappe, `origin/develop`
    jamais récupérée) lève MergeProbeError : se rabattre sur main ferait
    chercher les fusions au mauvais endroit, sans le dire."""
    configured = _out(repo, "config", "--get", TARGET_GIT_KEY)
    if configured:
        if _commit(repo, configured):
            return configured
        raise MergeProbeError("%s = %s not found in %s (no fetch is done: "
                              "fix the value or fetch the branch)"
                              % (TARGET_GIT_KEY, configured, repo))
    head = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and _commit(repo, head):
        return head
    for candidate in DEFAULT_TARGETS:
        if _commit(repo, candidate):
            return candidate
    return None


def _tip(repo: str, branch: str) -> str | None:
    for ref in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        found = _commit(repo, ref)
        if found:
            return found
    return None


def _on_first_parent(repo: str, commit: str, head: str) -> bool:
    chain = _out(repo, "rev-list", "--first-parent", "--max-count=%d" % MAX_TARGET_COMMITS,
                 head)
    return bool(chain) and commit in chain.split()


def _mentions(message: str, branch: str) -> bool:
    start = message.find(branch)
    while start >= 0:
        end = start + len(branch)
        before = message[start - 1] if start else " "
        after = message[end] if end < len(message) else " "
        if before not in _REF_CHARS and (after not in _REF_CHARS or (
                after == "." and (end + 1 >= len(message)
                                  or message[end + 1] not in _REF_CHARS))):
            return True
        start = message.find(branch, start + 1)
    return False


def merge_mentioning(repo: str, branch: str, head: str,
                     since_ts: float | None = None) -> str | None:
    """Le plus récent commit de FUSION de `head` dont le message cite la
    branche (nom entier : `agent/x` ne cite pas `agent/x-2`), ou None. Un
    commit ordinaire qui cite la branche (compte rendu de revue) ne compte pas."""
    args = ["log", "--merges", "--format=%H%x00%B%x1e", "-F", "--grep=%s" % branch,
            "--max-count=%d" % MAX_MERGE_MENTIONS]
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    out = _out(repo, *args, head)
    for record in (out or "").split("\x1e"):
        sha, _sep, message = record.strip().partition("\x00")
        if sha and _mentions(message, branch):
            return sha
    return None


def branch_probe(repo: str, branch: str, target: str, *, seen_head: str | None = None,
                 since_ts: float | None = None) -> dict:
    """La branche est-elle entrée dans `target` ? Rend `{merged, how, ref, tip,
    ahead, detail}` ; `ahead` : la pointe a du travail absent de la cible
    (l'appelant la retient comme `branch_head`)."""
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    tip = _tip(repo, branch)
    out = {"merged": False, "how": None, "ref": None, "tip": tip, "ahead": False}

    def seen() -> dict | None:
        if not seen_head:
            return None
        found = probe(repo, seen_head, target)
        if found["merged"]:
            return dict(out, merged=True, how="vu-" + found["how"], ref=found["ref"],
                        detail="last commit seen ahead (%s): %s"
                               % (seen_head[:12], found["detail"]))
        return None

    def mention(why: str) -> dict:
        sha = merge_mentioning(repo, branch, head, since_ts)
        if sha:
            return dict(out, merged=True, how="message", ref=sha,
                        detail="merge commit %s of %s that cites %s" % (sha[:12], target,
                                                                          branch))
        return dict(out, detail=why)

    if tip is None:
        return seen() or mention("branch %s not found (neither local nor origin/)" % branch)
    if _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0:
        if not _on_first_parent(repo, tip, head):
            return dict(out, merged=True, how="ancestor", ref=tip,
                        detail="%s (%s) entered %s through a merge commit"
                               % (branch, tip[:12], target))
        return seen() or mention("%s has no work of its own: its tip %s is on the "
                                 "line of %s" % (branch, tip[:12], target))
    out["ahead"] = True
    found = probe(repo, tip, target)
    if found["merged"]:
        return dict(out, merged=True, how=found["how"], ref=found["ref"],
                    detail="%s: %s" % (branch, found["detail"]))
    return dict(out, detail="%s ahead of %s (%s)" % (branch, target, tip[:12]))


def lot_ref_hash(repo: str) -> bool:
    """`git config ameesh.lotRef hash` : les commits de fusion du dépôt citent
    leurs lots par `#<id>` dans leur titre (à n'activer que si ces numéros sont
    des lots ameesh, jamais des PR GitHub)."""
    return (_out(repo, "config", "--get", LOT_REF_GIT_KEY) or "").strip().lower() == LOT_REF_HASH


def merge_commits(repo: str, target: str, since_ts: float | None = None,
                  limit: int = MAX_MERGE_SCAN) -> list[dict]:
    """Les commits de FUSION de `target` (au plus `limit`, du plus récent au
    plus ancien, postérieurs à `since_ts`) : `{sha, ts, message, parents}`."""
    args = ["log", "--merges", "--format=%H%x00%ct%x00%P%x00%B%x1e", "--max-count=%d" % int(limit)]
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    proc = _git(repo, *args, target)
    if proc.returncode != 0:
        raise MergeProbeError("git log %s: %s" % (
            target, proc.stderr.decode("utf-8", "replace").strip()))
    rows = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        sha, _sep, rest = record.strip().partition("\x00")
        stamp, _sep, rest = rest.partition("\x00")
        parents, _sep, message = rest.partition("\x00")
        if sha:
            try:
                ts = float(stamp)
            except ValueError:
                ts = 0.0
            rows.append({"sha": sha, "ts": ts, "message": message.strip(),
                         "parents": parents.split()})
    return rows


def cited_lots(message: str, *, hash_refs: bool = False) -> dict[int, str]:
    """`{lot: forme}` des lots que désigne le message d'un commit de fusion :
    une ligne `ameesh-work: <id>` (forme `ameesh-work`), le titre de la fusion
    d'une branche `lot/<id>-…` (forme `lot-branch`, L118b), et, avec
    `hash_refs`, `#<id>` dans le titre (forme `#id`) — pas dans le corps, qui
    peut recopier les titres des commits fusionnés. Un lot déclaré partiel
    (`ameesh-work-partial: <id>`) n'est pas désigné."""
    found: dict[int, str] = {}
    lines = (message or "").strip().splitlines()
    title = lines[0].strip() if lines else ""
    for match in WORK_TRAILER_RE.finditer(message or ""):
        found.setdefault(int(match.group(1)), "ameesh-work")
    branch = LOT_BRANCH_MERGE_RE.match(title)
    if branch:
        found.setdefault(int(branch.group("lot")), "lot-branch")
    if hash_refs:
        for match in HASH_REF_RE.finditer(title):
            found.setdefault(int(match.group(1)), "#id")
    for match in PARTIAL_TRAILER_RE.finditer(message or ""):
        found.pop(int(match.group(1)), None)
    return found


def merge_pr_number(message: str) -> int | None:
    """Le numéro de la PR d'un commit de fusion GitHub d'une branche
    `lot/<id>-…` (son corps n'est pas dans le message : `partial_of_pr`)."""
    match = _lot_branch_merge(message)
    return int(match.group("pr")) if match and match.group("pr") else None


def merge_branch(message: str) -> str | None:
    """La branche `lot/<id>-…` que fusionne un commit de fusion (son titre)."""
    match = _lot_branch_merge(message)
    return match.group("branch") if match else None


def _lot_branch_merge(message: str):
    lines = (message or "").strip().splitlines()
    return LOT_BRANCH_MERGE_RE.match(lines[0].strip() if lines else "")


def short_branch(name: str | None) -> str:
    """`lot/12-x` de `refs/heads/lot/12-x`, `origin/lot/12-x`… (comparaison)."""
    name = (name or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def partial_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare ne livrer qu'en partie (L118b)."""
    return {int(m.group(1)) for m in PARTIAL_TRAILER_RE.finditer(body or "")}


def final_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare livrer en dernier : une ligne
    `ameesh-work: <id> final` (#759). Une PR finale ferme le lot, même si une
    autre PR ouverte ou une branche déclarée y reste liée ; `ameesh-work-partial:`
    l'emporte (une PR partielle ne ferme jamais)."""
    return {int(m.group(1)) for m in WORK_TRAILER_RE.finditer(body or "")
            if m.group(2)}


def merge_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                 hash_refs: bool = False) -> dict | None:
    """Le PREMIER commit de fusion postérieur à `since_ts` (création du lot,
    à `CLOCK_SLACK_S` près) qui désigne le lot `lot` (`cited_lots`), avec
    `how`, ou None."""
    found = None
    for merge in merges:                     # du plus récent au plus ancien
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            found = dict(merge, how=how)
    return found


def merges_partial(merges: list[dict], lot: int, *,
                   since_ts: float | None = None) -> list[dict]:
    """#312 r1 : les commits de fusion postérieurs à `since_ts` dont le
    message déclare le lot partiel (`ameesh-work-partial: <id>`), du plus
    ancien au plus récent."""
    return [m for m in reversed(merges)
            if not (since_ts and m["ts"] < float(since_ts) - CLOCK_SLACK_S)
            and int(lot) in partial_lots(m["message"])]


def merges_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                  hash_refs: bool = False) -> list[dict]:
    """Les commits de fusion postérieurs à `since_ts` (à `CLOCK_SLACK_S` près)
    qui désignent le lot (`cited_lots`), du plus ancien au plus récent, avec
    `how` (L118b : le premier dont la PR n'est pas partielle ferme le lot)."""
    out = []
    for merge in reversed(merges):           # du plus ancien au plus récent
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            out.append(dict(merge, how=how))
    return out


def _citation(merge: dict, lot: int, target: str, *, sha: bool = True) -> str:
    """Comment la fusion a été constatée, en clair (compte rendu ; sans le
    commit, `sha=False`, pour la note du lot, qui le porte déjà)."""
    title = (merge["message"].splitlines() or [""])[0].strip()
    return "merge commit %sof %s that %s (\"%s\")" % (
        "%s " % merge["sha"][:12] if sha else "", target,
        "carries \"ameesh-work: %d\"" % lot if merge["how"] == "ameesh-work"
        else "merges the branch lot/%d-…" % lot if merge["how"] == "lot-branch"
        else "cites #%d in its title (%s %s)" % (lot, LOT_REF_GIT_KEY, LOT_REF_HASH),
        title[:80] + ("…" if len(title) > 80 else ""))


def implicit_branch(repo: str, lot: int) -> str | None:
    """L118b : la branche `lot/<id>` ou `lot/<id>-…` d'un lot qui n'en porte
    pas, locale ou `origin/`, si elle est UNIQUE (deux candidates : on ne
    devine pas)."""
    out = _out(repo, "for-each-ref", "--format=%(refname)",
               "refs/heads/lot/%d" % lot, "refs/heads/lot/%d-*" % lot,
               "refs/remotes/origin/lot/%d" % lot, "refs/remotes/origin/lot/%d-*" % lot)
    names = set()
    for ref in (out or "").split():
        for prefix in ("refs/heads/", "refs/remotes/origin/"):
            if ref.startswith(prefix):
                names.add(ref[len(prefix):])
    return next(iter(names)) if len(names) == 1 else None


def github_slug(repo: str) -> str | None:
    """`owner/repo` du dépôt `origin` s'il est sur GitHub, sinon None."""
    url = (_out(repo, "remote", "get-url", "origin") or "").strip()
    match = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
                     r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def gh_pr_body(repo: str, number: int) -> str:
    """Le corps de la PR `number` du dépôt GitHub de `repo` (`gh`) ; lève
    GitError s'il est illisible."""
    from . import plan_github
    slug = github_slug(repo)
    if not slug:
        raise GitError("origin of %s is not a GitHub repository" % repo)
    try:
        data = plan_github._json(plan_github.Gh(), ["pr", "view", str(int(number)),
                                                    "--repo", slug, "--json", "body"])
    except plan_github.GithubError as exc:
        raise GitError(str(exc)) from exc
    return (data or {}).get("body") or ""


def _moved_past(repo: str, seen: str | None, seen_ts: float | None, tip: str,
                merge: dict, merge_seen_ts: float | None) -> bool:
    """#312 r2, L118d : la branche a-t-elle livré du travail au-delà de la
    fusion partielle `merge` ? Il faut que le relevé l'ait vue en avance sur
    sa cible avec une pointe `seen` (`branch_head`) qui n'était pas dans la
    branche fusionnée (second parent) ni dans la cible d'alors, puis la voir
    entrée dans la cible (ancêtre de la pointe fusionnée `tip`). Une branche
    seulement avancée jusqu'à la cible, sans travail nouveau, n'y suffit pas :
    son nouveau sommet est la fusion partielle elle-même ou la suite de la
    cible. #346 : la pointe doit avoir été relevée (`seen_ts`) après que le
    relevé a vu la fusion (`merge_seen_ts`), deux instants de l'horloge de la
    base, jamais une date git, que son auteur choisit ; et toute erreur de git
    laisse le lot ouvert (« non ancêtre » : code 1 seulement)."""
    parents = merge.get("parents") or []
    if len(parents) < 2 or not seen or seen_ts is None or merge_seen_ts is None \
            or float(seen_ts) <= float(merge_seen_ts):
        return False
    for outside in (parents[1], merge["sha"]):
        # ni dans la branche fusionnée, ni dans la cible d'alors
        if _git(repo, "merge-base", "--is-ancestor", seen, outside).returncode != 1:
            return False
    return _git(repo, "merge-base", "--is-ancestor", seen, tip).returncode == 0


def _read_pr_body(pr_body, repo: str, number: int, sweep: dict):
    """#312 r1 : le corps de la PR, ou la GitError qui le rend illisible —
    sans appel pendant le recul de cette PR, après un échec de `gh` sur ce
    dépôt dans ce relevé, ni au-delà de `MAX_PR_BODY_READS` lectures : une
    PR toujours illisible ne cache pas longtemps les suivantes."""
    import time
    if (repo, number) in _pr_body_cache:
        return _pr_body_cache[(repo, number)]
    now = time.time()
    until, delay = _pr_body_backoff.get((repo, number), (0.0, 0.0))
    if now < until:
        return GitError("gh failed on this PR; next read in %ds" % int(until - now + 0.999))
    if repo in sweep["failed"]:
        return GitError("gh already failed in this sweep; the next sweep goes on")
    if sweep["reads"] >= MAX_PR_BODY_READS:
        return GitError("%d PR bodies read in this sweep; the next sweep goes on"
                        % MAX_PR_BODY_READS)
    sweep["reads"] += 1
    try:
        body = pr_body(repo, number)
    except GitError as exc:
        delay = min(delay * 2, PR_BODY_BACKOFF_MAX_S) if delay else PR_BODY_BACKOFF_S
        _pr_body_backoff[(repo, number)] = (time.time() + delay, delay)
        sweep["failed"].add(repo)
        return exc
    _pr_body_backoff.pop((repo, number), None)
    if len(_pr_body_cache) >= PR_BODY_CACHE_MAX:
        _pr_body_cache.pop(next(iter(_pr_body_cache)))     # le plus ancien lu
    _pr_body_cache[(repo, number)] = body
    return body


def sync_branches(db, *, host: str | None = None, agents=None, dry_run: bool = False,
                  actor: str = "", limit: int = MAX_BRANCH_LOTS, pr_body=None) -> dict:
    """L118 : ferme les lots ouverts dont la fusion dans leur cible est constatée.

    Examinés : les lots ouverts qui portent une branche ou ont un assigné.
    Le dépôt d'un lot est le dossier de travail de son assigné au registre ;
    avec `host`, seuls les assignés de cet hôte sont examinés (les dossiers
    des autres hôtes n'y sont pas), avec `agents`, seulement ceux-là. Preuve :
    la branche du lot (`branch_probe`), sinon un commit de fusion de la cible
    qui désigne le lot (`ameesh-work: <id>`, ou `#<id>` si le dépôt l'active :
    `merge_citing`). La fermeture passe par `work.close_merged` (idempotente,
    jamais de réouverture) avec le commit de fusion et une note qui dit comment
    elle a été constatée, et s'écrit dans le fil du lot. Rend le compte rendu.

    L118b : un lot sans branche prend pour branche implicite son unique
    `lot/<id>[-…]` (`implicit_branch`), et la fusion GitHub d'une branche
    `lot/<id>-…` le désigne (« Merge pull request #N from owner/lot/<id>-… »).
    Le corps de cette PR (`pr_body(repo, N)`, défaut : `gh`) est lu : une PR
    qui se déclare partielle (`ameesh-work-partial: <id>`) ne ferme pas le
    lot, et un corps illisible le laisse ouvert (un lot fermé n'est jamais
    rouvert : on ne ferme pas sans savoir)."""
    import os

    from . import fil, registry

    st = storage.of(db)
    rows = st.work.open_for_sweep(limit)
    # les commits de fusion lus une fois par dépôt et par cible : depuis le
    # lot ouvert le plus ancien, chaque lot ne retient que les siens
    since_all = min((float(r["created_ts"]) - CLOCK_SLACK_S for r in rows
                     if r.get("created_ts")), default=None)
    targets: dict = {}
    hashes: dict = {}
    merges: dict = {}
    regs: dict = {}                          # une lecture du registre par assigné
    bodies: dict = {}                        # (dépôt, PR) → corps, ou l'erreur
    sweep = {"reads": 0, "failed": set()}    # lectures de corps de ce relevé
    pr_body = pr_body or gh_pr_body
    results: list[dict] = []
    for row in rows:
        lot = int(row["id"])
        branch = row.get("branch") or None
        assignee = (row.get("assignee") or "").strip()
        entry = {"work_item": lot, "branch": branch,
                 "target": row.get("branch_target"), "assignee": assignee or None,
                 "result": "skipped", "how": None, "ref": None, "detail": ""}
        if agents is not None and assignee not in agents:
            continue
        if assignee and ":" not in assignee and assignee not in regs:
            regs[assignee] = registry.get(db, assignee)
        reg = regs.get(assignee) if assignee and ":" not in assignee else None
        if reg is None:
            if host is None and branch:
                entry["detail"] = "no agent assigned: repository unknown"
                results.append(entry)
            continue
        if host is not None and (reg.get("host") or "") != host:
            continue
        repo = os.path.expanduser(reg.get("cwd") or "")
        if not repo or not os.path.isdir(repo):
            entry.update(result="no-repo", detail="working directory of %s missing (%s)"
                         % (assignee, repo or "not declared"))
            results.append(entry)
            continue
        try:
            if row.get("branch_target"):
                target = row["branch_target"]
            else:
                if repo not in targets:
                    try:
                        targets[repo] = default_target(repo)
                    except MergeProbeError as exc:
                        targets[repo] = exc
                if isinstance(targets[repo], MergeProbeError):
                    raise targets[repo]
                target = targets[repo]
            if not target:
                entry.update(result="no-target", detail="no target: `git config %s "
                             "<branch>` in %s, or --target" % (TARGET_GIT_KEY, repo))
                results.append(entry)
                continue
            entry["target"] = target
            found = None
            implicit = False
            if not branch:
                branch = implicit_branch(repo, lot)
                implicit = bool(branch)
                entry["branch"] = branch
            if branch:
                found = branch_probe(repo, branch, target, seen_head=row.get("branch_head"),
                                     since_ts=row.get("created_ts"))
            if repo not in hashes:
                hashes[repo] = lot_ref_hash(repo)
            if (repo, target) not in merges:
                merges[(repo, target)] = merge_commits(repo, target, since_all)
            citing = merges_citing(merges[(repo, target)], lot,
                                   since_ts=row.get("created_ts"), hash_refs=hashes[repo])
            if row.get("branch"):
                # #312 r1 : une branche enregistrée est seule à compter ; la
                # fusion d'une autre `lot/<id>-…` ne ferme pas le lot
                mine = short_branch(row["branch"])
                citing = [m for m in citing if m["how"] != "lot-branch"
                          or merge_branch(m["message"]) == mine]
            # L118b : une fusion GitHub de `lot/<id>-…` ne ferme le lot que si
            # sa PR ne se déclare pas partielle ; la première qui livre tout
            unreadable = None                # pourquoi une PR reste illisible
            partial = None                   # la dernière fusion partielle, et pourquoi
            cited = None
            # #312 r1 : une fusion locale qui se déclare partielle retient
            # aussi la preuve par la branche
            for merge in merges_partial(merges[(repo, target)], lot,
                                        since_ts=row.get("created_ts")):
                partial = (merge, "merge commit %s declares itself partial "
                           "(\"ameesh-work-partial: %d\"): lot kept open"
                           % (merge["sha"][:12], lot))
            for merge in citing:
                number = merge_pr_number(merge["message"])
                if not number:
                    cited = merge
                    break
                if (repo, number) not in bodies:
                    bodies[(repo, number)] = _read_pr_body(pr_body, repo, number, sweep)
                body = bodies[(repo, number)]
                if isinstance(body, GitError):
                    # #312 r1 : une PR plus récente lisible et complète peut
                    # encore fermer le lot
                    unreadable = unreadable or (
                        "PR #%d merged, but its body is unreadable (%s): not closed until "
                        "it says whether it is partial" % (number, body))
                    continue
                if lot in partial_lots(body):
                    if partial is None or merge["ts"] >= partial[0]["ts"]:
                        partial = (merge, "PR #%d declares itself partial "
                                   "(\"ameesh-work-partial: %d\"): lot kept open"
                                   % (number, lot))
                    continue
                cited = merge
                break
            # une branche enregistrée déjà prouvée fusionnée ferme comme avant
            # quand le corps est illisible (hôte sans gh) : seule la preuve
            # implicite attend de savoir ; une fusion partielle retient toujours
            proven = bool(found and found["merged"] and row.get("branch"))
            if partial and partial[0]["sha"] != row.get("partial_merge"):
                # #346 r2 : l'instant où le relevé voit la fusion partielle
                # pour la première fois, à l'horloge de la base ; #403 : le
                # dry-run juge comme le vrai relevé, sans écrire
                if not dry_run:
                    st.work.set_partial_seen(lot, partial[0]["sha"])
                row = dict(row, partial_merge=partial[0]["sha"], partial_seen_ts=None)
            if partial and found and found["merged"] and found["tip"] \
                    and _moved_past(repo, row.get("branch_head"), row.get("branch_head_ts"),
                                    found["tip"], partial[0], row.get("partial_seen_ts")):
                # #312 r2 : seule la dernière fusion de la branche compte ; la
                # branche vue depuis en avance, puis fusionnée (avance rapide),
                # a livré la suite (L118d : pas une simple avance jusqu'à la cible)
                partial = None
            held = partial[1] if partial else unreadable
            if held and cited is None and (partial or not proven):
                found = dict(found or {"tip": None, "ahead": False, "ref": None},
                             merged=False, how="partial", detail=held)
            elif found is None or not found["merged"]:
                if cited:
                    found = dict(found or {"tip": None, "ahead": False}, merged=True,
                                 how=cited["how"], ref=cited["sha"],
                                 detail=_citation(cited, lot, target),
                                 note=_citation(cited, lot, target, sha=False))
                elif found is None:
                    found = {"merged": False, "how": None, "ref": None, "tip": None,
                             "ahead": False,
                             "detail": "no merge commit of %s names the lot "
                                       "(line \"ameesh-work: %d\", nor a merge of lot/%d-…%s)"
                                       % (target, lot, lot, ", nor \"#%d\" in a title" % lot
                                          if hashes[repo] else "")}
        except (MergeProbeError, GitError) as exc:
            entry.update(result="error", detail=str(exc))
            results.append(entry)
            continue
        entry.update(how=found["how"], ref=found["ref"], detail=found["detail"], result="open",
                     repo=repo)
        if not found["merged"]:
            stale = row.get("branch_head_ts") is None or (
                row.get("partial_merge") and (row.get("partial_seen_ts") is None
                                              or row["branch_head_ts"] <= row["partial_seen_ts"]))
            if branch and found["ahead"] and found["tip"] and not dry_run \
                    and (found["tip"] != row.get("branch_head") or stale):
                # #346 r2 : une pointe inchangée relevée avant la fusion
                # partielle l'est de nouveau, puisqu'elle est encore là
                if implicit:
                    # L118b : la branche implicite devient celle du lot (journal)
                    try:
                        work_mod.set_branch(db, lot, branch, actor=actor or "git:%s" % target,
                                            source="the merge sweep: lot/%d branch" % lot)
                    except work_mod.WorkError:
                        pass
                st.work.set_branch_head(lot, branch, found["tip"])
            results.append(entry)
            continue
        if dry_run:
            entry["result"] = "would-merge"
            results.append(entry)
            continue
        if found.get("note"):
            source = "the merge sweep, %s" % found["note"]
        else:
            source = "branch %s (%s)" % (branch, found["how"])
        done = work_mod.close_merged(
            db, lot, sha=found["ref"] or "", actor=actor or "git:%s" % target, source=source)
        entry.update(result=done["result"], detail="%s — %s" % (found["detail"], done["detail"]))
        if done["result"] == "merged":
            text = ("Lot #%d \"%s\" delivered: %s (%s, commit %s). Closed by the runner's "
                    "merge sweep."
                    % (lot, row.get("title") or "",
                       "a merge commit of %s names it" % target if found.get("note")
                       else "branch %s entered %s" % (branch, target),
                       found["how"], (found["ref"] or "?")[:12]))
            fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
                       text=text, project=fil.project_for(db.cfg, fil.agent_project(reg)),
                       lot=str(lot), meta={"kind": "event"})
        results.append(entry)
    return {"host": host, "dry_run": bool(dry_run), "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged")}
