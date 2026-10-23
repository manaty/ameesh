# SPDX-License-Identifier: AGPL-3.0-only
"""#3236 : un brief par alerte de jauge et par fenêtre.

La perte projetée d'un `plan_underused` oscille autour de son seuil (50 % par
défaut) : l'alerte disparaît d'une passe puis revient, et `resets_at` gigue
d'une seconde entre deux relevés. Le contrat est « une fois à la levée, une
relance si l'alerte dure 6 h » : ces deux mouvements ne doivent pas refaire
briefer la même fenêtre. Une fenêtre suivante, elle, est une nouvelle alerte.
"""
from __future__ import annotations

import time
import unittest

from ameesh import notify, registry

from .support import PgTestCase

HEURE = 3600.0


class PlanUnderusedBriefWindowTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name, team in (("orch", "a"), ("dev1", "a"), ("dev2", "a")):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
            self.db.execute("UPDATE agent_registry SET team = %s WHERE name = %s",
                            (team, name))
        registry.upsert(self.db, "auditeur", harness="claude", host=self.cfg.host)
        # #3036 : les briefs ne partent qu'à un agent dont le bail est vivant.
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'")
        # un début de fenêtre aligné à la minute : la gigue de +1 s reste dans
        # le même seau, et le test ne dépend pas de l'heure du passage.
        self.now = float(int(time.time() // 60) * 60)

    def mails(self, name):
        return self.db.query("SELECT body, kind, payload FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))

    def plan(self, *, since=None, resets_at=None, reason="fin_de_fenetre"):
        since = self.now - HEURE if since is None else since
        resets_at = since + 5 * HEURE if resets_at is None else resets_at
        return {"type": "plan_underused", "harness": "claude", "account": "tertiaire",
                "gauge": "five_hour", "reason": reason, "since": since,
                "resets_at": resets_at,
                "detail": "account claude/tertiaire, window five_hour: 37 % used",
                "idle_agents": ["dev1", "dev2"], "orchestrators": ["orch"]}

    def notifier(self):
        clock = [self.now]
        n = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                            sender=object(), log=lambda _t: None,
                            clock=lambda: clock[0])
        return n, clock

    def test_clignotement_ne_refait_pas_briefer_la_meme_fenetre(self):
        notifier, _clock = self.notifier()
        alert = self.plan()
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(len(self.mails("auditeur")), 1)

        # la perte projetée repasse sous le seuil : l'alerte est absente d'une
        # passe. Le notificateur ne doit pas oublier qu'il a déjà briefé.
        notifier.run_pass(self.db, current=[])
        self.assertEqual(len(self.mails("orch")), 1)

        # elle revient : même fenêtre, même geste, pas de second courrier.
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1,
                         "un clignotement refait briefer la même fenêtre")

    def test_gigue_d_une_seconde_ne_refait_pas_briefer(self):
        """Témoin du lot : deux passes dont `since` bouge d'une seconde."""
        notifier, _clock = self.notifier()
        alert = self.plan()
        notifier.run_pass(self.db, current=[alert])
        notifier.run_pass(self.db, current=[self.plan(since=alert["since"] + 1)])
        self.assertEqual(len(self.mails("orch")), 1,
                         "une gigue d'une seconde est vue comme une alerte neuve")

    def test_raison_qui_bascule_ne_refait_pas_briefer(self):
        notifier, _clock = self.notifier()
        notifier.run_pass(self.db, current=[self.plan()])
        notifier.run_pass(self.db, current=[self.plan(reason="sous_rythme")])
        self.assertEqual(len(self.mails("orch")), 1,
                         "un changement de raison refait briefer la même fenêtre")

    def test_relance_apres_six_heures(self):
        notifier, clock = self.notifier()
        alert = self.plan()
        notifier.run_pass(self.db, current=[alert])
        clock[0] += 6 * HEURE
        notifier.run_pass(self.db, current=[alert])
        rows = self.mails("orch")
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(rows[1]["body"].startswith("Reminder"))
        clock[0] += 6 * HEURE
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 2)

    def test_fenetre_suivante_est_une_nouvelle_alerte(self):
        notifier, _clock = self.notifier()
        first = self.plan()
        notifier.run_pass(self.db, current=[first])
        self.assertEqual(len(self.mails("orch")), 1)
        second = self.plan(since=first["since"] + 5 * HEURE,
                           resets_at=first["resets_at"] + 5 * HEURE)
        notifier.run_pass(self.db, current=[second])
        self.assertEqual(len(self.mails("orch")), 2)


if __name__ == "__main__":
    unittest.main()
