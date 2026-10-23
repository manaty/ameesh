# SPDX-License-Identifier: AGPL-3.0-only
"""Durable state and reservation adapter for #1467; no extra remote rights."""
from __future__ import annotations

from . import land, merge_queue, storage
from .train import TrainError


class Ledger:
    def __init__(self, db, project, target, owner, *, service=None):
        key = land.lock_key(project, target)
        if key != land.lock_key(project, merge_queue._remote_branch(target)):
            raise TrainError("canonical target required for the shared train reservation")
        if service is not None and not callable(service):
            raise TrainError("explicit train service lease reader required")
        self.db = db
        self.ops = storage.of(db).work
        self.key = key
        # land_lock allows the same owner to restart at a new epoch. A train
        # must have a different owner from ordinary single-lot land even when
        # both execute inside the same runner.
        self.owner = "train:" + owner
        #: #3142: () -> (key, owner, epoch) of the train service lease held
        #: by this process, or None. Only its holder adopts a reservation
        #: another runner's train left behind.
        self.service = service
        #: #3142: the last adoption, for the passage's diagnostic.
        self.adopted = None

    def acquire(self, epoch):
        held = self.ops.land_lock(self.key, self.owner, land.LOCK_TTL_S, epoch=epoch)
        if held is None and epoch is None:
            state = self.load()
            return self.adopt(state, None, land.LOCK_TTL_S)
        return held

    def adopt(self, state, previous, ttl):
        """#3142: take over another `train:*` runner's reservation of this
        target, under the live service lease; the new epoch fences the old
        holder. None when this process does not hold the service lease, or
        the reservation is not a train's, or anything moved meanwhile."""
        lease = self.service() if self.service is not None else None
        if not lease:
            return None
        adopted = self.ops.train_adopt(self.key, self.owner,
                                       revision=int((state or {}).get("revision", 0)),
                                       previous=previous, service=tuple(lease), ttl_s=ttl)
        if adopted is None:
            return None
        self.adopted = adopted
        return int(adopted["epoch"])

    def release(self, epoch):
        self.ops.land_unlock(self.key, self.owner, epoch)

    def load(self):
        return self.ops.train_read(self.key)

    def save(self, state, expected, epoch):
        result = self.ops.train_save(self.key, state, revision=expected,
                                     owner=self.owner, epoch=epoch)
        if result is None:
            raise TrainError("target lease or train revision changed; stale intent refused")
        return result
