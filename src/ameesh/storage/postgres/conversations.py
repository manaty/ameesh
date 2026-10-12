# SPDX-License-Identifier: AGPL-3.0-only
"""L78 conversation storage, identical on psycopg and psql."""
from __future__ import annotations

import json

from ..conversations import Conversations as Contract

_FIELDS = """agent, seq, session, turn, kind, author, text, truncated,
             meta - '_conversation_policy' AS meta,
             event_id, project, lot, extract(epoch FROM created_at)::float8 AS ts"""


# Earlier producers could persist tool output as assistant text. No heuristic
# can establish provenance afterwards; quarantine every unmarked legacy row.
_POLICY_KEY = "_conversation_policy"
_POLICY = "2"
_TRUSTED = "meta ->> '_conversation_policy' = '2'"


class Conversations(Contract):
    def __init__(self, db):
        self.db = db

    def append(self, *, agent, session, turn, kind, author, text, truncated, meta,
               event_id, project, lot, thread_text):
        meta = dict(meta, **{_POLICY_KEY: _POLICY})
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO conversation_heads(agent) VALUES (%s)"
                       " ON CONFLICT DO NOTHING", (agent,))
            tx.query("SELECT seq FROM conversation_heads WHERE agent = %s FOR UPDATE", (agent,))
            # A separate snapshot after the lock sees a concurrent replay's commit.
            if event_id is not None:
                existing = tx.query("SELECT " + _FIELDS + ", " + _TRUSTED
                                    + " AS trusted FROM conversation_log"
                                    " WHERE agent = %s AND event_id = %s", (agent, event_id))
                if existing:
                    row = existing[0]
                    if not row.pop("trusted"):
                        row.update(text="", meta={}, truncated=True)
                    return row
            seq = tx.query("UPDATE conversation_heads SET seq = seq + 1"
                           " WHERE agent = %s RETURNING seq", (agent,))[0]["seq"]
            return tx.query("INSERT INTO conversation_log"
                            " (agent, seq, session, turn, kind, author, text, truncated, meta,"
                            " event_id, project, lot, thread_text)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)"
                            " RETURNING " + _FIELDS,
                            (agent, seq, session, turn, kind, author, text, truncated,
                             json.dumps(meta, ensure_ascii=False, allow_nan=False),
                             event_id, project, lot, thread_text))[0]

    def read(self, agent, *, after=0, limit=100):
        return self.db.query("""
            WITH page AS (SELECT """ + _FIELDS + """ FROM conversation_log
                          WHERE agent = %s AND seq > %s AND """ + _TRUSTED + """ ORDER BY seq LIMIT %s)
            SELECT coalesce((SELECT seq FROM conversation_heads WHERE agent = %s), 0)
                       AS last_seq,
                   (SELECT min(seq) FROM conversation_log WHERE agent = %s AND """ + _TRUSTED + """ ) AS oldest_seq,
                   coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY seq) FROM page),
                            '[]'::jsonb) AS entries,
                   EXISTS (SELECT 1 FROM conversation_log WHERE agent = %s
                            AND """ + _TRUSTED + """ AND seq > coalesce((SELECT max(seq) FROM page), %s)) AS more
            """, (agent, after, limit, agent, agent, agent, after))[0]

    def recent(self, agents, *, limit=20):
        if not agents:
            return []
        return self.db.query("""
            SELECT names.agent, coalesce(head.seq, 0) AS last_seq, page.entries
            FROM jsonb_array_elements_text(%s::jsonb) AS names(agent)
            LEFT JOIN conversation_heads head ON head.agent = names.agent
            CROSS JOIN LATERAL (
                SELECT coalesce(jsonb_agg(to_jsonb(rows) ORDER BY seq), '[]'::jsonb) AS entries
                FROM (SELECT """ + _FIELDS + """ FROM conversation_log
                      WHERE agent = names.agent AND """ + _TRUSTED + """ ORDER BY seq DESC LIMIT %s) AS rows
            ) AS page ORDER BY names.agent
            """, (json.dumps(list(dict.fromkeys(agents))), limit))

    def pending_threads(self, agent, *, limit=32):
        return self.db.query("SELECT " + _FIELDS + ", thread_text FROM conversation_log"
                             " WHERE agent = %s AND thread_text IS NOT NULL"
                             " AND thread_entry IS NULL AND " + _TRUSTED + " ORDER BY seq LIMIT %s FOR UPDATE",
                             (agent, limit))

    def thread_delivered(self, agent, seq, entry_id):
        self.db.execute("UPDATE conversation_log SET thread_entry = %s, thread_text = NULL"
                        " WHERE agent = %s AND seq = %s AND thread_entry IS NULL",
                        (entry_id, agent, seq))

    def prune(self, agent, *, before_ts):
        with self.db.transaction() as tx:
            tx.query("SELECT seq FROM conversation_heads WHERE agent = %s FOR UPDATE", (agent,))
            where = (" WHERE agent = %s AND created_at < to_timestamp(%s)"
                     " AND (thread_text IS NULL OR thread_entry IS NOT NULL)")
            count = tx.query("SELECT count(*) AS n FROM conversation_log" + where,
                             (agent, before_ts))[0]["n"]
            tx.execute("DELETE FROM conversation_log" + where, (agent, before_ts))
            return int(count)
