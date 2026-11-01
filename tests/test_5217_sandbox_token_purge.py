# SPDX-License-Identifier: AGPL-3.0-only
"""#5217 — rétention et purge des empreintes de jetons de tour sandbox.

Un tour clos ne peut plus vérifier son jeton : l'empreinte n'est plus qu'une
preuve d'émission, gardée `sandbox_token_retention_days` jours (30 par défaut)
puis purgée par le ménage, par passes bornées. Le tour, son résultat et ses
coûts restent.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest import mock

from ameesh import sandbox_relay
from ameesh.runner import Runner

from .support import PgTestCase


def _digest(turn_id: str) -> str:
    return hashlib.sha256(("token:" + turn_id).encode()).hexdigest()


class _Turn:
    """Seed enough of `sandbox_turns` for the purge's age predicate."""

    def seed(self, turn_id, state, *, completed_days=None, deadline_days=1):
        self.db.execute(
            "insert into sandbox_turns (turn_id, spend_key, agent, owner, lease_epoch,"
            " scope, scope_digest, retry_family, scope_generation, attempt, max_attempts,"
            " profile_ref, profile_digest, placement_ref, placement_digest, canon_ref,"
            " limits, image_digest, provider, namespace, job_name, state, deadline_ts,"
            " completed_at, source)"
            " values (%s,%s,'agent','owner',1,'{}'::jsonb,%s,%s,1,1,1,'p','pd','pl','pld',"
            " 'canon','{}'::jsonb,'img','kubernetes','ns',%s,%s,"
            " clock_timestamp() - make_interval(days => %s),"
            " case when %s::int is null then null"
            "      else clock_timestamp() - make_interval(days => %s::int) end,"
            " '{}'::jsonb)",
            (turn_id, "sk-" + turn_id, "sd-" + turn_id, "rf-" + turn_id,
             "job-" + turn_id, state, int(deadline_days),
             completed_days, completed_days))
        self.db.execute(
            "insert into sandbox_relay_tokens (token_sha256, turn_id, host)"
            " values (%s,%s,'host')", (_digest(turn_id), turn_id))

    def turns(self):
        return {row["turn_id"] for row in
                self.db.query("select turn_id from sandbox_relay_tokens")}


class PurgeTest(_Turn, PgTestCase):

    def setUp(self):
        super().setUp()
        self.db.execute("delete from sandbox_relay_tokens")
        self.db.execute("delete from sandbox_turns")
        self.seed("open", "running", completed_days=None, deadline_days=-1)
        self.seed("recent", "succeeded", completed_days=1)
        self.seed("old", "succeeded", completed_days=40)
        self.seed("cancelled", "cancelled", completed_days=60)
        self.seed("expired", "expired", completed_days=None, deadline_days=40)

    def test_only_closed_turns_past_retention_are_purged(self):
        self.assertEqual(sandbox_relay.purge(self.db, days=30), 3)
        self.assertEqual(self.turns(), {"open", "recent"})

    def test_expired_turn_without_completion_uses_its_deadline(self):
        self.seed("expired-recent", "expired", completed_days=None, deadline_days=1)
        self.assertEqual(sandbox_relay.purge(self.db, days=30), 3)
        self.assertNotIn("expired", self.turns())
        self.assertIn("expired-recent", self.turns())

    def test_bounded_batches_and_empty_pass(self):
        self.assertEqual(sandbox_relay.purge(self.db, days=30, limit=1), 1)
        self.assertEqual(sandbox_relay.purge(self.db, days=30, limit=1), 1)
        self.assertEqual(sandbox_relay.purge(self.db, days=30, limit=1), 1)
        self.assertEqual(sandbox_relay.purge(self.db, days=30, limit=1), 0)

    def test_retention_is_positive_and_the_limit_bounded(self):
        for bad in (0, -1, True, 1.5):
            with self.subTest(days=bad), self.assertRaises(ValueError):
                sandbox_relay.purge(self.db, days=bad)
        for bad in (0, -3, True):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                sandbox_relay.purge(self.db, days=30, limit=bad)

    def test_the_turn_and_its_costs_survive_the_purge(self):
        self.db.execute(
            "insert into turn_costs (agent, harness, model, usd, input_tokens,"
            " output_tokens, source, lease_owner, lease_epoch, turn_id)"
            " values ('agent','deepseek','model',0.5,1,1,'relay','owner',1,'old')")
        self.assertEqual(sandbox_relay.purge(self.db, days=30), 3)
        self.assertEqual(
            self.db.query("select count(*) as n from sandbox_turns")[0]["n"], 5)
        self.assertEqual(
            self.db.query("select count(*) as n from turn_costs")[0]["n"], 1)

    def test_retention_default_and_environment(self):
        from ameesh import config as config_mod
        self.assertEqual(config_mod.Config().sandbox_token_retention_days, 30)
        loaded = config_mod.load(env={"AMEESH_SANDBOX_TOKEN_RETENTION_DAYS": "7",
                                      "AMEESH_STATE": self.state})
        self.assertEqual(loaded.sandbox_token_retention_days, 7)
        # invalide ou négatif : le défaut, jamais une purge surprise
        loaded = config_mod.load(env={"AMEESH_SANDBOX_TOKEN_RETENTION_DAYS": "-3",
                                      "AMEESH_STATE": self.state})
        self.assertEqual(loaded.sandbox_token_retention_days, 30)


class RunnerPassTest(_Turn, PgTestCase):

    def setUp(self):
        super().setUp()
        self.db.execute("delete from sandbox_relay_tokens")
        self.db.execute("delete from sandbox_turns")
        self.seed("old", "succeeded", completed_days=40)

    def runner(self):
        owner = object.__new__(Runner)
        owner.db = self.db
        owner.dry_run = False
        owner.mediated = False
        owner.cfg = SimpleNamespace(sandbox_token_retention_days=30)
        owner._sandbox_token_purge_next = None
        owner._sandbox_token_purge_error = None
        return owner

    def test_housekeeping_purges_once_a_day(self):
        owner = self.runner()
        self.assertEqual(owner.purge_sandbox_tokens_once(self.db, clock=100.0), 1)
        self.assertEqual(self.turns(), set())
        with mock.patch.object(sandbox_relay, "purge") as purge:
            self.assertIsNone(owner.purge_sandbox_tokens_once(self.db, clock=101.0))
        purge.assert_not_called()

    def test_zero_retention_keeps_the_digests(self):
        owner = self.runner()
        owner.cfg.sandbox_token_retention_days = 0
        self.assertIsNone(owner.purge_sandbox_tokens_once(self.db, clock=100.0))
        self.assertEqual(self.turns(), {"old"})
