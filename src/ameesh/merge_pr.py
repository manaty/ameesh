# SPDX-License-Identifier: AGPL-3.0-only
"""REL-11 (#1003) — fusion d'une PR touchant `.github/workflows/**` hors API gh.

Pourquoi : GitHub refuse (`gh pr merge`, HTTP 403) la fusion d'une PR dont le
diff touche `.github/workflows/**` quand le jeton de l'hôte n'a pas le scope
OAuth `workflow` (incident #562/REL-11 : la PR de release a été fusionnée par
un commit de fusion local poussé en SSH, la PR est passée MERGED). Le geste
est reproductible, mais il doit être encadré : il écrit directement sur la
branche cible, donc il peut contourner ce que l'API vérifie (protection de
branche, checks requis).

    ameesh work merge-pr <pr> --head <sha gelé> --lot N [--repo owner/repo]
                          [--target branche] [--fetch-url URL] [--push-url URL]
                          [--dry-run] [--json]

Le geste, et ses gardes (toutes vérifiées AVANT toute écriture) :

1. **une seule identité de dépôt** (#1003 B/P1, R1 delta puis delta cx3) : le
   checkout, le `--repo` de la PR, `--fetch-url` et `--push-url` doivent
   désigner le MÊME dépôt — clé `owner/repo` pour GitHub, chemin réel pour un
   transport local — et le checkout doit porter cette identité (son `origin`).
   Les deux formes ne se mélangent pas : un transport local ne prouve pas une
   clé GitHub (ni l'inverse), c'est le même refus. Une divergence, ou une
   identité non vérifiable (pas d'`origin`, URL inconnue), refuse avant la
   première lecture : depuis un clone de A, `--repo B` avec les remotes de B
   ne peut pas faire admettre A par les conditions de B, ni l'inverse ;
2. **le lot** (`--lot`, obligatoire) est le porteur de l'autorisation : il doit
   être gelé sur `--head` et porter un verdict de relecture `ok` sur ce même
   commit (classe lue par la file : un lot léger en est dispensé). C'est
   l'admission **de la file de fusion elle-même** (`merge_queue.readiness`)
   qui est rejouée ici — cible protégée, gel, verdict, classe, CI verte sur la
   tête gelée — et non une copie : cette voie n'est jamais plus faible que la
   file ;
3. **le projet** est résolu par la configuration radar (`merge_queue.
   radar_project`) : ses conditions (`protected`, `ci_required`, `gates`)
   s'appliquent. Une cible protégée est refusée (c'est le geste de
   l'orchestrateur, hors de cette commande) ; `ci_required` exige la CI verte
   du commit gelé (`radar.commit_ci`), et les `gates` du projet tournent sur
   l'arbre fusionné avant le push ;
4. **la PR** est lue par `gh` : elle est ouverte, non brouillon, sa base est
   la cible, sa tête est le SHA gelé donné, son `mergeStateStatus` est
   `CLEAN`/`HAS_HOOKS` et tous ses checks sont terminés et verts
   (`radar.ci_green`) — rien n'est affaibli : une PR bloquée, en retard
   (`BEHIND`) ou dont un check n'est pas vert est refusée ;
5. **le diff** est calculé localement (`merge-base` puis `diff --name-only`) :
   la PR doit toucher au moins un chemin `.github/workflows/**` — cette voie
   ne remplace donc jamais la fusion par l'API pour les autres PR ;
6. **le SHA gelé** est relu sur le dépôt distant (`refs/pull/<pr>/head`) et
   doit être exactement `--head` ;
7. **la cible n'a pas bougé** : la tête fraîche de la cible est un ancêtre du
   gel (`merge-base --is-ancestor`) ; sinon rien n'est écrit et il faut
   refusionner la cible dans la branche, refaire CI et relecture ;
8. **l'arbre du commit de fusion est identique à l'arbre du gel** : le
   `merge --no-ff` local doit produire exactement l'arbre relu ; deux
   parents (tête de cible, gel), message qui nomme la PR et les gardes ;
9. **garde fraîche juste avant l'écriture** : la PR est relue une seconde
   fois (état, tête, base, mergeabilité, checks), l'admission de la file est
   rejouée sur cette vue, et `refs/pull/<pr>/head` comme la tête de la cible
   sont relus sur le distant. Toute mutation depuis la construction du commit
   de fusion arrête le geste sans écrire. La fenêtre entre cette dernière
   lecture et le push n'est pas fermable par git seul : le compare-and-swap
   ci-dessous ferme la course sur la cible, pas une fermeture de PR ou un
   check devenu rouge dans cet instant — la relecture finale le constate et
   ne déclare jamais le lot sur une PR non `MERGED` ;
10. **le push est un compare-and-swap** : `--force-with-lease=<ref>:<tête>`
    sur un remote **hors jeton** (SSH) ; si la cible a bougé entre la lecture
    et le push, git refuse — jamais `--force` ;
11. **relecture** : la PR est relue jusqu'à `MERGED`, son commit de fusion doit
    être celui poussé, la tête de la cible relue doit être ce commit ; le lot
    n'est déclaré fusionné qu'après cette relecture.

An **already `MERGED`** PR is bound by the same link (#1003 N1, P2 blocker):
its content evidence (`refs/pull/<pr>/head`, ancestor of the target) is not
enough to declare a lot. `--lot` must carry the freeze of that head, on the
PR's branch and target; a missing, closed or frozen-elsewhere lot, or one on
another branch/target, refuses without declaring anything. `--dry-run` never
declares, `already` included.

Le remote de lecture peut rester celui du jeton (lire un workflow ne demande
pas le scope) ; seul le push doit être hors jeton. `--dry-run` déroule toutes
les gardes (y compris la garde fraîche) et construit le commit de fusion sans
rien pousser ni déclarer : c'est la vérification que la relecture peut exiger
d'un geste de fusion.

Sans remote hors jeton (pas de clé SSH sur l'hôte), la commande s'arrête
avant toute écriture et nomme la décision owner : `gh auth refresh -s
workflow`, PAT fine-grained `Workflows: write`, GitHub App, ou une clé SSH
de déploiement. Voir docs/EXPLOITATION.md, section « Fusion hors API d'une PR
touchant .github/workflows/** ».
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from . import land, merge_queue, migrations, plan_git, radar, resolutions, work
from .canon import FETCH_TIMEOUT, GitError

#: le chemin qui fait refuser la fusion par l'API
WORKFLOW_PREFIX = ".github/workflows/"
#: identité du commit de fusion, reproductible ; GitHub attribue le push à la
#: clé SSH, pas à cette identité
IDENTITY = ("ameesh", "ameesh@localhost")
#: relecture « PR MERGED » après le push : nombre d'essais et attente
POLL_S = float(os.environ.get("AMEESH_MERGE_PR_POLL_S", "5"))
ATTEMPTS = int(os.environ.get("AMEESH_MERGE_PR_ATTEMPTS", "12"))
#: `git ls-remote`/`rev-parse` courts ; le fetch et le push ont FETCH_TIMEOUT
SHORT_TIMEOUT = 60.0
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HTTPS = re.compile(r"^https?://", re.I)
#: une URL qui porte des identifiants (`https://user:jeton@…`) : jamais
#: affichée telle quelle, ni dans un constat ni dans une erreur
_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")
#: une clé GitHub nue `owner/repo` (le `--repo` de la PR) : exactement deux
#: segments, sans espaces ni chemin absolu
_GITHUB_KEY = re.compile(r"^[^/\s]+/[^/\s]+$")

def _redact(text: str) -> str:
    """Retire les identifiants d'une URL avant de la citer ou de l'écrire."""
    return _CREDENTIALS.sub(r"\1***@", text or "")


class MergePrError(RuntimeError):
    """Un geste refusé ou impossible : message actionnable, rien n'est écrit."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _json(proc, what: str) -> dict:
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MergePrError("%s: sortie illisible (%s)" % (what, exc)) from exc
    if not isinstance(data, dict):
        raise MergePrError("%s: sortie inattendue %s" % (what, (proc.stdout or "")[:120]))
    return data


