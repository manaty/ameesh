# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP2) — Postgres port of the elected-role standings.

`election.py` holds the decisions (availability, readiness, capacity, score,
rank delay, hysteresis) without SQL; this module is its storage port. The
table is private to the election (`migrations/0476_role_standings.sql`): one
writer per candidate host, readers are the same hosts' runners and
`ameesh role status`, never the mediated executor. That is why it is not a
`storage.interface` domain: adding one would widen the executor contract for
a table the device must not reach.

Append-only by design: the hysteresis needs one hour of readings, so the
score is kept as a short history and pruned after `HISTORY_S`. Timestamps are
Postgres' clock (never the host's), so a diverging host clock cannot stretch
an hour into a minute.
"""
from __future__ import annotations

import math

#: the availability window of the study: the last 7 days of readings
WINDOW_S = 7 * 86400
#: the hysteresis needs one hour; keep a day of margin, never grow forever
HISTORY_S = 2 * 86400

#: candidate rows of an elected role, written by `canon sync` on each host.
#: `canon_fresh`/`placement_profile_fresh` recheck the two conditions of the
#: claim CAS (`canon_claim_predicate_sql`) that a stored verdict can outlive:
#: a canon renamed since the sync, or a profile (host/harness/provider/model/
#: credentials) changed since the evaluation. A reader that ignored them would
#: rank a candidate that `registry.claim` then refuses.
_CANDIDATE_COLUMNS = (
    "c.host, c.canon, c.cwd, c.placement_ok, c.placement_diagnostic, "
    "c.placement_ref, c.updated_at, "
    "(c.canon = coalesce(r.canon, '')) AS canon_fresh, "
    "(c.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile("
    "c.host, r.harness, r.provider, r.model, r.credential_mode))"
    " AS placement_profile_fresh")
_CANDIDATE_FROM = ("agent_candidates c JOIN agent_registry r"
                   " ON r.name = c.name")


def now_ts(db) -> float:
    """The database clock as an epoch float (the only clock used here).

    `now()` (transaction start), not the host clock: freshness is a database
    fact, and a candidate cannot stretch its hour by moving its own clock.
    """
    row = db.query("SELECT extract(epoch FROM now())::float8 AS ts")[0]
    return float(row["ts"])


def candidate_rows(db, name: str) -> list[dict]:
    """The `agent_candidates` verdicts of `name`, one per candidate host."""
    return db.query(
        "SELECT " + _CANDIDATE_COLUMNS + " FROM " + _CANDIDATE_FROM +
        " WHERE c.name = %s ORDER BY c.host", (name,))


def candidate(db, name: str, host: str) -> dict | None:
    rows = db.query(
        "SELECT " + _CANDIDATE_COLUMNS + " FROM " + _CANDIDATE_FROM +
        " WHERE c.name = %s AND c.host = %s", (name, host))
    return rows[0] if rows else None


def elected_roles(db, host: str) -> list[dict]:
    """Rows of the elected roles for which `host` is a declared candidate.

    A stopped role is not elected: it is left out here, so no new standing is
    published until a human revives it (the lease, if any, still wears out).
    """
    return db.query(
        """
        SELECT name, aliases, chantier, harness, host, cwd, status, status_text,
               model, responsible, team, canon, canon_ref, releve, candidats,
               session_account, operator_paused, operator_pause_reason,
               budget_usd, spent_usd
          FROM agent_registry
         WHERE releve = 'election'
           AND %s = ANY(candidats)
           AND status <> 'stopped'
           AND mode = 'execute'
         ORDER BY name
        """, (host,))


def latest_reading(db, host: str) -> dict | None:
    """The newest resource reading of `host`, with its age on the DB clock."""
    rows = db.query(
        """
        SELECT extract(epoch FROM sampled_at)::float8 AS sampled_ts,
               extract(epoch FROM now())::float8 AS now_ts,
               load1, cpu_count, mem_available_bytes, turns_in_progress,
               ameesh_host_gate_state(host, gate_state) AS gate_state
          FROM host_resources
         WHERE host = %s
         ORDER BY sampled_at DESC, id DESC
         LIMIT 1
        """, (host,))
    return rows[0] if rows else None


def samples(db, host: str, since_ts: float) -> list[float]:
    """The sample instants of `host` in `[since_ts, now]`, ascending.

    A reading dated in the future is ignored: it cannot prove that the host
    was alive, and it must not flatter its availability.
    """
    rows = db.query(
        """
        SELECT extract(epoch FROM sampled_at)::float8 AS ts
          FROM host_resources
         WHERE host = %s AND sampled_at > to_timestamp(%s::float8)
           AND sampled_at <= now()
         ORDER BY sampled_at, id
        """, (host, float(since_ts)))
    return [float(row["ts"]) for row in rows]


#: #3921: width of a `host_presence` bucket (migration 0496). Two readings of
#: one bucket are less than this apart, so a bucket is covered end to end by
#: any `cover_s` at least this wide.
PRESENCE_BUCKET_S = 120.0

#: #3921: the start of the bucket holding `now()`: the buckets before it are
#: complete and past
_PRESENT_BUCKET = ("to_timestamp(floor(extract(epoch FROM now()) / %d) * %d)"
                   % (PRESENCE_BUCKET_S, PRESENCE_BUCKET_S))
#: readings one by one, as segments of one reading
_RAW_SEGMENTS = (
    "SELECT extract(epoch FROM sampled_at) AS f, extract(epoch FROM sampled_at) AS l,"
    " 1::bigint AS n FROM host_resources"
    " WHERE host = %s AND sampled_at > to_timestamp(%s::float8) AND sampled_at <= now()")
#: the coverage of segments ordered in time: a segment covers itself up to
#: `now`, then its last reading covers up to the next segment, `cover` and
#: `now` — exactly the per-reading sum when no gap inside a segment exceeds
#: `cover`
_COVERAGE_SQL = """
    SELECT coalesce(sum(n), 0)::int AS samples,
           min(f)::float8 AS first_ts, max(l)::float8 AS last_ts,
           coalesce(sum(greatest(0, least(%s::numeric, l) - f)
                        + greatest(0, least(%s::numeric, l + %s::numeric,
                                            coalesce(next_f, l + %s::numeric)) - l)),
                    0)::float8 AS covered_s
      FROM (SELECT f, l, n, lead(f) OVER (ORDER BY f, l) AS next_f
              FROM ({segments}) seg) ordered
