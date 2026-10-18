#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2339: exercise installed auto-take against a migrated schema, read-only.

Run with the installed venv's python -I: imports must come from the code
still serving runners, never from the migration snapshot or PYTHONPATH.
This fresh connection checks SQL compatibility, not existing cached plans;
long-lived connections must disable automatic preparation (db.py, #2339).
"""
import dataclasses
import sys
import threading
from types import SimpleNamespace


def probe(cfg=None):
    from ameesh import backlog, config, db, runner, storage

    cfg = dataclasses.replace(cfg or config.load(), read_only=True)
    conn = db.connect(cfg)
    try:
        listing = storage.of(conn).operations.listing()
        # Exercise the SELECT * that failed after 0456 even if lots/backlog
        # are available (auto_take would otherwise skip review proposals).
        storage.of(conn).work.review_proposals(
            1, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE)
        names = [r['name'] for r in listing if r.get('host') == cfg.host]
        worker = SimpleNamespace(is_alive=lambda: True)
        state = SimpleNamespace(
            db=conn, cfg=cfg, once=False, mediated=False, dry_run=True,
            take=dict(idle_s=backlog.DEFAULT_TAKE_IDLE_S,
                      max_per_hour=backlog.DEFAULT_TAKE_MAX_PER_HOUR,
                      lot_idle_s=backlog.DEFAULT_LOT_IDLE_S,
                      lot_max_per_hour=backlog.DEFAULT_LOT_MAX_PER_HOUR),
            take_balance={}, host_pressure=lambda: {'blocked': False},
            lock=threading.Lock(), workers={n: worker for n in names or ['schema-probe']})
        runner.Runner.take_pass(state)
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        probe()
    except Exception as exc:
        print('auto-take compatibility failed (%s): %s' % (
            type(exc).__name__, ' '.join(str(exc).split())[:300]), file=sys.stderr)
        sys.exit(1)
    print('installed auto-take: compatible (read-only)')
