# SPDX-License-Identifier: AGPL-3.0-only
"""REL-11 (#1003) — merging a PR that touches `.github/workflows/**` outside the gh API.

Why: GitHub refuses (`gh pr merge`, HTTP 403) the merge of a PR whose diff
touches `.github/workflows/**` when the host's token lacks the `workflow`
OAuth scope (incident #562/REL-11: the release PR was merged by a local merge
commit pushed over SSH; GitHub marked the PR MERGED). The gesture is
reproducible, but it must be framed: it writes directly on the target branch,
so it can bypass what the API verifies (branch protection, required checks).

    ameesh work merge-pr <pr> --head <frozen sha> --lot N [--repo owner/repo]
                          [--target branch] [--fetch-url URL] [--push-url URL]
                          [--dry-run] [--json]

The gesture, and its guards (all checked BEFORE any write):

1. **one repository identity** (#1003 B/P1, R1 delta then cx3 delta): the
   checkout, the PR's `--repo`, `--fetch-url` and `--push-url` must designate
   the SAME repository — `owner/repo` key for GitHub, real path for a local
   transport — and the checkout must carry that identity (its `origin`). The
   two forms never mix: a local transport does not prove a GitHub key (nor
   the reverse), it is the same refusal. A divergence, or an unverifiable
   identity (no `origin`, unknown URL), refuses before the first read: from a
   clone of A, `--repo B` with B's remotes cannot make A be admitted by B's
   conditions, nor the reverse;
2. **the lot** (`--lot`, required) carries the authorization: it must be
   frozen on `--head` and carry a review verdict `ok` on that same commit
   (the class is read by the queue: a light lot is exempt). It is the
   admission **of the merge queue itself** (`merge_queue.readiness`) that is
   replayed here — protected target, freeze, verdict, class, green CI on the
   frozen head — and not a copy: this path is never weaker than the queue;
3. **the project** is resolved by the radar configuration
   (`merge_queue.radar_project`): its conditions (`protected`, `ci_required`,
   `gates`) apply. A protected target is refused (that is the orchestrator's
   gesture, outside this command); `ci_required` requires the frozen commit's
   green CI (`radar.commit_ci`), and the project's `gates` run on the merged
   tree before the push;
4. **the PR** is read by `gh`: open, not a draft, its base is the target, its
   head is the given frozen SHA, its `mergeStateStatus` is `CLEAN`,
   `HAS_HOOKS` or `UNSTABLE` — nothing is weakened: a blocked PR or behind
   (`BEHIND`) is refused; on `CLEAN`/`HAS_HOOKS` all its checks are concluded
   and green (`radar.ci_green`), on `UNSTABLE` (#4549) the only tolerated
   non-green checks are optional ones and a run superseded by a newer green
   suite that ran no test (the packet proof and the aggregate detail still
   gate), while every required check is proven by the same gate as the fire;
   the target's **required checks** (#2273) are read like the fire
   does (`fire.required_checks`: branch protection completed by
   `radar.projects.<p>.required_checks`) and must be present and `SUCCESS` on
   the frozen head — an absent `tests` check is not proven by a green
   `couverture`, and a `SKIPPED` check proves nothing; when the protection
   imposes an application, the check runs of the exact head are read for
   their source, so a same-named check from another application or a commit
   status does not satisfy it; an unreadable protection without a
   declaration refuses, and a declared empty list covers only the proven
   GitHub-plan 403 (no protection can exist on that repository), never a
   rights refusal. This path writes outside the API, so it never
   bypasses the protection it cannot see; when the base is not the
   repository's default target, it must not already be merged into that
   default (#2603: a stacked PR would land on a dead branch) — the guard
   refuses before any write and gives the `gh pr edit --base` to run;
5. **the diff** is computed locally (`merge-base` then `diff --name-only`):
   the PR must touch at least one `.github/workflows/**` path — this path
   therefore never replaces the API merge for the other PRs;
6. **the frozen SHA** is read back on the remote repository
   (`refs/pull/<pr>/head`) and must be exactly `--head`;
7. **the target has not moved**: the target's fresh head is an ancestor of
   the freeze (`merge-base --is-ancestor`); otherwise nothing is written and
   the target must be merged back into the branch, CI and review redone;
8. **the merge commit tree is identical to the freeze tree**: the local
   `merge --no-ff` must produce exactly the reviewed tree; two parents
   (target head, freeze), a message naming the PR and the guards;
9. **fresh guard just before the write**: the PR is read a second time
   (state, head, base, mergeability, checks), the queue admission is replayed
   on that view, and `refs/pull/<pr>/head` as well as the target head are
   read again on the remote. Any mutation since the merge commit was built
   stops the gesture without writing. The window between this last read and
   the push cannot be closed by git alone: the compare-and-swap below closes
   the race on the target, not a PR closure or a check turning red in that
   instant — the final read-back notes it and never declares the lot on a
   non-`MERGED` PR;
10. **the push is a compare-and-swap**: `--force-with-lease=<ref>:<head>` on
    a remote **outside the token** (SSH); if the target moved between the
    read and the push, git refuses — never `--force`;
11. **read-back**: the PR is read until `MERGED`, its merge commit must be
    the one pushed, the target head read back must be that commit; the lot is
    declared merged only after that read-back.

An **already `MERGED`** PR is bound by the same link (#1003 N1, P2 blocker):
its content evidence (`refs/pull/<pr>/head`, ancestor of the target) is not
enough to declare a lot. `--lot` must carry the freeze of that head, on the
PR's branch and target; a missing, closed or frozen-elsewhere lot, or one on
another branch/target, refuses without declaring anything. `--dry-run` never
declares, `already` included.

The read remote can stay the token one (reading a workflow does not need the
scope); only the push must be outside the token. `--dry-run` runs every guard
(the fresh one included) and builds the merge commit without pushing or
declaring: it is the verification a review can require from a merge gesture.

Without a remote outside the token (no SSH key on the host), the command
stops before any write and names the owner's decision: `gh auth refresh -s
workflow`, a fine-grained PAT with `Workflows: write` given to the API, a
GitHub App, or an SSH deploy key. See docs/EXPLOITATION.md, section "Merge
outside the gh API of a PR touching .github/workflows/**".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from ... import (fire, land, merge_queue, migrations, packet_guard, packet_reservation,
                 plan_git, radar, resolutions, work)
from ...canon import FETCH_TIMEOUT, GitError

#: the path that makes the API refuse the merge
WORKFLOW_PREFIX = ".github/workflows/"
#: reproducible merge commit identity; GitHub attributes the push to the SSH
#: key, not to this identity
IDENTITY = ("ameesh", "ameesh@localhost")
#: "PR MERGED" read-back after the push: number of attempts and wait
POLL_S = float(os.environ.get("AMEESH_MERGE_PR_POLL_S", "5"))
ATTEMPTS = int(os.environ.get("AMEESH_MERGE_PR_ATTEMPTS", "12"))
#: short `git ls-remote`/`rev-parse` calls; fetch and push use FETCH_TIMEOUT
SHORT_TIMEOUT = 60.0
#: L165 (EP4) : marge de bail renouvelée juste avant le push externe. Elle
#: couvre le pire cas du push (`FETCH_TIMEOUT`) et une lecture de la PR après
#: (`SHORT_TIMEOUT`). La course résiduelle (un réseau qui bloque au-delà) est
#: documentée dans `docs/HOST-HANDOFFS.md` ; le CAS de la cible et
#: `--match-head-commit` restent les dernières gardes.
PILOT_CALL_MARGIN_S = FETCH_TIMEOUT + SHORT_TIMEOUT
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HTTPS = re.compile(r"^https?://", re.I)
#: a URL carrying credentials (`https://user:token@…`): never shown as such,
#: neither in a report nor in an error
_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")
#: a bare GitHub key `owner/repo` (the PR's `--repo`): exactly two segments,
#: no spaces and no absolute path
_GITHUB_KEY = re.compile(r"^[^/\s]+/[^/\s]+$")

def _redact(text: str) -> str:
    """Strip the credentials from a URL before quoting or writing it."""
    return _CREDENTIALS.sub(r"\1***@", text or "")


class MergePrError(RuntimeError):
    """A refused or impossible gesture: actionable message, nothing written."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _json(proc, what: str) -> dict:
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MergePrError("%s: unreadable output (%s)" % (what, exc)) from exc
    if not isinstance(data, dict):
        raise MergePrError("%s: unexpected output %s" % (what, (proc.stdout or "")[:120]))
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
    """The push remote: never the token one on github.com (https), which is
    exactly what lacks the `workflow` scope."""
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
    """`origin/main`, `refs/heads/main`, `refs/remotes/origin/main` and `main`
    all name `main` — the merge queue's normalization, reused here so that a
    `branch_target` written `refs/remotes/origin/main` is not refused against a
    PR based on `main` (#1253, cx3)."""
    return merge_queue._remote_branch((target or "").strip())