"""


def coverage(db, host: str, since_ts: float, *, now: float, cover_s: float) -> dict:
    """#3921: the coverage of the readings of `host` in `]since_ts, now()]`.

    Same result as summing, over `samples(db, host, since_ts)`, the time each
    reading covers (`min(now, ts + cover_s, next ts) - ts`, never negative),
    but computed by the database in one row. The complete buckets of
    `host_presence` are read as segments (first, last, count); only the
    readings of the partial buckets at both ends of the window are read one by
    one (all of them when `cover_s` is narrower than a bucket, where a bucket
    may hide an uncovered gap). ~5 000 rows per host for 7 days instead of
    ~140 000. Returns `samples`, `first_ts`, `last_ts` (None without a
    reading) and `covered_s`. Readings dated in the future are ignored, as in
    `samples`.
    """
    since, cover = float(since_ts), float(cover_s)
    head = (host, since)
    if cover < PRESENCE_BUCKET_S:
        segments, params = _RAW_SEGMENTS, head
    else:
        # the bucket holding `since` is read raw even when `since` starts it:
        # the window excludes `since` itself (`sampled_at > since`)
        lo = (math.floor(since / PRESENCE_BUCKET_S) + 1) * PRESENCE_BUCKET_S
        segments = (
            "SELECT extract(epoch FROM first_at) AS f, extract(epoch FROM last_at) AS l,"
            " readings AS n"
            " FROM host_presence WHERE host = %s AND readings > 0"
            " AND bucket_start >= to_timestamp(%s::float8)"
            " AND bucket_start < " + _PRESENT_BUCKET
            + " UNION ALL " + _RAW_SEGMENTS + " AND sampled_at < to_timestamp(%s::float8)"
            + " UNION ALL " + _RAW_SEGMENTS
            + " AND sampled_at >= greatest(to_timestamp(%s::float8), " + _PRESENT_BUCKET + ")")
        params = (host, lo) + head + (lo,) + head + (lo,)
    rows = db.query(_COVERAGE_SQL.format(segments=segments),
                    (float(now), float(now), cover, cover) + params)
    row = rows[0] if rows else {}
    return {"samples": int(row.get("samples") or 0),
            "first_ts": row.get("first_ts"), "last_ts": row.get("last_ts"),
            "covered_s": float(row.get("covered_s") or 0.0)}


def sample_interval_s(db, host: str, *, now: float | None = None,
                      window_s: float = 3600.0) -> float | None:
    """The median interval between the last hour of readings, or None.

    That is the host's own cadence, used with `host_lifecycle.staleness_s` to
    judge a fresh reading and a fresh standing — no fixed freshness constant.
    #3921: the median is taken by the database (one row instead of the hour of
    readings); the gaps are those of `samples`, zero gaps excluded.
    """
    now = now_ts(db) if now is None else float(now)
    rows = db.query(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::float8 AS median
          FROM (SELECT extract(epoch FROM lead(sampled_at) OVER (ORDER BY sampled_at, id)
                                          - sampled_at)::float8 AS gap
                  FROM host_resources
                 WHERE host = %s AND sampled_at > to_timestamp(%s::float8)
                   AND sampled_at <= now()) g
         WHERE gap > 0
        """, (host, now - float(window_s)))
    median = rows[0].get("median") if rows else None
    return None if median is None else float(median)


