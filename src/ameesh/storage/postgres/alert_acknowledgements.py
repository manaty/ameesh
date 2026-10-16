# SPDX-License-Identifier: AGPL-3.0-only
"""L87c: durable acknowledgements and their retained audit history."""
from __future__ import annotations

from .. import interface

_FIELDS = ("id, alert_key, severity, observation_host, actor, principal, "
           "extract(epoch from acknowledged_at)::float8 as acknowledged_ts")


class AlertAcknowledgements(interface.AlertAcknowledgements):
    def live(self) -> list[dict]:
        return self.db.query("select " + _FIELDS + " from alert_acknowledgements"
                             " where invalidated_at is null order by id")

    def acknowledge(self, key, severity, actor, principal, *, observation_host=None) -> dict:
        with self.db.transaction() as tx:
            # Serializes creation as well as replacement: SELECT FOR UPDATE
            # alone would not lock an absent acknowledgement.
            tx.query("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("alert-ack:" + key + ":" + principal,))
            tx.execute("update alert_acknowledgements set invalidated_at = clock_timestamp(),"
                       " invalidation_reason = 'severity_changed'"
                       " where alert_key = %s and principal = %s"
                       " and invalidated_at is null and severity <> %s",
                       (key, principal, severity))
            rows = tx.query("insert into alert_acknowledgements"
                            " (alert_key, severity, actor, principal, observation_host)"
                            " values (%s, %s, %s, %s, %s)"
                            " on conflict (alert_key, principal) where invalidated_at is null"
                            " do nothing returning " + _FIELDS,
                            (key, severity, actor, principal, observation_host))
            changed = bool(rows)
            if not rows:
                rows = tx.query("update alert_acknowledgements"
                                " set confirmed_at = clock_timestamp()"
                                " where alert_key = %s and principal = %s"
                                " and invalidated_at is null returning " + _FIELDS,
                                (key, principal))
            return dict(rows[0], changed=changed)

    def invalidate(self, id, *, reason, observed_ts) -> bool:
        # A snapshot collected before an acknowledgement must not cancel it.
        with self.db.transaction() as tx:
            rows = tx.query("select alert_key, principal from alert_acknowledgements"
                            " where id = %s", (int(id),))
            if not rows:
                return False
            row = rows[0]
            tx.query("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("alert-ack:" + row["alert_key"] + ":" + row["principal"],))
            return bool(tx.query(
                "update alert_acknowledgements set invalidated_at = clock_timestamp(),"
                " invalidation_reason = %s where id = %s and invalidated_at is null"
                " and confirmed_at <= to_timestamp(%s) returning id",
                (reason, int(id), observed_ts)))

    def clock(self) -> float:
        return float(self.db.query(
            "select extract(epoch from clock_timestamp())::float8 as ts")[0]["ts"])