def _run(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT, stdin: bytes | None = None):
    return resolutions._run(cwd, *args, timeout=timeout, stdin=stdin)


def _check(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT) -> str:
    proc = _run(cwd, *args, timeout=timeout)
    if proc.returncode != 0:
        raise MergePrError("git %s: %s" % (args[0] if args else "?",
                                           _redact(resolutions._err(proc)[-300:])))
    return resolutions._text(proc)


def _out(cwd: str, *args: str, timeout: float = SHORT_TIMEOUT) -> str:
    proc = _run(cwd, *args, timeout=timeout)
    return resolutions._text(proc) if proc.returncode == 0 else ""


def _same(sha, other) -> bool:
    return bool(sha) and bool(other) and str(sha).lower() == str(other).lower()


def check_push_url(url: str) -> str:
    """Le remote de push : jamais celui du jeton sur github.com (https), qui
    est exactement ce qui manque du scope `workflow`."""
    text = (url or "").strip()
    if not text:
        raise MergePrError("no push remote: give --push-url (an SSH remote whose key can "
                           "write) or fix the token scope and use `gh pr merge`")
    if _HTTPS.match(text) and migrations.github_repo(text):
        raise MergePrError(
            "push URL %s goes through the token (https): that is the path GitHub "
            "refuses for a workflow PR; give the SSH remote (git@github.com:owner/repo.git)"
            % _redact(text))
    return text


def check_head(head: str) -> str:
    text = (head or "").strip().lower()
    if not FULL_SHA.match(text):
        raise MergePrError("--head: the full frozen commit (40 hex), not %r" % (head,))
    return text


def _branch_name(target: str) -> str:
    """`origin/main`, `refs/heads/main`, `main` : le même nom de branche."""
    text = (target or "").strip()
    if text.startswith("refs/heads/"):
        text = text[len("refs/heads/"):]
    if text.startswith("origin/"):
        text = text.split("/", 1)[1]
    return text