def _unstable_ci_ok(rollup, project=None, branch=None, *, slug: str = "",
                    head: str = "", gh=None) -> bool:
    """#4549 : sous `UNSTABLE`, les seuls non-verts tolérés sont les checks
    facultatifs et un run supplanté par une suite verte sans test.

    Les checks **requis** sont déjà prouvés par `_required_refusal` (même
    règle que le feu) ; restent les portes qui ne sont pas des checks requis
    de la forge : les statuts exigés — classés par la même porte que le feu,
    un statut en erreur se consigne et ne bloque pas (N2 de dev1) —, la
    preuve du portier `paquet` et le détail de l'agrégat `tests` gardent
    leurs refus. Un négatif facultatif n'arrête donc pas le geste, comme la
    protection GitHub ne l'exige pas.

    #5298 : un flux de part d'un attempt antérieur n'est remplacé que sur la
    preuve identifiée de `fire.replaced_flux_checks` (même exécution, même
    part, tentative plus récente verte). Sans `slug`/`head`/`gh`, aucune
    preuve n'est lue et le flux rouge bloque (doute fermé)."""
    status_gaps, _consigned = fire._required_status_gate(
        rollup, radar.required_statuses(project, branch))
    if status_gaps:
        return False
    replaced = ()
    try:
        replaced = fire.replaced_flux_checks(rollup, slug=slug, head=head, gh=gh)
    except Exception:  # noqa: BLE001 - une preuve illisible ne verdit jamais
        replaced = ()
    proof_gaps, _ = fire._packet_proof_gate(rollup, replaced)
    if proof_gaps:
        return False
    detail_gaps, _ = fire._aggregate_detail_gate(rollup)
    return not detail_gaps


