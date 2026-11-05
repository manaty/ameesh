# SPDX-License-Identifier: AGPL-3.0-only
"""Periodic runner tasks release SQL sessions before external work (#653).

The primary driver and its LISTEN session remain open. A periodic task used
to retain its third connection during HTTP balance reads, git scans and
cleanup.

#5979 (diagnostic #5972 B): one PostgreSQL session per statement was cheap
only behind a per-host pool (#653), never installed on the VMs; each
statement then cost a full backend, about 20 to 25 new sessions/s for the
fleet of runners. A session is now held per thread from the first statement
of a pass and returned:

- by `close()` at the end of the pass (every caller closes in `finally`);
- by `release()`, called before a known external wait (provider HTTP);
- by the reaper, once the session has been idle for `idle_s` seconds: a
  pass that runs git, gh, ssh or a subprocess between two statements does
  not keep an idle backend during it;
- after any error, so a broken session is never reused.

Statements outside `transaction()` stay in autocommit: no transaction ever
spans two statements, hence none spans an external wait. An explicit
transaction keeps its session busy until it ends; the reaper never touches a
busy session and never closes another thread's transaction.
"""
from contextlib import contextmanager
import threading
import time

from . import db as db_mod

# Idle time after which a held session is returned to the server. A burst of
# statements in a pass is separated by milliseconds; git, gh and ssh take
# longer. Bounded so that a pass never holds an idle backend for long.
IDLE_S = 1.0


class _Held:
    __slots__ = ("driver", "busy", "last")

    def __init__(self, driver):
        self.driver = driver
        self.busy = True
        self.last = time.monotonic()


class ScopedDb:
    def __init__(self, cfg, family=None, *, idle_s=IDLE_S):
        self.cfg = cfg
        self.name = cfg.driver
        #: #5976 : famille de ses sessions dans pg_stat_activity
        self.family = family
        self.idle_s = float(idle_s)
        self._local = threading.local()
        self._cond = threading.Condition()
        self._held = {}  # thread ident -> _Held, under self._cond
        self._reaper = None

    @contextmanager
    def _connection(self):
        active = getattr(self._local, 'active', None)
        if active is not None:
            yield active
            return
        me = threading.get_ident()
        with self._cond:
            held = self._held.get(me)
            if held is not None:
                held.busy = True
        if held is None:
            with db_mod.application(self.family):
                held = _Held(db_mod.connect(self.cfg))
            with self._cond:
                self._held[me] = held
                self._start_reaper()
        try:
            yield held.driver
        except BaseException:
            with self._cond:
                if self._held.get(me) is held:
                    del self._held[me]
            _close(held.driver)
            raise
        else:
            with self._cond:
                held.busy = False
                held.last = time.monotonic()
                self._cond.notify_all()

    def _start_reaper(self):
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, name="scoped-db-reaper",
                                            daemon=True)
            self._reaper.start()

    def _reap(self):
        while True:
            due = []
            with self._cond:
                if not self._held:
                    self._reaper = None
                    return
                now = time.monotonic()
                wait = self.idle_s
                for ident, held in list(self._held.items()):
                    if held.busy:
                        continue
                    left = held.last + self.idle_s - now
                    if left <= 0:
                        due.append(self._held.pop(ident))
                    else:
                        wait = min(wait, left)
                if not due:
                    self._cond.wait(max(wait, 0.01))
            for held in due:
                _close(held.driver)

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

    def release(self):
        """Return this thread's idle session before an external wait.

        Inside a transaction nothing is released: the transaction ends first.
        """
        if getattr(self._local, 'active', None) is not None:
            return
        with self._cond:
            held = self._held.get(threading.get_ident())
            if held is None or held.busy:
                return
            del self._held[threading.get_ident()]
            self._cond.notify_all()
        _close(held.driver)

    def close(self):
        # End of the pass: only this thread's session, never another
        # thread's transaction. The object stays usable (a later statement
        # opens a new session).
        self.release()


def release(db):
    """Return a `ScopedDb` session before an external wait; no-op otherwise."""
    if isinstance(db, ScopedDb):
        db.release()


def _close(driver):
    try:
        driver.close()
    except Exception:
        pass