def _pr_guards(view: dict, pr: int, head: str, target: str) -> None:
    """Les gardes portées par la vue de la PR, rejouées avant toute écriture."""
    state = (view.get("state") or "").upper()
    if state != "OPEN":
        raise MergePrError("PR #%d is %s, not open: nothing to merge" % (pr, state or "?"))
    if view.get("isDraft"):
        raise MergePrError("PR #%d is a draft: mark it ready for review first" % pr)
    if not _same(view.get("headRefOid"), head):
        raise MergePrError("PR #%d head moved: it is at %s, the freeze is %s; nothing "
                           "written — refreeze and re-review the new head"
                           % (pr, (view.get("headRefOid") or "?")[:12], head[:12]))
    base = (view.get("baseRefName") or "").strip()
    if base and _branch_name(base) != _branch_name(target):
        raise MergePrError("PR #%d targets %s, not %s" % (pr, base, target))
    merge_state = (view.get("mergeStateStatus") or "").upper()
    if merge_state not in ("CLEAN", "HAS_HOOKS"):
        raise MergePrError(
            "PR #%d is not mergeable now (mergeStateStatus %s): the direct path never "
            "weakens what the API checks — required checks, reviews or the base state "
            "must be settled first" % (pr, merge_state or "?"))
    if not radar.ci_green(view.get("statusCheckRollup") or []):
        raise MergePrError("PR #%d: the checks on %s are not all concluded and green"
                           % (pr, head[:12]))


def _project_of(cfg, repo: str):
    """Le projet radar du dépôt, ou None (comme la file : pas de conditions
    déclarées). Une configuration illisible ou ambiguë refuse, jamais ne
    suppose l'absence de conditions."""
    try:
        return merge_queue.radar_project(cfg, repo)
    except merge_queue.QueueError as exc:
        raise MergePrError("cannot read the landing conditions of %s: %s" % (repo, exc)) \
            from exc


def _admission(db, cfg, lot: int, repo: str, project, owner: str, view: dict) -> dict:
    """L'admission de la file de fusion, rejouée telle quelle (#1003 B1) :
    gel, verdict `ok` sur la tête (classe lue sur le diff gelé), cible non
    protégée, CI verte de la PR sur cette tête, `ci_required` du projet. La
    clone radar est le dépôt de travail de ce geste : `readiness` y lit la
    branche du lot sur le distant, jamais dans le checkout de l'auteur."""
    branch = (view.get("headRefName") or "").strip()

    def ci_status(name: str):
        if not branch or name != branch:
            return None
        return {"number": view.get("number"), "url": view.get("url") or "",
                "head": (view.get("headRefOid") or "").lower(),
                "green": radar.ci_green(view.get("statusCheckRollup") or []),
                "draft": bool(view.get("isDraft"))}

    clone = resolutions.Replayer(repo, owner)
    try:
        ready = merge_queue.readiness(db, lot, repo, ci=ci_status, clone=clone,
                                      project=project, cfg=cfg)
    except merge_queue.QueueError as exc:
        raise MergePrError("lot #%d is not admitted by the merge queue: %s" % (lot, exc)) \
            from exc
    return ready


def _ls_remote_ref(owner: str, url: str, ref: str) -> str:
    """La valeur d'une ref sur le distant, en minuscules ; "" si illisible."""
    text = _out(owner, "ls-remote", url, ref)
    return text.split()[0].lower() if text.split() else ""


def _remote_identity(url: str):
    """L'identité d'un remote (#1003 B/P1) : `("github", owner/repo)` quand
    l'URL désigne un dépôt GitHub, `("path", chemin réel)` quand c'est un
    transport local (`file://` compris), None quand elle n'est pas
    vérifiable — une identité non vérifiable ne s'accepte pas."""
    text = (url or "").strip()
    if not text:
        return None
    slug = migrations.github_repo(text)
    if slug:
        return ("github", migrations.repo_key(slug))
    path = text[len("file://"):] if text.startswith("file://") else text
    if os.path.isabs(path) and os.path.exists(path):
        return ("path", os.path.realpath(path))
    return None


def _slug_identity(slug: str):
    """L'identité du dépôt que `--repo` désigne : `("path", chemin réel)`
    quand c'est un chemin local existant, `("github", owner/repo)` quand
    c'est une clé GitHub nue, None quand elle n'est pas vérifiable. Une
    identité non vérifiable ne s'accepte pas, et deux formes différentes ne
    se lient jamais l'une à l'autre : un transport local n'est pas la preuve
    d'une clé GitHub (R1 delta, cx3, 2026-10-13)."""
    text = (slug or "").strip()
    local = _remote_identity(text)
    if local is not None:
        return local
    if _GITHUB_KEY.match(text):
        return ("github", migrations.repo_key(text))
    return None


