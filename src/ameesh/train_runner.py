# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit assembly and inactive-by-default runner service (#1467).

The pilot must supply the qualified policy commit, complete CI contract and
exact provenance provider. No live profile or provider selection is installed.
A passage receives a fresh scoped SQL connection;
only its fencing epoch survives between calls, never a closed connection.
"""
from __future__ import annotations

import contextlib
import os
import math
import threading
import time
import uuid
from dataclasses import replace

from . import (merge_queue, radar, resolutions, storage, train, train_admissions,
               train_candidates, train_ci, train_fire, train_git, train_history,
               train_migrations, train_policy, train_runtime, train_store)

#: #2446: one host executes the train service. The lease is independent of the
#: per-target packet reservation; it is renewed while the service runs and
#: released on a clean stop. A host that loses it stops scheduling passages
#: until it can acquire it again. #3134: the runner's shutdown closes it even
#: when a passage outlives the stop (the process then exits immediately); the
#: TTL only covers a crash. 900 s is kept: the lease is renewed between
#: passages only, and a passage (prevalidation, CI read) may exceed 300 s.
#: #3142: its holder adopts the target reservations a previous holder's
#: runner left behind (`train_store.Ledger.adopt`), fenced by a new epoch.
SERVICE_LEASE_KEY = "train-service"
SERVICE_LEASE_TTL_S = 900.0
SERVICE_LEASE_RETRY_S = 60.0
#: L165 (EP5): how often a host without the elected role looks again.
ROLE_RETRY_S = 60.0
#: #3366: how long the service waits after a canon-pin reload refusal (an
#: unreadable or invalid configuration): it retries instead of composing.
RELOAD_RETRY_S = 60.0
#: #3134: how long the runner's shutdown waits for a train effect still in
#: flight (push, promotion, SQL write) before releasing the lease. Far below
#: the runner units' `TimeoutStopSec`; past it the lease expires with its TTL.
SERVICE_STOP_EFFECT_S = 120.0


class HostLease:
    """The exclusive service lease of one host, under the existing lock table."""

    def __init__(self, cfg, *, key=SERVICE_LEASE_KEY, ttl=SERVICE_LEASE_TTL_S,
                 retry=SERVICE_LEASE_RETRY_S, clock=time.time):
        if not isinstance(key, str) or not key.strip():
            raise train.TrainError("explicit train service lease key required")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0 \
                or isinstance(retry, bool) or not isinstance(retry, (int, float)) \
                or retry <= 0:
            raise train.TrainError("positive train service lease duration required")
        self.base_key = key
        self.key = key
        #: L165 (EP5): tenure binding. With an elected role, the service key
        #: carries the role epoch (`train-service:<role>:<epoch>`): a new
        #: holder of the role does not wait for the dead holder's service
        #: lease TTL (900 s) before composing, which would break the 5-minute
        #: election target. The old key expires on its own; the role lease is
        #: the cross-host exclusion, this key is the second line within one
        #: tenure. Without a role the key is unchanged (host pin).
        self.role = None
        self.role_epoch = None
        # #2446/Codex B2: `land_lock` lets the SAME owner take a new epoch
        # immediately. One owner per service incarnation is therefore
        # mandatory: two processes configured with the same runner id must
        # contend instead of both considering themselves the holder.
        self.owner = "train-service:%s:%s" % (
            cfg.runner or cfg.host or "unknown", uuid.uuid4().hex)
        self.ttl, self.retry, self.clock = float(ttl), float(retry), clock
        self.epoch = None
        self.next_at = 0.0
        # #3134: the runner's main thread closes the lease while the service
        # thread may still be inside a passage. Every train effect of a
        # passage runs under this lock (`effect`): the close waits for the
        # effect in flight, and every later effect is refused. A closed lease
        # is never acquired again by this incarnation. `depth` counts the
        # effects open on the thread holding the lock: an effect nested in
        # the one in flight (promote -> ledger.acquire, publish -> reserve)
        # finishes it; only a new top-level effect is refused.
        self.lock = threading.RLock()
        self.closed = False
        self.depth = 0

    def bind_role(self, name: str, epoch: int | None) -> bool:
        """Bind the service key to the role tenure; True when the key changed.

        The epoch is an integer from the role's live lease. A new epoch means
        a new holder (EP1 increments it on every claim): it gets a fresh
        key, so a service lease left by a host that stopped abruptly cannot
        hold the new holder for its TTL. The old key is left to expire; the
        caller may release it first when it still holds it.
        """
        if not name or epoch is None:
            return False
        epoch = int(epoch)
        if self.role == name and self.role_epoch == epoch:
            return False
        self.role, self.role_epoch = name, epoch
        self.key = "%s:%s:%d" % (self.base_key, name, epoch)
        return True

    def acquire(self, db, now=None):
        """Take the service lease; False when another host holds it."""
        now = self.clock() if now is None else float(now)
        with self.lock:
            if self.closed:
                self.epoch, self.next_at = None, now + self.retry
                return False
            epoch = storage.of(db).work.land_lock(self.key, self.owner, self.ttl)
            if epoch is None:
                self.epoch, self.next_at = None, now + self.retry
                return False
            self.epoch, self.next_at = int(epoch), now + self.ttl / 3.0
            return True

    def renew(self, db, now=None):
        """Keep the same epoch; False when the lease is gone."""
        now = self.clock() if now is None else float(now)
        with self.lock:
            if self.epoch is None:
                return False
            epoch = storage.of(db).work.land_lock(self.key, self.owner, self.ttl,
                                                  epoch=self.epoch)
            if epoch != self.epoch:
                self.epoch, self.next_at = None, now + self.retry
                return False
            self.next_at = now + self.ttl / 3.0
            return True

    def release(self, db):
        """Release this service's lease; never touch another holder's."""
        with self.lock:
            if self.epoch is None:
                return False
            epoch, self.epoch = self.epoch, None
            return bool(storage.of(db).work.land_unlock(self.key, self.owner, epoch))

    @contextlib.contextmanager
    def effect(self):
        """#3134: one train effect, under the held lease; refused once closed or lost."""
        with self.lock:
            if self.depth == 0 and (self.closed or self.epoch is None):
                raise train.TrainError("train service lease released; stale train effect refused")
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

    def close(self, db, *, timeout=None):
        """#3134: fence every later effect, then release the lease.

        Returns None when an effect is still running after ``timeout``: the
        lease is then kept (it expires with its TTL) rather than released
        under a write in flight; otherwise what ``release`` returns.
        """
        self.closed = True
        if not self.lock.acquire(timeout=-1 if timeout is None else max(0.0, float(timeout))):
            return None
        try:
            return self.release(db)
        finally:
            self.lock.release()


