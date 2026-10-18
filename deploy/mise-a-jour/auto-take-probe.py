#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2339: exercise installed auto-take against a migrated schema, read-only.

Run with the installed venv's python -I: imports must come from the code
still serving runners, never from the migration snapshot or PYTHONPATH.
This fresh connection checks SQL compatibility, not existing cached plans;
long-lived connections must disable automatic preparation (db.py, #2339).

#2359: the runner is built by the installed constructor, as `agent-runner`
builds it, so this probe never mirrors the attributes take_pass reads.
Exit 1: installed auto-take fails on the schema; exit 2: the probe could not
set up the installed runner, compatibility unknown.
"""
import dataclasses
import sys
from types import SimpleNamespace

INCOMPATIBLE, UNVERIFIED = 1, 2


class ProbeUnusable(Exception):
    """The installed code does not accept this probe: compatibility unknown."""


def probe(cfg=None):
    from ameesh import backlog, config, db, runner, storage

    cfg = dataclasses.replace(cfg or config.load(), read_only=True)
    conn = db.connect(cfg)
    try:
        try:
            listing = storage.of(conn).operations.listing()
            # Exercise the SELECT * that failed after 0456 even if lots/backlog
            # are available (auto_take would otherwise skip review proposals).
            storage.of(conn).work.review_proposals(
                1, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE)
            # Same options as `agent-runner`; dry_run: nothing is taken.
            state = runner.Runner(
                cfg, conn, dry_run=True,
                take=dict(idle_s=backlog.DEFAULT_TAKE_IDLE_S,
                          max_per_hour=backlog.DEFAULT_TAKE_MAX_PER_HOUR,
                          lot_idle_s=backlog.DEFAULT_LOT_IDLE_S,
                          lot_max_per_hour=backlog.DEFAULT_LOT_MAX_PER_HOUR),
                take_balance={})
            # This host's agents stand in for its live workers.
            names = [r['name'] for r in listing if r.get('host') == cfg.host]
            worker = SimpleNamespace(is_alive=lambda: True)
            with state.lock:
                state.workers.update({n: worker for n in names or ['schema-probe']})
        except db.DbError:
            raise
        except (Exception, SystemExit) as exc:
            raise ProbeUnusable('%s: %s' % (type(exc).__name__, exc)) from exc
        # Whatever fails here fails in the runners too: incompatible.
        state.take_pass()
    finally:
        conn.close()


def one_line(exc):
    return ' '.join(str(exc).split())[:300]


if __name__ == '__main__':
    try:
        probe()
    except ProbeUnusable as exc:
        print('auto-take compatibility not verified (probe unusable): %s'
              % one_line(exc), file=sys.stderr)
        sys.exit(UNVERIFIED)
    except Exception as exc:
        print('auto-take compatibility failed (%s): %s' % (
            type(exc).__name__, one_line(exc)), file=sys.stderr)
        sys.exit(INCOMPATIBLE)
    print('installed auto-take: compatible (read-only)')
