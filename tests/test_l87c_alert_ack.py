# SPDX-License-Identifier: AGPL-3.0-only
"""L87c: acknowledgement through HTTP/CLI, retained audit, notify lifecycle."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import alert_ack, exploitation, mail, notify, registry, serve, storage

from .support import PgTestCase
from .test_l87_serve_actions import PostMixin


def alert(**extra):
    return dict({"schema": "ameesh-alert/1", "type": "host_disk_low", "host": "h1",
                 "since": 1234, "critical": False, "reason": "seuil",
                 "responsible": "human:owner", "detail": "low disk"}, **extra)


class IdentityTest(unittest.TestCase):
    def test_key_keeps_episode_and_subject_but_ignores_detail_and_severity(self):
        a = alert()
        self.assertRegex(alert_ack.key_of(a), r"^[0-9a-f]{64}$")
        self.assertEqual(alert_ack.key_of(a), alert_ack.key_of(alert(
            detail="changed", value=100, critical=True, reason="critique")))
        self.assertNotEqual(alert_ack.severity(a), alert_ack.severity(alert(critical=True)))
        for change in ({"host": "h2"}, {"since": 5678}):
            self.assertNotEqual(alert_ack.key_of(a), alert_ack.key_of(alert(**change)))
        a = dict(type="turn_unpriced", agent="a", since=1, model="one")
        self.assertNotEqual(alert_ack.key_of(a), alert_ack.key_of(dict(a, model="two")))

    def test_orphan_routing_reads_the_lot_once_per_pass(self):
        reads = []
        owner = {"human": "human:first"}

        def responsible(router, lot):
            if lot is not None:
                reads.append(lot)
                return owner["human"]
            return None

        cfg = SimpleNamespace(canon=None, github=None)
        n = notify.Notifier(cfg, notify.parse_config({"types": ["orphan_lot"]}),
                            sender=notify.DrySender(), log=lambda _: None)
        a = dict(type="orphan_lot", lot=1, agent="stopped", reason="stopped")
        with mock.patch.object(notify.Router, "lot_responsible", autospec=True,
                               side_effect=responsible):
            n.run_pass(None, [a])
            self.assertEqual(reads, [1])
            self.assertEqual(next(iter(n.state["active"].values()))["human"], "human:first")
            owner["human"] = "human:second"
            n.run_pass(None, [a])
        self.assertEqual(reads, [1, 1])
        self.assertEqual(next(iter(n.state["active"].values()))["human"], "human:second")


class AcknowledgementTest(PostMixin, PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("truncate alert_acknowledgements, api_idempotency restart identity")
        self.cfg = dataclasses.replace(self.cfg, notify={
            "default_human": "human:owner", "default": ["desktop"]})
        self.a = alert()
        self.snapshot = mock.patch.object(exploitation, "alerts", return_value=[self.a])
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)
        self.sent = []
        self.now = 10000.0

    def ack(self, a=None, **kwargs):
        return alert_ack.acknowledge(self.cfg, self.db, alert_ack.key_of(a or self.a),
                                     actor=kwargs.pop("actor", "human:owner"), **kwargs)

    def notifier(self, state=None, **kwargs):
        return notify.Notifier(self.cfg, notify.parse_config(self.cfg.notify), state,
            sender=type("Sender", (), {"send": lambda _, c, m: self.sent.append(m)})(),
            clock=lambda: self.now, log=lambda _: None, **kwargs)

    def history(self):
        return self.db.query("select id, actor, principal, invalidation_reason"
                              " from alert_acknowledgements order by id")

    def test_idempotent_and_durable_with_actor_and_principal(self):
        first = self.ack(actor="human:deputy", principal="human:owner")
        self.assertTrue(first["changed"])
        second = self.ack()
        self.assertFalse(second["changed"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["actor"], "human:deputy")
        other = self.connect()
        try:
            self.assertEqual(storage.of(other).alert_acknowledgements.live()[0]["id"],
                             first["id"])
        finally:
            other.close()
        self.assertEqual(self.history()[0]["principal"], "human:owner")

    def test_only_human_recipient_and_current_key_are_allowed(self):
        for kwargs, code in (({"actor": "agent:a"}, "bad_actor"),
                             ({"actor": "human:other"}, "forbidden")):
            with self.assertRaises(alert_ack.AckRefused) as exc:
                self.ack(**kwargs)
            self.assertEqual(exc.exception.code, code)
        for key, code in (("bad", "bad_key"), ("f" * 64, "not_found")):
            with self.assertRaises(alert_ack.AckRefused) as exc:
                alert_ack.acknowledge(self.cfg, self.db, key, actor="human:owner")
            self.assertEqual(exc.exception.code, code)
        with mock.patch.object(storage, "is_remote", return_value=True):
            with self.assertRaises(alert_ack.AckRefused) as exc:
                self.ack()
            self.assertEqual(exc.exception.code, "native_only")
        self.assertEqual(self.history(), [])

    def test_local_pool_ack_is_explicitly_refused_before_ordinary_pass(self):
        for kind in notify.LOCAL_TYPES:
            with self.subTest(kind=kind):
                self.a = alert(type=kind)
                exploitation.alerts.return_value = [self.a]
                with self.assertRaises(alert_ack.AckRefused) as exc:
                    self.ack()
                self.assertEqual(exc.exception.code, "local_alert")
                exploitation.alerts.return_value = []
                self.notifier().run_pass(self.db)
                self.assertIs(exploitation.alerts.call_args.kwargs["local_pool"], False)
                self.assertEqual(self.history(), [])
                exploitation.alerts.return_value = [self.a]

    def test_http_refuses_local_pool_ack_with_explicit_code(self):
        self.start(serve.Context(cfg=self.cfg, human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))
        for kind in notify.LOCAL_TYPES:
            with self.subTest(kind=kind):
                self.a = alert(type=kind)
                exploitation.alerts.return_value = [self.a]
                key = alert_ack.key_of(self.a)
                status, _h, body = self.post("/api/v1/alerts/%s/ack" % key,
                                            key="local-" + kind)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"], "local_alert", body)
        self.assertEqual(self.history(), [])

    def test_concurrent_acknowledgements_record_one_actor(self):
        barrier = threading.Barrier(2)
        key = alert_ack.key_of(self.a)
        def perform(actor):
            db = self.connect()
            try:
                barrier.wait(timeout=5)
                return storage.of(db).alert_acknowledgements.acknowledge(
                    key, alert_ack.severity(self.a), actor, "human:owner")
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(perform, ("human:one", "human:two")))
        self.assertEqual(sum(r["changed"] for r in results), 1)
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(len(self.history()), 1)

    def test_old_snapshot_does_not_invalidate_new_ack(self):
        domain = storage.of(self.db).alert_acknowledgements
        observed = domain.clock()
        self.ack()
        alert_ack.reconcile(self.db, [], observed_ts=observed)
        self.assertEqual(len(domain.live()), 1)

    def test_old_snapshot_does_not_invalidate_a_confirmed_ack(self):
        self.ack()
        domain = storage.of(self.db).alert_acknowledgements
        observed = domain.clock()
        self.assertFalse(self.ack()["changed"])
        alert_ack.reconcile(self.db, [], observed_ts=observed)
        self.assertEqual(len(domain.live()), 1)

    def test_http_post_replay_and_read_only_list_keep_the_alert(self):
        self.start(serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))
        key = alert_ack.key_of(self.a)
        status, _h, before = self.get("/api/v1/alerts")
        self.assertEqual(status, 200, before)
        before = json.loads(before)
        self.assertFalse(before["alerts"][0]["acknowledged"])
        path = "/api/v1/alerts/%s/ack" % key
        status, _h, out = self.post(path)
        self.assertEqual(status, 200, out)
        self.assertEqual((out["by"], out["for"], out["action"]),
                         ("human:owner", "human:owner", "ack_alert"))
        status, headers, replay = self.post(path)
        self.assertEqual((status, replay, headers.get("Idempotent-Replayed")),
                         (200, out, "true"))
        status, _h, after = self.get("/api/v1/alerts")
        after = json.loads(after)
        self.assertEqual((status, len(after["alerts"])), (200, 1), after)
        self.assertEqual(after["alerts"][0]["key"], key)
        self.assertTrue(after["alerts"][0]["acknowledged"])
        status, _h, out = self.post(path, {"actor": "human:other"}, key="key-0000000022")
        self.assertEqual((status, out["error"]), (400, "bad_body"))
        self.assertEqual(len(self.history()), 1)

    def test_http_refuses_other_human_and_bad_or_absent_key(self):
        self.start(serve.Context(cfg=self.cfg, human="human:other"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))
        for key, status, code in ((alert_ack.key_of(self.a), 403, "forbidden"),
                                   ("bad", 400, "bad_key"),
                                   ("f" * 64, 404, "not_found")):
            actual, _h, body = self.post("/api/v1/alerts/%s/ack" % key,
                                        key="request-" + key)
            self.assertEqual((actual, body["error"]), (status, code), body)
        self.assertEqual(self.history(), [])

    def test_notify_stays_silent_across_restart_and_resolution_then_rearms(self):
        self.ack()
        n = self.notifier()
        n.run_pass(self.db, [self.a])
        n = self.notifier(json.loads(json.dumps(n.state)))
        n.run_pass(self.db, [self.a])
        self.assertEqual(self.sent, [])
        n.run_pass(self.db, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.history()[0]["invalidation_reason"], "resolved")
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 1)

    def test_lost_local_state_does_not_lose_ack_or_resolution(self):
        self.ack()
        n = self.notifier()  # no file state at all
        n.run_pass(self.db, [])
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 1)

    def test_other_host_cannot_resolve_a_local_sensor_ack(self):
        self.a["observation_host"] = self.cfg.host
        self.ack()
        other_cfg = dataclasses.replace(self.cfg, host="another-observer")
        other = notify.Notifier(other_cfg, notify.parse_config(self.cfg.notify),
                                sender=notify.DrySender(), log=lambda _: None)
        other.run_pass(self.db, [])
        self.assertEqual(self.history()[0]["invalidation_reason"], None)
        self.notifier().run_pass(self.db, [])
        self.assertEqual(self.history()[0]["invalidation_reason"], "resolved")

    def test_severity_change_unmutes_and_is_audited(self):
        self.ack()
        n = self.notifier()
        n.run_pass(self.db, [self.a])
        self.a.update(critical=True, reason="critique")
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.history()[0]["invalidation_reason"], "severity_changed")
        self.assertTrue(self.ack()["changed"])
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 1)
        self.a.update(critical=False, reason="seuil")
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.history()), 2)

    def test_recipient_change_is_not_silenced(self):
        self.ack()
        n = self.notifier()
        n.run_pass(self.db, [self.a])
        self.a["responsible"] = "human:other"
        n.run_pass(self.db, [self.a])
        self.assertEqual(len(self.sent), 1)

    def test_ack_stops_failed_channel_retry_and_resolution(self):
        n = self.notifier()
        def fail(*args):
            raise notify.ChannelError("unavailable")
        n.sender.send = fail
        n.run_pass(self.db, [self.a])
        self.ack()
        n.sender.send = lambda c, m: self.sent.append(m)
        n.run_pass(self.db, [self.a])
        n.run_pass(self.db, [])
        self.assertEqual(self.sent, [])

    def test_ack_removes_a_rate_limited_summary_waiting_to_retry(self):
        self.cfg = dataclasses.replace(self.cfg, notify=dict(self.cfg.notify, rate_per_minute=1))
        n = self.notifier()
        n.state["rate"]["human:owner"] = [self.now]
        n.state["last_summary"]["human:owner"] = self.now
        n.run_pass(self.db, [self.a])
        self.assertTrue(n.state["summary"])
        self.ack()
        self.now += 120
        n.run_pass(self.db, [self.a])
        self.assertEqual(n.state["summary"], {})
        self.assertEqual(self.sent, [])

    def test_another_pass_perimeter_does_not_declare_resolution(self):
        custom = {"stale_lot_s": 3600.0}
        self.ack(**custom)
        domain = storage.of(self.db).alert_acknowledgements
        self.assertEqual(json.loads(domain.live()[0]["perimeter"]), custom)
        seen = []

        def evaluate(thresholds):
            seen.append(thresholds)
            return [self.a]

        alert_ack.reconcile(self.db, [], observed_ts=domain.clock(),
                            perimeter=alert_ack.canonical(alert_ack.default_thresholds()),
                            evaluate=evaluate)
        self.assertIsNone(self.history()[0]["invalidation_reason"])
        self.assertEqual(seen, [custom])
        # The same pass, once in the ack's own perimeter: resolved.
        alert_ack.reconcile(self.db, [], observed_ts=domain.clock(),
                            perimeter=alert_ack.canonical(custom), evaluate=evaluate)
        self.assertEqual(self.history()[0]["invalidation_reason"], "resolved")
        self.assertEqual(seen, [custom])          # one re-observation per perimeter

    def test_a_severity_change_in_the_ack_perimeter_rearms_it(self):
        custom = {"stale_lot_s": 3600.0}
        self.ack(**custom)
        changed = dict(self.a, critical=True, reason="critique")
        alert_ack.reconcile(
            self.db, [], observed_ts=storage.of(self.db).alert_acknowledgements.clock(),
            perimeter=alert_ack.canonical(alert_ack.default_thresholds()),
            evaluate=lambda thresholds: [changed])
        self.assertEqual(self.history()[0]["invalidation_reason"], "severity_changed")

    def test_legacy_unknown_perimeter_is_judged_by_the_pass(self):
        self.ack()
        self.db.execute("update alert_acknowledgements set perimeter = null")
        alert_ack.reconcile(self.db, [], observed_ts=storage.of(self.db)
                            .alert_acknowledgements.clock(),
                            perimeter=alert_ack.canonical(alert_ack.default_thresholds()),
                            evaluate=lambda thresholds: [self.a])
        self.assertEqual(self.history()[0]["invalidation_reason"], "resolved")

    def test_a_perimeter_naming_an_unknown_threshold_is_reobserved_without_it(self):
        self.ack(stale_lot_s=3600.0)
        self.db.execute("update alert_acknowledgements"
                        " set perimeter = '{\"gone_s\":1,\"stale_lot_s\":3600.0}'")
        seen = []
        alert_ack.reconcile(self.db, [], observed_ts=storage.of(self.db)
                            .alert_acknowledgements.clock(),
                            perimeter=alert_ack.canonical(alert_ack.default_thresholds()),
                            evaluate=lambda thresholds: seen.append(thresholds) or [self.a])
        self.assertEqual(seen, [{"stale_lot_s": 3600.0}])
        self.assertIsNone(self.history()[0]["invalidation_reason"])

    def test_notify_does_not_resolve_an_ack_from_another_perimeter(self):
        self.ack(stale_lot_s=3600.0)
        n = self.notifier()
        n.run_pass(self.db, [])                   # defaults, the ack's own: kept
        self.assertIsNone(self.history()[0]["invalidation_reason"])
        n.run_pass(self.db, [], thresholds={"stale_lot_s": 3600.0})
        self.assertEqual(self.history()[0]["invalidation_reason"], "resolved")

    def test_notify_without_thresholds_declares_the_perimeter_it_observed(self):
        observed = []
        exploitation.alerts.side_effect = (
            lambda _cfg, _db, **kw: observed.append(kw) or [self.a])
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_LONG_TURN": "999"}), \
                mock.patch.object(alert_ack, "reconcile",
                                  wraps=alert_ack.reconcile) as reconcile:
            self.notifier().run_pass(self.db)
        self.assertEqual(len(observed), 1)
        # #3761 : `gate_cache` est un cache d'observation (verdicts de porte
        # déjà lus), pas un seuil ; le périmètre réconcilié ne le porte pas.
        # #4579 : `record_footprint` non plus (écriture des notes d'épisode).
        thresholds = {k: v for k, v in observed[0].items()
                  if k not in ("now", "local_pool", "unreadable", "gate_cache",
                               "record_footprint")}
        # #2105 : the pass observes with the perimeter it declares to
        # reconcile — without thresholds, the parser's defaults, environment
        # included.
        self.assertEqual(reconcile.call_args.kwargs["perimeter"],
                         alert_ack.canonical(thresholds))
        self.assertEqual(thresholds.get("long_turn_s"), 999.0)

    def test_ack_between_last_raise_and_resolution_silences_resolution(self):
        n = self.notifier()
        n.run_pass(self.db, [self.a])
        self.ack()
        n.run_pass(self.db, [])
        self.assertEqual(len(self.sent), 1)

    def test_group_only_mentions_unacknowledged_lots(self):
        self.a = dict(type="orphan_lot", agent="a", reason="stopped", lot=1,
                      responsible="human:owner")
        other = dict(self.a, lot=2)
        exploitation.alerts.return_value = [self.a, other]
        self.ack()
        n = self.notifier()
        n.run_pass(self.db, [self.a, other])
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("#1", self.sent[0].body)
        self.assertIn("#2", self.sent[0].body)
        self.ack(other)
        n.run_pass(self.db, [self.a, other])
        n.run_pass(self.db, [])
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_does_not_invalidate_ack(self):
        self.ack()
        self.notifier(dry_run=True).run_pass(self.db, [])
        self.assertEqual(self.history()[0]["invalidation_reason"], None)

    def test_cli_current_alert_ack_replay_and_agent_refusal(self):
        self.snapshot.stop()  # subprocesses see real persisted alerts
        registry.upsert(self.db, "worker", chantier="demo")
        self.db.execute("update agent_registry set responsible = 'human:owner'"
                        " where name = 'worker'")
        registry.set_status(self.db, "worker", "stopped", stop_reason="bail_expire")
        mail.send(self.db, "orch", "worker", "pending")
        self.db.execute("update agent_mailbox set created_at = now() - interval '1 hour'")
        listing = self.mesh("alerts", "--json")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        a = next(a for a in map(json.loads, listing.stdout.splitlines())
                 if a["type"] == "stopped_with_mail")
        args = ("alerts", "ack", a["key"], "--human", "human:owner", "--json")
        first = self.mesh(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)["changed"])
        second = self.mesh(*args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(json.loads(second.stdout)["changed"])
        listed = self.mesh("alerts", "--json")
        self.assertTrue(next(a for a in map(json.loads, listed.stdout.splitlines())
                             if a["type"] == "stopped_with_mail")["acknowledged"])
        refused = self.mesh(*args, env=self.env(AGENT_MAIL_NAME="worker"))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("agent session", refused.stderr)


if __name__ == "__main__":
    unittest.main()