def _bind_repo_identity(repo: str, slug: str, fetch_url: str, push_url: str) -> tuple:
    """Une seule identité de dépôt pour la PR, le lot, l'admission, la lecture
    et le push (#1003 B/P1).

    Le checkout (`repo`), `--fetch-url` et `--push-url` doivent désigner le
    MÊME dépôt : clé `owner/repo` pour GitHub, chemin réel pour un transport
    local. Le `slug` de la PR doit désigner cette même identité, dans la même
    forme : depuis un clone de A, `--repo B` avec des remotes de A est refusé
    comme une divergence, même quand A et B partagent les mêmes commits ; un
    transport local avec une clé GitHub n'est pas vérifiable et refuse aussi.
    Sans `origin` lisible, rien n'est vérifiable : le geste refuse avant toute
    lecture de PR, toute admission et toute écriture."""
    origin_url = plan_git._out(repo, "remote", "get-url", "origin") or ""
    origin = _remote_identity(origin_url)
    if origin is None:
        raise MergePrError(
            "cannot verify the repository of this checkout (%s): its origin is missing or "
            "unreadable — run the gesture from the checkout of the repository it merges; "
            "nothing written" % (_redact(origin_url) or "no origin"))
    for what, url in (("--fetch-url", fetch_url), ("--push-url", push_url)):
        ident = _remote_identity(url)
        if ident is None:
            raise MergePrError("cannot verify the identity of %s (%s): nothing written"
                               % (what, _redact(url)))
        if ident != origin:
            raise MergePrError(
                "%s (%s) and this checkout's origin (%s) are not the same repository: the "
                "lot, its admission, the PR read and the push must be one repository; "
                "nothing written" % (what, _redact(url), _redact(origin_url)))
    slug_ident = _slug_identity(slug)
    if slug_ident is None:
        raise MergePrError(
            "cannot verify the repository named by --repo %s: give owner/repo or the "
            "checkout's real path; nothing written" % (slug,))
    if slug_ident != origin:
        raise MergePrError(
            "--repo %s and this checkout's origin (%s) are not the same repository: the PR "
            "read by gh and the repository whose guards admit and whose branch is pushed "
            "must be one; nothing written" % (slug, _redact(origin_url)))
    return origin


def pr_view(slug: str, pr: int, gh=land._gh) -> dict:
    """La PR lue par `gh` : état, base, tête, mergeabilité, checks."""
    proc = gh(["pr", "view", str(pr), "--repo", slug, "--json",
               "number,state,isDraft,baseRefName,headRefOid,headRefName,mergeStateStatus,"
               "mergeCommit,statusCheckRollup,url,title,autoMergeRequest"], timeout=60.0)
    if proc.returncode != 0:
        raise MergePrError("gh pr view #%s: %s"
                           % (pr, (proc.stderr or proc.stdout or "").strip()[:300]))
    return _json(proc, "gh pr view #%s" % pr)


def message(pr: int, head: str, target: str, tip: str, tree: str, *,
            title: str = "", head_ref: str = "", lot: int | None = None) -> str:
    """Le message du commit de fusion : la PR, les gardes, le lot."""
    subject = "Merge PR #%d%s into %s (fusion hors gh, #1003/REL-11)" % (
        pr, " from %s" % head_ref if head_ref else "", target)
    lines = [subject, ""]
    if title:
        lines += ["%s (tête %s)." % (title, head[:12]), ""]
    lines += [
        "Commit de fusion local : tête gelée == tête de la PR (%s), %s %s ancêtre du gel,"
        % (head[:12], target, tip[:12]),
        "arbre du commit de fusion == arbre du gel (%s), push --force-with-lease "
        "(compare-and-swap)." % tree,
        "L'API gh refuse la fusion d'une PR touchant %s** sans le scope workflow."
        % WORKFLOW_PREFIX,
    ]
    if lot is not None:
        lines += ["", "ameesh-work: %d" % lot]
    return "\n".join(lines) + "\n"


def _scratch() -> str:
    path = tempfile.mkdtemp(prefix="ameesh-merge-pr-")
    _check(path, "init", "-q")
    return path


