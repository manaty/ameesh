# SPDX-License-Identifier: AGPL-3.0-only
"""#6161 : associer au gel la seule branche publiée portant sa tête."""
from __future__ import annotations

import subprocess

MISSING_MARK = "ameesh-freeze-missing-branch:"


def missing_warning(item_id: int | None = None) -> str:
    """Shared machine marker and a shell-safe, concrete lot identifier."""
    lot = str(item_id) if item_id is not None else "LOT_ID"
    return ("%s no branch associated: the composer will not see this lot; "
            "set it with ameesh work branch %s BRANCH_NAME" % (MISSING_MARK, lot))


def published_branch(repo: str, sha: str, *, item_id: int | None = None) -> tuple[str | None, str]:
    """Lecture réseau avant la transaction ; seule une tête exacte fait preuve.

    Un ancêtre commun ne permet pas de choisir la branche de travail. Les refs
    locales périmées, tags et alias distants HEAD ne sont jamais des candidats.
    """
    from .work import WorkError

    lot = str(item_id) if item_id is not None else "LOT_ID"
    remedy = "ameesh work branch %s BRANCH_NAME" % lot
    warning = missing_warning(item_id)
    if not repo:
        return None, warning + " (no readable repository)"
    candidates = set()
    try:
        remotes = subprocess.run(
            ["git", "-C", repo, "remote"], capture_output=True, text=True,
            timeout=20, check=False)
        if remotes.returncode:
            return None, warning + " (remotes unavailable)"
        names = remotes.stdout.splitlines()
        if not names:
            return None, warning + " (no configured remote)"
        for remote in names:
            result = subprocess.run(
                ["git", "-C", repo, "ls-remote", "--heads", "--", remote],
                capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                return None, warning + " (remote heads unavailable)"
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2:
                    return None, warning + " (invalid remote heads response)"
                head, ref = fields
                if head.lower() == sha.lower() and ref.startswith("refs/heads/"):
                    candidates.add(ref.removeprefix("refs/heads/"))
    except (OSError, subprocess.TimeoutExpired):
        return None, warning + " (remote heads unavailable)"
    candidates = sorted(candidates)
    if len(candidates) > 1:
        raise WorkError("freeze refused (branch-ambiguous): several published branches "
                        "point to %s: %s; associate one with %s before freezing"
                        % (sha[:12], ", ".join(candidates), remedy))
    return (candidates[0], "") if candidates else (None, warning + " (no published tip matches)")


def attach_hook(item_id: int, branch: str, sha: str, actor: str, following):
    """Association et gel sous la même transaction, sans écraser un choix."""
    def hook(tx, milestone, after_commit):
        from . import work
        attached = work.attach_implicit_branch(
            tx, item_id, branch, source="inferred at freeze %s (#6161)" % sha[:12], actor=actor)
        if attached is None:
            current = work.get(tx, item_id)
            if not current or current.get("branch") != branch:
                raise work.WorkError(
                    "freeze refused (branch-not-associated): branch changed or was detached; "
                    "associate the branch explicitly before freezing")
        if following is not None:
            following(tx, milestone, after_commit)
    return hook


def guard_ownership(db, item: dict, sha: str, *, branch: str = "", repo: str = "",
                    exclude_freeze_id: int | None = None) -> None:
    """#6834 : refuser l'emprunt d'une tête ou branche d'un autre lot ouvert.

    Un regel de sa propre tête et les têtes partagées dans un paquet ouvert
    enregistré ne sont pas des emprunts. Ces exceptions ne prêtent jamais
    la branche d'un autre lot. Dans le hook transactionnel, exclure le jalon
    qui vient d'être inséré : il ne prouve pas une propriété antérieure.
    """
    from . import packet_link, plan_git, storage, work, work_lifecycle

    st = storage.of(db)
    others = [row for state in work_lifecycle.STATES
              if state not in work_lifecycle.COMPLETED
              for row in st.work.items(state=state, assignee=None, limit=None)
              if int(row["id"]) != int(item["id"])
              and row.get("state") not in work_lifecycle.COMPLETED]
    if not others:
        return
    marks = {}
    for mark in st.progress.lot_milestones(
            [int(item["id"]), *[int(row["id"]) for row in others]]):
        if mark.get("id") != exclude_freeze_id:
            marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    own_freeze = plan_git.latest_freeze(marks.get(int(item["id"]), [])) or {}
    own_head = str(own_freeze.get("sha") or "").lower() == sha.lower()
    packages = {int(member["package_id"])
                for member in packet_link.for_item(db, int(item["id"]))
                if member.get("package_state") not in work_lifecycle.COMPLETED}
    packages.add(int(item["id"]))  # le lot courant peut être porteur
    peers = set()
    for member in packet_link.memberships(db, package_ids=sorted(packages)):
        if member.get("package_state") not in work_lifecycle.COMPLETED:
            peers.update((int(member["package_id"]), int(member["work_item_id"])))

    def repository(row):
        try:
            return work.lot_repository(row, db.cfg)
        except Exception:  # dépôt non résolu : ne comparer que l'app connue
            return "app:" + str(row.get("app") or "")

    for other in others:
        frozen = plan_git.latest_freeze(marks.get(int(other["id"]), [])) or {}
        reason = ""
        heads = {str(frozen.get("sha") or "").lower(),
                 str(other.get("branch_head") or "").lower()}
        other_branch = str(other.get("branch") or "").strip()
        if repo and other_branch and repository(item) == repository(other):
            for ref in ("refs/heads/" + other_branch, "refs/remotes/origin/" + other_branch):
                head = plan_git.local_commit(repo, ref)
                if head:
                    heads.add(head.lower())
        if sha.lower() in heads and not own_head and int(other["id"]) not in peers:
            reason = "head %s" % sha[:12]
        elif (branch and branch == str(other.get("branch") or "").strip()
              and repository(item) == repository(other)):
            reason = "branch %s" % branch
        if reason:
            raise work.WorkError(
                "freeze refused (owned-by-open-lot): %s belongs to open lot #%s; "
                "create and associate this lot's own branch with ameesh work branch %s "
                "BRANCH_NAME, commit its delta and freeze that head; "
                "for notes only, close this lot as done instead of freezing"
                % (reason, other["id"], item["id"]))
