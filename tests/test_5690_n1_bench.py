# SPDX-License-Identifier: AGPL-3.0-only
"""#5690: the "old writer / new reader" bench, and the N-1 review question.

The staging failure of 2026-11-04 00:42Z: the new code read
`canon_state.sync_completed_at`, but runners still on ab776f2 completed the
shared sync without writing it (`complete_sync`, pre-0541). The result was
refused as too old and the restarted runner missed the "redémarrer" guard
(#5653/#5688). The additive gate is syntactic and no test or review point
covered "a new column read by the new code, left unwritten by the old writers
during a wave deployment".

The bench replays the N-1 write against the current schema (defaults and
compatibility triggers included), then the new reader must hold. Here the
write is the exact pre-0541 `complete_sync` SQL; migration 0542's trigger dates
the result from the claim, so the new reader still reuses it while a genuinely
old result stays refused. The review question is added to the reviewer mail
when the frozen diff carries SQL.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ameesh import canon_sync
from ameesh import review_designation as rd
from .support import PgTestCase

HOST = "n1-5690"
INTERVAL = 300
MAX_AGE = INTERVAL * canon_sync.SYNC_REUSE_MAX_INTERVALS
SIGNATURE = "a" * 64
TOKEN = "old-runner"

#: `complete_sync` des runners d'avant 0541 (ab776f2) : il avance le jeton
#: terminé et la signature, sans connaître `sync_completed_at` (0541) ni
#: `sync_completed_claim_at` (0542).
PRE_0541_COMPLETE = (
    "UPDATE canon_state SET sync_completed_token = %s, sync_signature = %s "
    "WHERE host = %s AND canon = %s AND sync_token = %s")

FAMILIES = ("claude", "codex", "deepseek")
SHA = "a" * 40
LOT = {"id": 5690, "title": "Compatibilité N-1", "app": "ameesh",
       "assignee": "mty-amh-impl-01"}


class N1WriterBenchTest(PgTestCase):
    """L'écriture N-1 est rejouée ; le nouveau lecteur doit tenir."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM canon_state WHERE host = %s", (HOST,))
        self.store = canon_sync.storage.of(self.db).canon

    def claim(self):
        self.assertTrue(self.store.claim_sync(HOST, "", TOKEN, INTERVAL))

    def test_old_writer_sql_may_not_name_the_added_column(self):
        with self.assertRaisesRegex(ValueError, "sync_completed_at"):
            self.previous_write(
                "UPDATE canon_state SET sync_completed_at = now() WHERE host = %s",
                (HOST,), added=("sync_completed_at",))
        with self.assertRaisesRegex(ValueError, "sync_completed_at"):
            self.previous_write(
                'UPDATE canon_state SET "sync_completed_at" = now() WHERE host = %s',
                (HOST,), added=("sync_completed_at",))
        with self.assertRaisesRegex(ValueError, "ne peut pas être vide"):
            self.previous_write("SELECT 1", added=())
        with self.assertRaisesRegex(ValueError, "séquence de colonnes"):
            self.previous_write("SELECT 1", added="sync_completed_at")
        # un commentaire qui nomme la colonne ne fait pas refuser l'écriture
        self.previous_write(
            "-- sans sync_completed_at, comme le code d'avant\nSELECT 1",
            added=("sync_completed_at",))

    def test_previous_insert_leaves_the_added_columns_unwritten(self):
        self.previous_insert(
            "canon_state",
            {"host": HOST, "canon": "", "status": "ok",
             "sync_completed_token": TOKEN, "sync_signature": SIGNATURE,
             "sync_claimed_at": datetime.now(timezone.utc)},
            added=("sync_completed_at", "sync_completed_claim_at"))
        row = self.db.query(
            "SELECT sync_completed_at, sync_completed_claim_at FROM canon_state "
            "WHERE host = %s AND canon = ''", (HOST,))[0]
        self.assertIsNone(row["sync_completed_at"])
        self.assertIsNone(row["sync_completed_claim_at"])

    def test_previous_update_leaves_the_added_columns_to_the_new_schema(self):
        self.claim()
        self.previous_update(
            "canon_state",
            {"sync_completed_token": TOKEN, "sync_signature": SIGNATURE},
            where={"host": HOST, "canon": ""},
            added=("sync_completed_at", "sync_completed_claim_at"))
        row = self.db.query(
            "SELECT sync_completed_at, sync_completed_claim_at, "
            "extract(epoch FROM clock_timestamp() - sync_completed_claim_at)::float8 "
            "AS age FROM canon_state WHERE host = %s AND canon = ''", (HOST,))[0]
        self.assertIsNone(row["sync_completed_at"])
        self.assertIsNotNone(row["sync_completed_claim_at"])
        self.assertLess(row["age"], 30.0)

    def test_a_sync_completed_by_the_old_writer_is_dated_and_reusable(self):
        self.claim()
        self.previous_write(PRE_0541_COMPLETE, (TOKEN, SIGNATURE, HOST, "", TOKEN),
                            added=("sync_completed_at", "sync_completed_claim_at"))
        shared = self.store.shared_sync(HOST, "")
        self.assertIsNotNone(shared)
        self.assertEqual(shared["sync_signature"], SIGNATURE)
        self.assertIsNotNone(shared["completed_age_s"])
        self.assertLess(shared["completed_age_s"], 30.0)
        self.assertTrue(canon_sync._reusable_completed(shared, SIGNATURE, MAX_AGE))

    def test_a_result_completed_long_ago_by_the_old_writer_stays_refused(self):
        self.claim()
        self.previous_write(PRE_0541_COMPLETE, (TOKEN, SIGNATURE, HOST, "", TOKEN),
                            added=("sync_completed_at", "sync_completed_claim_at"))
        # l'hôte s'est arrêté : la réclamation et la fin terminée vieillissent
        self.db.execute(
            "UPDATE canon_state SET sync_claimed_at = clock_timestamp() - "
            "make_interval(secs => %s), sync_completed_claim_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''",
            (float(MAX_AGE + 60), float(MAX_AGE + 60), HOST))
        shared = self.store.shared_sync(HOST, "")
        self.assertGreater(shared["completed_age_s"], MAX_AGE)
        self.assertFalse(canon_sync._reusable_completed(shared, SIGNATURE, MAX_AGE))