def record(db, name: str, host: str, *, availability: float, capacity: float,
           score: float, ready: bool, diagnostic: str,
           notify_ok: bool = False, notify_diagnostic: str = "") -> None:
    """Append one standing; the database stamps it with its own clock.

    #3568: `notify_ok` is the candidate's LOCAL ability to push an alert
    (route and secret resolved by ``role_timers.notify_reach``). It is
    published with the standing so that the readers can delegate the alert
    delivery to a candidate that can actually push; it never changes the
    role's eligibility (`ready`).
    """
    db.execute(
        """
        INSERT INTO role_standings
            (name, host, availability, capacity, score, ready, diagnostic,
             notify_ok, notify_diagnostic)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (name, host, float(availability), float(capacity), float(score),
         bool(ready), diagnostic or "", bool(notify_ok),
         notify_diagnostic or ""))


def latest(db, name: str) -> dict[str, dict]:
    """The newest standing of `name` per candidate host (`{host: row}`)."""
    rows = db.query(
        """
        SELECT DISTINCT ON (host)
               host, id,
               extract(epoch FROM evaluated_at)::float8 AS evaluated_ts,
               extract(epoch FROM now())::float8 AS now_ts,
               availability, capacity, score, ready, diagnostic,
               notify_ok, notify_diagnostic
          FROM role_standings
         WHERE name = %s
         ORDER BY host, evaluated_at DESC, id DESC
        """, (name,))
    return {row["host"]: row for row in rows}


def history(db, name: str, since_ts: float) -> list[dict]:
    """Standings of `name` after `since_ts`, oldest first, all hosts."""
    return db.query(
        """
        SELECT host, id,
               extract(epoch FROM evaluated_at)::float8 AS evaluated_ts,
               availability, capacity, score, ready, diagnostic
          FROM role_standings
         WHERE name = %s AND evaluated_at > to_timestamp(%s::float8)
         ORDER BY evaluated_at, id
        """, (name, float(since_ts)))


def prune(db, *, keep_s: float = HISTORY_S) -> int:
    """Drop the standings older than `keep_s`; returns how many rows went."""
    rows = db.query(
        "DELETE FROM role_standings"
        " WHERE evaluated_at < now() - make_interval(secs => %s::float8)"
        " RETURNING id", (float(keep_s),))
    return len(rows)
