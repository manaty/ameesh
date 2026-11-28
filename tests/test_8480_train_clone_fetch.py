# SPDX-License-Identifier: AGPL-3.0-only
"""#8480 : la tête gelée d'un paquet composé par un autre runner est récupérée
dans le clone du train avant toute preuve, sinon un incident nommé.

Le clone du train est propre au propriétaire de la réservation
(`Passage.repository_path`). Le 28/11, le plan 165f1938 composé et publié par
le runner mty-amh-impl-08 a été repris par deepseek1 sur le même hôte : la
preuve des parts lisait 341acab2 dans un clone qui ne l'avait jamais reçu et
le train est resté deux heures en `draining`, sans alerte au pilote.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import (exploitation, alert_briefs, plan_git, resolutions, train,
                    train_git, train_runner, train_runtime)
from .test_l160e2_land import _Land
from .test_l160b_radar import _git
from .test_7967_train_sommeil import TrainStuckHourTest

BRANCH = "integration/train-20261128-1406-165f1938763407b2"


class OtherCloneTest(_Land):
    """Tête composée et publiée depuis un autre clone, preuve dans celui du train."""
    def setUp(self):
        super().setUp()
        composer = resolutions.Replayer(self.repo, os.path.join(self.tmp, "composer.clone"))
        self.composer = train_git.Repository(self.project, composer)
        members = [self.queued(1)]
        base = self.composer.target("main")
        merged = self.composer.compose("main", base, members)
        self.plan = dict(id="165f1938", source=self.composer.source, target="main",
                         base=base, branch=BRANCH, members=members, **merged)
        # La relève : un autre runner, donc un autre clone, reprend le plan.
        mine = resolutions.Replayer(self.repo, os.path.join(self.tmp, "train.clone"))
        self.repository = train_git.Repository(self.project, mine)
        self.repository.target("main")

    def publish(self):
        _git(self.composer.clone.path, "push", "-q", self.origin,
             "%s:refs/heads/%s" % (self.plan["head"], BRANCH))

    def present(self):
        return plan_git._commit(self.repository.clone.path, self.plan["head"])

    def test_the_head_published_elsewhere_is_fetched_before_the_proof(self):
        self.publish()
        self.assertIsNone(self.present())      # le constat du 28/11
        self.repository.materialize(self.plan)
        self.assertEqual(self.present(), self.plan["head"])
        # une ref locale garde l'objet atteignable dans le clone
        self.assertEqual(_git(self.repository.clone.path, "rev-parse",
                              "refs/ameesh/trains/" + BRANCH), self.plan["head"])

    def test_a_present_head_needs_no_fetch(self):
        with mock.patch.object(resolutions, "_run", wraps=resolutions._run) as run:
            self.composer.materialize(self.plan)
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args])

    def test_a_head_still_absent_after_fetch_is_a_named_incident(self):
        with self.assertRaises(train.TrainObjectMissing) as caught:
            self.repository.materialize(self.plan)
        self.assertEqual(caught.exception.incident, dict(
            kind="train-object-missing", plan="165f1938", sha=self.plan["head"]))
        self.assertIn(BRANCH, str(caught.exception))
        self.assertIn(self.plan["head"][:12], str(caught.exception))

    def test_a_plan_branch_outside_the_train_namespace_is_refused(self):
        with self.assertRaises(train.TrainError):
            self.repository.materialize(dict(self.plan, branch="develop"))

    def test_runtime_reads_ci_only_after_materializing(self):
        self.publish()
        seen = []
        runtime = object.__new__(train_runtime.Runtime)
        runtime.repository = self.repository
        runtime.ci_adapter = mock.Mock()
        runtime.ci_adapter.read.side_effect = lambda plan: seen.append(
            self.present()) or {"status": "pending"}
        self.assertEqual(runtime.ci(self.plan), {"status": "pending"})
        self.assertEqual(seen, [self.plan["head"]])


class EngineIncidentTest(unittest.TestCase):
    """L'incident garde le plan en `draining` et remonte nommé à l'appelant."""
    def test_draining_keeps_the_target_and_names_the_incident(self):
        state = dict(source="s", target="develop", phase="draining",
                     plan=dict(id="165f1938", head="a" * 40), attempts=[{}])
        ports = mock.Mock()
        ports.acquire.return_value = 7
        ports.load.return_value = state
        ports.delivered.return_value = False
        ports.ci.side_effect = train.TrainObjectMissing("165f1938", "a" * 40, "missing")
        ports.save.side_effect = lambda doc, *a, **k: doc
        engine = train.Engine(ports, source="s", target="develop",
                              policy=train.Policy(enabled=True))
        with mock.patch.object(train.Engine, "_members_delivered", return_value=False), \
                mock.patch.object(train.Engine, "_expired_reservation", return_value=None), \
                mock.patch.object(train.Engine, "_save", side_effect=lambda doc: doc):
            result = engine.step(1000.0)
        self.assertEqual(result["result"], "refused")
        self.assertEqual(result["incident"]["kind"], "train-object-missing")
        self.assertEqual(state["phase"], "draining")
        ports.release.assert_not_called()

    def test_an_expired_reservation_still_names_the_incident(self):
        state = dict(source="s", target="develop", phase="draining",
                     plan=dict(id="165f1938", head="a" * 40), attempts=[{}])
        engine = object.__new__(train.Engine)
        with mock.patch.object(train.Engine, "_ci", side_effect=train.TrainObjectMissing(
                "165f1938", "a" * 40, "missing")):
            result = engine._expired_step(state, 900.0)
        self.assertEqual(result["result"], "draining")
        self.assertEqual(result["incident"]["sha"], "a" * 40)


class PilotOnceTest(unittest.TestCase):
    def passage(self):
        passage = object.__new__(train_runner.Passage)
        passage.cfg, passage.owner = object(), "runner-deepseek1@ameesh-agents-2"
        passage.project = types.SimpleNamespace(name="nexlink", target="develop")
        passage.notify = mock.Mock()
        return passage

    def test_the_pilot_mail_is_idempotent_per_plan_and_commit(self):
        result = {"result": "refused", "error": "train object missing after fetch",
                  "incident": dict(kind="train-object-missing", plan="165f1938",
                                   sha="a" * 40)}
        with mock.patch("ameesh.train_canon.pilot_of",
                        return_value=lambda db: "orchestrateur"), \
                mock.patch("ameesh.mail.send") as send:
            self.passage()._notify_incident(object(), result)
            self.passage()._notify_incident(object(), result)
        self.assertEqual(send.call_count, 2)
        keys = {c.kwargs["idempotency_key"] for c in send.call_args_list}
        self.assertEqual(keys, {"train-object-missing:165f1938:%s:orchestrateur" % ("a" * 40)})
        self.assertEqual(send.call_args.args[2], "orchestrateur")
        self.assertIn("165f1938", send.call_args.args[3])

    def test_other_refusals_send_no_mail(self):
        with mock.patch("ameesh.mail.send") as send:
            self.passage()._notify_incident(object(), {"result": "refused", "error": "x"})
            self.passage()._notify_incident(object(), {"result": "draining"})
        send.assert_not_called()


class DrainingCapTest(TrainStuckHourTest):
    """Un `draining` alerte au plus tard à 2 h, même sous une cadence de 12 h."""
    rules = types.SimpleNamespace(next_packet=lambda at: (
        23 * 3600.0 if at < 23 * 3600.0 else 35 * 3600.0))

    def alerts(self, phase, age):
        started = 11 * 3600.0
        db = self.Db([self.row(phase=phase, started_ts=started)])
        with mock.patch.object(exploitation, "_packet_calendar", return_value=self.rules):
            return exploitation._train_stuck(db, started + age, cfg=object())

    def test_draining_alerts_after_two_hours(self):
        self.assertEqual(self.alerts("draining", 119 * 60), [])
        alerts = self.alerts("draining", 121 * 60)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["threshold"], 2)

    def test_other_phases_keep_the_project_cadence(self):
        self.assertEqual(self.alerts("ci", 3 * 3600.0), [])


class BureauBriefTest(unittest.TestCase):
    def test_train_stuck_reaches_the_desk(self):
        alert = {"type": "train_stuck", "detail": "train nexlink:develop stuck",
                 "project": "nexlink", "orchestrators": []}
        listing = [{"name": "bureau", "lease_live": True}]
        with mock.patch.object(alert_briefs, "_external", return_value=False), \
                mock.patch.object(alert_briefs, "_split_live",
                                  side_effect=lambda out, *a, **k: (dict(out), {})):
            briefs = alert_briefs.briefs(alert, listing, None, (),
                                         desk_names=("bureau",))
        self.assertIn("bureau", briefs)


if __name__ == "__main__":
    unittest.main()
