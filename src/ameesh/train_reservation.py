# SPDX-License-Identifier: AGPL-3.0-only
"""Bind #1680 to a train ledger; the qualified deadline is always injected.

No duration or supplier is selected here. The standalone engine can still be
used without this adapter; runner service assembly requires it explicitly.
"""
from __future__ import annotations

import copy
import math
import time

from .train import TERMINAL, TrainError, digest
from .train_store import Ledger


class Reservation:
    def __init__(self, provider, *, deadline, clock=time.time):
        if provider is None or not callable(deadline) or not callable(clock):
            raise TrainError("qualified reservation provider and absolute deadline required")
        self.provider, self.deadline, self.clock = provider, deadline, clock

    def bind(self, db, project, target, owner, contract):
        return ReservedLedger(db, project, target, owner,
                              reservation=self, contract=contract)


class ReservedLedger(Ledger):
    def __init__(self, db, project, target, owner, *, reservation, contract):
        super().__init__(db, project, target, owner)
        self.project, self.target = project, target
        self.reservation, self.provider = reservation, reservation.provider
        self.contract = contract
        self.latest = None

    def _ticket(self, state, epoch=None):
        try:
            ticket = self.provider.Ticket.from_json(state.get("reservation"))
        except self.provider.ReservationError as exc:
            raise TrainError(str(exc)) from exc
        plan = state.get("plan") or {}
        expected = dict(key=self.key, owner=self.owner, ci_contract=self.contract,
                        branch=plan.get("branch"), base=plan.get("base"), head=plan.get("head"))
        if any(getattr(ticket, name) != value for name, value in expected.items()) \
                or (epoch is not None and ticket.epoch != epoch):
            raise TrainError("reservation does not bind this ledger, epoch, plan and CI contract")
        return ticket

    def _renew(self, ticket, now):
        try:
            self.latest = self.provider.renew(self.db, ticket, now=now)
        except self.provider.ReservationError as exc:
            raise TrainError(str(exc)) from exc
        return self.latest

    def acquire(self, epoch):
        state = super().load()
        if state and state.get("phase") not in TERMINAL and state.get("plan"):
            # A restart adopts the persisted epoch. It must never recreate an
            # expired lease by passing epoch=None to ordinary land_lock.
            try:
                ticket = self._ticket(state, epoch)
                return self._renew(ticket, self.reservation.clock()).epoch
            except TrainError:
                return None
        return super().acquire(epoch)

    def load(self):
        state = super().load()
        if self.latest is not None and state and state.get("reservation"):
            saved = self._ticket(state)
            if (saved.key, saved.owner, saved.epoch, saved.deadline) == (
                    self.latest.key, self.latest.owner, self.latest.epoch, self.latest.deadline):
                state["reservation"] = self.latest.to_json()
        return state

    def save(self, state, expected, epoch):
        state = copy.deepcopy(state)
        if state.get("plan") and state.get("phase") not in TERMINAL:
            now = self.reservation.clock()
            if not state.get("reservation"):
                if state.get("phase") != "publishing":
                    raise TrainError("packet reservation must precede publication")
                plan = state["plan"]
                deadline = self.reservation.deadline(copy.deepcopy(plan), now)
                if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                        or not math.isfinite(deadline) or deadline <= now:
                    raise TrainError("qualified future absolute reservation deadline required")
                try:
                    self.latest = self.provider.begin(self.db, self.project, self.target,
                        owner=self.owner, epoch=epoch, branch=plan["branch"],
                        base=plan["base"], head=plan["head"], ci_contract=self.contract,
                        deadline=deadline, now=now)
                except self.provider.ReservationError as exc:
                    raise TrainError(str(exc)) from exc
            else:
                self._renew(self._ticket(state, epoch), now)
            state["reservation"] = self.latest.to_json()
        return super().save(state, expected, epoch)

    def before_publish(self, plan, epoch):
        state = self.load()
        if not state or digest(state.get("plan")) != digest(plan):
            raise TrainError("publication requires the persisted reserved plan")
        self._renew(self._ticket(state, epoch), self.reservation.clock())

    def next_maintenance(self):
        state = self.load()
        if not state or state.get("phase") in TERMINAL or not state.get("reservation"):
            return None
        return self.provider.next_renew(self._ticket(state))

    def maintain(self, now):
        """SQL only: no runtime construction, Git, mechanics or CI access."""
        state = self.load()
        if not state or state.get("phase") in TERMINAL or not state.get("reservation"):
            return None
        ticket = self._renew(self._ticket(state), now)
        state["reservation"] = ticket.to_json()
        super().save(state, state["revision"], ticket.epoch)
        return self.provider.next_renew(ticket)
