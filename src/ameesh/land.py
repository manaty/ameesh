# SPDX-License-Identifier: AGPL-3.0-only
"""Merge of the head of the queue by ameesh (lot L160e2, study L160 § 3.4).

Run by the executor at each radar pass, for a project that declares
`radar.projects.<p>.land` (`local` or `pr`; `off` by default: the queue
orders, the agent merges) and its `gates`, for the head of each queue:

1. lease per target (`merge_locks`): one merge at a time, which removes
   the catch-up merges; the queue pass holds it for the length of one pass,
   renewed by a heartbeat, so a dead pass frees it at the short pass TTL
   (`AMEESH_LAND_PASS_TTL`, 180 s), never after the train's 30 min, even
   while a PR waits between two passes (#3993: the heartbeat is carried to
   the next pass); the epoch fence still refuses a late holder's writes;
2. in the radar's private clone: the target, fresh from `origin`, and the
   frozen commit of the lot merged into it (`--no-ff`, line `ameesh-work:
   <lot>`), `rerere` primed by every resolution prepared with the lot; a
   conflict left ejects the lot and wakes its agent;
3. the project's gates on the merged tree (shell commands, in the clone's
   worktree); a failure: same;
4. `local`: the merge commit pushed to the target, never forced (the target
   moved meanwhile: the lot stays at the head, retried at the next pass);
   `pr`: the merge commit pushed to the lot branch (a fast-forward of the
   freeze), the queue row `landing` until the PR's CI on that commit is
   green, then the PR merged by the API (`gh pr merge --match-head-commit`);
5. a notice in the lot's thread (passive mail): resolutions replayed,
   gates; the lot is closed by the branch sweep (L118), which sees the
   merge.

    ameesh work land <lot> [--project P]   the same, by hand, for the head
                                           of its queue
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import warnings
from urllib.parse import quote

from . import merge_queue, plan_git, storage, work
from .canon import FETCH_TIMEOUT, GitError

#: the merge lease shared with the train and the direct fire (the target
#: reservation of a packet CI); renewed before each gesture
LOCK_TTL_S = float(os.environ.get("AMEESH_LAND_LOCK_TTL", "1800"))
#: #3932: the queue landing holds the lease for the length of ONE pass only.
#: A pass can spend the gate timeout in a subprocess, so a short TTL would
#: expire mid-gate; `_PassLeaseRenewal` renews it every third of this TTL
#: while the pass runs. A pass that dies after taking the lease (killed
#: runner, restarted daemon sweep) stops renewing: the target is free after
#: this bound, at a new epoch, well before the train's 30 min. Never used to
#: take a new lease: only the holder renews its own `(owner, epoch)`.
LAND_PASS_TTL_S = float(os.environ.get("AMEESH_LAND_PASS_TTL", "180"))
#: one gate at most
GATE_TIMEOUT_S = float(os.environ.get("AMEESH_LAND_GATE_TIMEOUT", "1800"))
#: L165 (EP4) : marge du bail élu renouvelée juste avant un push/merge externe
#: (`FETCH_TIMEOUT` du push + marge de lecture). La course résiduelle est
#: documentée dans `docs/HOST-HANDOFFS.md`.
AGENT_CALL_MARGIN_S = FETCH_TIMEOUT + 60.0
#: lines of a failed gate's output kept in the ejection
GATE_TAIL = 20
TRAILER = "ameesh-work"
#: ref of the fresh target in the clone
FRESH_REF = "refs/ameesh/land/target"

#: #2345 : une lecture passagère est réessayée d'un passage à l'autre, mais
#: pas sans fin. Le compteur est le nombre de notes `RETRY_NOTE` du journal
#: depuis l'admission courante (le radar réécrit `reason` à chaque passage :
#: il ne peut pas le porter). Au-delà, la ligne est éjectée (fail-closed) et
#: l'assigné réveillé ; 6 réessais ≈ 30 min au rythme de relevé par défaut
#: (300 s), réglable par `AMEESH_LAND_TRANSIENT_RETRIES`.
DEFAULT_TRANSIENT_RETRY_LIMIT = 6


def _retry_limit(environ=None) -> int:
    """#2371 : lire la borne sans faire échouer l'import de `land` (#2345).

    Une valeur absente ou vide garde le défaut ; une valeur non entière le
    garde aussi, avec un avertissement (l'import d'un module ne doit pas
    dépendre d'une variable d'environnement mal formée). Une valeur négative
    vaut 0 : la borne s'applique alors dès le premier réessai.
    """
    raw = (os.environ if environ is None else environ).get(
        "AMEESH_LAND_TRANSIENT_RETRIES")
    if raw is None or not str(raw).strip():
        return DEFAULT_TRANSIENT_RETRY_LIMIT
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        warnings.warn(
            "AMEESH_LAND_TRANSIENT_RETRIES=%r is not an integer: using %d"
            % (raw, DEFAULT_TRANSIENT_RETRY_LIMIT), RuntimeWarning, stacklevel=2)
        return DEFAULT_TRANSIENT_RETRY_LIMIT


TRANSIENT_RETRY_LIMIT = _retry_limit()
#: préfixe des notes de journal qui comptent ces réessais (`events_noted`).
RETRY_NOTE = "land transient retry"
#: #5179 : un échec d'atterrissage au journal du lot, `lock <hôte>:<fichier>` ou
#: `other` ; le journal, et non `reason`, que le radar réécrit (#2345)
FAILURE_NOTE = "land failure: "
#: #5179 : échecs consécutifs sur le même verrou git avant `merge_lock_stuck`
GIT_LOCK_ALERT_AFTER = int(os.environ.get("AMEESH_GIT_LOCK_ALERT_AFTER", "3"))
#: #5179 : au-delà sans nouvel échec, le blocage n'est plus tenu pour actuel
GIT_LOCK_ALERT_FRESH_S = 3600.0
_GIT_LOCK_ERROR = re.compile(r"'?(/[^'\n:]+\.lock)'?:? File exists")


class LandError(RuntimeError):
    """A lot that cannot be landed now: actionable message."""


class TransientLandError(LandError):
    """#2306 : une lecture distante a échoué pour une raison passagère (HTTP
    5xx, délai, coupure réseau, DNS, GOAWAY, sortie vide). Le refus tient
    (fail-closed), mais la même demande peut aboutir au passage suivant : la
    file garde sa ligne au lieu de l'éjecter. Une réponse malformée, un 4xx ou
    un manque prouvé restent des `LandError` ordinaires, qui éjectent."""


class ForbiddenLandError(LandError):
    """#2344 : la forge a **prouvé** un refus de droits (HTTP 403) sur une
    source de checks requis. Seule cette preuve autorise `fire.required_checks`
    à remplacer la source par la déclaration du projet (jeton sans
    administration, voie prévue pour Nexlink) ; tout autre échec — 404, DNS,
    GOAWAY, sortie vide, réponse malformée — reste un `LandError` (ou un
    `TransientLandError`) qui refuse, jamais un repli."""


def lock_key(project, target: str) -> str:
    return "%s:%s" % (project.name, merge_queue._remote_branch(target))


def _branch_of(target: str) -> str:
    """`origin/develop` → `develop`: the branch to push to."""
    return target.split("/", 1)[1] if target.startswith("origin/") else target


def _origin(project) -> str:
    """#3041 : l'URL `origin` du checkout, un chemin relatif lu depuis lui
    (elle est rejouée dans le clone du radar)."""
    from . import resolutions
    url = plan_git._out(project.repo, "remote", "get-url", "origin") or ""
    return resolutions._local_from(project.repo, url)


def _passive(db, to, lot: int, text: str, data: dict) -> None:
    """The notice, in the lot's thread, without waking anyone (key `radar`)."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                  payload={"radar": dict(data, kind="landed")})
    except Exception:  # noqa: BLE001 - a notice is never fatal
        pass


def _fire_guard(landing, *, gh=None) -> dict | None:
    """#1374, P1-2 : le feu et les blocages sont revalidés au dernier geste.

    Le retrait ou l'invalidation d'un feu retire la ligne de file au passage
    suivant ; entre les deux, `land` pourrait encore pousser ou fusionner. Sous
    son bail, juste avant le geste, il redemande l'autorisation : un lot qui a
    porté un feu ne part que sous un feu actif couvrant le gel de sa file et
    sans verdict bloqué non levé.

    #2286 : sur un atterrissage local, la garde exige aussi les checks requis
    déclarés du projet sur le gel : la voie file ne contourne pas la porte des
    checks requis. Rend l'éjection, ou None si la voie est libre.

    #2333 (canon §11 bis) : la file d'équipe ne contourne pas la règle du
    paquet. La tête de la ligne (`lot/**`, `agent/**`, `review/**`) ne se
    fusionne sur la cible d'intégration que portée par un paquet
    (`integration/**`) ou sous une exception déclarée au journal du lot, pour
    cette tête exacte — et cela **sans dépendre d'un feu** : une ligne déjà en
    file ou `work queue add` (qui ne passe ni par `give` ni par `_act`) est
    arrêtée ici, sur les voies `local` et `pr`.

    #2306 : une lecture passagère des checks (5xx, délai) ne prouve pas un
    manque ; la ligne reste en file et repart au passage suivant, au lieu
    d'éjecter le lot.

    #3550 : la ligne de file lue par le passage est un instantané ; une
    sortie de file survenue depuis retire l'autorisation de fusionner. Le
    dernier geste relit l'identité de l'entrée (génération, cible, gel) et
    s'arrête si elle a disparu ou changé."""
    from . import fire, packet_guard
    row = landing.row or {}
    if getattr(landing, "entry", None) is not None and landing.queue_gone():
        return {"lot": landing.lot, "result": "taken-out", "detail": (
            "the queue entry of lot #%d was removed or replaced before the merge: "
            "nothing merged" % landing.lot)}
    try:
        packet_guard.check_merge(landing.db, landing.lot, row.get("branch") or "",
                                 row.get("target") or "", row.get("frozen_sha") or "")
    except packet_guard.PacketGuardError as exc:
        return landing.eject(str(exc), "This queue entry is not a packet (canon §11 bis): "
                                       "take the lot out of the queue (`ameesh work queue "
                                       "remove %d`) and let the packet carry it, or declare "
                                       "the incident/unblock exception in the lot's note."
                              % landing.lot)
    try:
        ok, why = fire.land_guard(landing.db, landing.lot,
                                  landing.row.get("frozen_sha") or "",
                                  repo=landing.project.repo, project=landing.project,
                                  target=landing.row.get("target") or "", gh=gh)
    except TransientLandError as exc:
        return landing.retry(
            "the required checks of %s on the freeze %s cannot be read now (%s)"
            % (landing.row.get("target") or "?", (landing.row.get("frozen_sha") or "")[:12],
               exc))
    if ok:
        return None
    return landing.eject(why, "The fire or the required checks no longer authorize this "
                              "landing: restore them on the freeze (or ask the pilot for a "
                              "fire on the current freeze), or take the lot out of the queue "
                              "(`ameesh work queue remove %d`)." % landing.lot)


