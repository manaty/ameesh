# SPDX-License-Identifier: AGPL-3.0-only
"""REL-11 (#1003) — fusion d'une PR touchant `.github/workflows/**` hors API gh.

Pourquoi : GitHub refuse (`gh pr merge`, HTTP 403) la fusion d'une PR dont le
diff touche `.github/workflows/**` quand le jeton de l'hôte n'a pas le scope
OAuth `workflow` (incident #562/REL-11 : la PR de release a été fusionnée par
un commit de fusion local poussé en SSH, la PR est passée MERGED). Le geste
est reproductible, mais il doit être encadré : il écrit directement sur la
branche cible, donc il peut contourner ce que l'API vérifie (protection de
branche, checks requis).

    ameesh work merge-pr <pr> --head <sha gelé> [--repo owner/repo]
                          [--target branche] [--fetch-url URL] [--push-url URL]
                          [--lot N] [--dry-run] [--json]

Le geste, et ses gardes (toutes vérifiées AVANT toute écriture) :

1. **la PR** est lue par `gh` : elle est ouverte, non brouillon, sa base est
   la cible, sa tête est le SHA gelé donné, son `mergeStateStatus` est
   `CLEAN`/`HAS_HOOKS` et tous ses checks sont terminés et verts
   (`radar.ci_green`) — rien n'est affaibli : une PR bloquée, en retard
   (`BEHIND`) ou dont un check n'est pas vert est refusée ;
2. **le diff** est calculé localement (`merge-base` puis `diff --name-only`) :
   la PR doit toucher au moins un chemin `.github/workflows/**` — cette voie
   ne remplace donc jamais la fusion par l'API pour les autres PR ;
3. **le SHA gelé** est relu sur le dépôt distant (`refs/pull/<pr>/head`) et
   doit être exactement `--head` ;
4. **la cible n'a pas bougé** : la tête fraîche de la cible est un ancêtre du
   gel (`merge-base --is-ancestor`) ; sinon rien n'est écrit et il faut
   refusionner la cible dans la branche, refaire CI et relecture ;
5. **l'arbre du commit de fusion est identique à l'arbre du gel** : le
   `merge --no-ff` local doit produire exactement l'arbre relu ; deux
   parents (tête de cible, gel), message qui nomme la PR et les gardes ;
6. **le push est un compare-and-swap** : `--force-with-lease=<ref>:<tête>`
   sur un remote **hors jeton** (SSH) ; si la cible a bougé entre la lecture
   et le push, git refuse — jamais `--force` ;
7. **relecture** : la PR est relue jusqu'à `MERGED`, son commit de fusion doit
   être celui poussé, la tête de la cible relue doit être ce commit ; le lot
   (`--lot`) n'est déclaré fusionné qu'après cette relecture.

Le remote de lecture peut rester celui du jeton (lire un workflow ne demande
pas le scope) ; seul le push doit être hors jeton. `--dry-run` déroule 1 à 5
et construit le commit de fusion sans rien pousser ni déclarer : c'est la
vérification que la relecture peut exiger d'un geste de fusion.

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

from . import land, migrations, plan_git, radar, resolutions, work
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


def pr_view(slug: str, pr: int, gh=land._gh) -> dict:
    """La PR lue par `gh` : état, base, tête, mergeabilité, checks."""
    proc = gh(["pr", "view", str(pr), "--repo", slug, "--json",
               "number,state,isDraft,baseRefName,headRefOid,headRefName,mergeStateStatus,"
               "mergeCommit,statusCheckRollup,url,title"], timeout=60.0)
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

def direct_merge(slug: str, pr: int, head: str, *, target: str | None = None,
                 fetch_url: str | None = None, push_url: str | None = None,
                 gh=land._gh, dry_run: bool = False, lot: int | None = None,
                 attempts: int | None = None, poll_s: float | None = None,
                 sleep=time.sleep, work_dir: str | None = None) -> dict:
    """Fusionne la PR `pr` (tête `head`) sur `target` par commit local et push
    compare-and-swap hors jeton, avec les gardes du module. Rien n'est écrit
    avant la garde 5 ; `dry_run` s'arrête avant le push. Rend le constat
    (`state`: merged | already | already-unverified | dry-run |
    pushed-unverified)."""
    head = check_head(head)
    slug = (slug or "").strip().lower()
    if not slug:
        raise MergePrError("no GitHub repository: give --repo owner/repo")
    fetch_url = (fetch_url or "").strip() or ("https://github.com/%s.git" % slug)
    push_url = check_push_url(push_url or ("git@github.com:%s.git" % slug))
    view = pr_view(slug, pr, gh=gh)
    branch = (view.get("baseRefName") or "").strip()
    target = (target or "").strip() or branch
    try:
        target = plan_git.check_ref(target, "target")
    except ValueError as exc:
        raise MergePrError(str(exc)) from exc
    if branch and branch != target:
        raise MergePrError("PR #%d targets %s, not %s" % (pr, branch, target))

    owner = work_dir or _scratch()
    temporary = work_dir is None
    try:
        state = (view.get("state") or "").upper()
        if state == "MERGED":
            return _already(slug, pr, head, view, target=target, fetch_url=fetch_url,
                            owner=owner)
        if state != "OPEN":
            raise MergePrError("PR #%d is %s, not open: nothing to merge" % (pr, state or "?"))
        if view.get("isDraft"):
            raise MergePrError("PR #%d is a draft: mark it ready for review first" % pr)
        if not _same(view.get("headRefOid"), head):
            raise MergePrError("PR #%d head moved: it is at %s, the freeze is %s; nothing "
                               "written — refreeze and re-review the new head"
                               % (pr, (view.get("headRefOid") or "?")[:12], head[:12]))
        api_base = (view.get("baseRefOid") or "").lower()
        merge_state = (view.get("mergeStateStatus") or "").upper()
        if merge_state not in ("CLEAN", "HAS_HOOKS"):
            raise MergePrError(
                "PR #%d is not mergeable now (mergeStateStatus %s): the direct path never "
                "weakens what the API checks — required checks, reviews or the base state "
                "must be settled first" % (pr, merge_state or "?"))
        if not radar.ci_green(view.get("statusCheckRollup") or []):
            raise MergePrError("PR #%d: the checks on %s are not all concluded and green"
                               % (pr, head[:12]))

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
                    "merge_state": merge_state, "message": text}
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
    parser.add_argument("--lot", type=int, default=None,
                        help="also declare the merge on this lot, after the read-back")
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
        slug = (args.repo or migrations.origin_repo(cwd) or "").strip().lower()
        if not slug:
            raise MergePrError("cannot read the GitHub repository from origin: give --repo "
                               "owner/repo")
        origin = plan_git._out(cwd, "remote", "get-url", "origin") or ""
        fetch_url = args.fetch_url or origin or ("https://github.com/%s.git" % slug)
        push_url = check_push_url(args.push_url or ("git@github.com:%s.git" % slug))
        out = direct_merge(slug, args.pr, args.head, target=args.target,
                           fetch_url=fetch_url, push_url=push_url, dry_run=args.dry_run,
                           lot=args.lot)
        if args.lot and out.get("state") in ("merged", "already"):
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
