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

#4086: the same table carries the **restart reservation** of an executor that
stops while it holds an elected role (drain, `vm.sh redemarrer`, update). Its
row names the stopping host and the sentinel challenger `RESTART`; it is read
by every candidate (`restart_reservation`), unlike a release mark, and the
loader of release marks leaves it out. No migration: the row fits the
existing key `(host, role, challenger)`.
"""
from __future__ import annotations

#: a release mark is small and rare; keep two days like the standings history
HISTORY_S = 2 * 86400
#: #4086: the challenger of a restart reservation (never a host name)
RESTART = "@restart"


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
         WHERE host = %s AND challenger <> %s
           AND released_until > now() - make_interval(secs => %s::float8)
         ORDER BY released_until, challenger
        """, (host, RESTART, float(HISTORY_S)))


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


def reserve_restart(db, host: str, name: str, ttl_s: float, *,
                    owner: str, epoch: int) -> bool:
    """#4086: `host` stops while holding `name` and reserves it for `ttl_s`.

    Written BEFORE the lease is returned, so no candidate sees the role free
    without the reservation. B1 of the review: written only while `owner`
    holds the exact live lease `epoch`, under the registry row lock that
    `registry.claim` takes first. A worker whose lease expired and was taken
    elsewhere, without having seen it, writes nothing. Returns whether the
    reservation was written.
    """
    rows = db.query(
        """
        WITH holder AS (
            SELECT name FROM agent_registry
             WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
               AND lease_expires_at > clock_timestamp()
               FOR UPDATE
        )
        INSERT INTO role_handover (host, name, challenger, released_until)
        SELECT %s, holder.name, %s,
               clock_timestamp() + make_interval(secs => %s::float8)
          FROM holder
        ON CONFLICT (host, name, challenger) DO UPDATE
           SET released_until = excluded.released_until,
               updated_at = now()
        RETURNING host
        """, (name, owner, int(epoch), host, RESTART, max(0.0, float(ttl_s))))
    return bool(rows)


def restart_reservation(db, name: str) -> dict | None:
    """#4086: the live restart reservation of `name`, if any.

    `{"host", "left_s"}` on the database clock (`clock_timestamp()`, not the
    reader's transaction start). Each one was written under the exact live
    lease of its host (`reserve_restart`) and any claim consumes them all
    (`clear_restart`), so a live one names the last holder; the latest wins
    all the same.
    """
    rows = db.query(
        """
        SELECT host,
               extract(epoch FROM released_until - clock_timestamp())::float8 AS left_s
          FROM role_handover
         WHERE name = %s AND challenger = %s
           AND released_until > clock_timestamp()
         ORDER BY updated_at DESC, released_until DESC
         LIMIT 1
        """, (name, RESTART))
    return rows[0] if rows else None


def clear_restart(db, name: str) -> None:
    """#4086: `name` is held again; every restart reservation of it is spent."""
    db.execute(
        "DELETE FROM role_handover WHERE name = %s AND challenger = %s",
        (name, RESTART))


def prune(db, *, keep_s: float = HISTORY_S) -> int:
    """Drop the marks older than `keep_s`; returns how many rows went."""
    rows = db.query(
        "DELETE FROM role_handover"
        " WHERE released_until < now() - make_interval(secs => %s::float8)"
        " RETURNING host", (float(keep_s),))
    return len(rows)
