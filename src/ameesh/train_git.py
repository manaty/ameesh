# SPDX-License-Identifier: AGPL-3.0-only
"""Git side of #1467, reusing land's merge and remote epoch fence.

No live configuration, dispatch or executor hook is installed by this module.
The caller supplies a private Replayer and holds the existing target lease.
"""
from __future__ import annotations

import json
import os
import subprocess

from . import land, merge_queue, plan_git, radar, resolutions, train_ci, train_transports
from .canon import FETCH_TIMEOUT, GitError
from .train import MemberDeferred, TrainError, digest, identity


class Repository:
    def __init__(self, project, replayer, *, transports=None):
        self.project, self.clone = project, replayer
        self.source = merge_queue.source_of(project.repo)
        self.transports = transports

    def _guard(self, target):
        branch = merge_queue._remote_branch(target)
        plan_git.check_ref(branch, "train target")
        if radar.protected_target(self.project, branch):
            raise TrainError("train target is protected")
        if branch == "release" or branch.startswith("release/"):
            raise TrainError("release targets cannot enter an automatic train")
        if branch.startswith("integration/"):
            raise TrainError("a train cannot target another integration branch")
        if merge_queue.source_of(self.project.repo) != self.source:
            raise TrainError("repository origin changed")
        return branch

    def target(self, target):
        self._guard(target)
        self.clone.ensure()
        return land.fresh_target(self.clone, self.project, target)

    def check_members(self, target, rows):
        branch = self._guard(target)
        if not rows or any(r["source"] != self.source or
                           merge_queue._remote_branch(r["target"]) != branch for r in rows):
            raise TrainError("train members belong to another source or target")
        tips, _ = merge_queue.fetch_fresh(self.project.repo, [r["branch"] for r in rows],
                                         self.clone)
        moved = ["#%s branch=%s frozen=%s published=%s" % (
                    r["work_item_id"], r["branch"], r["frozen_sha"],
                    tips.get(r["branch"]) or "missing")
                 for r in rows if tips.get(r["branch"]) != r["frozen_sha"]]
        if moved:
            raise TrainError("member moved beyond its exact freeze: " + "; ".join(moved))

    def _transport(self, target, row, base, *, expected=None):
        try:
            receipt = train_transports.lookup(self.transports, source=self.source,
                target=target, row=row, base=base, path=self.clone.path, expected=expected)
            if receipt is None:
                return None
            if plan_git._commit(self.clone.path, receipt["head"]) != receipt["head"]:
                ref = "refs/ameesh/train-transports/" + digest(receipt)
                source = land._origin(self.project) or self.project.repo
                proc = resolutions._run(self.clone.path, "fetch", "--quiet", "--no-tags",
                    source, "+%s:%s" % (receipt["ref"], ref), timeout=FETCH_TIMEOUT)
                if proc.returncode or plan_git._commit(self.clone.path, ref) != receipt["head"]:
                    raise TrainError("qualified transport object unavailable or ref moved")
            train_transports.verify(self.clone.path, receipt, row)
            return receipt
        except (TrainError, GitError) as exc:
            raise MemberDeferred(row, "transport #%s base=%s: %s" % (
                row["work_item_id"], base, exc)) from exc

    def validate_transports(self, plan):
        members = {row["work_item_id"]: row for row in plan["members"]}
        receipts = train_transports.for_plan(plan)
        for lot, receipt in receipts.items():
            self._transport(plan["target"], members[lot], receipt.get("base"), expected=receipt)
        return list(receipts.values())

    def compose(self, target, base, rows, nonce="", *, transports=None):
        branch = self._guard(target)
        if not rows or self.target(branch) != base:
            raise TrainError("composition requires members and the exact current base")
        self.check_members(branch, rows)
        if transports is not None and not isinstance(transports, list):
            raise TrainError("persisted transport list required for reconstruction")
        pending = list(transports or [])
        head, receipts = base, []
        for row in rows:
            expected = next((r for r in pending if isinstance(r, dict) and
                             digest(r.get("admission")) == digest(identity(row))), None)
            receipt = (self._transport(branch, row, head, expected=expected)
                       if transports is None or expected is not None else None)
            if receipt is not None:
                head = receipt["head"]
                receipts.append(receipt)
                if expected is not None:
                    pending.remove(expected)
                continue
            merged = land.merge(self.clone, head, row, [],
                                generated=getattr(self.project, "generated", ()))
            if merged["state"] != "clean":
                # A resolution needs exact-delta qualification before the
                # adapter can accept it; never inherit a pair's old approval.
                raise MemberDeferred(row, "train conflict needs a reviewed delta: "
                                 "#%s base=%s frozen=%s state=%s files=%s" % (
                                     row["work_item_id"], head, row["frozen_sha"],
                                     merged["state"], ", ".join(merged.get("files", ()))))
            # Stable intermediate identities let an exact transport context
            # survive reconstruction after the first publication was lost.
            tree = plan_git._out(self.clone.path, "rev-parse", merged["sha"] + "^{tree}")
            head = self._commit(tree, list(dict.fromkeys([head, row["frozen_sha"]])),
                "Train intermediate\n\n" + json.dumps(dict(source=self.source,
                    target=branch, base=head, admission=identity(row)), sort_keys=True))
        if pending:
            raise TrainError("persisted transport no longer belongs to this composition")
        resolutions._check(resolutions._run(self.clone.path, "checkout", "-q", "-f", "--detach", head),
                           "checkout composed train")
        checked = land.gates(self.project, self.clone.path,
                             files=land.changed(self.clone.path, base, head))
        if not checked["ok"]:
            raise TrainError("train gate failed: %s" % checked["gate"])
        # Gates are checks, never unreviewed edits to the merged tree.
        dirty = resolutions._run(self.clone.path, "status", "--porcelain", "--untracked-files=no")
        if dirty.returncode or resolutions._text(dirty).strip():
            raise TrainError("train gate changed tracked files")
        tree = resolutions._check(resolutions._run(self.clone.path, "rev-parse", head + "^{tree}"),
                                   "train tree")
        # One deterministic final commit allows another host to reconstruct
        # an intent saved just before publication without inventing a new SHA.
        message = "Integration train\n\n" + json.dumps(
            dict(source=self.source, target=branch, base=base, nonce=nonce,
                 members=[identity(r) for r in rows]), sort_keys=True) + "\n\n"
        if receipts:
            message += "transport-proof: " + digest(receipts) + "\n\n"
        message += "".join("ameesh-work: %d\n" % r["work_item_id"] for r in rows)
        parents = list(dict.fromkeys([base] + [r["frozen_sha"] for r in rows]
                                     + [r["head"] for r in receipts]))
        result = dict(head=self._commit(tree, parents, message), tree=tree)
        if receipts:
            result["transports"] = receipts
        return result

    def _commit(self, tree, parents, message):
        if not isinstance(tree, str) or not merge_queue._HEX.fullmatch(tree) \
                or len(tree) not in (40, 64):
            raise TrainError("exact composed tree required")
        env = dict(os.environ, GIT_AUTHOR_NAME="ameesh train",
                   GIT_AUTHOR_EMAIL="train@ameesh.invalid", GIT_COMMITTER_NAME="ameesh train",
                   GIT_COMMITTER_EMAIL="train@ameesh.invalid",
                   GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
        proc = subprocess.run(["git", "-C", self.clone.path, "commit-tree", tree,
                               *(arg for p in parents for arg in ("-p", p))],
                              input=message, text=True, capture_output=True, env=env,
                              timeout=FETCH_TIMEOUT, check=False)
        if proc.returncode:
            raise TrainError("cannot create deterministic train commit")
        return proc.stdout.strip()

    def _validate(self, plan):
        branch = self._guard(plan["target"])
        self.clone.ensure()
        if plan["source"] != self.source:
            raise TrainError("plan source does not match repository")
        ref = "refs/heads/" + plan["branch"]
        if not plan["branch"].startswith("integration/train-"):
            raise TrainError("publication outside the train namespace")
        plan_git.check_ref(ref, "train branch")
        receipts = self.validate_transports(plan)
        if not plan_git._commit(self.clone.path, plan["head"]):
            rebuilt = self.compose(branch, plan["base"], plan["members"], plan.get("nonce", ""),
                                   transports=receipts)
            rebuilt = {key: rebuilt[key] for key in ("head", "tree")}
            if rebuilt != {"head": plan["head"], "tree": plan["tree"]}:
                raise TrainError("reconstructed train differs from persisted intent")
        tree = plan_git._out(self.clone.path, "rev-parse", plan["head"] + "^{tree}")
        if tree != plan["tree"]:
            raise TrainError("train head does not contain the recorded tree")
        if resolutions._run(self.clone.path, "merge-base", "--is-ancestor",
                             plan["base"], plan["head"]).returncode:
            raise TrainError("train is not a descendant of its exact base")
        parents = (plan_git._out(self.clone.path, "rev-list", "--parents", "-n", "1", plan["head"])
                   or "").split()[1:]
        if any(receipt["head"] not in parents for receipt in receipts):
            raise TrainError("packet lost a qualified transport parent")
        return branch, ref

    def _lease(self, plan, key, epoch, owner):
        branch, ref = self._validate(plan)
        url = land._origin(self.project) or self.project.repo
        lease_ref = self.project.lease_ref + branch
        if radar.lease_collides(self.project.lease_ref, branch):
            raise TrainError("lease namespace collides with target")
        mine = land.take_remote_lease(self.clone.path, url, lease_ref, key, epoch, owner)
        return branch, ref, url, lease_ref, mine

    def _read(self, url, ref):
        proc = resolutions._run(self.clone.path, "ls-remote", url, ref, timeout=FETCH_TIMEOUT)
        if proc.returncode:
            raise TrainError("cannot read remote train or target")
        rows = resolutions._text(proc).split()
        return rows[0] if rows else ""

    def publish(self, plan, *, key, epoch, owner, workflows):
        # Publication itself triggers push workflows. Reject a known stale
        # contract locally, before even updating the remote lease reference.
        self._validate(plan)
        train_ci.verify_local(self.clone.path, plan, workflows)
        branch, ref, url, lease_ref, mine = self._lease(plan, key, epoch, owner)
        if self._read(url, "refs/heads/" + branch) != plan["base"]:
            raise TrainError("target changed before train publication")
        old = self._read(url, ref)
        if old == plan["head"]:
            return
        if old:
            raise TrainError("immutable train ref already names another head")
        after = land.lease_commit(self.clone.path, key, epoch, owner, parent=mine)
        proc = resolutions._run(self.clone.path, "push", "--quiet", "--no-verify", "--atomic",
                                 "--force-with-lease=%s:" % ref,
                                 "--force-with-lease=%s:%s" % (lease_ref, mine), url,
                                 "%s:%s" % (plan["head"], ref), "%s:%s" % (after, lease_ref),
                                 timeout=FETCH_TIMEOUT)
        if proc.returncode:
            raise TrainError("atomic train publication refused")

    def promote(self, plan, *, key, epoch, owner):
        branch, ref, url, lease_ref, mine = self._lease(plan, key, epoch, owner)
        if self._read(url, ref) != plan["head"]:
            raise TrainError("train ref moved before promotion")
        target_ref = "refs/heads/" + branch
        old = self._read(url, target_ref)
        if old == plan["head"]:
            return  # reconcile an acknowledged or lost response
        if old != plan["base"]:
            raise TrainError("target moved: complete CI and fire no longer cover this merge")
        after = land.lease_commit(self.clone.path, key, epoch, owner, parent=mine)
        proc = resolutions._run(self.clone.path, "push", "--quiet", "--no-verify", "--atomic",
                                 "--force-with-lease=%s:%s" % (target_ref, plan["base"]),
                                 "--force-with-lease=%s:%s" % (lease_ref, mine), url,
                                 "%s:%s" % (plan["head"], target_ref),
                                 "%s:%s" % (after, lease_ref), timeout=FETCH_TIMEOUT)
        if proc.returncode:
            raise TrainError("atomic train promotion refused; reconcile remote outcome")