def _fetch(owner: str, fetch_url: str, target: str, pr: int) -> tuple[str, str]:
    """La tête de la cible et la tête de la PR, lues sur le remote de
    lecture (le ref `refs/pull/<pr>/head` de GitHub, autoritatif)."""
    proc = _run(owner, "fetch", "--quiet", "--no-tags", fetch_url,
                "+refs/heads/%s:refs/ameesh/merge-pr/target" % target,
                "+refs/pull/%d/head:refs/ameesh/merge-pr/head" % pr,
                timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise MergePrError(
            "cannot read %s and refs/pull/%d/head from %s (%s): the read remote must "
            "reach the repository" % (target, pr, _redact(fetch_url),
                                      _redact(resolutions._err(proc)[-300:])))
    tip = _check(owner, "rev-parse", "refs/ameesh/merge-pr/target")
    head = _check(owner, "rev-parse", "refs/ameesh/merge-pr/head")
    return tip, head


def _ls_remote(owner: str, url: str, target: str) -> str:
    text = _out(owner, "ls-remote", url, "refs/heads/%s" % target)
    return text.split()[0].lower() if text.split() else ""


def _already_lot_links(db, lot: int, head: str, view: dict, target: str, repo: str,
                       cfg) -> None:
    """An already-MERGED PR (#1003 N1, P2 blocker): the lot must carry the
    freeze of this head, on the PR's branch and target, before any declaration.

    A PR merged outside this command never passed the queue admission: linking
    it by `--lot` alone would close an unrelated lot. Nothing is written here —
    a missing or different link raises, and `work.merged` is not called. The
    correct case (lot frozen on the head, same branch, same target) keeps its
    previous behaviour."""
    try:
        row = work.get(db, lot)
    except Exception as exc:  # noqa: BLE001 - registre illisible : rien n'est déclaré
        raise MergePrError("lot #%d cannot be read (%s): the merge of an already-MERGED PR "
                           "is not declared on it" % (lot, type(exc).__name__)) from exc
    if row is None:
        raise MergePrError("lot #%d not found: the merge of an already-MERGED PR is not "
                           "declared on it" % lot)
    if (row.get("state") or "").strip() == "closed":
        raise MergePrError("lot #%d is closed (%s): the merge of an already-MERGED PR is not "
                           "declared on it" % (lot, row.get("close_reason") or "closed"))
    # N1 review (cx3): the MERGED view must name the frozen head (headRefOid),
    # not only the fetched ref; a silent view proves nothing.
    oid = (view.get("headRefOid") or "").strip().lower()
    if not oid or not _same(oid, head):
        raise MergePrError("PR #%s head is %s, the freeze is %s: nothing declared"
                           % (view.get("number") or "?", oid[:12] or "?", head[:12]))
    try:
        marks = work.milestones(db, lot)
    except Exception as exc:  # noqa: BLE001 - registre illisible : rien n'est déclaré
        raise MergePrError("lot #%d milestones cannot be read (%s): nothing declared"
                           % (lot, type(exc).__name__)) from exc
    freeze = plan_git.latest_freeze(marks)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    if not frozen:
        raise MergePrError("lot #%d has no frozen commit: the merge of an already-MERGED PR "
                           "is not declared on it" % lot)
    if not _same(frozen, head):
        raise MergePrError("lot #%d is frozen on %s, not on the PR head %s: nothing declared "
                           "(an already-MERGED PR does not close another lot's freeze)"
                           % (lot, frozen[:12], head[:12]))
    branch = (view.get("headRefName") or "").strip()
    lot_branch = (row.get("branch") or "").strip()
    if not branch or not lot_branch or lot_branch != branch:
        raise MergePrError("lot #%d is on branch %s, the PR head branch is %s: nothing "
                           "declared" % (lot, lot_branch or "?", branch or "?"))
    project = None
    if cfg is not None:
        project = _project_of(cfg, repo)
    app = (row.get("app") or "").strip()
    if project is not None and app and app != project.name:
        raise MergePrError("lot #%d is declared on project %s, %s belongs to project %s: "
                           "nothing declared" % (lot, app, repo, project.name))
    expected = (row.get("branch_target") or "").strip()
    if not expected and project is not None:
        expected = (getattr(project, "target", "") or "").strip()
    if not expected:
        try:
            expected = (plan_git.default_target(repo) or "").strip()
        except (GitError, OSError):
            expected = ""
    if not expected or _branch_name(expected) != _branch_name(target):
        raise MergePrError("lot #%d targets %s, the PR targets %s: nothing declared"
                           % (lot, expected or "?", target))


def _already(slug: str, pr: int, head: str, view: dict, *, target: str,
             fetch_url: str, owner: str) -> dict:
    """PR déjà MERGED : on vérifie que le gel est bien dans la cible."""
    tip, fetched = _fetch(owner, fetch_url, target, pr)
    contains = _run(owner, "merge-base", "--is-ancestor", head, tip).returncode == 0
    out = {"state": "already" if contains and _same(fetched, head) else "already-unverified",
           "pr": pr, "url": view.get("url") or "", "target": target, "frozen": head,
           "tip": tip, "merge": ((view.get("mergeCommit") or {}).get("oid") or "").lower(),
           "content_verified": contains,
           "why": "" if contains else
           "the frozen head %s is not an ancestor of %s %s (squash/rebase merge?): "
           "check the merged content by hand" % (head[:12], target, tip[:12])}
    return out


# --------------------------------------------------------------------------
# le geste
# --------------------------------------------------------------------------

def direct_merge(slug: str, pr: int, head: str, *, lot: int, repo: str, db=None, cfg=None,
                 project=None, target: str | None = None,
                 fetch_url: str | None = None, push_url: str | None = None,
                 gh=land._gh, dry_run: bool = False,
                 attempts: int | None = None, poll_s: float | None = None,
                 sleep=time.sleep, work_dir: str | None = None) -> dict:
    """Fusionne la PR `pr` (tête `head`, lot `lot`) sur `target` par commit
    local et push compare-and-swap hors jeton, avec les gardes du module —
    dont l'admission de la file (`merge_queue.readiness`), rejouée avant
    toute écriture puis une seconde fois juste avant le push. Rien n'est
    écrit avant la garde fraîche ; `dry_run` s'arrête avant le push. Rend le
    constat (`state`: merged | already | already-unverified | dry-run |
    pushed-unverified)."""
    head = check_head(head)
    if lot is None:
        raise MergePrError("--lot is required: the frozen lot carries the freeze and the "
                           "review verdict this gesture replays, and the merge is declared "
                           "on it")
    slug = (slug or "").strip()
    if not slug:
        raise MergePrError("no GitHub repository: give --repo owner/repo")
    slug_ident = _slug_identity(slug)
    if slug_ident is None:
        raise MergePrError("cannot verify the repository named by --repo %s: give owner/repo "
                           "or the checkout's real path; nothing written" % (slug,))
    if slug_ident[0] == "github":
        slug = slug_ident[1]
    fetch_url = (fetch_url or "").strip() or ("https://github.com/%s.git" % slug)
    push_url = check_push_url(push_url or ("git@github.com:%s.git" % slug))
    # #1003 B/P1 : une seule identité de dépôt, vérifiée avant la première
    # lecture ; une divergence n'écrit rien et n'admet rien.
    _bind_repo_identity(repo, slug, fetch_url, push_url)
    view = pr_view(slug, pr, gh=gh)
    branch = (view.get("baseRefName") or "").strip()
    target = (target or "").strip() or branch
    try:
        target = plan_git.check_ref(target, "target")
    except ValueError as exc:
        raise MergePrError(str(exc)) from exc
    if branch and _branch_name(branch) != _branch_name(target):
        raise MergePrError("PR #%d targets %s, not %s" % (pr, branch, target))

    owner = work_dir or _scratch()
    temporary = work_dir is None
    try:
        state = (view.get("state") or "").upper()
        if state == "MERGED":
            # N1 (#1003): an already-MERGED PR does not declare a lot without
            # the lot/freeze/branch/target link; nothing is written otherwise.
            _already_lot_links(db, lot, head, view, target, repo, cfg)
            return _already(slug, pr, head, view, target=target, fetch_url=fetch_url,
                            owner=owner)
        if project is None:
            project = _project_of(cfg, repo)
        # #1656: the direct merge replays the whole proof here; an armed
        # auto-merge would merge the head without it — GitHub checks its
        # --match-head-commit only when it is armed, never after a head
        # rewrite. Disarm before any guard: ameesh is the only merge actor.
        if view.get("autoMergeRequest"):
            error = land.pr_disable_auto(slug, pr, gh=gh)
            if error is not None:
                raise MergePrError(
                    "PR #%d: auto-merge armed and not disarmed (%s): nothing written; disarm "
                    "it (gh pr merge --disable-auto %d) and retry" % (pr, error, pr))
        _pr_guards(view, pr, head, target)
        ready = _admission(db, cfg, lot, repo, project, owner, view)
        if not _same(ready.get("frozen_sha"), head):
            raise MergePrError("lot #%d is admitted on %s, not on the freeze %s: refreeze and "
                               "re-review" % (lot, (ready.get("frozen_sha") or "?")[:12],
                                              head[:12]))
        if _branch_name(ready.get("target")) != _branch_name(target):
            raise MergePrError("lot #%d is queued for %s, not %s: merge it on its own target"
                               % (lot, ready.get("target") or "?", target))
        api_base = (view.get("baseRefOid") or "").lower()
        merge_state = (view.get("mergeStateStatus") or "").upper()

        tip, fetched = _fetch(owner, fetch_url, target, pr)
        if not _same(fetched, head):
            raise MergePrError("the remote PR head is %s, the freeze is %s: nothing written"
                               % (fetched[:12], head[:12]))
        base = _out(owner, "merge-base", tip, head)
        if not base:
            raise MergePrError("no common ancestor between %s and %s" % (tip[:12], head[:12]))
        files = [f for f in _check(owner, "diff", "--name-only", base, head).splitlines()
                 if f.strip()]
        touched = [f for f in files if f.startswith(WORKFLOW_PREFIX)]
        if not touched:
            raise MergePrError("PR #%d does not touch %s**: use `gh pr merge` (the API), "
                               "this path is reserved for the workflow-scope refusal"
                               % (pr, WORKFLOW_PREFIX))
        if _run(owner, "merge-base", "--is-ancestor", tip, head).returncode != 0:
            raise MergePrError(
                "%s moved since the freeze (%s is not an ancestor of %s): nothing written — "
                "refusion %s into the branch, rerun the exact CI and the review, freeze again"
                % (target, tip[:12], head[:12], target))

        _check(owner, "checkout", "-q", "--detach", tip)
        proc = _run(owner, "merge", "--no-ff", "--no-commit", "--quiet", head,
                    timeout=FETCH_TIMEOUT)
        if proc.returncode != 0:
            conflicts = [f for f in resolutions.unmerged(owner)]
            _run(owner, "merge", "--abort")
            detail = ", ".join(conflicts[:6]) or _redact(resolutions._err(proc)[-200:])
            raise MergePrError("the merge of %s on %s is not clean (%s): nothing written"
                               % (head[:12], tip[:12], detail))
        tree = _check(owner, "write-tree")
        frozen_tree = _check(owner, "rev-parse", "%s^{tree}" % head)
        if tree != frozen_tree:
            _run(owner, "merge", "--abort")
            raise MergePrError(
                "the merge tree %s differs from the frozen tree %s: the merge would not be a "
                "no-op — nothing written" % (tree[:12], frozen_tree[:12]))
        text = message(pr, head, target, tip, tree, title=(view.get("title") or ""),
                       head_ref=(view.get("headRefName") or ""), lot=lot)
        proc = _run(owner, "-c", "user.name=%s" % IDENTITY[0],
                    "-c", "user.email=%s" % IDENTITY[1],
                    "commit", "--quiet", "--no-verify", "-F", "-", stdin=text.encode(),
                    timeout=SHORT_TIMEOUT)
        if proc.returncode != 0:
            raise MergePrError("cannot write the merge commit: %s"
                               % _redact(resolutions._err(proc)[-300:]))
        merge_sha = _check(owner, "rev-parse", "HEAD")
        parents = _check(owner, "rev-list", "--parents", "-n", "1", merge_sha).split()
        if parents != [merge_sha, tip, head]:
            raise MergePrError("the merge commit %s does not have (%s, %s) as parents: %s"
                               % (merge_sha[:12], tip[:12], head[:12], " ".join(parents)))
        evidence = {"pr": pr, "url": view.get("url") or "", "target": target,
                    "frozen": head, "tip": tip, "api_base": api_base, "merge": merge_sha,
                    "tree": tree, "frozen_tree": frozen_tree,
                    "workflow_files": touched, "changed_files": len(files),
                    "merge_state": merge_state,
                    "admission": {"target": ready.get("target") or "",
                                  "class": ready.get("class") or "",
                                  "class_why": ready.get("class_why") or ""},
                    "message": text}
        # #1003 B1 : les gates du projet radar, sur l'arbre fusionné, comme la
        # file les fait avant de pousser (aucun gate déclaré : rien à lancer).
        if project is not None and (getattr(project, "gates", ())
                                    or getattr(project, "path_gates", ())):
            checked = land.gates(project, owner, files=files)
            if not checked["ok"]:
                _run(owner, "merge", "--abort")
                raise MergePrError("gate `%s` failed on the merged tree: %s"
                                   % (checked["gate"], checked["output"]))
            evidence["gates"] = {"ok": True, "gate": checked["gate"]}
        # #1003 B2 : garde fraîche juste avant l'écriture — la PR, l'admission
        # de la file et les deux refs distantes sont relues ; toute mutation
        # depuis la construction du commit de fusion arrête sans écrire.
        fresh = pr_view(slug, pr, gh=gh)
        _pr_guards(fresh, pr, head, target)
        ready = _admission(db, cfg, lot, repo, project, owner, fresh)
        if not _same(ready.get("frozen_sha"), head) \
                or _branch_name(ready.get("target")) != _branch_name(target):
            raise MergePrError("lot #%d moved during the gesture (admission now %s on %s): "
                               "nothing written — refreeze and re-review"
                               % (lot, (ready.get("frozen_sha") or "?")[:12],
                                  ready.get("target") or "?"))
        remote_pr = _ls_remote_ref(owner, fetch_url, "refs/pull/%d/head" % pr)
        if not _same(remote_pr, head):
            raise MergePrError("the remote PR head is now %s, the freeze is %s: nothing "
                               "written — refreeze and re-review the new head"
                               % (remote_pr[:12] or "?", head[:12]))
        remote_target = _ls_remote(owner, fetch_url, target)
        if not _same(remote_target, tip):
            raise MergePrError("%s moved to %s during the gesture (read %s): nothing written "
                               "— refusion %s into the branch, rerun the exact CI and the "
                               "review, freeze again"
                               % (target, remote_target[:12] or "?", tip[:12], target))
        if dry_run:
            evidence.update(state="dry-run", pushed=False, remote_tip="")
            return evidence

        ref = "refs/heads/%s" % target
        try:
            proc = _run(owner, "push", "--quiet", "--no-verify",
                        "--force-with-lease=%s:%s" % (ref, tip), push_url,
                        "%s:%s" % (merge_sha, ref), timeout=FETCH_TIMEOUT)
            error = "" if proc.returncode == 0 else _redact(resolutions._err(proc)[-300:])
        except GitError as exc:  # sortie du push inconnue : relire la cible
            error = _redact("%s: %s" % (type(exc).__name__, exc))
        remote_tip = _ls_remote(owner, fetch_url, target)
        if error and not _same(remote_tip, merge_sha):
            raise MergePrError("push refused or unreadable (%s); %s is at %s, not %s — nothing "
                               "was written by this call; read it again before retrying"
                               % (error, target, remote_tip[:12] or "?", merge_sha[:12]))

        ok, last, final, merged_sha = False, None, "", ""
        attempts = ATTEMPTS if attempts is None else max(1, int(attempts))
        poll_s = POLL_S if poll_s is None else max(0.0, float(poll_s))
        for attempt in range(attempts):
            last = pr_view(slug, pr, gh=gh)
            final = (last.get("state") or "").upper()
            merged_sha = ((last.get("mergeCommit") or {}).get("oid") or "").lower()
            if final == "MERGED" and _same(merged_sha, merge_sha) \
                    and _same(remote_tip, merge_sha):
                ok = True
                break
            if attempt + 1 < attempts:
                sleep(poll_s)
        remote_tip = _ls_remote(owner, fetch_url, target)
        evidence.update(state="merged" if ok else "pushed-unverified", pushed=True,
                        remote_tip=remote_tip, pr_state=final, pr_merge=merged_sha)
        if not ok:
            evidence["why"] = (
                "pushed %s on %s, but the read-back is not conclusive (PR %s, merged commit "
                "%s, %s tip %s): check the PR and the branch by hand before declaring the lot"
                % (merge_sha[:12], target, final or "?", merged_sha[:12] or "?",
                   target, remote_tip[:12] or "?"))
        return evidence
    finally:
        if temporary:
            shutil.rmtree(owner, ignore_errors=True)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "merge-pr", help="merge a PR touching .github/workflows/** when the token lacks the "
                         "workflow scope (REL-11/#1003): local merge with guards, "
                         "compare-and-swap push over a non-token remote, read-back")
    parser.add_argument("pr", type=int)
    parser.add_argument("--head", required=True,
                        help="the frozen/reviewed commit, full 40-hex SHA")
    parser.add_argument("--repo", default=None, help="GitHub owner/repo (default: origin)")
    parser.add_argument("--target", default=None,
                        help="target branch (default: the PR base)")
    parser.add_argument("--fetch-url", default=None,
                        help="remote read for the PR and the target (default: origin)")
    parser.add_argument("--push-url", default=None,
                        help="non-token remote pushed to (default: git@github.com:owner/repo.git)")
    parser.add_argument("--lot", type=int, required=True,
                        help="the frozen lot that authorizes the gesture (freeze, review "
                             "verdict, admission); the merge is declared on it")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every guard and build the merge commit; push nothing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="")
    parser.set_defaults(func=func)


