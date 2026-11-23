# SPDX-License-Identifier: AGPL-3.0-only
"""#8138 : la pulsation fraîche d'un candidat prime sur la passe périmée.

Une passe lit `canon_ref` à son début ; l'étape canon-executant de la
séquence avance ensuite l'épingle (#8109). Le seul enregistrement de la
passe porte donc l'ancien commit, et `staging_canon_stale` restait levée
jusqu'à la passe suivante (environ 12 h). Pour une même (clé, hôte), le
relevé le plus récent entre l'enregistrement de passe et une pulsation
fraîche portant `canon_ref` fait foi ; une pulsation ancienne ne masque
pas une passe plus récente. Un refus (#7053) sur la même copie survit à
une pulsation plus récente qui n'en porte pas.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import staging_watch

OLD = "a" * 40
NEW = "b" * 40
NOW = 1_800_000_000.0
DAY = 86400.0


def pass_row(*, ident, host="ameesh-agents-1", ref=OLD, **extra):
    return dict(id=ident, ts=NOW - 600, data=dict(
        state="completed", host=host, canon_ref=ref,
        notify="mesh-design", **extra))


def tick(*, ident, host="ameesh-agents-1", ref=NEW, when=NOW, **extra):
    return {("mesh", host): dict(id=ident, ts=when, data=dict(
        key="mesh", role="mesh-design", host=host,
        candidates=[host], canon_ref=ref, **extra))}


class SelectedReadingTest(unittest.TestCase):
    """Le relevé retenu pour une même (clé, hôte)."""

    def selected(self, canons, ticks):
        cfg = types.SimpleNamespace(canon="/canon", canon_ref="")
        with mock.patch.object(staging_watch, "executor_canons",
                               return_value=canons), \
                mock.patch.object(staging_watch, "executor_ticks",
                                  return_value=ticks), \
                mock.patch.object(staging_watch, "canon_drift_findings",
                                  return_value=[]) as findings, \
                mock.patch.object(canon_mod, "git_toplevel",
                                  return_value="/repo"), \
                mock.patch.object(canon_mod, "git_commit", return_value=NEW):
            staging_watch.canon_alerts(object(), now=NOW, cfg=cfg)
        return findings.call_args.args[0]

    def test_a_fresh_tick_after_the_pass_wins(self):
        records = self.selected(
            [("mesh", pass_row(ident=10))], tick(ident=11))
        self.assertEqual(len(records), 1)
        key, data = records[0]
        self.assertEqual((key, data["canon_ref"], data["host"],
                          data["notify"]),
                         ("mesh", NEW, "ameesh-agents-1", "mesh-design"))

    def test_an_older_tick_does_not_mask_a_newer_pass(self):
        records = self.selected(
            [("mesh", pass_row(ident=12, ref=NEW))],
            tick(ident=11, ref=OLD))
        self.assertEqual(records[0][1]["canon_ref"], NEW)

    def test_a_stale_or_non_candidate_tick_keeps_the_pass(self):
        self.assertEqual(self.selected(
            [("mesh", pass_row(ident=10))],
            tick(ident=11, when=NOW - staging_watch.EXECUTOR_TICK_MAX_S - 1)
        )[0][1]["canon_ref"], OLD)
        outside = tick(ident=11)
        outside[("mesh", "ameesh-agents-1")]["data"]["candidates"] = ["h2"]
        self.assertEqual(self.selected(
            [("mesh", pass_row(ident=10))], outside)[0][1]["canon_ref"], OLD)

    def test_another_host_does_not_speak_for_the_pass(self):
        records = self.selected(
            [("mesh", pass_row(ident=10, host="ameesh-agents-1"))],
            tick(ident=11, host="ameesh-agents-2"))
        self.assertEqual(
            {(key, data.get("host"), data["canon_ref"])
             for key, data in records},
            {("mesh", "ameesh-agents-1", OLD),
             ("mesh", "ameesh-agents-2", NEW)})


class AlertWitnessTest(unittest.TestCase):
    """Les témoins du lot : plus de fausse alerte, pas de refus perdu."""

    def alerts(self, canons, ticks, *, since=NOW - 2 * DAY):
        cfg = types.SimpleNamespace(canon="/canon", canon_ref="")
        with mock.patch.object(staging_watch, "executor_canons",
                               return_value=canons), \
                mock.patch.object(staging_watch, "executor_ticks",
                                  return_value=ticks), \
                mock.patch.object(staging_watch, "_first_ahead",
                                  return_value=lambda ref, head: since), \
                mock.patch.object(canon_mod, "git_toplevel",
                                  return_value="/repo"), \
                mock.patch.object(canon_mod, "git_commit", return_value=NEW):
            return staging_watch.canon_alerts(object(), now=NOW, cfg=cfg)

    def test_a_pass_that_advanced_the_pin_then_a_fresh_tick_is_silent(self):
        # The pass read the old pin before its own canon-executant step; the
        # tick at the advanced pin is newer and must silence the alert.
        self.assertEqual(self.alerts(
            [("mesh", pass_row(ident=10))], tick(ident=11)), [])

    def test_the_pass_alone_still_alerts(self):
        alert, = self.alerts([("mesh", pass_row(ident=10))], {})
        self.assertEqual((alert["type"], alert["canon_ref"], alert["host"]),
                         ("staging_canon_stale", OLD, "ameesh-agents-1"))

    def test_an_older_tick_does_not_hide_a_newer_pass(self):
        self.assertEqual(self.alerts(
            [("mesh", pass_row(ident=12, ref=NEW))],
            tick(ident=11, ref=OLD)), [])

    def test_a_refusal_of_the_same_copy_survives_a_newer_tick(self):
        refused = dict(state="refused", reason="canon check")
        alert, = self.alerts(
            [("mesh", pass_row(ident=10, canon_refresh=refused))],
            tick(ident=11, ref=OLD), since=NOW)
        self.assertEqual(alert["canon_ref"], OLD)
        self.assertIn("refused (canon check", alert["detail"])

    def test_a_moved_copy_drops_the_old_refusal(self):
        refused = dict(state="refused", reason="canon check")
        self.assertEqual(self.alerts(
            [("mesh", pass_row(ident=10, canon_refresh=refused))],
            tick(ident=11, ref=NEW)), [])


if __name__ == "__main__":
    unittest.main()