# --------------------------------------------------------------------------
# the merge in the clone
# --------------------------------------------------------------------------

def fresh_target(replayer, project, target: str) -> str:
    """The target's tip, fetched from `origin` when there is one, else the
    agents' repository."""
    from . import resolutions
    url = _origin(project)
    source = url or project.repo
    branch = _branch_of(target)
    proc = resolutions._run(replayer.path, "fetch", "--quiet", source,
                            "+refs/heads/%s:%s" % (branch, FRESH_REF), timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise GitError("git fetch %s %s: %s" % (source, branch, resolutions._err(proc)))
    return resolutions._check(resolutions._run(replayer.path, "rev-parse", FRESH_REF),
                              "rev-parse")


def _shell(command: str, path: str, timeout: float) -> str | None:
    """Runs `command` in `path`: None if it passes, else the tail of its output."""
    try:
        proc = subprocess.run(command, shell=True, cwd=path, capture_output=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "timeout after %.0f s" % timeout
    if proc.returncode == 0:
        return None
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").splitlines()
    return "\n".join(out[-GATE_TAIL:]) or "exit %d" % proc.returncode


def _regenerate(path: str, generated, touched, *, timeout: float = GATE_TIMEOUT_S) -> dict:
    """L160g (#473): regenerates, on the merged tree in `path`, each
    generated file in `touched`, stages
    it, then runs its check: `{ok, files, file, output}`."""
    from . import resolutions
    done = []
    for item in generated:
        if item.path not in touched:
            continue
        failed = _shell(item.regen, path, timeout)
        if failed is None:
            staged = resolutions._run(path, "add", "-A", "--", item.path)
            if staged.returncode != 0:
                failed = "cannot stage generated output: %s" % resolutions._err(staged)
            else:
                failed = _shell(item.check, path, timeout) if item.check else None
                if failed is None and resolutions._run(
                        path, "diff", "--exit-code", "--", item.path).returncode != 0:
                    failed = "generated check changed the staged output"
        if failed is not None:
            return {"ok": False, "files": done, "file": item.path, "output": failed}
        done.append(item.path)
    return {"ok": True, "files": done, "file": None, "output": ""}


def merge(replayer, base: str, row: dict, rows: list[dict], *, generated=()) -> dict:
    """Merges the frozen commit of `row` into `base` in the clone, `rerere`
    primed by the resolutions `rows` of the lot. Leaves the merge committed
    at HEAD: `{state: clean|replayed|conflict|generated, files, sha,
    regenerated}`. `generated` (L160g, `radar.Generated`): those files are
    never taken from a merge nor from rerere — regenerated on the merged
    tree for every merge, including input-only changes, then checked (`generated`:
    the regeneration or its check failed, `output`)."""
    from . import resolutions
    path, run = replayer.path, resolutions._run
    lot = int(row["work_item_id"])
    paths = {g.path for g in generated}
    commits = [r["commit_sha"] for r in rows
               if lot in (int(r["work_item_id"]), int(r["other_item_id"]))]
    missing = set(replayer.prime(commits)) if commits else set()
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", base), "checkout")
    replayer._empty_cache()
    trained = set(replayer.trained())
    for sha in commits:
        if sha in trained and sha not in missing:
            shutil.copytree(replayer._store(sha), replayer._rr_cache(), dirs_exist_ok=True)
    proc = run(path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
               "merge", "--no-ff", "--no-commit", "--quiet", row["frozen_sha"])
    state, files, regenerated = "clean", [], []
    try:
        if proc.returncode != 0:
            files = resolutions.unmerged(path)
            resolutions.exclude_generated(path, files, paths)
            resolutions._check(run(path, "-c", "rerere.enabled=true", "-c",
                                   "rerere.autoUpdate=false", "rerere"), "rerere")
            others = [f for f in files if f not in paths]
            remaining = [f for f in resolutions.rerere_remaining(path) if f not in paths] \
                if others else []
            left = sorted(set(remaining) | set(resolutions.with_markers(path, others)))
            if not files or left:
                run(path, "merge", "--abort")
                return {"state": "conflict", "files": left or files,
                        "error": "" if files else resolutions._err(proc)[:300]}
            if others:
                resolutions._check(run(path, "add", "--", *others), "add")
                state = "replayed"
        touched = paths
        if touched:
            made = _regenerate(path, generated, touched)
            if not made["ok"]:
                run(path, "merge", "--abort")
                return {"state": "generated", "files": [made["file"]],
                        "output": made["output"]}
            regenerated = made["files"]
        message = "Merge #%d « %s » (%s)\n\n%s: %d\n" % (
            lot, row.get("title") or "", row.get("branch") or row["frozen_sha"][:12],
            TRAILER, lot)
        resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                               "--no-verify", "-F", "-", stdin=message.encode()), "commit")
    finally:
        replayer._empty_cache()
    sha = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
    return {"state": state, "files": [f for f in files if f not in paths], "sha": sha,
            "regenerated": regenerated}


def changed(path: str, base: str, sha: str) -> list | None:
    """L160g (#473): the files the merge `sha` changes on `base`; None if
    git cannot tell (then every path gate runs)."""
    from . import resolutions
    proc = resolutions._run(path, "diff", "--name-only", "--no-renames", base, sha)
    if proc.returncode != 0:
        return None
    return [f for f in resolutions._text(proc).splitlines() if f.strip()]


def gates(project, path: str, *, files=None, timeout: float = GATE_TIMEOUT_S) -> dict:
    """Runs the project's gates in `path`: `{ok, gate, output}` (the
    failed gate and the tail of its output); with its path gates (L160g)
    for the `files` the merge touches (`radar.gates_for`)."""
    from . import radar
    for gate in radar.gates_for(project, files):
        failed = _shell(gate, path, timeout)
        if failed is not None:
            return {"ok": False, "gate": gate, "output": failed}
    return {"ok": True, "gate": None, "output": ""}


# --------------------------------------------------------------------------
# GitHub (land: pr)
# --------------------------------------------------------------------------

#: #194 r8: the exit status of a `gh` call that timed out — its effect unknown
GH_TIMEOUT = 124


def _gh(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(["gh", *args], GH_TIMEOUT, "", str(exc))
    except OSError as exc:
        return subprocess.CompletedProcess(["gh", *args], 1, "", str(exc))


def _json(proc, what: str, kind):
    """#194 r8: the output of `gh … --json`, of the type expected, or LandError."""
    try:
        data = json.loads(proc.stdout or ("[]" if kind is list else "{}"))
    except ValueError as exc:
        raise LandError("%s: unreadable output (%s)" % (what, exc)) from exc
    if not isinstance(data, kind) or (kind is list and not all(
            isinstance(r, dict) for r in data)):
        raise LandError("%s: unexpected output %s" % (what, (proc.stdout or "")[:100]))
    return data


def pr_status(slug: str, branch: str, gh=_gh, *, project=None) -> dict | None:
    """The open PR of `branch`: `{number, head, base, ci: green|red|pending,
    auto}`. `auto` (#1656) says GitHub auto-merge is armed: its
    `--match-head-commit` is checked when it is armed, never again, so a head
    rewritten under it would be merged without a new proof."""
    from . import radar
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json",
               "number,headRefOid,baseRefName,statusCheckRollup,autoMergeRequest"])
    if proc.returncode != 0:
        raise LandError("gh pr list: %s" % (proc.stderr or proc.stdout).strip()[:300])
    rows = _json(proc, "gh pr list", list)
    if not rows:
        return None
    rollup = radar.merge_rollup(rows[0].get("statusCheckRollup"), slug=slug,
                                head=rows[0].get("headRefOid") or "", branch=branch, gh=gh)
    required = radar.required_statuses(project, branch)
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    # #1748 (a): all green but a required status not yet published: the
    # package validation is still to come, never a green
    ci = "green" if radar.ci_green(rollup, required) else "pending" \
        if pending or not rollup or radar.ci_green(rollup) else "red"
    return {"number": rows[0]["number"], "head": rows[0].get("headRefOid"),
            "base": rows[0].get("baseRefName"), "ci": ci,
            "auto": bool(rows[0].get("autoMergeRequest"))}


def base_oid(slug: str, base: str, gh=_gh) -> str:
    """The resolved head of `base`, read through the REST API (#3308).

    `gh pr view --json baseRefOid` does not exist on gh 2.45 (the host still
    runs it): asking for that field makes the whole PR read fail. The branch
    is resolved with `git/ref/heads/<base>`, which every gh version exposes.
    The target is never guessed: a missing or unreadable ref raises
    `LandError`, and an answer that is not a full commit id is refused.
    """
    slug = str(slug or "").strip()
    base = str(base or "").strip()
    if not slug or not base:
        raise LandError("no base branch to resolve in %s: nothing guessed"
                        % (slug or "the repository"))
    path = "repos/%s/git/ref/heads/%s" % (slug, quote(base, safe=""))
    proc = gh(["api", path])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300] \
            or "exit %d" % proc.returncode
        raise LandError("gh api %s: %s" % (path, detail))
    data = _json(proc, "gh api %s" % path, dict)
    obj = data.get("object")
    oid = str((obj or {}).get("sha") or "").strip().lower() if isinstance(obj, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise LandError("gh api %s: no resolved commit (got %r)" % (path, oid))
    return oid


def pr_disable_auto(slug: str, number, gh=_gh) -> str | None:
    """#1656: disarms GitHub auto-merge on PR `number`; the error, or None.

    A `gh pr merge --auto --match-head-commit` only checks the head when it
    is armed: GitHub keeps no link to that commit. A head rewritten after
    (even under `[skip ci]`) is then merged with no new qualification. Any
    PR the queue manages is merged by ameesh itself, on the exact frozen
    head; its auto-merge, whoever armed it, is disarmed first."""
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--disable-auto"])
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode


def pr_merge(slug: str, number, sha: str, method: str, gh=_gh) -> str | None:
    """Merges the PR by the API at that very commit; the error, or None. A
    timeout starts with `TIMED_OUT`: GitHub may have merged it all the same."""
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--%s" % method,
               "--match-head-commit", sha], timeout=120.0)
    if proc.returncode == 0:
        return None
    # #194 r3: a failure without output is still a failure
    error = (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode
    return TIMED_OUT + error if proc.returncode == GH_TIMEOUT else error


TIMED_OUT = "timed out: "


def pr_merged(slug: str, branch: str, sha: str, gh=_gh) -> dict | None:
    """#194 r8: the merged PR of `branch` whose head is `sha` (`{number,
    base}`), None if there is none — a merge that completed after its call
    timed out, or between two passes. Raises when GitHub cannot be read."""
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "merged",
               "--json", "number,headRefOid,baseRefName"])
    if proc.returncode != 0:
        raise LandError("gh pr list --state merged: %s"
                        % (proc.stderr or proc.stdout).strip()[:300])
    for row in _json(proc, "gh pr list --state merged", list):
        if merge_queue._same(row.get("headRefOid"), sha):
            return {"number": row.get("number"), "base": row.get("baseRefName")}
    return None