def _human(out: dict) -> str:
    state = out.get("state")
    if state == "dry-run":
        return ("PR #%s: guards pass, merge commit %s would be pushed on %s (tree %s == frozen "
                "tree %s, %s workflow file(s)); nothing pushed"
                % (out["pr"], out["merge"][:12], out["target"], out["tree"][:12],
                   out["frozen_tree"][:12], len(out.get("workflow_files") or [])))
    if state == "already":
        return ("PR #%s is already MERGED: %s is on %s (%s)"
                % (out["pr"], out["frozen"][:12], out["target"], out["tip"][:12]))
    if state == "already-unverified":
        return "PR #%s is already MERGED but %s" % (out["pr"], out.get("why") or "")
    if state == "merged":
        text = ("PR #%s merged: %s on %s (tree %s == frozen tree %s, push --force-with-lease)"
                % (out["pr"], out["merge"][:12], out["target"], out["tree"][:12],
                   out["frozen_tree"][:12]))
        recorded = out.get("recorded")
        if recorded:
            text += "; lot #%s declared merged" % out.get("lot")
        elif out.get("record_warning"):
            text += "; WARNING lot not declared: %s" % out["record_warning"]
        return text
    return "PR #%s: %s" % (out["pr"], out.get("why") or state)


def run(cfg, db, args) -> int:
    import sys
    try:
        cwd = os.getcwd()
        slug = (args.repo or migrations.origin_repo(cwd) or "").strip()
        if not slug:
            raise MergePrError("cannot read the GitHub repository from origin: give --repo "
                               "owner/repo")
        # la voie opérateur ne fusionne qu'une PR GitHub : un chemin local
        # n'est pas vérifiable contre un slug GitHub (delta cx3) et la
        # commande le refuse, plutôt que de laisser `gh` échouer plus loin.
        slug_ident = _slug_identity(slug)
        if slug_ident is None:
            raise MergePrError("cannot verify the repository named by --repo %s: give "
                               "owner/repo; nothing written" % (slug,))
        if slug_ident[0] != "github":
            raise MergePrError("--repo %s is a local transport: this command merges a GitHub "
                               "PR, give --repo owner/repo; nothing written" % (slug,))
        origin = plan_git._out(cwd, "remote", "get-url", "origin") or ""
        fetch_url = args.fetch_url or origin or ("https://github.com/%s.git" % slug)
        push_url = check_push_url(args.push_url or ("git@github.com:%s.git" % slug))
        out = direct_merge(slug, args.pr, args.head, lot=args.lot, repo=cwd, db=db, cfg=cfg,
                           target=args.target, fetch_url=fetch_url, push_url=push_url,
                           dry_run=args.dry_run)
        # N1 (#1003): `--dry-run` never declares anything, including on an
        # already-MERGED PR (the `already` finding is not a write).
        if out.get("state") in ("merged", "already") and not args.dry_run:
            out["lot"] = args.lot
            sha = out.get("merge") or out.get("tip") or ""
            try:
                if not sha:
                    raise MergePrError("no merge commit to declare")
                out["recorded"] = work.merged(
                    db, args.lot, sha=sha, actor=args.actor,
                    note="ameesh work merge-pr %d --head %s (fusion hors gh, "
                         "#1003/REL-11)" % (args.pr, out["frozen"][:12]))
            except Exception as exc:  # noqa: BLE001 - la fusion est un fait, jamais défaite
                out["record_warning"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else _human(out))
        if out["state"] in ("merged", "already", "dry-run"):
            return 0
        return 1 if out["state"] == "already-unverified" else 2
    except (MergePrError, GitError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