#: #3134: the runtime calls that write. The target reservation check
#: (`ledger.acquire`) precedes every effect; publication is fenced at its
#: push and composition at its migration reservations, so a long
#: prevalidation or replay never holds the service lease's lock.
_FENCED = (("save", "release", "enroll", "quarantine", "start_ci", "reconcile", "promote"),
           ("acquire", "before_publish"),
           ("publish",),
           ("reserve", "landed"))


def _fenced(runtime, guard):
    """#3134: run each write of ``runtime`` under the service lease's ``guard``."""
    if guard is None:
        return runtime

    def fence(call):
        def run(*args, **kwargs):
            with guard():
                return call(*args, **kwargs)
        return run

    ports = (runtime, runtime.ledger, runtime.repository, runtime.migrations)
    for port, names in zip(ports, _FENCED):
        if port is None:
            continue
        for name in names:
            call = getattr(port, name, None)
            if callable(call):
                setattr(port, name, fence(call))
    return runtime


class Passage:
    #: #2914: the canon watch; None (also for a passage built without
    #: ``__init__`` on a bench) keeps the historical behaviour.
    canon = None

    def __init__(self, cfg, project, *, owner, policy, policy_path,
                 policy_commit, workflows, provider, mechanics, notify, impact=None,
                 reservation=None, transports=None, candidate=None, initial=None,
                 canon=None):
        if not isinstance(owner, str) or not owner.strip():
            raise train.TrainError("explicit runner reservation owner required")
        if not isinstance(policy, train.Policy) or not callable(notify):
            raise train.TrainError("explicit train policy and diagnostic callback required")
        if candidate is not None and not callable(candidate):
            raise train.TrainError("explicit freeze_candidate reader required")
        if initial is not None and (candidate is None or not callable(initial)):
            raise train.TrainError("initial queue history requires an explicit candidate reader")
        self.cfg, self.project, self.owner = cfg, project, owner
        self.policy, self.policy_path, self.policy_commit = policy, policy_path, policy_commit
        self.workflows, self.provider, self.notify = workflows, provider, notify
        self.mechanics = mechanics
        self.impact = impact
        self.reservation = reservation
        self.transports = transports
        self.candidate, self.initial = candidate, initial
        #: #2914: the pinned commit against the gate's (``train_canon.Watch``);
        #: None on a bench that builds the passage by hand.
        self.canon = canon
        self.maintenance_at = None
        self.epoch = None
        #: #3134: the service lease's `effect`, set by `Service`; None outside it.
        self.guard = None
        #: #3142: the service lease itself, set by `Service`: its holder adopts
        #: the target reservation another runner's train left behind.
        self.lease = None

    def held_lease(self):
        """#3142: (key, owner, epoch) of the service lease this process holds, or None."""
        lease = self.lease
        epoch = getattr(lease, "epoch", None)
        if lease is None or epoch is None or getattr(lease, "closed", False):
            return None
        return lease.key, lease.owner, epoch

    def reservation_ledger(self, db):
        if self.reservation is None:
            raise train.TrainError("explicit qualified packet reservation required")
        return self.reservation.bind(db, self.project,
            merge_queue._remote_branch(self.project.target), self.owner,
            train_ci.contract_digest(self.workflows), service=self.held_lease)

    def maintain(self, db, now):
        with (self.guard() if self.guard is not None else contextlib.nullcontext()):
            self.maintenance_at = self.reservation_ledger(db).maintain(now)
        return self.maintenance_at

    def repository_path(self):
        target = merge_queue._remote_branch(self.project.target)
        source = merge_queue.source_of(self.project.repo)
        return os.path.join(self.cfg.state_dir, "trains", train.digest(
            [self.project.name, source, target, self.owner]) + ".clone")

    def incident_facts(self, db, *, fallback=None):
        """Read past packet facts without loading current policy or runtime."""
        from .train_incidents import PacketFacts
        return PacketFacts(db, self.project, self.repository_path(),
                           impact=self.impact, fallback=fallback)

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
        if not getattr(rules, "packet_cutoffs", None) \
                or not callable(getattr(rules, "last_packet", None)) \
                or not callable(getattr(rules, "next_packet", None)):
            raise train.TrainError("explicit compiled packet calendar required; no release fallback")
        # Missing fields never resurrect the old named-fire requirement.
        # Unsupported process changes require an explicit adapter first.
        try:
            if type(rules.merge_named_fire) is not bool \
                    or rules.merge_repeat_freeze is not False \
                    or rules.merge_on_green_packet is not True \
                    or rules.merge_post_merge_red not in ("forward_or_revert", "freeze"):
                raise train.TrainError("unsupported compiled packet merge policy")
            fires = (train_fire.Fires if rules.merge_named_fire else train_fire.Automatic)
        except AttributeError as exc:
            raise train.TrainError("explicit compiled packet merge policy required") from exc
        policy = replace(self.policy, schedule=rules)
        requirement = train_policy.Compiled(rules)
        # #2503: a dynamic contract resolves the due parts in the private
        # clone, against the plan's base and head; a static contract ignores
        # the path and the proof cache.
        directory = self.repository_path()
        ci = train_ci.GitHub(radar.github_slug(self.project), self.workflows,
                             repo=directory, cache=train_ci.parts_cache(self.cfg))
        # The radar, another target, and another runner may all replay merges
        # concurrently. They must never reset this passage's worktree.
        clone = resolutions.Replayer(self.project.repo, directory, script=None,
                                     target=self.project.target)
        transports = (None if self.transports is None else
                      lambda **context: self.transports(db, **context))
        # #2400: composition moves the L122 reservation before publication and
        # records the landed number after a confirmed promotion. The allocator
        # is bound to this passage's scoped connection and dies with it.
        migrations = train_migrations.Allocator(db, cfg=self.cfg, actor="ameesh train")
        repository = train_git.Repository(self.project, clone, transports=transports,
                                          migrations=migrations)
        ledger = (self.reservation_ledger(db) if self.reservation is not None else
                  train_store.Ledger(db, self.project, target, self.owner,
                                     service=self.held_lease))
        authority = fires(self.cfg, ledger) if rules.merge_named_fire else fires(ledger, rules)
        history = train_history.Recorded(db, clone.path, provider=self.provider, review_rules=rules)
        admissions = train_admissions.Admissions(db, self.project, history, policy=requirement)
        inventory = enrollment = None
        if self.candidate is not None:
            inventory = train_candidates.Inventory(db, self.project, candidate=self.candidate)
            preparation = train_candidates.Preparation(db, self.project,
                repository=repository, history=history, policy=requirement)
            enrollment = train_candidates.Enrollment(preparation, admissions, initial=self.initial)
        def prevalidate(head, base):
            """#2644 : la prévalidation de la composition, avant publication."""
            from . import prevalidation, prevalidation_calls
            try:
                result = prevalidation_calls.gate_train(
                    self.cfg, repo=clone.path, head=head, base=base,
                    canon_repo=self.policy_path, canon_commit=self.policy_commit,
                    project=self.project.name,
                    repo_slug=radar.github_slug(self.project), say=self.notify)
            except prevalidation.PrevalidationError as exc:
                if exc.cause == "run-red":
                    # Un rouge exécuté n'est pas une publication incertaine :
                    # l'Engine l'arrête (paused, réservation rendue).
                    raise train.PrevalidationRed(exc.why or "prevalidation red") from exc
                raise train.TrainError(
                    "prevalidation refused (%s): %s"
                    % (exc.cause, exc.why or "")) from exc
            if result.get("state") == "unavailable":
                self.notify("prévalidation indisponible, publication maintenue: %s"
                            % (result.get("why") or result.get("cause") or "?"), result)
            return result

        return _fenced(train_runtime.Runtime(ledger=ledger, repository=repository, ci=ci,
            fires=authority, admissions=admissions,
            target=target, policy=policy, notify=self.notify, inventory=inventory,
            enrollment=enrollment, migrations=migrations, prevalidation=prevalidate),
            self.guard)

    def step(self, db, now):
        """One bounded event/pass; never wait for CI and never retain ``db``."""
        if not self.policy.enabled:
            return {"result": "off"}
        runtime = self.runtime(db)
        refused = self.canon_refusal(db, runtime)
        return refused if refused is not None else self._step(runtime, now)

    def scheduled_step(self, db, now):
        """Use the very same compiled object for execution and the next wake."""
        if not self.policy.enabled:
            return {"result": "off"}, None
        runtime = self.runtime(db)
        deadline = runtime.policy.schedule.next_packet(now)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                or not math.isfinite(deadline) or deadline <= now:
            raise train.TrainError("compiled canon must provide a future packet cutoff")
        refused = self.canon_refusal(db, runtime)
        return (refused if refused is not None else self._step(runtime, now)), deadline

    def canon_refusal(self, db, runtime):
        """#2914: refuse by name to compose while the pin differs from the gate.

        A pinned commit that is not the gate's (or a gate commit that cannot be
        read) would count reviews differently from the merge gate. No new
        packet is composed; a packet already in flight keeps its terminal
        path (CI, promotion or drain), and a paused train stays paused. The
        host refresh may first qualify the pin automatically when the audited
        diff leaves the train rules unchanged (#3483); this guard refuses any
        mismatch that remains after refresh.
        """
        if self.canon is None:
            return None
        reading = self.canon.read(db)
        if reading.aligned:
            return None
        state = runtime.load()
        phase = (state or {}).get("phase")
        in_flight = phase is not None and phase not in train.TERMINAL \
            and phase != "composing"
        if in_flight or phase == "paused":
            self.notify("canon drift, %s train kept: %s" % (phase, reading.describe()),
                        state)
            self.canon.alert(db, reading, "active")
            return None
        reason = "composition refused: %s" % reading.describe()
        self.notify(reason, state)
        self.canon.alert(db, reading, "active")
        return {"result": "canon-drift", "reason": reason, "canon": reading.to_json()}

    def _step(self, runtime, now):
        engine = train.Engine(runtime, source=runtime.repository.source,
                              target=runtime.target_branch, policy=runtime.policy)
        engine.epoch = self.epoch
        try:
            result = engine.step(now)
            adopted = getattr(runtime.ledger, "adopted", None)
            if adopted:
                self.notify("train reservation adopted from %s (epoch %s -> %s)" % (
                    adopted["previous_owner"], adopted["previous_epoch"], adopted["epoch"]),
                    adopted)
            if runtime.enrollment_result is not None:
                result["enrollment"] = runtime.enrollment_result
            return result
        finally:
            self.epoch = engine.epoch
            self.maintenance_at = (runtime.ledger.next_maintenance()
                if self.reservation is not None and self.epoch is not None else None)