def _pr_guards(view: dict, pr: int, head: str, target: str, project=None, *,
               slug: str = "", gh=None) -> None:
    """The guards carried by the PR view, replayed before any write.

    #4549 : `UNSTABLE` n'est plus refusé d'emblée. GitHub le rend quand des
    checks facultatifs ou un run supplanté ne sont pas verts, alors que les
    checks requis le sont ; les checks requis restent prouvés par
    `_admission` (`_required_refusal`) et les statuts exigés ici. `BLOCKED`,
    `BEHIND` ou `DIRTY` restent refusés."""
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
    if merge_state not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        raise MergePrError(
            "PR #%d is not mergeable now (mergeStateStatus %s): the direct path never "
            "weakens what the API checks — required checks, reviews or the base state "
            "must be settled first" % (pr, merge_state or "?"))
    branch = (view.get("headRefName") or "").strip() or None
    required = radar.required_statuses(project, branch)
    rollup = view.get("statusCheckRollup") or []
    if merge_state == "UNSTABLE":
        if not _unstable_ci_ok(rollup, project, branch, slug=slug, head=head,
                               gh=gh):
            raise MergePrError(
                "PR #%d: the only tolerated non-green checks under UNSTABLE are "
                "optional ones and a run superseded by a newer green suite; the "
                "packet proof or a required status is still missing" % pr)
        return
    if not radar.ci_green(rollup, required):
        raise MergePrError("PR #%d: the checks on %s are not all concluded and green"
                           % (pr, head[:12]))


