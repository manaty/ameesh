# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: the `Idempotency-Key` of the API's actions (L87,
migration 0440).

`reserve` purges the expired rows in a transaction of its own, which never
waits (`skip locked`), then reserves in ONE transaction: an insert that does
nothing on conflict, and the read-back. Two requests with the same key order
themselves on the primary key: the second waits for the first's commit, finds
its row and reads it back. The reservation holds one row lock only, and the
purge waits on none, so concurrent reservations cannot form a lock cycle
(#1497 N3). The response is read as text and decoded here, the same way
under both database drivers.
"""
from __future__ import annotations

import json

from .. import interface


class ApiRequests(interface.ApiRequests):

    def reserve(self, human, key, fingerprint, *, retention_s) -> dict | None:
        with self.db.transaction() as tx:
            tx.execute("delete from api_idempotency where (human, key) in ("
                       " select human, key from api_idempotency"
                       " where created_at < now() - make_interval(secs => %s)"
                       " for update skip locked)", (int(retention_s),))
        with self.db.transaction() as tx:
            if tx.query("insert into api_idempotency (human, key, fingerprint)"
                        " values (%s, %s, %s) on conflict (human, key) do nothing"
                        " returning key", (human, key, fingerprint)):
                return None
            rows = tx.query("select fingerprint, status, response::text as response"
                            " from api_idempotency where human = %s and key = %s",
                            (human, key))
        if not rows:
            # purged or released between the two statements: busy, never "new"
            return {"fingerprint": fingerprint, "status": None, "response": None}
        row = rows[0]
        return {"fingerprint": row["fingerprint"],
                "status": None if row["status"] is None else int(row["status"]),
                "response": None if row["response"] is None else json.loads(row["response"])}

    def complete(self, human, key, status, response) -> bool:
        return bool(self.db.query(
            "update api_idempotency set status = %s, response = %s::jsonb, done_at = now()"
            " where human = %s and key = %s and status is null returning key",
            (int(status), json.dumps(response, ensure_ascii=False, sort_keys=True,
                                     default=str), human, key)))

    def release(self, human, key) -> bool:
        return bool(self.db.query(
            "delete from api_idempotency where human = %s and key = %s and status is null"
            " returning key", (human, key)))
