# SPDX-License-Identifier: AGPL-3.0-only
"""#4468 — Postgres port of the "who was last entrusted the alerts pass" memory.

`election.alerts_delegate` applies the same liveness rule to every routed
candidate: a host keeps the pass while its shared `notify_liveness` beat is
fresh, a host that has not beaten yet gets a bounded grace, and a host that
ran and then stopped beating yields to another routed candidate. The last
part needs the instant the pass was handed over, otherwise the choice either
stays on the oldest dead beat or jumps to the newest one.

The table (`migrations/0514_alerts_delegations.sql`) is private to the
election, like `role_standings`: `role_run` writes it when it is the delegate
and holds the shared execution lease (`merge_locks`, key `alerts-pass:<role>`),
every candidate reads it. `entrust` is fenced by that lease in the same
statement, so a wrapper that already lost it cannot record a stale handover.
The timestamp is the database clock, as for the standings: a host clock cannot
stretch the grace.
"""
from __future__ import annotations


def entrust(db, role: str, host: str, *, lock_key: str, owner: str,
            epoch: int) -> bool:
    """Record `host` as the delegate, under the live execution lease.

    The `held` CTE is the fence: an INSERT without a conflict must also see the
    live `merge_locks` row, which a plain `ON CONFLICT ... WHERE` would not
    check. The INSERT is the statement itself, not a data-modifying CTE: the
    psql driver wraps `query()` in an outer CTE for its JSON output, and
    PostgreSQL refuses a data-modifying statement nested in a CTE ("must be at
    the top level"). With the write in a CTE the handover was silently never
    recorded under that driver (#3568 witness).
    The same host does not renew its handover: a pass that runs every 30 s and
    exits without beating would otherwise reset its own grace forever (review
    cx3). Only a host change starts a new handover window.

    Returns False when the lease is gone (another host may run), when the
    handover is unchanged, or when the statement did not write.
    """
    rows = db.query(
        """
        WITH held AS (
            SELECT 1
              FROM merge_locks
             WHERE target = %s AND owner = %s AND epoch = %s
               AND expires_at > clock_timestamp()
        )
        INSERT INTO alerts_delegations (role, host, entrusted_at)
        SELECT %s, %s, clock_timestamp() FROM held
        ON CONFLICT (role) DO UPDATE
           SET host = excluded.host,
               entrusted_at = excluded.entrusted_at
         WHERE alerts_delegations.host IS DISTINCT FROM excluded.host
        RETURNING role
        """, (lock_key, owner, int(epoch), role, host))
    return bool(rows)


def latest(db, role: str) -> dict | None:
    """The last handover of `role` (`host`, `entrusted_ts`), or None."""
    rows = db.query(
        "SELECT host, extract(epoch FROM entrusted_at)::float8 AS entrusted_ts"
        " FROM alerts_delegations WHERE role = %s", (role,))
    return dict(rows[0]) if rows else None