def _merged_after_disarm(slug: str, branch: str, sha: str, gh) -> tuple[dict | None, str]:
    """#1718: `pr_merged` read after a failed disarm, and what to add to the
    ejection when it cannot be read. Its LandError must not reach the
    retry path of `land`: the entry would go back to `queued` (or stay
    `landing`) silently, auto-merge still armed — the ejection that asks
    to disarm by hand is kept, fail closed."""
    try:
        return pr_merged(slug, branch, sha, gh=gh), ""
    except LandError as exc:
        return None, "; merged PRs unreadable (%s)" % str(exc)[:200]


# --------------------------------------------------------------------------
# one landing
# --------------------------------------------------------------------------

#: #194 r5: the lease of a target, on the remote itself (local mode): moved by
#: each new holder before anything else, checked by every landing push
LEASE_REF = "refs/ameesh/land/"
_LEASE_ENV = {"GIT_AUTHOR_NAME": "ameesh", "GIT_AUTHOR_EMAIL": "ameesh@localhost",
              "GIT_COMMITTER_NAME": "ameesh", "GIT_COMMITTER_EMAIL": "ameesh@localhost"}
_LEASE_RE = re.compile(r"^ameesh land lease (\S+) epoch ([0-9]+) \((.*?)\)")
#: where the clone keeps the remote lease ref it last read
_SEEN_LEASE = "refs/ameesh/seen-lease"


class _LeaseRefused(LandError):
    """#194 r6: the remote lease ref could not be taken: nothing is written."""