def _row(name, *, harness="deepseek"):
    """Un candidat du registre ; la sélection ne fait aucune E/S."""
    return {"name": name, "team": "ameesh", "harness": harness,
            "session_harness": harness, "status": "idle", "mode": "execute",
            "status_text": "",
            "capabilities": ["read", "report-drift", "propose"],
            "roles": ["implementation"]}


class _Port:
    """Le raccord de désignation simulé : une seule écriture, aucun envoi."""

    def __init__(self):
        self.messages = []

    def latest_freeze(self, lot):
        return {"id": 1, "sha": SHA}

    def recorded(self, lot, freeze_id):
        return None

    def commit(self, lot, *, freeze, required, state, reviewers, missing, why,
               authors, policy_ref="", notify="", messages=(), unknown_authors=()):
        self.messages = [dict(message) for message in messages]
        return {"state": state, "required": required,
                "reviewers": [dict(reviewer) for reviewer in reviewers],
                "missing": missing, "why": why, "freeze": dict(freeze),
                "authors": [dict(author) for author in authors],
                "unknown_authors": [str(name) for name in unknown_authors],
                "policy_ref": policy_ref, "notify": notify,
                "messages": [dict(message) for message in messages]}


class ReviewQuestionTest(unittest.TestCase):
    """#5690 : un gel qui porte du SQL pose la question N-1 au relecteur."""

    def designate(self, paths):
        port = _Port()
        out = rd.apply(
            LOT, policy={"rules": [{"paths": ["**/*.sql"], "reviews": 2}]},
            paths=paths, port=port, freeze_sha=SHA, authors=[],
            candidates=rd.candidates_from_registry(
                [_row("claude-one", harness="claude"),
                 _row("codex-one", harness="codex")],
                families=FAMILIES),
            families=FAMILIES, required=1, risk="sensitive")
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(len(port.messages), 1)
        return port.messages[0]["body"]

    def test_the_question_is_asked_when_sql_is_frozen(self):
        body = self.designate(["src/ameesh/migrations/0543_n1.sql"])
        self.assertIn(rd.N1_QUESTION, body)

    def test_the_question_stays_out_without_sql(self):
        body = self.designate(["src/ameesh/db.py"])
        self.assertNotIn("N-1 compatibility", body)

    def test_sql_migration_recognises_sql_paths(self):
        self.assertTrue(rd.sql_migration(["supabase/migrations/0001_x.sql"]))
        self.assertFalse(rd.sql_migration(["src/ameesh/db.py", "README.md"]))
        self.assertFalse(rd.sql_migration([]))