class Service:
    """One independent runner task: startup, canon cutoffs and explicit events.

    No timer polls CI or reviews. A producer supplies explicit ``event`` hints;
    a failed passage waits for a new event rather than retrying by itself.
    Connections and runtime ports never survive a passage.
    """
    def __init__(self, cfg, passages, *, stop, report, clock=time.time, lease=None,
                 role=None, refresh=None):
        self.cfg, self.passages = cfg, tuple(passages)
        self.stop, self.report, self.clock = stop, report, clock
        self.lease = lease
        #: #3366: reload the effective canonical pin before each passage;
        #: None keeps the historical startup pin (bench, direct callers).
        self.refresh = refresh
        #: L165 (EP5): elected role (agent name) whose lease gates the passages;
        #: None keeps the historical `train.host` pin only.
        self.role = role
        self._role_held = None
        for passage in self.passages:
            passage.guard = lease.effect if lease is not None else None
            passage.lease = lease
        self.condition = threading.Condition()
        self.pending = set(range(len(self.passages)))
        self.deadlines = {}
        self.maintenance = {}
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

    def _reload_pin(self):
        """#3366: reload the pin before this passage, or give a retry delay.

        A configuration that cannot be re-read or validated is a named refusal,
        never a silent fallback to the startup pin: the service reports it and
        retries shortly. A changed pin is logged once per change."""
        if self.refresh is None:
            return None
        try:
            changes = self.refresh()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - a bad config never stops the runner
            self.report("train canon pin not reloaded (%s: %s)"
                        % (type(exc).__name__, exc))
            return self.clock() + RELOAD_RETRY_S
        for change in changes or ():
            self.report("train canon pin reloaded for %s: %s -> %s"
                        % (change.get("project") or "?",
                           str(change.get("previous") or "")[:12] or "?",
                           str(change.get("pin") or "")[:12] or "?"))
        return None

    def pass_once(self, index, now):
        from .scoped_db import ScopedDb
        if self.stop.is_set():
            return None
        retry = self._reload_pin()
        if retry is not None:
            return retry
        db = ScopedDb(self.cfg)
        try:
            result, deadline = self.passages[index].scheduled_step(db, now)
            if isinstance(result, dict) and result.get("result") == "landed":
                # #2965: the target just freed. A cutoff the landed packet
                # overran is composed at once, with the candidates admissible
                # at that cutoff, instead of waiting for the next calendar
                # hour (the "créneau manqué, traité dans la foulée" rule).
                with self.condition:
                    self.pending.add(index)
            return deadline
        except Exception as exc:
            self.report("train passage failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        finally:
            db.close()

    def maintain_once(self, index, now):
        from .scoped_db import ScopedDb
        if self.stop.is_set():
            return None
        db = ScopedDb(self.cfg)
        try:
            return self.passages[index].maintain(db, now)
        except Exception as exc:
            self.report("train reservation maintenance failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        finally:
            db.close()

    def _role_ready(self) -> bool:
        """Is this host the live holder of the train's elected role?

        A role that cannot be read is not held: the service waits instead of
        running on a guess. A passage is only scheduled while the role is
        held, and losing it releases the service lease so the new holder can
        take over without waiting for its TTL.
        """
        if not self.role:
            return True
        from . import role_run
        from .scoped_db import ScopedDb
        db = None
        error = ""
        try:
            db = ScopedDb(self.cfg)
            report = role_run.live(db, self.role)
            held = bool(report["held"] and report["host"] == self.cfg.host)
            if held and self.lease is not None:
                epoch = report.get("epoch")
                if (self.lease.role_epoch is not None
                        and (self.lease.role != self.role
                             or self.lease.role_epoch != epoch)):
                    # Release the previous tenure's key BEFORE binding the new
                    # one; a dead holder cannot do it and the key expires on
                    # its own, which is why the new epoch never waits for it.
                    self._release_tenure()
                self.lease.bind_role(self.role, epoch)
        except Exception as exc:  # noqa: BLE001 - a broken read is not a lease
            held = False
            error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if db is not None:
                db.close()
        with self.condition:
            if held and self._role_held is not True:
                self.pending.update(range(len(self.passages)))
                self.condition.notify_all()
                self.report("train role %s acquired on %s" % (self.role, self.cfg.host))
            elif not held and self._role_held is not False:
                self.report("train role %s not held by %s: passages wait"
                            % (self.role, self.cfg.host))
            self._role_held = held
        if error:
            self.report("train role %s unreadable (%s)" % (self.role, error))
        return held

    def _lease_ready(self, now=None):
        """Acquire or renew the exclusive service lease; False means wait."""
        if self.lease is None:
            return True
        from .scoped_db import ScopedDb
        now = self.clock() if now is None else float(now)
        db = ScopedDb(self.cfg)
        try:
            if self.lease.epoch is None:
                if not self.lease.acquire(db, now):
                    return False
                with self.condition:
                    self.pending.update(range(len(self.passages)))
                self.report("train service lease acquired (%s)" % self.lease.owner)
                return True
            if now >= self.lease.next_at:
                if self.lease.renew(db, now):
                    return True
                self.report("train service lease lost; waiting to acquire it again")
                return False
            return True
        except Exception as exc:
            self.report("train service lease unreadable (%s: %s)"
                        % (type(exc).__name__, exc))
            return False
        finally:
            db.close()

    def _release_tenure(self):
        """Release the current tenure but keep the lease re-acquirable.

        A role loss or an epoch change is not a shutdown: #3134's `close` is
        final for the incarnation, so it belongs to `finish`/`finally` only.
        Here the key is simply freed (or left to expire if the database is
        gone); the service may acquire the next tenure's key later.
        """
        if self.lease is None or self.lease.epoch is None:
            return
        from .scoped_db import ScopedDb
        db = ScopedDb(self.cfg)
        try:
            self.lease.release(db)
        except Exception as exc:
            self.report("train service lease release failed (%s: %s)"
                        % (type(exc).__name__, exc))
        finally:
            db.close()

    def _release_lease(self, timeout=None):
        """Close the lease, from the service thread or the runner's shutdown."""
        if self.lease is None:
            return
        from .scoped_db import ScopedDb
        db = ScopedDb(self.cfg)
        try:
            if self.lease.close(db, timeout=timeout) is None and timeout:
                self.report("train service lease kept: a train effect is still running;"
                            " it expires with its TTL")
        except Exception as exc:
            self.report("train service lease release failed (%s: %s)"
                        % (type(exc).__name__, exc))
        finally:
            db.close()

    def run(self):
        try:
            while not self.stop.is_set():
                if not self._role_ready():
                    if self.lease is not None and self.lease.epoch is not None:
                        self._release_tenure()
                        self.report("train role lost: service lease released")
                    # #3483: standby hosts also maintain their local canon pin.
                    self._reload_pin()
                    with self.condition:
                        if self.stop.is_set():
                            return
                        self.condition.wait(timeout=ROLE_RETRY_S)
                    continue
                if not self._lease_ready():
                    self._reload_pin()
                    with self.condition:
                        if self.stop.is_set():
                            return
                        self.condition.wait(timeout=self.lease.retry)
                    continue
                with self.condition:
                    if self.stop.is_set():
                        return
                    now = self.clock()
                    ready = self.pending | {i for i, at in self.deadlines.items() if at <= now}
                    renew = {i for i, at in self.maintenance.items() if at <= now} - ready
                    if not ready and not renew:
                        times = list(self.deadlines.values()) + list(self.maintenance.values())
                        if self.lease is not None:
                            times.append(self.lease.next_at)
                        delay = min(times) - now if times else None
                        self.condition.wait(timeout=delay)
                        continue
                    self.pending.difference_update(ready)
                for index in sorted(renew):
                    if self.stop.is_set():
                        return
                    deadline = self.maintain_once(index, self.clock())
                    with self.condition:
                        self.maintenance.pop(index, None)
                        if deadline is not None:
                            self.maintenance[index] = deadline
                for index in sorted(ready):
                    if self.stop.is_set():
                        return
                    deadline = self.pass_once(index, self.clock())
                    with self.condition:
                        self.deadlines.pop(index, None)
                        if deadline is not None:
                            self.deadlines[index] = deadline
                        self.maintenance.pop(index, None)
                        at = getattr(self.passages[index], "maintenance_at", None)
                        if isinstance(at, (int, float)) and math.isfinite(at):
                            self.maintenance[index] = at
        finally:
            self._release_lease()

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="integration-trains")
        self.thread.start()

    def shutdown(self, *, join=2.0):
        # The runner sets its stop event before calling this method.
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=join)
            if self.thread.is_alive():
                # #3134: a passage outlives the stop. The runner will exit
                # immediately (`_sortie_sure`) and this thread's `finally`
                # never runs: fence every later effect now, and release the
                # lease at once unless an effect is in flight (`finish`).
                self._release_lease(timeout=0)

    def finish(self, timeout=SERVICE_STOP_EFFECT_S):
        """#3134: at the end of the runner's shutdown, wait for the effect
        still in flight, then release the lease before the process exits."""
        if self.lease is not None and self.lease.epoch is not None:
            self._release_lease(timeout=timeout)