def lease_commit(path: str, key: str, epoch: int, owner: str, *, parent: str | None = None,
                 landed: int | None = None) -> str:
    """A commit (empty tree) that stands for the lease `key` at `epoch`, held
    by `owner`; child of `parent` (the ref's value it replaces); `landed`:
    the commit that a landing push writes over the holder's own."""
    env = dict(os.environ, **_LEASE_ENV)
    tree = subprocess.run(["git", "-C", path, "mktree"], input=b"", capture_output=True,
                          env=env, check=False)
    if tree.returncode != 0:
        raise GitError("git mktree: %s" % tree.stderr.decode("utf-8", "replace")[:200])
    message = "ameesh land lease %s epoch %d (%s)%s" % (
        key, epoch, owner, ": landed lot %d" % landed if landed is not None else "")
    proc = subprocess.run(["git", "-C", path, "commit-tree", tree.stdout.decode().strip(),
                           *(["-p", parent] if parent else []), "-m", message],
                          capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        raise GitError("git commit-tree: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout.decode().strip()


def remote_lease(path: str, url: str, ref: str) -> tuple:
    """The remote lease ref, fetched into the clone: `(sha or None, epoch or
    None, owner or None)`. Raises when the remote cannot be read."""
    from . import resolutions
    proc = resolutions._run(path, "ls-remote", url, ref, timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("git ls-remote %s: %s" % (ref, resolutions._err(proc)[-200:]))
    found = resolutions._text(proc).split()
    if not found:
        return None, None, None
    sha = found[0]
    proc = resolutions._run(path, "fetch", "--quiet", url, "+%s:%s" % (ref, _SEEN_LEASE),
                            timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("git fetch %s: %s" % (ref, resolutions._err(proc)[-200:]))
    text = resolutions._text(resolutions._run(path, "log", "-1", "--format=%s", sha))
    match = _LEASE_RE.match(text)
    return (sha, int(match.group(2)), match.group(3)) if match else (sha, None, None)


def take_remote_lease(path: str, url: str, ref: str, key: str, epoch: int,
                      owner: str) -> str:
    """#194 r6: the remote lease ref IS the lock. Taken by a compare-and-swap
    from the value read to this holder's epoch commit (never `--force`: a late
    stale publisher cannot roll the epoch back). Kept as is when it already
    stands for this holder at this epoch. Returns the value this holder now
    expects; `_LeaseRefused` if a newer epoch holds it or the push is refused."""
    from . import resolutions
    sha, held_epoch, held_by = remote_lease(path, url, ref)
    if held_epoch == epoch and held_by == owner:
        return sha
    if held_epoch is not None and held_epoch > epoch:
        raise _LeaseRefused("the remote lease %s is at epoch %d (%s), newer than ours (%d)"
                            % (ref, held_epoch, held_by, epoch))
    mine = lease_commit(path, key, epoch, owner, parent=sha)
    proc = resolutions._run(path, "push", "--quiet", "--no-verify",
                            "--force-with-lease=%s:%s" % (ref, sha or ""), url,
                            "%s:%s" % (mine, ref), timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise _LeaseRefused("lease ref %s not taken (%s)" % (ref, resolutions._err(proc)[-200:]))
    return mine


def _retry_attempts(ops, lot: int, since_ts) -> int:
    """#2345 : les réessais passagers déjà consentis depuis l'admission de la
    ligne, lus dans le journal du lot (`RETRY_NOTE`, né après `queued_ts`).

    Le journal, et non `reason` : le radar réécrit `reason` à chaque passage
    (ordre, report), ce qui remettrait le compteur à zéro. Un journal
    illisible ne fait pas éjecter — la fusion reste de toute façon refusée
    (fail-closed) —, il ne fait que ne pas borner ce passage-ci."""
    try:
        return len(ops.events_noted(RETRY_NOTE, float(since_ts or 0), ids=[int(lot)]))
    except Exception:  # noqa: BLE001 - compteur illisible : on retente sans lui
        return 0


class _PassLeaseRenewal:
    """#3932: renews the target lease while one landing pass runs.

    The queue landing can spend a long time in a gate subprocess; the lease
    must stay alive without falling back to the train's 30 min TTL. A
    separate thread renews the SAME ``(owner, epoch)`` every third of the
    pass TTL (never a new acquisition, so the epoch fence is untouched).

    A renewal that the database refuses (row expired or taken by another
    owner, epoch changed) is final: ``lost`` is set and ``_Landing.held``
    refuses every external gesture from then on. A database that is briefly
    unreachable only skips a beat; the main thread's own ``held()`` check
    remains the last gate before a push or a merge.

    #3993: a failed beat is not silent. The first failure of a run, and the
    first success after failures, go to the pass's error channel (stderr);
    ``failures`` and ``first_error`` let a refused gesture name the cause.

    #3993: between two passes (a PR waiting for its CI, a merge in doubt) the
    same heartbeat keeps running (`carry`), at the short TTL, until the next
    pass of this process takes it back (`_reclaim`) or `LOCK_TTL_S` has
    elapsed. A runner that dies frees the target at the short bound, not
    after the train's 30 min; a live but stuck one never holds it longer.
    """

    def __init__(self, renew, *, ttl: float, interval: float | None = None,
                 key: str = ""):
        self._renew = renew
        self.ttl = float(ttl)
        self.interval = max(0.05, float(ttl / 3.0 if interval is None else interval))
        self.key = key
        self.lost = False
        self.failures = 0
        self.first_error = ""
        self._until: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="land-lease-renewal")
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._stop.is_set():
                    return
                if self._until is not None and time.monotonic() >= self._until:
                    # #3993: never held between passes beyond the train's TTL;
                    # the last short renewal expires on its own
                    return
                try:
                    renewed = bool(self._renew(self.ttl))
                except Exception as exc:  # noqa: BLE001 - database hiccup: retry the next beat
                    self._failed(exc)
                    continue
                if not renewed:
                    self.lost = True
                    if self.failures:
                        print("ameesh: merge lease %s lost after %d failed renewal(s) "
                              "(first: %s)" % (self.key, self.failures, self.first_error),
                              file=sys.stderr)
                    return
                if self.failures:
                    print("ameesh: merge lease %s renewed again after %d failed renewal(s)"
                          % (self.key, self.failures), file=sys.stderr)
                    self.failures, self.first_error = 0, ""

    def _failed(self, exc: BaseException) -> None:
        self.failures += 1
        if self.failures == 1:
            self.first_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            print("ameesh: merge lease %s not renewed (%s); retried every %.0fs, "
                  "it expires in %.0fs without a renewal"
                  % (self.key, self.first_error, self.interval, self.ttl), file=sys.stderr)

    def cause(self) -> str:
        """#3993: why the heartbeat could not vouch for the lease, or ''."""
        if not self.failures:
            return ""
        return "lease heartbeat: %d failed renewal(s), first: %s" % (
            self.failures, self.first_error)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Never hold the pass on a heartbeat blocked in a slow database
            # call: the thread is a daemon and a late renewal of a released
            # lease is a no-op (the epoch/owner check refuses it).
            thread.join(timeout=5.0)

    def carry(self, bound: float) -> bool:
        """#3993: keep beating after the pass, for `bound` seconds at most;
        False when the lease is already lost (nothing to carry)."""
        if not self._lock.acquire(timeout=5.0):
            return False          # a beat blocked in the database: not vouched for
        try:
            if self.lost or self._stop.is_set():
                return False
            self._until = time.monotonic() + max(0.0, float(bound))
            return True
        finally:
            self._lock.release()


#: #3993: heartbeats carried between two passes, per `(key, owner)`. Only
#: this process's next pass takes one back; a dead process carries nothing.
_CARRIED: dict = {}
_CARRIED_LOCK = threading.Lock()


def _carry(key: str, owner: str, renewal: _PassLeaseRenewal) -> bool:
    if not renewal.carry(LOCK_TTL_S):
        return False
    with _CARRIED_LOCK:
        previous = _CARRIED.pop((key, owner), None)
        _CARRIED[(key, owner)] = renewal
    if previous is not None and previous is not renewal:
        previous.stop()
    return True


def _reclaim(key: str, owner: str) -> None:
    """#3993: the next pass stops the carried heartbeat before renewing the
    lease itself; whatever it held is renewed (or lost) by that pass."""
    with _CARRIED_LOCK:
        carried = _CARRIED.pop((key, owner), None)
    if carried is not None:
        carried.stop()


class _Landing:
    """One landing under the lease `(key, owner, epoch)` (#194 r5): every
    queue write is a compare-and-swap on the lease, in the same statement — a
    stale holder's writes fail, and it reports `busy`; the lease is released
    at the end of the pass, except where the lot waits on its PR or on a
    merge in doubt: those paths keep it for the next pass (`land_head`)."""

    def __init__(self, db, project, row, key, owner, epoch, locks, projects=(),
                 cfg=None, clone=None, replayer=None, parts=None, fence=None,
                 ttl=LOCK_TTL_S, renewal=None):
        self.db, self.project, self.row = db, project, row
        self.lot = int(row["work_item_id"])
        self.ops = storage.of(db).work
        self.key, self.owner, self.epoch, self.locks = key, owner, epoch, locks
        self.ttl = float(ttl)
        self.renewal = renewal
        #: #3932: the paths that leave the lot `landing` on its PR — waiting
        #: for its CI, a refused merge, a merge in doubt — keep the target
        #: reserved between passes. Every other path releases it at the end
        #: of the pass; `land_head` applies the flag in its `finally`.
        self.keep = False
        self.projects = projects
        self.cfg, self.clone = cfg, clone
        #: L165 (EP4) : garde du bail élu, appliquée juste avant chaque effet
        #: externe (push de la cible, push de la branche, fusion de la PR).
        self.fence = fence
        #: L1651 : le clone radar (pour relire la source de sélection versionnée
        #: de la cible) et les parts dues déjà requalifiées par l'appelant
        #: (§12 : le manifeste de paquet remplace la CI individuelle)
        self.replayer, self.parts = replayer, parts
        self.entry = tuple(row.get(k) for k in
                           ("generation", "target", "frozen_id", "frozen_sha"))

    @property
    def lease(self):
        return (self.key, self.owner, self.epoch)

    def busy(self, why: str) -> dict:
        self.locks.pop(self.key, None)
        cause = self.renewal.cause() if self.renewal is not None else ""
        return {"lot": self.lot, "result": "busy",
                "error": "%s (%s)" % (why, cause) if cause else why}

    def retry(self, why: str) -> dict:
        """#2306 : un refus passager garde la ligne en file (état `queued`,
        raison datée) et la rend au passage suivant.

        #2345 : le nombre de réessais est borné (`TRANSIENT_RETRY_LIMIT`) ;
        au-delà, la ligne est éjectée en fermé (rien n'a été fusionné) et
        l'assigné est réveillé au lieu de tourner indéfiniment."""
        attempts = _retry_attempts(self.ops, self.lot,
                                   self.row.get("queued_ts")) + 1
        if attempts > TRANSIENT_RETRY_LIMIT:
            return self.eject(
                "%s (still failing after %d retries: giving up)"
                % (why, TRANSIENT_RETRY_LIMIT),
                "The forge read kept failing at each pass: the line is ejected "
                "fail-closed (nothing was merged). Check the forge or the token, then "
                "requeue the lot (`ameesh work queue add %d`)." % self.lot)
        if not self.write(
                state="queued",
                reason="%s: retried at the next pass (retry %d/%d)"
                       % (why, attempts, TRANSIENT_RETRY_LIMIT),
                note="%s %d/%d: %s" % (RETRY_NOTE, attempts, TRANSIENT_RETRY_LIMIT, why),
                actor=work.SYSTEM_SENDER):
            return self.busy("lease on %s lost: retry not written" % self.key)
        return {"lot": self.lot, "result": "retry", "error": why}

    def held(self) -> bool:
        """The lease is still ours, renewed, right before a push or a merge.

        #3932: the pass heartbeat is the first proof; a lease it saw lost is
        never relied on again, even if this second renewal happened to work.
        """
        if self.renewal is not None and self.renewal.lost:
            self.locks.pop(self.key, None)
            return False
        if self.ops.land_lock(self.key, self.owner, self.ttl, epoch=self.epoch) is not None:
            return True
        self.locks.pop(self.key, None)
        return False

    def queue_gone(self) -> bool:
        """#3550: the queue entry this landing follows is no longer there, or
        has been replaced (another generation, target or freeze). The stale
        snapshot from the pass never authorizes a merge."""
        entry = next((r for r in self.ops.queue()
                      if int(r["work_item_id"]) == self.lot), None)
        return entry is None or tuple(entry.get(k) for k in (
            "generation", "target", "frozen_id", "frozen_sha")) != self.entry

    def fence_hold(self) -> None:
        """L165 (EP4) : renouvelle le bail élu juste avant l'effet externe.

        Refusé (`LandError`) si l'époque a changé, si le bail n'a pas la marge
        de l'appel, ou si le rôle s'est arrêté. L'effet externe n'est alors
        jamais atteint ; `land_head` journalise l'erreur et rend la ligne.
        """
        if self.fence is None:
            return
        from . import pilot_gesture
        try:
            self.fence.hold(self.db, margin_s=AGENT_CALL_MARGIN_S)
        except pilot_gesture.GestureRefused as exc:
            raise LandError(str(exc)) from exc

    def write(self, **kw) -> bool:
        return self.ops.queue_update(self.lot, lease=self.lease, entry=self.entry, **kw) is not None

    def eject(self, reason: str, todo: str, **extra) -> dict:
        row = self.row
        if not self.write(state="ejected", reason=reason,
                          note="merge queue of %s: ejected, %s" % (row["target"], reason),
                          actor=work.SYSTEM_SENDER):
            return self.busy("lease on %s lost: ejection not written" % self.key)
        sent = merge_queue._wake(self.db, row.get("assignee"), self.lot, (
            "Merge queue of %s, lot #%d « %s »: ameesh could not merge it, %s. %s"
            % (row["target"], self.lot, row.get("title") or "", reason, todo)),
            {"project": self.project.name, "target": row["target"], "kind": "land",
             "lot": self.lot, "reason": reason})
        return dict({"lot": self.lot, "to": row.get("assignee"), "kind": "ejected",
                     "message_id": sent, "result": "ejected"}, **extra)

    def record(self, reason: str, note: str, *, state: str = "landed") -> str:
        # Once the remote merge may have happened, persistence errors must
        # never send execution back through the pre-merge retry/reset path.
        for _ in range(2):
            try:
                return self._record(reason, note, state=state)
            except Exception as exc:  # noqa: BLE001 - post-merge fact, retry once
                error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        merge_queue._wake(self.db, self.row.get("assignee"), self.lot,
                          "Merge outcome could not be recorded after two attempts: %s. %s. "
                          "Check the remote merge and queue by hand." % (reason, error),
                          {"project": self.project.name, "target": self.row["target"],
                           "kind": "land", "lot": self.lot, "reason": UNCERTAIN + error})
        return "unrecorded"

    def _record(self, reason: str, note: str, *, state: str = "landed") -> str:
        """#194 r8: a merge that happened is a fact, recorded whatever became
        of the lease: under the lease; else under the freeze (the lot still
        `landing` on the same freeze and landing commit); else — a newer
        freeze or entry is there, never overwritten — as history only, a
        line in the lot's journal. Returns `lease`, `freeze` or `history`."""
        row = self.row
        freeze = (row["frozen_sha"], row.get("landing_sha"))
        if self.write(state=state, reason=reason, note=note, actor=work.SYSTEM_SENDER, freeze=freeze):
            return "lease"
        if self.ops.queue_update(self.lot, state=state, reason=reason, note=note,
                                 actor=work.SYSTEM_SENDER,
                                 freeze=freeze, entry=self.entry) is not None:
            return "freeze"
        self.ops.note(self.lot, row.get("item_state") or "", "%s (history: %s)" % (reason, note),
                      work.SYSTEM_SENDER)
        return "history"

    def required_ci(self):
        if getattr(self.project, "ci_required", False):
            try:
                parts = self.parts
                if parts is None and getattr(self.project, "ci_parts", None):
                    # L1651 (#1650) : la preuve de complétude est refaite ici,
                    # sur la cible de cette entrée de file, avec la source de
                    # sélection versionnée relue maintenant (le contrat peut
                    # avoir bougé depuis l'admission).
                    parts = merge_queue._parts_reader(
                        self.project, self.project.repo, self.row.get("target") or "",
                        self.row["frozen_sha"], self.replayer, cfg=self.cfg)
                merge_queue._required_ci(self.project, self.row["frozen_sha"], parts=parts,
                                         branch=self.row.get("branch"))
            except merge_queue.QueueError as exc:
                return self.eject(str(exc), "Restore green CI on the frozen commit, then "
                                  "ameesh work queue add %d." % self.lot)
        return None

    def review_after_merge(self, *, merged_sha: str = "", pr=None) -> dict:
        """L539 : ouvre le lot de relecture d'un lot **léger** (canon §2), une
        seule fois, après que la fusion est un fait enregistré. La classe est
        relue sur le diff que la fusion a fait entrer (`class_at_merge`) —
        jamais `cible..gel`, vide une fois le gel devenu ancêtre de la cible ;
        un `light` qui touche la porte de fusion ouvre deux avis (#2374). Ne
        lève jamais."""
        from . import review_lot
        try:
            item = work.get(self.db, self.lot)
            repo = self.clone or self.project.repo
            klass = review_lot.class_at_merge(
                self.db, self.lot, item=item, repo=repo,
                target=self.row.get("target") or "", sha=merged_sha)
            # #2585 : le dépôt sert aussi de preuve que le gel relu est entré
            # dans le commit fusionné.
            return review_lot.open_after_merge(
                self.db, lot=self.lot, klass=klass, item=item, merged_sha=merged_sha,
                pr=pr, target=self.row.get("target") or "", repo=repo)
        except Exception as exc:  # noqa: BLE001 - la fusion est un fait, jamais défait
            return {"lot": self.lot, "id": None, "created": False, "class": "",
                    "why": "lot de relecture non ouvert : %s: %s"
                           % (type(exc).__name__, str(exc)[:200])}

    def landed(self, merged: dict, base, *, pr=None) -> dict:
        row, project = self.row, self.project
        # #1682: a merge GitHub's auto-merge made at the frozen head, before
        # the queue replayed anything, is constated: the queue's gates never
        # ran on the merged tree, and the record must not say they did
        constated = merged.get("state") == "constated"
        who = "merged by GitHub auto-merge, constated by ameesh" if constated else "merged by ameesh"
        reason = "%s (%s)" % (who, merged["sha"][:12])
        gates_text = ", ".join("`%s`" % g for g in project.gates)
        _passive(self.db, row.get("assignee"), self.lot, (
            "Merge queue of %s, lot #%d « %s »: %s (%s%s)%s. %s The branch sweep closes the lot."
            % (row["target"], self.lot, row.get("title") or "", who, merged["sha"][:12],
               ", PR #%s" % pr if pr else ", on %s" % (base or "?")[:12],
               "; resolutions replayed by rerere on %s" % ", ".join(merged["files"][:6])
               if merged.get("state") == "replayed" else "",
               "The queue's gates (%s) did not run on the merged tree: only the PR's "
               "required checks did." % gates_text if constated
               else "Gates passed on the merged tree: %s." % gates_text)),
            {"project": project.name, "target": row["target"], "commit": merged["sha"]})
        how = self.record(reason, "merge queue of %s: %s, %s%s" % (
            row["target"], "constated GitHub auto-merge" if constated else "landed by ameesh",
            merged["sha"][:12], ", PR #%s" % pr if pr else ""))
        review = self.review_after_merge(merged_sha=str(merged.get("sha") or ""), pr=pr)
        return dict({"lot": self.lot, "result": "landed", "sha": merged["sha"], "pr": pr,
                     "review_lot": review},
                    **({"warning": "queue entry changed: recorded as history only (a newer entry is "
                        "queued)"} if how == "history" else {"warning": "remote merge succeeded; recording failed"}
                       if how == "unrecorded" else {}))


def land_head(cfg, db, project, row: dict, *, owner: str, replayer=None, locks=None,
              gh=_gh, parts=None) -> dict:
    """Lands (or follows the landing of) `row`, the head of its queue.
    Returns `{lot, result: landed|landing|retry|ejected|taken-out|busy|error, …}`.
    `taken-out` (#3550): the queue entry was removed or replaced since the
    pass read it; nothing is merged.

    L1651 (#1650) : `parts` porte la requalification d'un lot dont la CI
    individuelle est remplacée par celle d'un paquet (§12, #1467) — les
    contextes dus que l'appelant atteste, ou `()` ; None : la porte du projet
    (`ci_parts`) s'applique telle quelle."""
    from . import radar, resolutions
    locks = locks if locks is not None else {}
    lot = int(row["work_item_id"])
    source = merge_queue.normalize_source(row.get("source") or "")
    if not source or source != merge_queue.source_of(project.repo):
        return {"lot": lot, "result": "error", "error": (
            "queue source %s does not match project %s repository; select the source "
            "project (or requeue a legacy entry without a source)"
            % (source or "<missing>", project.name))}
    # L165 (EP4) : faire atterrir la file est un geste du pilote hors base (le
    # push suit). Un passage radar n'a pas d'identité de session : sans bail
    # élu à vérifier, `gate` ne fait rien. Le bail est renouvelé juste avant
    # chaque push/merge dans `_Landing.fence_hold`.
    from . import pilot_gesture
    try:
        fence = pilot_gesture.gate(cfg, db, "queue", "land #%d" % lot,
                                   project=project.name)
    except pilot_gesture.GestureRefused as exc:
        return {"lot": lot, "result": "error", "error": str(exc)}
    projects = tuple(radar.settings_of(cfg).projects.values())
    key = lock_key(project, row["target"])
    ops = storage.of(db).work
    # #3550: `row` is the pass's snapshot, not the queue's current state. A
    # take-out (or a replacement entry) before this landing means there is no
    # work left to land: stop before taking the lease or reading the forge.
    identity = ("generation", "target", "frozen_id", "frozen_sha")
    current = next((r for r in ops.queue() if int(r["work_item_id"]) == lot), None)
    if current is None or tuple(current.get(k) for k in identity) != \
            tuple(row.get(k) for k in identity):
        return {"lot": lot, "result": "taken-out", "detail": (
            "the queue entry of lot #%d was removed or replaced before its landing: "
            "nothing merged" % lot)}
    # #3932: the lease only covers this ONE pass. It is taken for the short
    # pass TTL and renewed by the heartbeat below while the pass runs; a pass
    # that dies stops renewing and frees the target at that bound, well before
    # the train's 30 min. The epoch fence is unchanged: a late holder's queue
    # writes and its `held()` checks fail at the new epoch. #3993: the
    # heartbeat a previous pass carried is stopped first; this pass renews.
    _reclaim(key, owner)
    epoch = ops.land_lock(key, owner, LAND_PASS_TTL_S, epoch=locks.get(key)) \
        if key in locks else None
    if epoch is None:
        epoch = ops.land_lock(key, owner, LAND_PASS_TTL_S)
    if epoch is None:
        locks.pop(key, None)
        return {"lot": lot, "result": "busy"}
    locks[key] = epoch
    renewal = _PassLeaseRenewal(
        lambda ttl: ops.land_lock(key, owner, ttl, epoch=epoch) is not None,
        ttl=LAND_PASS_TTL_S, key=key)
    landing = None
    try:
        renewal.start()
        if replayer is None:
            replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name),
                                            target=project.target, db=db)
        landing = _Landing(db, project, row, key, owner, epoch, locks, projects,
                           cfg=cfg, clone=getattr(replayer, "path", None),
                           replayer=replayer, parts=parts, fence=fence,
                           ttl=LAND_PASS_TTL_S, renewal=renewal)
        slug = radar.github_slug(project) if project.land == "pr" else None
        try:
            merge_queue.radar_project(cfg, project.repo)
        except merge_queue.QueueError as exc:
            return landing.eject(str(exc), "Resolve the repository landing configuration, "
                                 "then ameesh work queue add %d." % lot)
        glob = radar.protected_target(project, row["target"])
        if glob:
            # L160g (#473): queued before the target was protected — never
            # landed; ejected under the lease, like any ejection
            return landing.eject("%s is a protected target (%s)" % (row["target"], glob),
                                 "Merging into %s is the orchestrator's gesture: ask it."
                                 % row["target"])
        # #2428: the merge, the gates and the push read and write the radar
        # clone's worktree. Another pass, a stacked trial or a turn's command
        # may share that clone: the lock keeps one tree per operation. A busy
        # clone surfaces as CloneBusy (a GitError), and the line is retried.
        with replayer.locked():
            return _land(landing, replayer, slug, gh)
    except (GitError, LandError, OSError, ValueError) as exc:
        error = {"lot": lot, "result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        if landing is not None:
            if row["state"] == "landing" and project.land == "pr":
                # its branch holds the merge commit: it stays `landing`, the
                # target stays reserved, and the PR is read again at the next
                # pass (an error never merges)
                landing.keep = True
                landing.write(reason="reading PR failed (%s): retried" % str(exc)[:200])
            else:
                note = {} if isinstance(exc, resolutions.CloneBusy) \
                    else {"note": _failure_note(str(exc)), "actor": work.SYSTEM_SENDER}
                landing.write(state="queued",
                              reason="landing failed (%s): retried" % str(exc)[:200], **note)
        return error
    finally:
        # #3932: the lease only covers the pass, except where the lot waits on
        # its PR (CI pending, refused merge) or on a merge in doubt: there it
        # stays held, so neither the train nor a direct fire can merge into
        # the target under a PR whose CI qualified an older base. #3993: it is
        # held by the same short heartbeat, carried until the next pass of
        # this process (at most `LOCK_TTL_S`): a runner that dies frees it at
        # the short bound. Every other exit — result, early return, exception
        # — releases it. A release that fails is not fatal: without its
        # heartbeat, the short TTL frees the target at a new epoch (the old
        # holder is fenced by its epoch).
        if landing is not None and landing.keep and _carry(key, owner, renewal):
            pass
        elif landing is not None and landing.keep:
            renewal.stop()
            print("ameesh: merge lease %s not kept for the next pass (%s)"
                  % (key, renewal.cause() or "lost"), file=sys.stderr)
            locks.pop(key, None)
        else:
            renewal.stop()
            try:
                ops.land_unlock(key, owner, epoch)
            except Exception as exc:  # noqa: BLE001 - the TTL still frees the lease
                print("ameesh: merge lease %s not released (%s: %s); it expires in %.0fs"
                      % (key, type(exc).__name__, exc, LAND_PASS_TTL_S), file=sys.stderr)
            locks.pop(key, None)


def _land(landing: _Landing, replayer, slug, gh) -> dict:
    from . import radar, resolutions
    project, row, lot = landing.project, landing.row, landing.lot
    if row["state"] == "landing" and project.land == "pr":
        return _follow_pr(landing, slug, gh=gh, replayer=replayer)

    replayer.ensure()
    url = _origin(project) or project.repo
    branch = _branch_of(row["target"])
    lease_ref = (getattr(project, "lease_ref", "") or LEASE_REF) + branch
    mine = None
    if project.land == "local":
        prefix = getattr(project, "lease_ref", "") or LEASE_REF
        targets = {branch}
        for other in landing.projects:
            targets.add(other.target or plan_git.default_target(other.repo))
        # Queue targets can differ from settings (a lot's branch_target).
        # Reserve lease namespaces across all configured projects/queues,
        # even if two checkout paths happen to hide the same remote.
        targets.update(r["target"] for r in landing.ops.queue())
        collision = next((t for t in sorted(targets) if radar.lease_collides(prefix, t)), None)
        if collision is not None:
            return {"lot": lot, "result": "error", "error": (
                "lease ref %s collides with the target %s: nothing written; set "
                "radar.projects.%s.lease_ref under %s" % (
                    lease_ref, collision, project.name, " or ".join(radar.LEASE_NAMESPACES)))}
    if project.land == "local":
        # #194 r6: the remote lease ref is the lock itself, taken (compare and
        # swap) BEFORE any write of this holder; the database expiry only
        # allows trying. Refused: nothing written, the lease released
        try:
            mine = take_remote_lease(replayer.path, url, lease_ref, landing.key,
                                     landing.epoch, landing.owner)
        except _LeaseRefused as exc:
            return {"lot": lot, "result": "error", "error": (
                "%s: nothing written, retried; if the remote refuses this namespace, set "
                "radar.projects.%s.lease_ref to a branch prefix (e.g. ameesh-land/)"
                % (exc, project.name))}
    # land: local — a lot left `landing` (lease lost or runner gone) is
    # landed again from the start: the push to the target is never forced
    if not landing.write(state="landing", note="merge queue of %s: landing (%s)"
                         % (row["target"], project.land), actor=work.SYSTEM_SENDER):
        return landing.busy("lease on %s lost before landing" % landing.key)
    if project.land == "pr":
        found = None
        status = pr_status(slug, row["branch"], gh=gh, project=project) if slug else None
        if status is None:
            # #1682: an armed auto-merge can merge the PR between two passes,
            # at the exact frozen head. That merge is a fact like any other:
            # found here, it is recorded instead of ejecting the queue entry.
            # A merge at any other head (the #1162 rewrite) finds no match and
            # keeps the ejection below.
            found = pr_merged(slug, row["branch"], row["frozen_sha"], gh=gh) if slug else None
            if found is not None:
                return _after_merge(landing, found["number"], found["base"], None,
                                    sha=row["frozen_sha"])
            return landing.eject("no open PR for %s" % row["branch"],
                                 "Open the PR, then ameesh work queue add %d." % lot)
        # #1656: never leave an active auto-merge on a PR the queue manages. It
        # would merge whatever head it finds — a rewritten one included, even
        # under `[skip ci]` — with no new fire, freeze or parts proof (GitHub
        # checks `--match-head-commit` only when the auto-merge is armed). ameesh
        # merges itself, on the exact frozen head; disarm first, fail closed —
        # before any ejection below (conflict, gate, CI), which would otherwise
        # leave it armed for the head the author pushes next.
        if status.get("auto"):
            error = pr_disable_auto(slug, status["number"], gh=gh)
            if error is not None:
                # #1682: the auto-merge may have fired between the read and
                # the disarm — the disarm then fails on an already merged PR.
                # A merge at the frozen head into its target is constated;
                # anything else keeps the fail-closed ejection.
                found, unread = _merged_after_disarm(slug, row["branch"], row["frozen_sha"], gh)
                if found is not None:
                    return _after_merge(landing, found["number"], found["base"], None,
                                        sha=row["frozen_sha"])
                return landing.eject(
                    "PR #%s: auto-merge armed and not disarmed (%s)%s"
                    % (status["number"], error, unread),
                    "Disarm it by hand (gh pr merge --disable-auto %s), then ameesh work "
                    "queue add %d: an armed auto-merge merges a head the queue has not "
                    "qualified (#1656)." % (status["number"], lot))
    base = fresh_target(replayer, project, row["target"])
    if project.land == "local" and resolutions._run(
            replayer.path, "merge-base", "--is-ancestor", row["frozen_sha"],
            base).returncode == 0:
        # #194 r4: another executor (an earlier epoch) already pushed it.
        # #1776: ancestry alone does not prove the content is still in the
        # tree — a composition reverted on the target leaves the freeze an
        # ancestor without its content. Judge the content as the branch sweep
        # does (plan_git.probe, #1825's revert proof) and eject with a way
        # forward rather than record a delivery that is no longer there.
        found = plan_git.probe(replayer.path, row["frozen_sha"], base, lot=lot)
        if not found["merged"]:
            return landing.eject(
                "the frozen content is no longer in %s: %s"
                % (row["target"], found["detail"]),
                "Rebase the branch on %s, freeze again at constant content, then "
                "ameesh work queue add %d." % (row["target"], lot))
        return landing.landed({"sha": base, "state": "already", "files": []}, base)
    refused = landing.required_ci()
    if refused is not None:
        return refused
    merged = merge(replayer, base, row, landing.ops.resolutions([lot]),
                   generated=getattr(project, "generated", ()))
    if merged["state"] == "generated":
        return landing.eject("generated file %s: regeneration or check failed on the merged "
                             "tree" % merged["files"][0],
                             "Its output:\n%s\nFix the generator or its inputs on your branch, "
                             "freeze again, then ameesh work queue add %d."
                             % (merged["output"], lot), output=merged["output"])
    if merged["state"] == "conflict":
        return landing.eject("conflict on %s with %s at %s" % (
            ", ".join(merged["files"][:6]) or "?", row["target"], base[:12]),
            "Integrate %s into your branch (or prepare the resolution), freeze again, "
            "then ameesh work queue add %d." % (row["target"], lot))
    checked = gates(project, replayer.path, files=changed(replayer.path, base, merged["sha"]))
    if not checked["ok"]:
        return landing.eject("gate `%s` failed on the merged tree" % checked["gate"],
                             "Its output:\n%s\nFix it on your branch, freeze again, then "
                             "ameesh work queue add %d." % (checked["output"], lot),
                             output=checked["output"])
    refused = landing.required_ci()
    if refused is not None:
        return refused
    if project.land == "local":
        if not landing.held():
            return landing.busy("lease on %s lost before pushing" % landing.key)
        stopped = _fire_guard(landing, gh=gh)
        if stopped is not None:
            return stopped
        # L165 (EP4) : bail élu renouvelé juste avant le push externe.
        landing.fence_hold()
        # #194 r6: one atomic push, two compare-and-swaps that the server
        # checks: the target on the base seen, the lease ref on the value this
        # holder wrote — moved to a child commit, so git sends that update
        # and receive-pack checks its old value (an unchanged ref is skipped)
        target_ref = "refs/heads/%s" % branch
        after = lease_commit(replayer.path, landing.key, landing.epoch, landing.owner,
                             parent=mine, landed=lot)
        proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify", "--atomic",
                                "--force-with-lease=%s:%s" % (target_ref, base),
                                "--force-with-lease=%s:%s" % (lease_ref, mine), url,
                                "%s:%s" % (merged["sha"], target_ref),
                                "%s:%s" % (after, lease_ref), timeout=FETCH_TIMEOUT)
        if proc.returncode != 0:
            if not landing.held():
                return landing.busy("lease on %s lost: push refused" % landing.key)
            if not landing.write(state="queued", reason="target moved during the merge: "
                                 "retried at the next pass", note="merge queue of %s: push "
                                 "refused (%s), retried" % (row["target"],
                                                            resolutions._err(proc)[-200:]),
                                 actor=work.SYSTEM_SENDER):
                return landing.busy("lease on %s lost: push refused" % landing.key)
            return {"lot": lot, "result": "retry", "error": resolutions._err(proc)}
        return landing.landed(merged, base)
    # land: pr — the merge commit goes to the lot branch, its CI decides
    if status.get("base") != branch:
        return landing.eject("PR #%s targets %s, not %s" % (
            status["number"], status.get("base") or "?", branch),
            "Point the PR at %s (or queue the lot for its target), then ameesh work "
            "queue add %d." % (branch, lot))
    if not merge_queue._same(status["head"], row["frozen_sha"]):
        return landing.eject("PR #%s is at %s, not at the freeze %s" % (
            status["number"], (status["head"] or "?")[:12], row["frozen_sha"][:12]),
            "Something pushed to the branch after its freeze: freeze again, then "
            "ameesh work queue add %d." % lot)
    if not landing.held():
        return landing.busy("lease on %s lost before pushing" % landing.key)
    stopped = _fire_guard(landing, gh=gh)
    if stopped is not None:
        return stopped
    # L165 (EP4) : bail élu renouvelé juste avant le push de la branche.
    landing.fence_hold()
    # #194 r4: compare and swap on the freeze (see `local`)
    # #3080: publish the landing identity before its head can be seen on the
    # forge. Otherwise fire can observe the push while the row is still
    # queued, mistake our merge for a third-party push and withdraw the fire.
    if not landing.write(state="landing", landing_sha=merged["sha"]):
        return landing.busy("lease on %s lost before pushing" % landing.key)
    branch_ref = "refs/heads/%s" % row["branch"]
    proc = resolutions._run(replayer.path, "push", "--quiet", "--no-verify",
                            "--force-with-lease=%s:%s" % (branch_ref, row["frozen_sha"]),
                            url, "%s:%s" % (merged["sha"], branch_ref),
                            timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        if not landing.held():
            return landing.busy("lease on %s lost: push refused" % landing.key)
        return landing.eject("push to %s refused (%s)" % (
            row["branch"], resolutions._err(proc)[-200:]),
            "Something pushed to the branch after its freeze: freeze again, then "
            "ameesh work queue add %d." % lot)
    if not landing.write(state="landing", landing_sha=merged["sha"],
                         reason="PR #%s: CI on the merge %s" % (status["number"],
                                                                merged["sha"][:12]),
                         note="merge queue of %s: %s merged into the branch (%s), waiting "
                         "for the CI of PR #%s" % (row["target"], row["target"],
                                                   merged["sha"][:12], status["number"]),
                         actor=work.SYSTEM_SENDER):
        return landing.busy("lease on %s lost after pushing" % landing.key)
    # #3932: the merge commit is on the branch, the PR now waits for its CI:
    # keep the target reserved between passes.
    landing.keep = True
    return {"lot": lot, "result": "landing", "sha": merged["sha"],
            "replayed": merged["files"] if merged["state"] == "replayed" else []}


def target_moved(replayer, project, row: dict) -> str | None:
    """#194 r4 (PR mode): the target's tip now, when it is no longer the base
    of the merge commit `landing_sha` (its first parent); None if it has not
    moved. Raises (LandError, GitError) when either cannot be read: the lot
    stays `landing` and is read again (never a merge on an unknown base)."""
    from . import resolutions
    url = _origin(project) or project.repo
    proc = resolutions._run(project.repo, "ls-remote", url,
                            "refs/heads/%s" % _branch_of(row["target"]), timeout=FETCH_TIMEOUT)
    tip = resolutions._text(proc).split()
    if proc.returncode != 0 or not tip:
        raise LandError("git ls-remote %s: %s" % (_branch_of(row["target"]),
                                                  resolutions._err(proc)[:200] or "no tip"))
    parent = resolutions._run(replayer.path, "rev-parse", "%s^1" % row["landing_sha"])
    if parent.returncode != 0:
        replayer.ensure()
        resolutions._run(replayer.path, "fetch", "--quiet", url,
                         "+refs/heads/%s:%s" % (row["branch"], FRESH_REF + "-lot"),
                         timeout=FETCH_TIMEOUT)
        parent = resolutions._run(replayer.path, "rev-parse", "%s^1" % row["landing_sha"])
    base = resolutions._check(parent, "rev-parse")
    return None if tip[0] == base else tip[0]


def pr_view(slug: str, number, gh=_gh) -> dict:
    """#194 r5: base and head of PR `number`, read together in one call."""
    proc = gh(["pr", "view", str(number), "--repo", slug, "--json",
               "baseRefName,headRefOid,state"])
    if proc.returncode != 0:
        raise LandError("gh pr view: %s" % (proc.stderr or proc.stdout or "").strip()[:300])
    data = _json(proc, "gh pr view", dict)
    return {"base": data.get("baseRefName"), "head": data.get("headRefOid"),
            "state": data.get("state")}


def merged_base(replayer, project, branch: str) -> str:
    """#2603 : la cible par défaut du dépôt, quand `branch` y est déjà
    fusionnée ; sinon "".

    La file exige déjà que la PR vise la cible du lot (`branch`) ; restait le
    cas d'une cible de travail elle-même déjà fusionnée dans la cible par
    défaut : fusionner la PR dessus pose le contenu sur une branche morte
    (home#112/#113 après la fusion de canon/2452). La cible par défaut est lue
    dans le dépôt du projet ; une cible configurée introuvable refuse
    (`LandError`) plutôt que de laisser fusionner à l'aveugle. Un `fetch`
    impossible refuse : relire la seule cible du lot ne prouve pas que la
    branche par défaut est lisible."""
    from . import resolutions

    base = _branch_of(branch or "")
    try:
        default = _branch_of(plan_git.default_target(project.repo) or "")
    except plan_git.MergeProbeError as exc:
        raise LandError("cannot read the default target of %s: %s: not landing on %s"
                        % (project.repo, exc, base or "?")) from exc
    if not base:
        return ""
    if not default:
        raise LandError("cannot determine the default target of %s: not landing on %s"
                        % (project.repo, base))
    if base == default:
        return ""
    url = _origin(project) or project.repo
    refs = ("refs/ameesh/land/base", "refs/ameesh/land/default")
    proc = resolutions._run(replayer.path, "fetch", "--quiet", "--no-tags", url,
                            "+refs/heads/%s:%s" % (base, refs[0]),
                            "+refs/heads/%s:%s" % (default, refs[1]),
                            timeout=FETCH_TIMEOUT)
    if proc.returncode != 0:
        raise LandError("cannot compare stacked base %s with %s: %s"
                        % (base, default, resolutions._err(proc)[:200]))
    compared = resolutions._run(replayer.path, "merge-base", "--is-ancestor",
                                refs[0], refs[1])
    if compared.returncode not in (0, 1):
        raise LandError("cannot compare stacked base %s with %s: %s"
                        % (base, default, resolutions._err(compared)[:200]))
    return default if compared.returncode == 0 else ""


def _follow_pr(landing: _Landing, slug, *, gh, replayer) -> dict:
    project, row, lot = landing.project, landing.row, landing.lot
    branch = _branch_of(row["target"])
    status = pr_status(slug, row["branch"], gh=gh, project=project) if slug else None
    if status is None and slug and row.get("landing_sha"):
        # #194 r8: merged since (a merge call that timed out, or between two
        # passes): checked like any merge of ameesh's, never ejected
        merged = pr_merged(slug, row["branch"], row["landing_sha"], gh=gh)
        if merged is not None:
            return _after_merge(landing, merged["number"], merged["base"], None)
    if status is None:
        if row.get("item_state") in work.MERGED_STATES:
            if not landing.write(state="landed", reason="merged",
                                 note="merge queue of %s: landed" % row["target"],
                                 actor=work.SYSTEM_SENDER):
                return landing.busy("lease on %s lost" % landing.key)
            review = landing.review_after_merge(merged_sha=_merged_sha(landing.db, lot))
            return {"lot": lot, "result": "landed", "review_lot": review}
        return landing.eject("its PR is no longer open",
                             "Reopen it, then ameesh work queue add %d." % lot)
    # #1656: an armed auto-merge would merge the head it finds without the
    # parts proof and without the base re-read below — and would land a head
    # rewritten after the fire. ameesh is the only actor that merges a PR it
    # manages: disarm first, fail closed.
    if status.get("auto"):
        error = pr_disable_auto(slug, status["number"], gh=gh)
        if error is not None:
            # #1682: the auto-merge may have fired between the read and the
            # disarm (the disarm then fails on a merged PR): a merge at the
            # landing head is constated, like the "merged since" path above.
            merged, unread = _merged_after_disarm(slug, row["branch"], row["landing_sha"], gh)
            if merged is not None:
                return _after_merge(landing, merged["number"], merged["base"], None)
            return landing.eject(
                "PR #%s: auto-merge armed and not disarmed (%s)%s — it could merge a head "
                "the queue has not qualified" % (status["number"], error, unread),
                "Disarm it by hand (gh pr merge --disable-auto %s), then ameesh work "
                "queue add %d; ameesh merges itself on the exact frozen head (#1656)."
                % (status["number"], lot))
    if status.get("base") != branch:
        return landing.eject("PR #%s targets %s, not %s" % (
            status["number"], status.get("base") or "?", branch),
            "Point the PR at %s, freeze again, then ameesh work queue add %d." % (branch, lot))
    stale_base = merged_base(replayer, project, branch)
    if stale_base:
        return landing.eject(
            "PR #%s targets %s, already merged into %s: merging it there would land its "
            "content on a dead branch" % (status["number"], branch, stale_base),
            "Retarget the PR (`gh pr edit --base %s %s`), freeze again, then "
            "ameesh work queue add %d." % (stale_base, status["number"], lot))
    if not merge_queue._same(status["head"], row.get("landing_sha")):
        return landing.eject("its branch moved while ameesh was landing it (%s)"
                             % (status["head"] or "?")[:12],
                             "Freeze again, then ameesh work queue add %d." % lot)
    if status["ci"] == "pending":
        # #3932: the PR waits for its CI on the merge commit: the target stays
        # reserved between passes, so no train or fire merges under a CI that
        # qualified an older base; the next pass renews the lease.
        landing.keep = True
        return {"lot": lot, "result": "landing", "pr": status["number"]}
    if status["ci"] == "red":
        return landing.eject("the CI of PR #%s is red on the merge %s"
                             % (status["number"], row["landing_sha"][:12]),
                             "Fix it on your branch (it already holds the merge of %s), "
                             "freeze again, then ameesh work queue add %d."
                             % (row["target"], lot))
    if not landing.held():
        return landing.busy("lease lost before merging PR #%s" % status["number"])
    # #194 r4: the CI ran on the merge with the target as it was
    moved = target_moved(replayer, project, row)
    if moved:
        return landing.eject("%s moved to %s since the merge %s: its CI ran on an older "
                             "base" % (row["target"], moved[:12], row["landing_sha"][:12]),
                             "Integrate %s again into your branch, freeze again, then "
                             "ameesh work queue add %d." % (row["target"], lot))
    # #194 r5: base and head read again together, right before the merge.
    # GitHub's merge takes no base nor epoch precondition: the window left —
    # between this read and `gh pr merge` — is a residual risk the owner
    # accepts or not (land: local has none); a wrong base is checked after
    now = pr_view(slug, status["number"], gh=gh)
    if now["base"] != branch or not merge_queue._same(now["head"], row["landing_sha"]):
        return landing.eject("PR #%s changed right before its merge (base %s, head %s)" % (
            status["number"], now["base"] or "?", (now["head"] or "?")[:12]),
            "Check the PR, freeze again, then ameesh work queue add %d." % lot)
    refused = landing.required_ci()
    if refused is not None:
        return refused
    if not landing.held():
        return landing.busy("lease lost before merging PR #%s" % status["number"])
    stopped = _fire_guard(landing, gh=gh)
    if stopped is not None:
        return stopped
    # L165 (EP4) : bail élu renouvelé juste avant `gh pr merge` ; la fusion
    # GitHub n'est pas dans la transaction, d'où la marge.
    landing.fence_hold()
    error = pr_merge(slug, status["number"], row["landing_sha"], project.merge_method, gh=gh)
    # #194 r8: past this call, the PR may be merged: nothing raises any more,
    # and every outcome is read back from GitHub
    try:
        now = pr_view(slug, status["number"], gh=gh)
        after, unread = now["base"], None
    except Exception as exc:  # noqa: BLE001 - the merge may have happened
        now, after, unread = None, None, "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if error is not None:
        if error.startswith(TIMED_OUT) and (now is None or now["state"] != "MERGED"):
            detail = unread if now is None else (
                "read-back state %s; remote operation may complete later" % now["state"])
            return _merged_in_doubt(landing, status["number"], None,
                                    "%s; %s" % (error[:200], detail), pending=True)
        if now is not None and now["state"] != "MERGED":
            # #194 r3: the branch holds the merge commit; the lot stays
            # `landing` with it (and the lease: a lasting refusal raises
            # `merge_blocked`); a merge completed later is found by `pr_merged`
            landing.keep = True
            landing.write(reason="PR merge refused (%s): retried" % error[:200])
            return {"lot": lot, "result": "retry", "error": error}
        if now is None:
            landing.keep = True
            landing.write(reason="PR merge refused (%s), PR unreadable (%s): retried"
                          % (error[:200], unread))
            return {"lot": lot, "result": "retry", "error": error}
    return _after_merge(landing, status["number"], after, unread)


def _after_merge(landing: _Landing, pr, after, unread, *, sha: str | None = None) -> dict:
    """#194 r8: PR `pr` is merged (by this call or found merged): landed if
    into its target, otherwise reported. `sha` names the merged head when the
    caller knows it (#1682: a merge found before the queue pushed its own
    merge commit, recorded as constated; the default is the `landing_sha`
    the queue recorded)."""
    state = "pr" if sha is None else "constated"
    if sha is None:
        sha = landing.row["landing_sha"]
    if after != _branch_of(landing.row["target"]):
        return _merged_in_doubt(landing, pr, after, unread)
    return landing.landed({"sha": sha, "state": state, "files": []}, None, pr=pr)


def _merged_sha(db, lot: int) -> str:
    """Le sha de fusion EFFECTIVE du lot, si connu (chemin « déjà fusionné »
    de `_follow_pr`, L539). Un jalon `merged` annulé par une correction
    `merged → build` est ignoré (#2317/N5) : sa fusion n'est pas la bonne."""
    try:
        rows = work.milestones(db, int(lot))
    except Exception:  # noqa: BLE001 - jalon illisible : sha inconnu
        return ""
    for row in rows or ():
        if (str(row.get("kind") or "") == "merged" and row.get("sha")
                and not row.get("annulled")):
            return str(row["sha"])
    return ""


#: #194 r6: reasons of a PR merged where it should not have been, or that
#: cannot be checked — the urgent alert `land_uncertain` reads them
WRONG_BASE = "WRONG BASE: "
UNCERTAIN = "UNCERTAIN: "


def _merged_in_doubt(landing: _Landing, pr, after, unread, *, pending=False) -> dict:
    """The PR is merged, but into another base (retargeted inside the last
    window) or its base cannot be read back; `pending`: its merge call timed
    out and completion is still unknown (OPEN or unreadable). The warning goes first,
    whatever happens to the lease; then the fact, under the lease or the
    freeze, else as history only — never over a newer freeze (#194 r8).
    `land_uncertain` alerts on it. Pending, the lot stays `landing` (the next
    pass reads it again: `pr_merged`)."""
    row, lot, branch = landing.row, landing.lot, _branch_of(landing.row["target"])
    reason = (WRONG_BASE + "PR #%s merged into %s, not %s: check it by hand"
              % (pr, after, branch)) if after else (
        UNCERTAIN + "PR #%s merge timed out, completion uncertain (%s): read again at the "
        "next pass, check it by hand" % (pr, unread)) if pending else (
        UNCERTAIN + "PR #%s merged, base unreadable after the merge (%s): check it by hand"
        % (pr, unread))
    merge_queue._wake(landing.db, row.get("assignee"), lot, (
        "Merge queue of %s, lot #%d: %s." % (row["target"], lot, reason)),
        {"project": landing.project.name, "target": row["target"], "kind": "land",
         "lot": lot, "reason": reason})
    how = landing.record(reason, "merge queue of %s: %s" % (row["target"], reason),
                         state="landing" if pending else "landed")
    # #3932: a merge whose completion is still unknown keeps the target
    # reserved between passes; the next pass reads the PR again.
    landing.keep = pending
    return {"lot": lot, "result": "uncertain" if after is None else "landed",
            "wrong_base": after, "pr": pr, "reason": reason, "recorded": how}


#: #194 r8: how long a doubt recorded as history only (a newer freeze queued
#: over it) stays an alert
UNCERTAIN_HISTORY_S = float(os.environ.get("AMEESH_LAND_UNCERTAIN_H", "24")) * 3600


def uncertain(db, now: float | None = None) -> list[dict]:
    """#194 r6: lots merged by ameesh into a wrong base, or not checked: in
    the queue, or (#194 r8) as history only, for `UNCERTAIN_HISTORY_S`."""
    now = time.time() if now is None else now
    ops = storage.of(db).work
    try:
        rows = ops.queue()
    except Exception:  # noqa: BLE001 - database without 0058
        return []
    out = [r for r in rows if (r.get("reason") or "").startswith((WRONG_BASE, UNCERTAIN))]
    seen = {int(r["work_item_id"]) for r in out}
    queued = {int(r["work_item_id"]): r for r in rows}
    for prefix in (WRONG_BASE, UNCERTAIN):
        try:
            noted = ops.events_noted(prefix, now - UNCERTAIN_HISTORY_S)
        except Exception:  # noqa: BLE001 - read again at the next pass
            noted = []
        for event in noted:
            lot = int(event["work_item_id"])
            if lot in seen or "(history: " not in (event.get("note") or ""):
                continue
            seen.add(lot)
            out.append(dict(queued.get(lot) or {}, work_item_id=lot,
                            assignee=event.get("assignee"),
                            reason=event["note"], updated_ts=event.get("created_ts")))
    return out


def run_pass(cfg, db, project, report: dict, *, owner: str, locks=None, gh=_gh) -> list[dict]:
    """After the queue pass (`report["queue"]`): lands the head of each
    queue of `project`, when it declares `land`. Never raises.

    #3560: a row the train enrolled (`packet_allocated`, its `packet` receipt
    on the current generation) is the train's: the train lands it with its
    packet (`train_git.promote`). The queue pass never lands it, whatever its
    state: landing it alone ejected it for want of a lot CI (`ci_required`,
    impossible on `agent/**` under §11 bis), and the ejected row then refused
    every later enrollment of the lot (#2376, #3537)."""
    if project.land == "off":
        return []
    out = []
    try:
        rows = {int(r["work_item_id"]): r for r in storage.of(db).work.queue()}
    except Exception as exc:  # noqa: BLE001 - database without 0058
        return [{"result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}]
    for target, listed in sorted(((report.get("queue") or {}).get("targets") or {}).items()):
        head = next((rows[o["lot"]] for o in listed if o["lot"] in rows
                     and rows[o["lot"]]["state"] in ("queued", "landing")
                     and not rows[o["lot"]].get("packet_allocated")), None)
        if head is None:
            continue
        try:
            out.append(land_head(cfg, db, project, head, owner=owner, locks=locks, gh=gh))
        except Exception as exc:  # noqa: BLE001 - never fatal for the pass
            out.append({"lot": int(head["work_item_id"]), "result": "error",
                        "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


# --------------------------------------------------------------------------
# alert merge_blocked
# --------------------------------------------------------------------------

BLOCKED_AFTER_S = float(os.environ.get("AMEESH_MERGE_BLOCKED_MIN", "30")) * 60


def _failure_note(error: str) -> str:
    """#5179 : la cause d'un échec, comparable d'un passage à l'autre."""
    found = _GIT_LOCK_ERROR.search(error)
    if found is None:
        return FAILURE_NOTE + "other"
    return "%slock %s:%s" % (FAILURE_NOTE, socket.gethostname(), found.group(1))


def lock_stuck(db, now: float | None = None) -> list[dict]:
    """#5179 : les lignes en file dont les `GIT_LOCK_ALERT_AFTER` derniers échecs
    tiennent au même verrou git (`git_lock` : hôte:fichier, `failures`).

    `blocked` ne les voit pas : chaque réessai rafraîchit `updated_ts`."""
    now = time.time() if now is None else now
    ops = storage.of(db).work
    out = []
    try:
        rows = [r for r in ops.queue() if r["state"] == "queued"]
    except Exception:  # noqa: BLE001 - database without 0058
        return []
    for row in rows:
        lot = int(row["work_item_id"])
        try:
            notes = ops.events_noted(FAILURE_NOTE, float(row.get("queued_ts") or 0), ids=[lot])
        except Exception:  # noqa: BLE001 - journal illisible : pas de conclusion
            continue
        causes = [str(n.get("note") or "") for n in notes]
        if not causes or not causes[-1].startswith(FAILURE_NOTE + "lock "):
            continue
        if now - float(notes[-1].get("created_ts") or 0) > GIT_LOCK_ALERT_FRESH_S:
            continue
        failures = 0
        for cause in reversed(causes):
            if cause != causes[-1]:
                break
            failures += 1
        if failures >= GIT_LOCK_ALERT_AFTER:
            out.append(dict(row, git_lock=causes[-1][len(FAILURE_NOTE) + 5:],
                            failures=failures))
    return out


def blocked(db, now: float | None = None) -> list[dict]:
    """Heads of queue (rank 1, or landing) that have not moved for
    `BLOCKED_AFTER_S`: the queue is stuck behind them.

    #3569: a row the train enrolled (`packet_allocated`) is not one: the
    queue pass skips it (`run_pass`, #3560) and the train moves it with its
    packet, whose CI may run longer than this delay."""
    now = time.time() if now is None else now
    try:
        rows = storage.of(db).work.queue()
    except Exception:  # noqa: BLE001 - database without 0058
        return []
    return [r for r in rows if r["state"] in ("queued", "landing")
            and not r.get("packet_allocated")
            and (r["state"] == "landing" or r.get("rank") == 1)
            and now - float(r.get("updated_ts") or now) >= BLOCKED_AFTER_S]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "land", help="merge the head of its queue now (L160e2; the executor does it at each "
                     "radar pass for a project that declares `land`)")
    parser.add_argument("id", type=int)
    parser.add_argument("--project", default=None,
                        help="the radar project (default: the only one declared)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    import socket
    import sys
    from . import radar
    try:
        projects = radar.settings_of(cfg).projects
        if args.project:
            project = projects.get(args.project)
        else:
            project = next(iter(projects.values())) if len(projects) == 1 else None
        if project is None:
            raise LandError("name the project (--project): declared here: %s"
                            % (", ".join(sorted(projects)) or "none (radar.projects)"))
        if project.land == "off":
            raise LandError("project %s does not declare `land` (local | pr)" % project.name)
        rows = [r for r in storage.of(db).work.queue()
                if int(r["work_item_id"]) == args.id and r["state"] in ("queued", "landing")]
        if not rows:
            raise LandError("lot #%d is not in a queue" % args.id)
        row = rows[0]
        if row.get("packet_allocated"):
            # #3560 : la ligne d'un lot inscrit par le train est portée par le
            # train, jamais atterrie seule (même règle que `run_pass`).
            raise LandError("lot #%d was enrolled by the train: its packet lands it, not the "
                            "queue" % args.id)
        if row["state"] == "queued" and row.get("rank") != 1:
            raise LandError("lot #%d is ranked %s in the queue of %s, not 1"
                            % (args.id, row.get("rank") or "—", row["target"]))
        out = land_head(cfg, db, project, row, owner="%s:%d" % (socket.gethostname(),
                                                                 os.getpid()))
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
              "lot #%d: %s%s" % (args.id, out["result"],
                                 " (%s)" % out.get("error") if out.get("error") else ""))
        return 0 if out["result"] in ("landed", "landing") else 1
    except (LandError, merge_queue.QueueError, GitError, radar.SettingsError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
