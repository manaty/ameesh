# SPDX-License-Identifier: AGPL-3.0-only
"""Event-driven integration trains (#1467).

This module is deliberately not installed in the executor yet. Ports must
qualify historical authors/reviewers and the pilot's exact-head fire before
an enabled train can run. No text in a lot journal is an authorization.

One call does bounded work, never waits for CI. The persisted state precedes
each external effect; a restart reconciles the same immutable plan. Every
recomposition (including a diagnostic half) gets its own complete CI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Protocol

from . import merge_queue

SCHEMA = "ameesh-train/1"
TERMINAL = ("landed", "empty", "paused", "invalidated")


class TrainError(RuntimeError):
    """A missing proof or an uncertain effect: do not merge."""


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    interval_s: int = 3600
    threshold: int = 5
    # No implicit extra CI for the packet-replaces-lot-CI rule (canon §12).
    ci_mode: str = "packet"
    max_failures: int = 8

    def __post_init__(self):
        if self.ci_mode not in ("packet", "targeted"):
            raise ValueError("ci_mode: packet | targeted expected")
        if self.interval_s <= 0 or self.threshold <= 0 or self.max_failures <= 0:
            raise ValueError("positive train limits expected")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def identity(row: dict) -> tuple:
    """Requeueing the same SHA must still invalidate an old admission."""
    return tuple(row[k] for k in ("work_item_id", "generation", "target", "frozen_id",
                                  "frozen_sha", "source"))


def ordered(rows: list[dict], dependencies: list[dict]) -> list[dict]:
    """Stable queue order, with prerequisites first; blocked/cyclic lots wait.

    `dependencies` is the unbounded storage read, including finished edges.
    An absent open prerequisite is never assumed to be merged.
    """
    remaining = sorted(rows, key=lambda r: (r.get("priority") or 2,
                                            r.get("rank") or math.inf,
                                            r.get("queued_ts") or 0,
                                            r["work_item_id"]))
    needs: dict = {}
    for edge in dependencies:
        if edge["blocking"]:
            needs.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    done, result = set(), []
    while remaining:
        ready = next((r for r in remaining if needs.get(int(r["work_item_id"]), set()) <= done),
                     None)
        if ready is None:
            break
        result.append(ready)
        done.add(int(ready["work_item_id"]))
        remaining.remove(ready)
    return result


def due(rows: list[dict], now: float, policy: Policy, *, last_started: float = 0) -> bool:
    if not rows:
        return False
    # First train: at most an hour from the oldest admitted lot. Subsequent
    # trains: at most an hour since the preceding train, never an idle burst.
    since = max(last_started, min(float(r["queued_ts"]) for r in rows))
    return len(rows) >= policy.threshold or now - since >= policy.interval_s


class Ports(Protocol):
    """Trusted adapters; implementations must fail closed on unreadable facts.

    `qualify` checks exact freeze, all historical author/reviewer families,
    unresolved B/P2-sensitive, delta reviews, protected/release targets, fire
    (including withdrawal) and targeted CI only in that policy mode. It
    returns JSON evidence, not a boolean inferred from the latest verdict.
    `authorize` validates a structured pilot fire on (source,target,base,
    head,tree,members,proof); it serializes withdrawal with `promote`.
    """
    def acquire(self, epoch: int | None) -> int | None: ...
    def release(self, epoch: int) -> None: ...
    def load(self) -> dict | None: ...
    def save(self, state: dict, expected: int, epoch: int) -> dict: ...
    def target(self) -> str: ...
    def entries(self) -> list[dict]: ...
    def dependencies(self, rows: list[dict]) -> list[dict]: ...
    def qualify(self, rows: list[dict], base: str, mode: str) -> dict: ...
    def compose(self, base: str, rows: list[dict], nonce: str) -> dict: ...
    def publish(self, plan: dict, epoch: int) -> None: ...
    def start_ci(self, plan: dict) -> None: ...
    def ci(self, plan: dict) -> dict: ...
    def authorize(self, plan: dict) -> dict: ...
    def promote(self, plan: dict, fire: dict, epoch: int) -> None: ...
    def quarantine(self, rows: list[dict], reason: str, epoch: int) -> None: ...
    def notify(self, reason: str, state: dict) -> None: ...


class Engine:
    def __init__(self, ports: Ports, *, source: str, target: str, policy: Policy):
        self.ports, self.source, self.target, self.policy = ports, source, target, policy
        self.epoch: int | None = None

    def _save(self, state: dict) -> dict:
        return self.ports.save(state, int(state.get("revision", 0)), self.epoch)

    def _held(self):
        if self.ports.acquire(self.epoch) != self.epoch:
            raise TrainError("target reservation lost")

    def _plan(self, state, members, now, *, diagnostic=False, suspects=None):
        proof = self.ports.qualify(members, state["base"], self.policy.ci_mode)
        if not isinstance(proof, dict) or not proof:
            raise TrainError("missing structured admission evidence")
        number = len(state["attempts"]) + 1
        nonce = digest([self.source, self.target, state["base"], state["started_ts"],
                        [identity(r) for r in members], proof, number])
        merged = self.ports.compose(state["base"], members, nonce)
        # The adapter refuses conflicts unless their exact merged delta is
        # reviewed. No implicit rerere approval and no inferred CI transfer.
        for field in ("head", "tree"):
            value = merged.get(field, "")
            if not merge_queue._HEX.fullmatch(value) or len(value) not in (40, 64):
                raise TrainError("compose returned no full %s" % field)
        token = digest([self.source, self.target, state["base"],
                        [identity(r) for r in members], merged, proof, number])
        plan = dict(schema=SCHEMA, source=self.source, target=self.target, base=state["base"],
                    members=copy.deepcopy(members), proof=proof, head=merged["head"],
                    tree=merged["tree"], diagnostic=diagnostic,
                    suspects=copy.deepcopy(suspects if suspects is not None else members),
                    nonce=nonce,
                    branch="integration/train-%s-%s" % (state["stamp"], token[:16]),
                    id=token, created_ts=now)
        state["plan"] = plan
        state["phase"] = "publishing"
        state["attempts"].append(dict(id=token, head=plan["head"], tree=plan["tree"],
                                      members=[r["work_item_id"] for r in members],
                                      diagnostic=diagnostic))
        return self._save(state)

    def _current(self, state):
        plan = state["plan"]
        if self.ports.target() != plan["base"]:
            raise TrainError("target changed during reserved CI: recompose and fire again")
        current = {r["work_item_id"]: r for r in self.ports.entries()}
        for row in plan["members"]:
            other = current.get(row["work_item_id"])
            if other is None or identity(other) != identity(row):
                raise TrainError("member withdrawn, refrozen or requeued")
        proof = self.ports.qualify(plan["members"], plan["base"], self.policy.ci_mode)
        if proof != plan["proof"]:
            raise TrainError("admission evidence changed: recompose and fire again")

    def _ci(self, state):
        ci = self.ports.ci(state["plan"])
        if ci.get("head") != state["plan"]["head"] \
                or ci.get("tree") != state["plan"]["tree"]:
            raise TrainError("CI evidence is for another commit or tree")
        if ci.get("status") not in ("pending", "absent", "green", "red"):
            raise TrainError("unknown CI outcome")
        if ci["status"] in ("green", "red") and not ci.get("complete"):
            raise TrainError("CI is not the complete required suite")
        return ci

    def step(self, now: float) -> dict:
        """One executor/event pass. No loops waiting for CI or a verdict."""
        if not self.policy.enabled:
            return {"result": "off"}
        epoch = self.ports.acquire(self.epoch)
        if epoch is None:
            self.epoch = None
            return {"result": "busy"}
        self.epoch = epoch
        state = self.ports.load()
        try:
            if state is not None and state["phase"] == "paused":
                return {"result": "paused"}
            if state is None or state["phase"] in TERMINAL:
                rows = self.ports.entries()
                rows = ordered(rows, self.ports.dependencies(rows))
                if not due(rows, now, self.policy,
                           last_started=float((state or {}).get("started_ts", 0))):
                    return {"result": "idle"}
                from datetime import datetime, timezone
                state = dict(schema=SCHEMA, source=self.source, target=self.target,
                             revision=int((state or {}).get("revision", 0)), phase="composing",
                             base=self.ports.target(), members=rows, started_ts=now,
                             stamp=datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M"),
                             attempts=[], failures=0, pending=[], excluded=[], good=[], deferred=[])
                state = self._save(state)
            if (state["source"], state["target"]) != (self.source, self.target):
                raise TrainError("persisted train belongs to another repository or target")
            if state["phase"] == "composing":
                state = self._plan(state, state["members"], now)
            if state["phase"] == "publishing":
                self._current(state)
                self._held()
                # Idempotent compare-and-swap publication of an immutable ref.
                try:
                    self.ports.publish(state["plan"], self.epoch)
                except TrainError as exc:
                    # A push may have succeeded despite a lost response.
                    # Keep the immutable publication intent for reconciliation.
                    self.ports.notify(str(exc), state)
                    return {"result": "publication-uncertain", "error": str(exc)}
                state["phase"] = "ci-intent"
                state = self._save(state)
                self._held()
                # An uncertain dispatch is never repeated. A restart in
                # ci-intent reconciles runs on the same exact head below.
                try:
                    self.ports.start_ci(state["plan"])
                except TrainError as exc:
                    self.ports.notify(str(exc), state)
                    return {"result": "ci-uncertain", "error": str(exc)}
                state["phase"] = "ci"
                state = self._save(state)
                return {"result": "ci", "plan": state["plan"]}
            if state["phase"] in ("ci", "ci-intent"):
                self._current(state)
                ci = self._ci(state)
                if ci.get("status") in ("pending", "absent"):
                    return {"result": "ci", "plan": state["plan"]}
                state["attempts"][-1]["ci"] = ci
                if ci["status"] == "red":
                    return self._red(state, now)
                if state["plan"]["diagnostic"]:
                    state["good"].extend(state["plan"]["members"])
                    return self._next_probe(state, now)
                state["phase"] = "ready"
                state = self._save(state)
            elif state["phase"] == "ready":
                # A fire may arrive long after the initial green. A rerun or
                # a newer workflow attempt must supersede that old evidence.
                self._current(state)
                ci = self._ci(state)
                if ci["status"] in ("pending", "absent"):
                    state["phase"] = "ci"
                    self._save(state)
                    return {"result": "ci", "plan": state["plan"]}
                state["attempts"][-1]["ci"] = ci
                if ci["status"] == "red":
                    return self._red(state, now)
                state = self._save(state)
            if state["phase"] == "ready":
                fire = self.ports.authorize(state["plan"])
                if not fire:
                    return {"result": "awaiting-fire", "plan": state["plan"]}
                self._held()
                # Persist intent before the external effect. A lost response
                # is reconciled; it must never restart the pre-merge path.
                state["phase"] = "promoting"
                state = self._save(state)
                self.ports.promote(state["plan"], fire, self.epoch)
                state["phase"] = "landed"
                state = self._save(state)
                return {"result": "landed", "plan": state["plan"]}
            if state["phase"] == "promoting":
                if self.ports.target() == state["plan"]["head"]:
                    state["phase"] = "landed"
                    state = self._save(state)
                    return {"result": "landed", "plan": state["plan"]}
                # An uncertain push/merge is inspected by the pilot. Do not
                # construct another train or resend an external merge.
                raise TrainError("uncertain promotion: inspect the target and train proof")
            raise TrainError("unknown persisted phase %s" % state["phase"])
        except TrainError as exc:
            if state is not None:
                # Keeping a failed promotion's intent makes uncertainty
                # durable even if notifying the pilot fails.
                if state.get("phase") != "promoting":
                    state["phase"] = "invalidated"
                    state["error"] = str(exc)
                    state = self._save(state)
                self.ports.notify(str(exc), state)
            return {"result": "refused", "error": str(exc)}
        finally:
            if state is None or state.get("phase") in TERMINAL:
                self.ports.release(epoch)
                self.epoch = None

    def _red(self, state, now):
        state["failures"] += 1
        members = state["plan"]["suspects"]
        if state["failures"] >= self.policy.max_failures:
            state["phase"] = "paused"
            state = self._save(state)
            self.ports.notify("repeated train CI failures", state)
            return {"result": "paused"}
        if len(members) == 1:
            # CI on the singleton is proof, never a guess from a failed job
            # name or an overlapping impact-map entry.
            self._held()
            self.ports.quarantine(members, "complete CI red on isolated member and its "
                                   "proven prerequisite context", self.epoch)
            state["excluded"].extend(members)
        else:
            middle = len(members) // 2
            state["pending"] = [members[:middle], members[middle:]] + state["pending"]
        return self._next_probe(state, now)

    def _next_probe(self, state, now):
        while state["pending"]:
            suspects = state["pending"].pop(0)
            # Test a half with its prerequisites. A removed prerequisite
            # defers dependents, without falsely blaming or ejecting them.
            excluded = {r["work_item_id"] for r in state["excluded"]}
            candidates = [r for r in state["members"] if r["work_item_id"] not in excluded]
            deps = self.ports.dependencies(candidates)
            available = ordered(candidates, deps)
            available_ids = {r["work_item_id"] for r in available}
            state["deferred"].extend(r for r in suspects if r["work_item_id"] not in available_ids)
            suspects = [r for r in suspects if r["work_item_id"] in available_ids]
            if not suspects:
                continue
            wanted = {r["work_item_id"] for r in suspects}
            changed = True
            while changed:
                more = {int(e["blocker_id"]) for e in deps
                        if e["blocking"] and int(e["work_item_id"]) in wanted}
                changed = bool(more - wanted)
                wanted.update(more)
            members = [r for r in available if r["work_item_id"] in wanted]
            state = self._plan(state, members, now, diagnostic=True, suspects=suspects)
            return {"result": "recomposed", "plan": state["plan"]}
        # Two green halves can fail together. If no singleton was excluded,
        # preserve the interaction evidence and ask the pilot; never cycle.
        if not state["excluded"]:
            state["phase"] = "paused"
            state = self._save(state)
            self.ports.notify("train interaction: green halves, red union", state)
            return {"result": "paused"}
        # Prerequisites removed by isolation remove their dependents as well.
        unique = list({r["work_item_id"]: r for r in state["good"]}.values())
        members = ordered(unique, self.ports.dependencies(unique))
        if not members:
            state["phase"] = "empty"
            state = self._save(state)
            return {"result": "empty"}
        state["good"], state["excluded"], state["pending"] = [], [], []
        # Subsequent diagnostics concern this new union only. Previously
        # quarantined members must never reappear as prerequisite context.
        state["members"] = members
        # Even if this set equals a green diagnostic half, compose a new plan
        # and run complete CI on its own head before the actual promotion.
        state = self._plan(state, members, now)
        return {"result": "recomposed", "plan": state["plan"]}
