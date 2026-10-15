# SPDX-License-Identifier: AGPL-3.0-only
"""Durable state and reservation adapter for #1467; no extra remote rights."""
from __future__ import annotations

from . import land, storage
from .train import TrainError


class Ledger:
    def __init__(self, db, project, target, owner):
        self.db = db
        self.ops = storage.of(db).work
        self.key = land.lock_key(project, target)
        # land_lock allows the same owner to restart at a new epoch. A train
        # must have a different owner from ordinary single-lot land even when
        # both execute inside the same runner.
        self.owner = "train:" + owner

    def acquire(self, epoch):
        return self.ops.land_lock(self.key, self.owner, land.LOCK_TTL_S, epoch=epoch)

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
