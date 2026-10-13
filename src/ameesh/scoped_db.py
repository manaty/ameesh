# SPDX-License-Identifier: AGPL-3.0-only
"""Periodic runner tasks release SQL sessions before external work (#653).

The primary driver and its LISTEN session remain open. A periodic task used
to retain its third connection during HTTP balance reads, git scans and
cleanup. Here a connection lasts one SQL operation or an entire explicit
transaction. No transaction or advisory lock is released between statements.
"""
from contextlib import contextmanager
import threading

from . import db as db_mod


class ScopedDb:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.driver
        self._local = threading.local()

    @contextmanager
    def _connection(self):
        active = getattr(self._local, 'active', None)
        if active is not None:
            yield active
            return
        driver = db_mod.connect(self.cfg)
        try:
            yield driver
        finally:
            driver.close()

    def query(self, sql, params=()):
        with self._connection() as driver:
            return driver.query(sql, params)

    def query_batch(self, items):
        with self._connection() as driver:
            return driver.query_batch(items)

    def execute(self, sql, params=()):
        with self._connection() as driver:
            return driver.execute(sql, params)

    def script(self, sql):
        with self._connection() as driver:
            return driver.script(sql)

    @contextmanager
    def transaction(self):
        with self._connection() as driver:
            previous = getattr(self._local, 'active', None)
            with driver.transaction() as tx:
                self._local.active = tx
                try:
                    yield self
                finally:
                    self._local.active = previous

    def close(self):
        # Every connection is closed by its operation's finally. Never close
        # another thread's transaction when a task finishes.
        pass
