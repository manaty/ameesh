# SPDX-License-Identifier: AGPL-3.0-only
"""#8391 : une pause opérateur datée et motivée se tait jusqu'à son échéance.

Pendant R1 v3 (27/11), une pause opérateur datée a produit 15 événements et
déclenché 12 tours en 40 minutes : l'alerte `operator_pause_long`/
`operator_pause_host` ignorait l'échéance et le motif. Ici :

* une pause datée et motivée ne produit aucune alerte avant son échéance ;
* à l'échéance dépassée elle alerte (même avant le seuil de durée), parce que
  l'exécuteur aurait dû lever la pause ;
* un avis unique par hôte part au pilote déclaré au canon à la pose, en
  courrier passif (`info`) : il ne réveille personne ; sans pilote lisible,
  aucun avis n'est déposé et le journal le dit ;
* une pause sans échéance garde l'alerte de durée de #3961, et un acteur
  machine (sommeil automatique) n'envoie aucun avis.
"""
from __future__ import annotations

import dataclasses
import os
import time
import unittest

from ameesh import agent_pause, exploitation, mail, notify, registry

from .support import PgTestCase


class PauseDateeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("worker", "other", "third"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        registry.upsert(self.db, "mesh-design", chantier="ameesh",
                        harness="deepseek", host=self.cfg.host, cwd=self.tmp)
        self.host = self.cfg.host
        self.canon = self._pilot_canon()

    def _pilot_canon(self) -> str:
        """Un canon minimal : `mesh-design` y tient le rôle d'orchestrateur.

        Le destinataire de l'avis de pose se lit au canon (#7660), jamais
        d'un nom écrit dans le cœur : le banc le déclare donc comme le vrai
        canon du mesh le fait."""
        root = os.path.join(self.tmp, "canon-8391")
        agents = os.path.join(root, "agents")
        os.makedirs(agents, exist_ok=True)
        with open(os.path.join(agents, "mesh-design.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntype: Agent\ntitle: mesh-design\nteam: ameesh\n"
                     "roles: [orchestrateur]\nharness: claude\n---\n")
        return root

    def pause(self, name="worker", **kw):
        args = dict(paused=True, reason="maintenance PR #1078", actor="human:alice")
        args.update(kw)
        return agent_pause.change(self.db, name, **args)

    def lift(self, name="worker"):
        return agent_pause.change(self.db, name, paused=False, reason="done",
                                  actor="human:alice")

    def age(self, name, minutes, *, until_in=None):
        """Recule le début de la pause et, si donné, fixe son échéance."""
        self.db.execute(
            "UPDATE agent_registry SET operator_pause_at = clock_timestamp()"
            " - make_interval(mins => %s) WHERE name = %s", (int(minutes), name))
        if until_in is not None:
            self.db.execute(
                "UPDATE agent_registry SET operator_pause_until = clock_timestamp()"
                " + make_interval(secs => %s) WHERE name = %s",
                (float(until_in), name))

    def _alertes(self, kind, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == kind]

    # -- silence et échéance ----------------------------------------------

    def test_une_pause_datee_et_motivee_se_tait_avant_lecheance(self):
        self.pause(until=time.time() + 4 * 3600)
        self.age("worker", 40, until_in=3 * 3600)
        self.assertEqual(self._alertes("operator_pause_long"), [])
        # un hôte majoritairement pausé se tait aussi tant que les échéances
        # sont à venir
        self.pause("other", until=time.time() + 4 * 3600)
        self.age("other", 40, until_in=3 * 3600)
        self.assertEqual(self._alertes("operator_pause_host"), [])

    def test_lecheance_depassee_alerte_meme_avant_le_seuil_de_duree(self):
        self.pause(until=time.time() + 300)
        self.age("worker", 10, until_in=-1)
        alertes = self._alertes("operator_pause_long")
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertTrue(alerte["deadline_passed"])
        self.assertIn("deadline has passed", alerte["detail"])
        self.assertIn("maintenance PR #1078", alerte["detail"])
        self.assertIn("ameesh resume worker --operator", alerte["detail"])

    def test_l_hote_date_se_tait_puis_alerte_a_lecheance(self):
        for name in ("worker", "other"):
            self.pause(name, until=time.time() + 4 * 3600)
            self.age(name, 40, until_in=3 * 3600)
        self.assertEqual(self._alertes("operator_pause_host"), [])
        for name in ("worker", "other"):
            self.age(name, 40, until_in=-1)
        alertes = self._alertes("operator_pause_host")
        self.assertEqual(len(alertes), 1)
        self.assertTrue(alertes[0]["deadline_passed"])
        self.assertIn("deadline passed for other, worker", alertes[0]["detail"])

    def test_une_pause_sans_echeance_garde_lalerte_de_duree(self):
        self.pause(indefinite=True)
        self.age("worker", 31)
        alertes = self._alertes("operator_pause_long")
        self.assertEqual(len(alertes), 1)
        self.assertFalse(alertes[0]["deadline_passed"])
        self.assertIn("no deadline", alertes[0]["detail"])

    def test_une_pause_sans_motif_ne_se_tait_pas(self):
        self.pause(indefinite=True)
        self.db.execute("UPDATE agent_registry SET operator_pause_reason = NULL"
                        " WHERE name = 'worker'")
        self.age("worker", 31)
        self.assertEqual(len(self._alertes("operator_pause_long")), 1)

    # -- avis de pose ------------------------------------------------------

    def notifier(self, *, canon=True, log=lambda text: None):
        cfg = (dataclasses.replace(self.cfg, canon=self.canon,
                                   canon_untrusted=True, canon_ref="",
                                   project="ameesh") if canon else self.cfg)
        return notify.Notifier(cfg, notify.parse_config({"default": []}),
                               state={}, log=log)

    def messages(self):
        return self.db.query(
            "SELECT recipient, body, payload FROM agent_mailbox ORDER BY id")

    def test_un_avis_unique_par_hote_a_la_pose_sans_reveil(self):
        for name in ("worker", "other"):
            self.pause(name, until=time.time() + 4 * 3600)
        notifier = self.notifier()
        notifier._pause_pose_notices(self.db, time.time())
        notifier._pause_pose_notices(self.db, time.time() + 30)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["recipient"], "mesh-design")
        self.assertEqual(message["payload"]["operator_pause"]["host"], self.host)
        self.assertEqual(message["payload"]["operator_pause"]["agents"],
                         ["other", "worker"])
        self.assertEqual(mail.passive_reason({"payload": message["payload"]}), "info")
        self.assertIn("No action expected before the deadline", message["body"])
        self.assertIn("maintenance PR #1078", message["body"])

    def test_un_nouvel_episode_sur_le_meme_hote_recoit_un_avis(self):
        notifier = self.notifier()
        self.pause(until=time.time() + 4 * 3600)
        notifier._pause_pose_notices(self.db, time.time())
        self.lift()
        notifier._pause_pose_notices(self.db, time.time() + 30)
        self.db.execute("DELETE FROM agent_mailbox")
        self.pause(until=time.time() + 4 * 3600)
        notifier._pause_pose_notices(self.db, time.time() + 60)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["recipient"], "mesh-design")

    def test_aucun_avis_pour_une_pause_sans_echeance_ou_machine(self):
        notifier = self.notifier()
        self.pause(indefinite=True)
        self.pause("other", reason="feuille de route épuisée (ameesh)",
                   actor="ameesh:file-decoupage", principal="human:alice",
                   until=time.time() + 4 * 3600)
        notifier._pause_pose_notices(self.db, time.time())
        self.assertEqual(self.messages(), [])

    def test_aucun_avis_une_fois_lecheance_depassee(self):
        notifier = self.notifier()
        self.pause(until=time.time() + 300)
        self.age("worker", 10, until_in=-1)
        notifier._pause_pose_notices(self.db, time.time())
        self.assertEqual(self.messages(), [])

    def test_sans_pilote_lisible_aucun_avis_et_le_journal_le_dit(self):
        logs = []
        notifier = self.notifier(canon=False, log=logs.append)
        self.pause(until=time.time() + 4 * 3600)
        notifier._pause_pose_notices(self.db, time.time())
        self.assertEqual(self.messages(), [])
        self.assertTrue(any("no readable pilot" in line for line in logs), logs)

    def test_sans_pilote_le_journal_parle_une_fois_par_episode(self):
        # #8431 : une ligne par épisode et par hôte, la passe suivante réessaie.
        logs = []
        notifier = self.notifier(canon=False, log=logs.append)
        self.pause(until=time.time() + 4 * 3600)
        for offset in (0, 30, 60):
            notifier._pause_pose_notices(self.db, time.time() + offset)
        lines = [line for line in logs if "no readable pilot" in line]
        self.assertEqual(len(lines), 1, logs)
        self.assertIn(self.host, lines[0])
        self.assertNotIn(self.host, notifier.state["pause_notices"])
        self.lift()
        notifier._pause_pose_notices(self.db, time.time() + 90)
        self.pause(until=time.time() + 4 * 3600)
        notifier._pause_pose_notices(self.db, time.time() + 120)
        lines = [line for line in logs if "no readable pilot" in line]
        self.assertEqual(len(lines), 2, logs)


if __name__ == "__main__":
    unittest.main()
