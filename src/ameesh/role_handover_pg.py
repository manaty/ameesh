# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP2) — Postgres port of the planned-handover release marks.

`runner.AgentWorker` decides when a holder releases an elected role to a
challenger and `runner.Runner` keeps the reclaim margin in memory
(`_handover_released`). That mark must survive an executor restart, otherwise
the resumed holder reclaims at once and can hand the role again to the same
challenger that cannot claim it (N of the #3011 review, basket #3099).

The table (`migrations/0478_role_handover.sql`) is private to the election,
like `role_standings`: one writer per candidate host, read only by that host's
runners. It stores one row per `(host, role, challenger)` release. The failed
cooldown is not stored: it is derived from `released_until + HYSTERESIS_S` by
the loader, so a restart recomputes the same window deterministically.

Timestamps are Postgres' clock (never the host's), as for the standings: a
diverging host clock cannot stretch a margin.
"""
from __future__ import annotations

#: a release mark is small and rare; keep two days like the standings history
HISTORY_S = 2 * 86400


def load(db, host: str) -> list[dict]:
    """The unexpired release marks of `host`, with the database clock.

    `released_ts` and `now_ts` are epoch floats of the same database clock;
    the caller converts the remaining delay on its own monotonic clock. A row
    whose cooldown (`released_until + HYSTERESIS_S`) is over is left out.
    """
    return db.query(
        """
        SELECT name, challenger,
               extract(epoch FROM released_until)::float8 AS released_ts,
               extract(epoch FROM now())::float8 AS now_ts
          FROM role_handover
         WHERE host = %s
           AND released_until > now() - make_interval(secs => %s::float8)
         ORDER BY released_until, challenger
        """, (host, float(HISTORY_S)))


def release(db, host: str, name: str, challenger: str, ttl_s: float) -> None:
    """Record that `host` released `name` to `challenger` for `ttl_s`.

    A new release to the same challenger replaces the earlier mark; a release
    to another challenger leaves the former row in place (the loader turns an
    expired row into that challenger's cooldown).
    """
    db.execute(
        """
        INSERT INTO role_handover (host, name, challenger, released_until)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s::float8))
        ON CONFLICT (host, name, challenger) DO UPDATE
           SET released_until = excluded.released_until,
               updated_at = now()
        """, (host, name, challenger, max(0.0, float(ttl_s))))


def prune(db, *, keep_s: float = HISTORY_S) -> int:
    """Drop the marks older than `keep_s`; returns how many rows went."""
    rows = db.query(
        "DELETE FROM role_handover"
        " WHERE released_until < now() - make_interval(secs => %s::float8)"
        " RETURNING host", (float(keep_s),))
    return len(rows)
