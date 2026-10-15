# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit assembly for one runner passage (#1467), not installed or enabled.

The pilot must supply the qualified policy commit, complete CI contract and
exact provenance provider. No live profile, provider selection or runner hook
is installed here. A passage receives the runner's current SQL connection;
only its fencing epoch survives between calls, never a closed connection.
"""
from __future__ import annotations

import os

from . import (merge_queue, radar, resolutions, train, train_admissions,
               train_ci, train_fire, train_git, train_history, train_policy,
               train_runtime, train_store)


class Passage:
    def __init__(self, cfg, project, *, owner, policy, policy_path,
                 policy_commit, workflows, provider, notify):
        if not isinstance(owner, str) or not owner.strip():
            raise train.TrainError("explicit runner reservation owner required")
        if not isinstance(policy, train.Policy) or not callable(notify):
            raise train.TrainError("explicit train policy and diagnostic callback required")
        self.cfg, self.project, self.owner = cfg, project, owner
        self.policy, self.policy_path, self.policy_commit = policy, policy_path, policy_commit
        self.workflows, self.provider, self.notify = workflows, provider, notify
        self.epoch = None

    def runtime(self, db):
        """Bind all ports to this passage's storage and private worktree.

        Construction reads the pinned canon object, but creates no clone or
        reservation and starts no CI. Disabled callers use ``step`` instead.
        """
        if self.provider is None:
            raise train.TrainError("qualified exact provenance provider required")
        if self.policy.ci_mode != "packet":
            raise train.TrainError("runner passage requires packet CI; no lot CI adapter installed")
        target = merge_queue._remote_branch(self.project.target)
        if not target:
            raise train.TrainError("explicit project target required for runner passage")
        requirement = train_policy.Versioned(self.policy_path, self.policy_commit, self.project.name)
        ci = train_ci.GitHub(radar.github_slug(self.project), self.workflows)
        source = merge_queue.source_of(self.project.repo)
        # The radar, another target, and another runner may all replay merges
        # concurrently. They must never reset this passage's worktree.
        directory = os.path.join(self.cfg.state_dir, "trains", train.digest(
            [self.project.name, source, target, self.owner]) + ".clone")
        clone = resolutions.Replayer(self.project.repo, directory, script=None)
        repository = train_git.Repository(self.project, clone)
        ledger = train_store.Ledger(db, self.project, target, self.owner)
        history = train_history.Recorded(db, clone.path, provider=self.provider)
        admissions = train_admissions.Admissions(db, self.project, history, policy=requirement)
        return train_runtime.Runtime(ledger=ledger, repository=repository, ci=ci,
            fires=train_fire.Fires(self.cfg, ledger), admissions=admissions,
            target=target, policy=self.policy, notify=self.notify)

    def step(self, db, now):
        """One bounded event/pass; never wait for CI and never retain ``db``."""
        if not self.policy.enabled:
            return {"result": "off"}
        runtime = self.runtime(db)
        engine = train.Engine(runtime, source=runtime.repository.source,
                              target=runtime.target_branch, policy=self.policy)
        engine.epoch = self.epoch
        try:
            return engine.step(now)
        finally:
            self.epoch = engine.epoch
