# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit assembly and inactive-by-default runner service (#1467).

The pilot must supply the qualified policy commit, complete CI contract and
exact provenance provider. No live profile or provider selection is installed.
A passage receives a fresh scoped SQL connection;
only its fencing epoch survives between calls, never a closed connection.
"""
from __future__ import annotations

import os
import math
import threading
import time
from dataclasses import replace

from . import (merge_queue, radar, resolutions, train, train_admissions,
               train_ci, train_fire, train_git, train_history, train_policy,
               train_runtime, train_store)


class Passage:
    def __init__(self, cfg, project, *, owner, policy, policy_path,
                 policy_commit, workflows, provider, mechanics, notify):
        if not isinstance(owner, str) or not owner.strip():
            raise train.TrainError("explicit runner reservation owner required")
        if not isinstance(policy, train.Policy) or not callable(notify):
            raise train.TrainError("explicit train policy and diagnostic callback required")
        self.cfg, self.project, self.owner = cfg, project, owner
        self.policy, self.policy_path, self.policy_commit = policy, policy_path, policy_commit
        self.workflows, self.provider, self.notify = workflows, provider, notify
        self.mechanics = mechanics
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
        if not self.policy.build_beta:
            raise train.TrainError("runner passage requires compiled canon cutoffs")
        target = merge_queue._remote_branch(self.project.target)
        if not target:
            raise train.TrainError("explicit project target required for runner passage")
        if self.mechanics is None:
            raise train.TrainError("explicit compiled canon mechanics provider required")
        try:
            rules = self.mechanics.load(self.policy_path, self.policy_commit, self.project.name)
        except self.mechanics.MechanicsError as exc:
            raise train.TrainError("cannot load compiled canon mechanics: %s" % exc) from exc
        if (rules.commit, rules.project) != (self.policy_commit, self.project.name):
            raise train.TrainError("compiled mechanics belongs to another commit or project")
        if rules.ci_packet != "full":
            raise train.TrainError("train requires full packet CI in the compiled canon")
        policy = replace(self.policy, schedule=rules)
        requirement = train_policy.Compiled(rules)
        ci = train_ci.GitHub(radar.github_slug(self.project), self.workflows)
        source = merge_queue.source_of(self.project.repo)
        # The radar, another target, and another runner may all replay merges
        # concurrently. They must never reset this passage's worktree.
        directory = os.path.join(self.cfg.state_dir, "trains", train.digest(
            [self.project.name, source, target, self.owner]) + ".clone")
        clone = resolutions.Replayer(self.project.repo, directory, script=None)
        repository = train_git.Repository(self.project, clone)
        ledger = train_store.Ledger(db, self.project, target, self.owner)
        history = train_history.Recorded(db, clone.path, provider=self.provider, review_rules=rules)
        admissions = train_admissions.Admissions(db, self.project, history, policy=requirement)
        return train_runtime.Runtime(ledger=ledger, repository=repository, ci=ci,
            fires=train_fire.Fires(self.cfg, ledger), admissions=admissions,
            target=target, policy=policy, notify=self.notify)

    def step(self, db, now):
        """One bounded event/pass; never wait for CI and never retain ``db``."""
        if not self.policy.enabled:
            return {"result": "off"}
        runtime = self.runtime(db)
        return self._step(runtime, now)

    def scheduled_step(self, db, now):
        """Use the very same compiled object for execution and the next wake."""
        if not self.policy.enabled:
            return {"result": "off"}, None
        runtime = self.runtime(db)
        deadline = runtime.policy.schedule.next_cutoff(now)
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= now:
            raise train.TrainError("compiled canon must provide a future cutoff")
        return self._step(runtime, now), deadline

    def _step(self, runtime, now):
        engine = train.Engine(runtime, source=runtime.repository.source,
                              target=runtime.target_branch, policy=runtime.policy)
        engine.epoch = self.epoch
        try:
            return engine.step(now)
        finally:
            self.epoch = engine.epoch


class Service:
    """One independent runner task: startup, canon cutoffs and explicit events.

    No timer polls CI or reviews. A producer supplies explicit ``event`` hints;
    a failed passage waits for a new event rather than retrying by itself.
    Connections and runtime ports never survive a passage.
    """
    def __init__(self, cfg, passages, *, stop, report, clock=time.time):
        self.cfg, self.passages = cfg, tuple(passages)
        self.stop, self.report, self.clock = stop, report, clock
        self.condition = threading.Condition()
        self.pending = set(range(len(self.passages)))
        self.deadlines = {}
        self.thread = None

    def wake(self):
        with self.condition:
            self.pending.update(range(len(self.passages)))
            self.condition.notify_all()

    def event(self, *, kind, project, target):
        """Untrusted hint only: Passage rereads all exact proofs before effects.

        Transport/producer registration is intentionally separate. A terminal
        CI event, historical verdict or fire change never supplies authority.
        """
        if kind not in ("ci-terminal", "verdict", "fire") or self.stop.is_set():
            return False
        selected = {i for i, p in enumerate(self.passages)
                    if (p.project.name, merge_queue._remote_branch(p.project.target))
                    == (project, target)}
        if not selected:
            return False
        with self.condition:
            self.pending.update(selected)
            self.condition.notify_all()
        return True

    def pass_once(self, index, now):
        from .scoped_db import ScopedDb
        if self.stop.is_set():
            return None
        db = ScopedDb(self.cfg)
        try:
            _, deadline = self.passages[index].scheduled_step(db, now)
            return deadline
        except Exception as exc:
            self.report("train passage failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        finally:
            db.close()

    def run(self):
        while not self.stop.is_set():
            with self.condition:
                if self.stop.is_set():
                    return
                now = self.clock()
                ready = self.pending | {i for i, at in self.deadlines.items() if at <= now}
                if not ready:
                    delay = min(self.deadlines.values()) - now if self.deadlines else None
                    self.condition.wait(timeout=delay)
                    continue
                self.pending.difference_update(ready)
            for index in sorted(ready):
                if self.stop.is_set():
                    return
                deadline = self.pass_once(index, self.clock())
                with self.condition:
                    self.deadlines.pop(index, None)
                    if deadline is not None:
                        self.deadlines[index] = deadline

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="integration-trains")
        self.thread.start()

    def shutdown(self):
        # The runner sets its stop event before calling this method.
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
