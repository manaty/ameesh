# SPDX-License-Identifier: AGPL-3.0-only
"""#3961 (issue #3014) : une pause opérateur a une échéance et se voit.

Le 27/10, une maintenance du maillage TPK IMA a posé `pause --operator` sur
62/65 agents ; la reprise conditionnelle n'a pas eu lieu et les agents sont
restés gelés 70 min, sans échéance ni alerte. Ici : la pose porte une
échéance (4 h par défaut, `--indefinite` explicite), l'exécuteur la lève à
l'échéance avec un événement d'audit, `ameesh alerts` signale une pause qui
dure et un hôte pausé, et le demandeur lit l'échéance dans le refus.
"""
from __future__ import annotations

import dataclasses
import json
import time
import types
import unittest

from ameesh import agent_pause, alert_briefs, exploitation, notify, registry, serve
from ameesh.runner import Runner

from .support import PgTestCase
from .test_l87_serve_actions import PostMixin


class EcheanceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("worker", "other", "third"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)

    def pause(self, name="worker", **kw):
        args = dict(paused=True, reason="maintenance PR #1078", actor="human:alice")
        args.update(kw)
        return agent_pause.change(self.db, name, **args)

    def events(self):
        return self.db.query(
            "SELECT agent, paused, reason, actor, principal,"
            "       extract(epoch from until)::float8 AS until_ts"
            "  FROM operator_pause_events ORDER BY id")

    def age(self, name, minutes, *, until_in=None):
        """Recule le début de la pause (et, si donné, met l'échéance à
        `until_in` secondes de maintenant)."""
        self.db.execute(
            "UPDATE agent_registry SET operator_pause_at = clock_timestamp()"
            " - make_interval(mins => %s) WHERE name = %s", (int(minutes), name))
        if until_in is not None:
            self.db.execute(
                "UPDATE agent_registry SET operator_pause_until = clock_timestamp()"
                " + make_interval(secs => %s) WHERE name = %s", (float(until_in), name))

    # -- pose -------------------------------------------------------------

    def test_pause_with_a_deadline_records_it_in_the_row_and_the_event(self):
        until = time.time() + 1800
        out = self.pause(until=until)
        self.assertTrue(out["changed"])
        self.assertFalse(out["capped"])
        self.assertAlmostEqual(out["until_ts"], until, delta=1)
        row = registry.get(self.db, "worker")
        self.assertAlmostEqual(row["operator_pause_until_ts"], until, delta=1)
        self.assertAlmostEqual(self.events()[0]["until_ts"], until, delta=1)

    def test_pause_without_deadline_is_capped_to_the_default(self):
        before = time.time()
        out = self.pause()
        self.assertTrue(out["capped"])
        self.assertGreaterEqual(out["until_ts"], before + agent_pause.DEFAULT_MAX_S - 1)
        self.assertLessEqual(out["until_ts"], time.time() + agent_pause.DEFAULT_MAX_S + 1)

    def test_indefinite_pause_has_no_deadline(self):
        out = self.pause(indefinite=True)
        self.assertTrue(out["changed"])
        self.assertFalse(out["capped"])
        self.assertIsNone(out["until_ts"])
        self.assertIsNone(registry.get(self.db, "worker")["operator_pause_until_ts"])

    def test_a_past_deadline_is_refused_without_event(self):
        out = self.pause(until=time.time() - 5)
        self.assertEqual((out["changed"], out["refusal"]), (False, "until_past"))
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertEqual(self.events(), [])

    def test_bad_deadline_requests_are_refused(self):
        for kw in ({"until": -1}, {"until": float("inf")}, {"until": True},
                   {"until": time.time() + 60, "indefinite": True}):
            with self.subTest(kw=kw):
                with self.assertRaises(agent_pause.PauseRefused):
                    self.pause(**kw)
        with self.assertRaises(agent_pause.PauseRefused):
            agent_pause.change(self.db, "worker", paused=False, reason="done",
                               actor="human:alice", until=time.time() + 60)
        self.assertEqual(self.events(), [])

    def test_any_lift_clears_the_deadline(self):
        self.pause(until=time.time() + 600)
        agent_pause.change(self.db, "worker", paused=False, reason="done",
                           actor="human:alice")
        row = self.db.query("SELECT operator_pause_until FROM agent_registry"
                            " WHERE name = 'worker'")[0]
        self.assertIsNone(row["operator_pause_until"])

    # -- échéance ---------------------------------------------------------

    def test_expire_lifts_only_due_pauses_once_with_an_audited_event(self):
        self.pause(until=time.time() + 600, principal="human:bob")
        self.pause("other", until=time.time() + 600)
        self.pause("third", indefinite=True)
        self.age("worker", 70, until_in=-1)
        done = agent_pause.expire(self.db)
        self.assertEqual([r["agent"] for r in done], ["worker"])
        self.assertEqual(done[0]["reason"], "maintenance PR #1078")
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertTrue(registry.get(self.db, "other")["operator_paused"])
        self.assertTrue(registry.get(self.db, "third")["operator_paused"])
        lifted = self.events()[-1]
        self.assertEqual((lifted["agent"], lifted["paused"], lifted["actor"],
                          lifted["principal"]),
                         ("worker", False, "human:alice", "human:bob"))
        self.assertTrue(lifted["reason"].startswith("deadline reached ("))
        self.assertIn("maintenance PR #1078", lifted["reason"])
        self.assertEqual(agent_pause.expire(self.db), [])     # une seule fois

    def test_runner_pass_lifts_the_due_pause_and_notes_it(self):
        self.pause(until=time.time() + 600)
        self.age("worker", 70, until_in=-1)
        fake = types.SimpleNamespace(db=self.db, cfg=self.cfg, dry_run=False,
                                     mediated=False)
        done = Runner.expire_operator_pauses_once(fake)
        self.assertEqual([r["agent"] for r in done], ["worker"])
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertIsNone(fake._operator_pause_error)
        # dry-run et exécuteur médié : rien n'est levé
        self.pause("other", until=time.time() + 600)
        self.age("other", 70, until_in=-1)
        for kw in ({"dry_run": True, "mediated": False},
                   {"dry_run": False, "mediated": True}):
            fake = types.SimpleNamespace(db=self.db, cfg=self.cfg, **kw)
            self.assertEqual(Runner.expire_operator_pauses_once(fake), [])
        self.assertTrue(registry.get(self.db, "other")["operator_paused"])

    def test_a_lifted_pause_lets_the_next_turn_start(self):
        epoch = registry.claim(self.db, "worker", "runner-test", 3600)["lease_epoch"]
        self.pause(until=time.time() + 600)
        self.assertFalse(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))
        self.age("worker", 70, until_in=-1)
        agent_pause.expire(self.db, "worker")
        self.assertTrue(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))

    # -- visibilité -------------------------------------------------------

    def test_requester_reads_the_deadline_in_the_refusal(self):
        until = time.time() + 600
        self.pause(until=until)
        ok, why, _note = registry.wake_status(self.db, self.cfg, "worker")
        self.assertFalse(ok)
        self.assertIn("under operator pause", why)
        self.assertIn("until %s" % agent_pause.until_label(until), why)
        self.pause("other", indefinite=True)
        _ok, why, _note = registry.wake_status(self.db, self.cfg, "other")
        self.assertIn("(no deadline)", why)

    def _alertes(self, kind, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw) if a["type"] == kind]

    def test_long_operator_pause_is_alerted_after_thirty_minutes(self):
        self.pause(indefinite=True)
        self.age("worker", 5)
        self.assertEqual(self._alertes("operator_pause_long"), [])
        self.age("worker", 31)
        alertes = self._alertes("operator_pause_long")
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual(alerte["agent"], "worker")
        self.assertGreaterEqual(alerte["value"], 31 * 60)
        self.assertEqual(alerte["threshold"], exploitation.DEFAULT_LONG_OPERATOR_PAUSE_S)
        self.assertIn("no deadline", alerte["detail"])
        self.assertIn("human:alice", alerte["detail"])
        self.assertIn("ameesh resume worker --operator", alerte["detail"])
        self.assertIn("orchestrators", alerte)
        self.assertEqual(self._alertes("operator_pause_long", long_operator_pause_s=0), [])

    def test_a_host_mostly_paused_raises_one_host_alert(self):
        self.pause(indefinite=True)
        self.age("worker", 20)
        # un seul agent tenu sur trois : pas d'alerte d'hôte
        self.assertEqual(self._alertes("operator_pause_host"), [])
        self.pause("other", until=time.time() + 3600)
        self.age("other", 20)
        alertes = self._alertes("operator_pause_host")
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual((alerte["host"], alerte["value"], alerte["agents"]),
                         (self.cfg.host, 2, ["other", "worker"]))
        self.assertIn("operator_pause_host", notify.DEFAULT_TYPES)

    def test_briefs_go_to_orchestrators_not_to_the_paused_agents(self):
        alerte = {"type": "operator_pause_host", "agent": None, "host": "ima",
                  "agents": ["ouvrier"], "orchestrators": ["orch"],
                  "detail": "2/3 agents of host ima under operator pause"}
        listing = [{"name": "ouvrier", "team": "ameesh"},
                   {"name": "orch", "team": "ameesh"},
                   {"name": "auditeur", "team": "ameesh"}]
        out = alert_briefs.briefs(alerte, listing, auditor_names=["auditeur"],
                                  orchestrator_names=("orch",))
        self.assertIn("orch", out)
        self.assertIn("auditeur", out)
        self.assertNotIn("ouvrier", out)
        self.assertIn("ameesh resume <agent> --operator", out["orch"])

    # -- CLI ---------------------------------------------------------------

    def test_cli_for_until_and_indefinite(self):
        out = self.mesh("pause", "worker", "--human", "human:alice", "--reason", "maint",
                        "--for", "30m", "--json")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        data = json.loads(out.stdout)
        self.assertAlmostEqual(data["until_ts"], time.time() + 1800, delta=30)
        out = self.mesh("pause", "other", "--human", "human:alice", "--reason", "maint",
                        "--indefinite")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("with no deadline", out.stdout)
        out = self.mesh("pause", "third", "--human", "human:alice", "--reason", "maint")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("the pause ends at", out.stderr)
        self.assertIn("operator pause set until", out.stdout)

    def test_cli_refuses_a_past_until_and_exclusive_options(self):
        out = self.mesh("pause", "worker", "--human", "human:alice", "--reason", "maint",
                        "--until", "2020-01-01T00:00:00Z")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("refused (until_past)", out.stderr)
        out = self.mesh("pause", "worker", "--human", "human:alice", "--reason", "maint",
                        "--for", "1h", "--indefinite")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])

    def test_parse_until(self):
        self.assertEqual(agent_pause.parse_until("1800000000"), 1800000000.0)
        self.assertEqual(agent_pause.parse_until("2027-01-15T10:00:00Z"),
                         agent_pause.parse_until("2027-01-15T10:00:00"))
        for bad in ("demain", "-5", "nan"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent_pause.parse_until(bad)


class EcheanceApiTest(PostMixin, PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency")
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host)
        self.start(serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))

    def test_api_pause_takes_until_and_refuses_a_bad_one(self):
        path = "/api/v1/agents/worker/pause"
        status, _h, out = self.post(path, {"reason": "maint", "until": "tomorrow"},
                                    key="bad-until")
        self.assertEqual(status, 400, out)
        until = time.time() + 900
        status, _h, out = self.post(path, {"reason": "maint", "until": until},
                                    key="good-until")
        self.assertEqual(status, 200, out)
        self.assertAlmostEqual(out["result"]["until_ts"], until, delta=1)
        self.assertAlmostEqual(registry.get(self.db, "worker")["operator_pause_until_ts"],
                               until, delta=1)


if __name__ == "__main__":
    unittest.main()