def _stacked_base_guard(owner: str, fetch_url: str, repo: str, view: dict,
                        target: str) -> None:
    """#2603 : refuse une PR dont la base, autre que la cible par défaut du
    dépôt, est déjà fusionnée dans cette cible par défaut.

    Fusionner là (comme home#112/#113 après la fusion de canon/2452) pose le
    contenu sur une branche morte : `canon/2452` ne reçoit plus rien dans
    `main`, le contenu est perdu. La garde ne s'applique qu'à une fusion
    demandée SUR cette base ; une PR dont la base est la cible par défaut, ou
    une base non encore fusionnée, passe. Cible par défaut illisible : refus,
    le doute ne fusionne pas sur une base de travail."""
    from ... import plan_git

    pr = int(view.get("number") or 0)
    base = _branch_name(view.get("baseRefName") or "")
    if not base or _branch_name(target) != base:
        return
    try:
        default = _branch_name(plan_git.default_target(repo) or "")
    except plan_git.MergeProbeError as exc:
        raise MergePrError(
            "PR #%d targets %s, and the repository's default target cannot be read (%s): "
            "nothing written — fix ameesh.target or retarget the PR by hand"
            % (pr, base, _redact(str(exc))[:200])) from exc
    if not default:
        raise MergePrError(
            "PR #%d targets %s, and the repository's default target cannot be determined: "
            "nothing written — fetch/configure the default branch or retarget the PR by hand"
            % (pr, base))
    if default == base:
        return
    refs = {"base": "refs/ameesh/merge-pr/base",
            "default": "refs/ameesh/merge-pr/default"}
    proc = _run(owner, "fetch", "--quiet", "--no-tags", fetch_url,
                "+refs/heads/%s:%s" % (base, refs["base"]),
                "+refs/heads/%s:%s" % (default, refs["default"]),
                timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise MergePrError(
            "PR #%d targets %s, not the default target %s, and the default cannot be read "
            "(%s): nothing written — retarget it (`gh pr edit --base %s %d`) or verify "
            "that %s is not already merged into %s by hand"
            % (pr, base, default, _redact(resolutions._err(proc)[-200:]), default, pr,
               base, default))
    compared = _run(owner, "merge-base", "--is-ancestor", refs["base"], refs["default"])
    if compared.returncode not in (0, 1):
        raise MergePrError("PR #%d: comparison of %s with %s unreadable: nothing written"
                           % (pr, base, default))
    if compared.returncode == 0:
        raise MergePrError(
            "PR #%d targets %s, already merged into %s: merging it there would land its "
            "content on a dead branch and lose it — retarget it (`gh pr edit --base %s %d`), "
            "then freeze and re-review" % (pr, base, default, default, pr))


def _project_of(cfg, repo: str):
    """The repository's radar project, or None (like the queue: no declared
    conditions). An unreadable or ambiguous configuration refuses; it never
    assumes the absence of conditions."""
    try:
        return merge_queue.radar_project(cfg, repo)
    except merge_queue.QueueError as exc:
        raise MergePrError("cannot read the landing conditions of %s: %s" % (repo, exc)) \
            from exc


def _required_refusal(slug: str, target: str, project, rollup, *, head: str = "",
                      gh=None, pairs_out: list | None = None) -> str:
    """Le refus opposé par les checks requis de la cible sur cette tête
    (#2273), ou "" quand ils sont tous présents et `SUCCESS`.

    La lecture est celle du feu (`fire.required_checks` : protection de la
    branche lue à la forge, complétée — jamais affaiblie — par
    `radar.projects.<p>.required_checks`) et la comparaison celle de
    `fire._required_gap` : un check absent, non conclu ou `SKIPPED` ne prouve
    rien. Quand la protection impose une application (`checks[].app_id`), la
    provenance se lit sur la tête exacte (`fire._head_check_runs`) : le rollup
    de `gh pr view` ne porte ni application ni source, donc un statut de commit
    ou un homonyme d'une autre application ne satisfait pas l'exigence. Une
    protection ou une source illisible sans déclaration refuse : le geste hors
    API n'est jamais plus faible que la file. Une déclaration **vide** ne
    couvre que le 403 de plan GitHub prouvé, qui dit qu'aucune protection ne
    peut exister sur ce dépôt (#3428) ; un 403 de droits reste refusé. Un
    transport local (chemin réel
    existant, celui que lie `_bind_repo_identity`) n'a pas de protection de
    branche, il n'y a donc rien à exiger ; un slug vide ou malformé n'est ni
    l'un ni l'autre et refuse, sans s'en remettre à l'appelant (#2289).

    #4549 (B de dev1, réécrit par l'auteur) : `pairs_out` reçoit les couples
    requis lus, pour que `_admission` sache si un `UNSTABLE` repose sur des
    checks requis prouvés ou sur une déclaration vide."""
    ident = _slug_identity(slug)
    if ident is None:
        return ("the required checks cannot be proven: the repository %r is neither "
                "owner/repo nor a local transport" % ((slug or "").strip(),))
    if ident[0] == "path":
        return ""
    slug = ident[1]
    if not target:
        return "the required checks of the target cannot be proven: no target branch"
    try:
        required = fire.required_checks(slug, target, project, gh=gh)
        pairs = fire._required_pairs(required)
    except land.LandError as exc:
        return "the required checks of %s cannot be proven: %s" % (target, exc)
    if pairs_out is not None:
        pairs_out.extend(pairs)
    provenance = None
    if any(app_id is not None for _, app_id in pairs):
        try:
            provenance = fire._head_check_runs(slug, head, gh=gh)
        except land.LandError as exc:
            return ("the source of the required checks of %s cannot be proven: %s"
                    % (target, exc))
    gaps = fire._required_gap(pairs, rollup, provenance, slug=slug,
                              gh=gh, head=head, target=target)
    if gaps:
        return "the required checks of %s are not proven: %s" % (target, ", ".join(gaps))
    return ""


def _admission(db, cfg, lot: int, repo: str, project, owner: str, view: dict, *,
               slug: str = "", target: str = "", head: str = "",
               gh=None, packet_exception=None) -> dict:
    """The merge queue admission, replayed as is (#1003 B1): freeze, `ok`
    verdict on the head (class read from the frozen diff), non-protected
    target, green CI of the PR on that head, the project's `ci_required`. The
    radar clone is this gesture's working repository: `readiness` reads the
    lot's branch there on the remote, never in the author's checkout.

    #2273 : la tête doit aussi prouver les checks requis de la cible (même
    lecture que `fire.ready_pr`) — ce chemin pousse hors API et ne peut donc
    pas s'en remettre à la protection qu'il contourne. `head` est le gel
    relu ; à défaut, la tête de la vue (déjà contrôlée par `_pr_guards`).
    `packet_exception` (nature, motif) porte l'exception déjà validée par un
    `--dry-run` jusqu'à l'admission : elle autorise sans journaliser."""
    branch = (view.get("headRefName") or "").strip()
    rollup = view.get("statusCheckRollup") or []
    base = (view.get("baseRefName") or "").strip()
    head = (head or (view.get("headRefOid") or "")).strip()
    # la cible de la commande fait foi ; la vue ne sert qu'à défaut (#2273 :
    # une vue sans base ne doit pas vider la lecture des checks requis)
    pairs: list = []
    refusal = _required_refusal(slug, (target or "").strip() or base, project, rollup,
                                head=head, gh=gh, pairs_out=pairs)
    if refusal:
        raise MergePrError("PR #%s on the freeze %s: %s"
                           % (view.get("number"), head[:12] or "?", refusal))
    unstable = (view.get("mergeStateStatus") or "").upper() == "UNSTABLE"
    if unstable and not pairs:
        # #4549 (B de dev1, réécrit par l'auteur) : sans paire requise lue,
        # rien ne prouve que le non-vert d'`UNSTABLE` est facultatif — la
        # déclaration peut être vide (403 de plan #3428) ou le transport
        # local sans protection. Le feu exige alors toute la CI verte ; le
        # geste hors API ne fait pas moins.
        raise MergePrError(
            "PR #%s on the freeze %s is UNSTABLE and %s declares no required "
            "check: all the checks must be concluded and green"
            % (view.get("number"), head[:12] or "?", target or base or "?"))

    #: #4549 : sous `UNSTABLE`, un non-vert facultatif ou un run supplanté ne
    #: bloque plus la file ; `_required_refusal` ci-dessus a déjà prouvé les
    #: checks requis, `_unstable_ci_ok` garde le portier et le détail.
    unstable_ok = unstable and _unstable_ci_ok(rollup, project, branch,
                                               slug=slug, head=head, gh=gh)

    def ci_status(name: str):
        if not branch or name != branch:
            return None
        return {"number": view.get("number"), "url": view.get("url") or "",
                "head": head.lower(),
                "green": (unstable_ok
                          or radar.ci_green(rollup,
                                           radar.required_statuses(project, branch))),
                "draft": bool(view.get("isDraft"))}

    clone = resolutions.Replayer(repo, owner)
    try:
        ready = merge_queue.readiness(db, lot, repo, ci=ci_status, clone=clone,
                                      project=project, cfg=cfg,
                                      packet_exception=packet_exception)
    except merge_queue.QueueError as exc:
        raise MergePrError("lot #%d is not admitted by the merge queue: %s" % (lot, exc)) \
            from exc
    return ready


def _ls_remote_ref(owner: str, url: str, ref: str) -> str:
    """The value of a ref on the remote, lowercased; "" when unreadable."""
    text = _out(owner, "ls-remote", url, ref)
    return text.split()[0].lower() if text.split() else ""


def _remote_identity(url: str):
    """A remote's identity (#1003 B/P1): `("github", owner/repo)` when the URL
    designates a GitHub repository, `("path", real path)` when it is a local
    transport (`file://` included), None when it is not verifiable — an
    unverifiable identity is not accepted."""
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
    """The identity of the repository `--repo` designates: `("path", real
    path)` when it is an existing local path, `("github", owner/repo)` when it
    is a bare GitHub key, None when it is not verifiable. An unverifiable
    identity is not accepted, and two different forms never bind to each
    other: a local transport is not the proof of a GitHub key (R1 delta, cx3,
    2026-10-13)."""
    text = (slug or "").strip()
    local = _remote_identity(text)
    if local is not None:
        return local
    if _GITHUB_KEY.match(text):
        return ("github", migrations.repo_key(text))
    return None


def _bind_repo_identity(repo: str, slug: str, fetch_url: str, push_url: str) -> tuple:
    """One repository identity for the PR, the lot, the admission, the read
    and the push (#1003 B/P1).

    The checkout (`repo`), `--fetch-url` and `--push-url` must designate the
    SAME repository: `owner/repo` key for GitHub, real path for a local
    transport. The PR's `slug` must designate that same identity, in the same
    form: from a clone of A, `--repo B` with A's remotes is refused as a
    divergence, even when A and B share the same commits; a local transport
    with a GitHub key is not verifiable and refuses too. Without a readable
    `origin`, nothing is verifiable: the gesture refuses before any PR read,
    any admission and any write."""
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
    """The PR read by `gh`: state, base, head, mergeability, checks."""
    proc = gh(["pr", "view", str(pr), "--repo", slug, "--json",
               "number,state,isDraft,baseRefName,headRefOid,headRefName,mergeStateStatus,"
               "mergeCommit,statusCheckRollup,url,title,autoMergeRequest"], timeout=60.0)
    if proc.returncode != 0:
        raise MergePrError("gh pr view #%s: %s"
                           % (pr, (proc.stderr or proc.stdout or "").strip()[:300]))
    view = _json(proc, "gh pr view #%s" % pr)
    view["statusCheckRollup"] = radar.merge_rollup(view.get("statusCheckRollup"), slug=slug,
                                                 head=view.get("headRefOid") or "",
                                                 branch=view.get("headRefName") or "", gh=gh)
    return view


def message(pr: int, head: str, target: str, tip: str, tree: str, *,
            title: str = "", head_ref: str = "", lot: int | None = None) -> str:
    """The merge commit message: the PR, the guards, the lot."""
    subject = "Merge PR #%d%s into %s (merge outside gh, #1003/REL-11)" % (
        pr, " from %s" % head_ref if head_ref else "", target)
    lines = [subject, ""]
    if title:
        lines += ["%s (head %s)." % (title, head[:12]), ""]
    lines += [
        "Local merge commit: frozen head == the PR head (%s), %s %s is an ancestor of the "
        "freeze," % (head[:12], target, tip[:12]),
        "merge commit tree == the freeze tree (%s), push --force-with-lease "
        "(compare-and-swap)." % tree,
        "The gh API refuses to merge a PR touching %s** without the workflow scope."
        % WORKFLOW_PREFIX,
    ]
    if lot is not None:
        lines += ["", "ameesh-work: %d" % lot]
    return "\n".join(lines) + "\n"


def _scratch(fetch_url: str = "") -> str:
    """The gate's working repository: a bare `git init`, no `origin`.

    #3648: it records the read remote as its forge (`ameesh.forge`, not an
    `origin` remote, which other readings of this repository would follow),
    so the provenance reading can fetch by exact SHA a prior freeze that a
    force-push left outside the fetched refs."""
    path = tempfile.mkdtemp(prefix="ameesh-merge-pr-")
    _check(path, "init", "-q")
    if fetch_url:
        _check(path, "config", plan_git.FORGE_CONFIG_KEY, fetch_url)
    return path


def _fetch(owner: str, fetch_url: str, target: str, pr: int) -> tuple[str, str]:
    """The target head and the PR head, read on the read remote (GitHub's
    `refs/pull/<pr>/head`, authoritative)."""
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
    except Exception as exc:  # noqa: BLE001 - unreadable registry: nothing is declared
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
    except Exception as exc:  # noqa: BLE001 - unreadable registry: nothing is declared
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
    """Already MERGED PR: check that the freeze is in the target."""
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
# the gesture
# --------------------------------------------------------------------------

def direct_merge(slug: str, pr: int, head: str, *, lot: int, repo: str, db=None, cfg=None,
                 project=None, target: str | None = None,
                 fetch_url: str | None = None, push_url: str | None = None,
                 gh=land._gh, dry_run: bool = False, exception: str = "", reason: str = "",
                 actor: str = "",
                 attempts: int | None = None, poll_s: float | None = None,
                 sleep=time.sleep, work_dir: str | None = None) -> dict:
    """Merge PR `pr` (head `head`, lot `lot`) onto `target` by a local commit
    and a compare-and-swap push outside the token, with the module's guards —
    including the queue admission (`merge_queue.readiness`), replayed before
    any write and a second time just before the push. Nothing is written
    before the fresh guard; `dry_run` stops before the push. Returns the
    report (`state`: merged | already | already-unverified | dry-run |
    pushed-unverified).

    #2333 (canon §11 bis): a lot branch (`lot/**`, `agent/**`, `review/**`)
    is not merged directly on the integration target; only an `integration/**`
    packet passes, or an exception `incident|unblock` declared in the lot's
    note (journaled here) — `packet_guard.check_merge` refuses before any
    write, the `MERGED` declaration path included."""
    head = check_head(head)
    if exception:
        try:
            packet_guard.marker(exception, head, reason)
        except packet_guard.PacketGuardError as exc:
            raise MergePrError(str(exc)) from exc
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
    # L165 (EP4) : la fusion hors API est un geste du pilote hors base. Le bail
    # élu est vérifié avant la première lecture GitHub ; il est renouvelé juste
    # avant le push avec la marge de l'appel. Un `--dry-run` n'agit pas et n'a
    # rien à fencer.
    fence = None
    if not dry_run:
        from ... import pilot_gesture
        fence = pilot_gesture.gate(cfg, db, "merge",
                                   "merge-pr #%d (lot #%d)" % (pr, lot))
    # #1003 B/P1: one repository identity, verified before the first read; a
    # divergence writes nothing and admits nothing.
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
    # #2333 (canon §11 bis): the packet rule is checked after the PR read and
    # before any scratch clone, GitHub mutation or write — the `already
    # MERGED` declaration too. L165 (EP4) : une exception explicite est une
    # écriture durable ; elle partage la transaction du hold élu, sinon une
    # bascule pendant `pr_view` laisserait une autorisation hors paquet à
    # l'ancien détenteur (revue cx2 B3). N1 (#1003): `--dry-run` validates an
    # exception but never journals it.
    def declare(database):
        try:
            packet_guard.check_merge(database, lot, view.get("headRefName") or "",
                                     target, head, exception=exception, reason=reason,
                                     actor=actor)
        except packet_guard.PacketGuardError as exc:
            raise MergePrError(str(exc)) from exc

    if fence is not None and exception and not dry_run:
        from ... import pilot_gesture
        with db.transaction() as tx:
            try:
                fence.hold(tx)
            except pilot_gesture.GestureRefused as exc:
                raise MergePrError(str(exc)) from exc
            declare(tx)
    else:
        declare(None if dry_run and exception else db)

    owner = work_dir or _scratch(fetch_url)
    reservation = None
    temporary = work_dir is None
    try:
        state = (view.get("state") or "").upper()
        if state == "MERGED":
            # N1 (#1003): an already-MERGED PR does not declare a lot without
            # the lot/freeze/branch/target link; nothing is written otherwise.
            # L165 (EP4) : la déclaration est un effet, fence-la même sans push.
            if fence is not None:
                from ... import pilot_gesture
                try:
                    fence.hold(db)
                except pilot_gesture.GestureRefused as exc:
                    raise MergePrError(str(exc)) from exc
            _already_lot_links(db, lot, head, view, target, repo, cfg)
            return _already(slug, pr, head, view, target=target, fetch_url=fetch_url,
                            owner=owner)
        if project is None:
            project = _project_of(cfg, repo)
        # #2813 (owner decision 20/10, supersedes #1656 here): the direct
        # merge no longer disarms a GitHub auto-merge. Packet carriers are
        # merged by ameesh at their published head (`packet_automerge`),
        # which disarms any GitHub auto-merge it finds; the guards below and
        # the fresh re-read before the push still bind this merge to `head`.
        _pr_guards(view, pr, head, target, project, slug=slug, gh=gh)
        _stacked_base_guard(owner, fetch_url, repo, view, target)
        ready = _admission(db, cfg, lot, repo, project, owner, view,
                           slug=slug, target=target, head=head, gh=gh,
                           packet_exception=((exception, reason)
                                             if dry_run and exception else None))
        if not _same(ready.get("frozen_sha"), head):
            raise MergePrError("lot #%d is admitted on %s, not on the freeze %s: refreeze and "
                               "re-review" % (lot, (ready.get("frozen_sha") or "?")[:12],
                                              head[:12]))
        if _branch_name(ready.get("target")) != _branch_name(target):
            raise MergePrError("lot #%d is queued for %s, not %s: merge it on its own target"
                               % (lot, ready.get("target") or "?", target))
        # #1680: use the queue's exact target spelling and project identity.
        # Before #1374 lands, origin/develop and develop are distinct keys;
        # inventing a canonical key here would bypass the installed queue.
        # #1703 N2: that identity is resolved by one shared helper
        # (`merge_queue.writer_project`): the declared radar project first,
        # else the admitted lot's app — the same name the queue carries.
        # #1703 N1: a dry run writes nothing (it stops before the push), so it
        # must not take, hold or refuse on that lease: taking it refused a
        # live merge while the proof ran, and blocked the queue meanwhile.
        if not dry_run:
            try:
                lease_project = merge_queue.writer_project(
                    cfg, repo, (work.get(db, lot).get("app") or ""), project=project)
            except merge_queue.QueueError as exc:
                raise MergePrError(str(exc)) from exc
            reservation = packet_reservation.WriterLease(db, lease_project, ready["target"])
            reservation.acquire()
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
                    "frozen": head, "tip": tip,
                    # #3314 (#3308) : `api_base` laissait croire à une lecture
                    # d'API ; `pr_view` ne demande pas `baseRefOid` (absent de
                    # gh 2.45), et ce champ est le tip de la cible rapatrié et
                    # vérifié plus haut — `tip` en est l'alias court.
                    "target_tip": tip, "merge": merge_sha,
                    "tree": tree, "frozen_tree": frozen_tree,
                    "workflow_files": touched, "changed_files": len(files),
                    "merge_state": merge_state,
                    "admission": {"target": ready.get("target") or "",
                                  "class": ready.get("class") or "",
                                  "class_why": ready.get("class_why") or "",
                                  "vehicle": ready.get("vehicle") or ""},
                    "message": text}
        if reservation is not None:
            # #1703 N2: the identity actually held is observable, so an
            # operator can compare it with the train's key on the same target.
            evidence["reservation"] = reservation.key
        # #1003 B1: the radar project's gates, on the merged tree, as the
        # queue does before pushing (no declared gate: nothing to run).
        if project is not None and (getattr(project, "gates", ())
                                    or getattr(project, "path_gates", ())):
            checked = land.gates(project, owner, files=files)
            if not checked["ok"]:
                _run(owner, "merge", "--abort")
                raise MergePrError("gate `%s` failed on the merged tree: %s"
                                   % (checked["gate"], checked["output"]))
            evidence["gates"] = {"ok": True, "gate": checked["gate"]}
        # #1003 B2: fresh guard just before the write — the PR, the queue
        # admission and both remote refs are read again; any mutation since
        # the merge commit was built stops without writing.
        fresh = pr_view(slug, pr, gh=gh)
        _pr_guards(fresh, pr, head, target, project, slug=slug, gh=gh)
        _stacked_base_guard(owner, fetch_url, repo, fresh, target)
        ready = _admission(db, cfg, lot, repo, project, owner, fresh,
                           slug=slug, target=target, head=head, gh=gh,
                           packet_exception=((exception, reason)
                                             if dry_run and exception else None))
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
            evidence.update(state="dry-run", pushed=False, remote_tip="",
                            reservation="not taken (dry-run)")
            return evidence

        ref = "refs/heads/%s" % target
        reservation.refresh()
        # L165 (EP4) : renouvellement du bail élu juste avant l'appel externe,
        # avec la marge du push et de sa lecture ; refus si le bail renouvelé
        # n'a pas cette marge (course résiduelle documentée).
        if fence is not None:
            from ... import pilot_gesture
            try:
                fence.hold(db, margin_s=PILOT_CALL_MARGIN_S)
            except pilot_gesture.GestureRefused as exc:
                raise MergePrError(str(exc)) from exc
        try:
            proc = _run(owner, "push", "--quiet", "--no-verify",
                        "--force-with-lease=%s:%s" % (ref, tip), push_url,
                        "%s:%s" % (merge_sha, ref), timeout=FETCH_TIMEOUT)
            error = "" if proc.returncode == 0 else _redact(resolutions._err(proc)[-300:])
        except GitError as exc:  # unknown push outcome: read the target again
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
    except packet_reservation.ReservationError as exc:
        raise MergePrError(str(exc)) from exc
    finally:
        if reservation is not None:
            reservation.release()
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
    parser.add_argument("--exception", choices=packet_guard.EXCEPTIONS, default="",
                        help="#2333/canon §11 bis: incident|unblock, for a lot branch merged "
                             "outside the packet; declared and journaled in the lot's note; "
                             "requires --reason")
    parser.add_argument("--reason", default="",
                        help="why the packet exception is taken (incident: target red; "
                             "unblock: CI stuck); written to the lot's journal")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every guard and build the merge commit; push nothing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="who declares the merge on the lot (default: the session's bound "
                             "identity, AGENT_MAIL_NAME); an unknown actor declares nothing")
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
        # the operator path merges only a GitHub PR: a local path is not
        # verifiable against a GitHub slug (cx3 delta) and the command
        # refuses it, rather than letting `gh` fail further on.
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
                           dry_run=args.dry_run, exception=args.exception or "",
                           reason=args.reason or "", actor=args.actor or "")
        # N1 (#1003): `--dry-run` never declares anything, including on an
        # already-MERGED PR (the `already` finding is not a write).
        if out.get("state") in ("merged", "already") and not args.dry_run:
            out["lot"] = args.lot
            sha = out.get("merge") or out.get("tip") or ""
            actor = (getattr(args, "actor", "") or "").strip()
            try:
                if not sha:
                    raise MergePrError("no merge commit to declare")
                # N2 (#1253): never record the milestone as UNKNOWN_ACTOR. The
                # CLI resolves --actor from the bound identity (AGENT_MAIL_NAME);
                # an unbound session leaves it empty or UNKNOWN_ACTOR, and the
                # merge is a fact that is not undone — only the declaration is
                # skipped, with a warning naming the fix.
                if not actor or actor == work.UNKNOWN_ACTOR:
                    raise MergePrError(
                        "unknown actor: pass --actor <agent|human:id>, or run from a bound "
                        "session; the merge is a fact, the lot is not declared")
                # This is an observed merge: direct_merge has verified the
                # lot/freeze/branch/target and read back the remote result.
                # Its scratch clone is gone; the caller's checkout need not
                # contain the commit required by a manual work.merged call.
                recorded = work.deliver(
                    db, args.lot, connector=work.merge_connector(),
                    evidence={"seen": "merge-pr", "sha": sha,
                              "pr": "%s#%d" % (slug, args.pr)}, actor=actor,
                    extra="ameesh work merge-pr %d --head %s (merge outside gh, "
                         "#1003/REL-11)" % (args.pr, out["frozen"][:12]))
                if recorded["result"] not in ("merged", "already"):
                    raise MergePrError(recorded["detail"])
                out["recorded"] = recorded
            except Exception as exc:  # noqa: BLE001 - the merge is a fact, never undone
                out["record_warning"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else _human(out))
        if out["state"] in ("merged", "already", "dry-run"):
            return 0
        return 1 if out["state"] == "already-unverified" else 2
    except (MergePrError, GitError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
