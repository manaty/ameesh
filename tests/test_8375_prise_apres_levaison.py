# SPDX-License-Identifier: AGPL-3.0-only
"""#8375 : un lot dont la réservation d'exécutant externe vient d'être levée
n'est pas aussitôt donné par L165.

Trois occurrences le 27/11 (#8325, #8348, #8361) : le bureau termine un lot
réservé, lève la réservation (`external-executor --clear`), et la passe de
prise suivante le donne à un agent de construction avant que l'orchestrateur
ne le close. La levée est un passage de main, pas une nouvelle demande.
"""
from __future__ import annotations

from unittest.mock import patch

from ameesh import backlog, work

from .test_l119_backlog import HEURE
from .test_l165_prise_lots import _Lots


class LevaisonExterneTest(_Lots):
    def external_agent(self, name="bureau", responsible="human:proprio"):
        """Un agent `externe` (0030) : joignable pour un déclaratif, jamais
        réveillé par un exécuteur."""
        self.agent(name, harness="codex", responsible=responsible)
        self.db.execute("UPDATE agent_registry SET mode = 'externe' WHERE name = %s",
                        (name,))

    def reserve(self, title="geste root", *, app="nexlink", executor="bureau", **kw):
        item = work.add(self.db, title=title, app=app, external_executor=executor,
                        actor="human:proprio", cfg=self.cfg, **kw)
        # L165 laisse un lot tout juste créé à qui l'a créé : le témoin porte
        # sur un lot installé.
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - make_interval(secs => %s) WHERE id = %s",
                        (HEURE, item["id"]))
        return work.get(self.db, item["id"])

    def clear(self, lot):
        work.set_external_executor(self.db, lot["id"], None, actor="human:proprio",
                                   cfg=self.cfg, clear=True)

    def age(self, lot, seconds=None):
        """Vieillit le journal du lot : la levée (et la preuve de livraison qui
        la précède) sort de la fenêtre de clôture."""
        seconds = (backlog.EXTERNAL_RELEASE_GRACE_S + 60
                   if seconds is None else seconds)
        self.db.execute(
            "UPDATE work_item_events SET created_at = created_at"
            " - make_interval(secs => %s) WHERE work_item_id = %s",
            (seconds, lot["id"]))

    def test_une_levaison_recente_ecarte_le_lot_de_la_prise(self):
        """Le témoin du lot : réserver, lever ; aucune prise ensuite."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        self.clear(lot)

        self.assertEqual(self.take(), [])
        after = work.get(self.db, lot["id"])
        self.assertIsNone(after["assignee"])
        self.assertFalse(any(e["note"].startswith(backlog.LOT_TAKE_NOTE)
                             for e in work.events(self.db, lot["id"])))

        # La garde ne bloque pas la file : un autre lot est pris normalement.
        ordinaire = self.lot("travail ordinaire")
        self.assertEqual([t["item"]["id"] for t in self.take()], [ordinaire["id"]])

    def test_une_levaison_sans_preuve_revient_a_la_file_apres_la_fenetre(self):
        """Réservation abandonnée : la fenêtre passée, le lot est ordinaire."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        self.clear(lot)
        self.assertEqual(self.take(), [])
        self.age(lot)
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

    def test_une_levaison_avec_preuve_de_livraison_reste_a_clore(self):
        """Un lot livré reste « à clore », même après la fenêtre de clôture."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        work.note(self.db, lot["id"],
                  "[bureau] PROD FAITE : livré, vérifié ; smoke 15/15 vert",
                  actor="bureau")
        self.clear(lot)
        # Le témoin « réserver, livrer, lever » : aucune prise, même dans la
        # minute qui suit la levée.
        self.assertEqual(self.take(), [])
        # Bien après la fenêtre : la preuve de livraison garde le lot hors de
        # la prise, il ne reste qu'à le clore.
        self.age(lot, 6 * HEURE)
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertFalse(any(e["note"].startswith(backlog.LOT_TAKE_NOTE)
                             for e in work.events(self.db, lot["id"])))

        # Une attribution explicite est la nouvelle demande : elle reste
        # possible (le pilote sort du « à clore » par un geste nommé).
        work.assign(self.db, lot["id"], "claude1")
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

        # Rendue à la file par son porteur, elle est reprise normalement :
        # la preuve de livraison ne fige plus le lot après le geste nommé.
        work.return_to_queue(self.db, lot["id"], actor="human:proprio",
                             cfg=self.cfg)
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

    def test_la_levaison_elle_meme_ne_rend_pas_le_lot_prenable(self):
        """Sans preuve de livraison, la fenêtre court depuis la levée : le
        lot n'est pas déjà prenable une seconde après."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        self.clear(lot)
        self.assertIn(lot["id"], backlog.external_release_blocked(
            self.db, [work.get(self.db, lot["id"])], now=self.now))
        self.age(lot)
        self.assertNotIn(lot["id"], backlog.external_release_blocked(
            self.db, [work.get(self.db, lot["id"])], now=self.now))

    def test_une_levaison_arrivee_apres_la_selection_annule_la_prise(self):
        """Course §8 : le lot est sélectionné, puis réservé et levé avant
        l'écriture ; la relecture sous le verrou de la prise l'annule."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.lot("geste root")
        check = work.check_assignee
        played = []

        def reserve_and_clear(*args, **kwargs):
            if not played:
                work.set_external_executor(self.db, lot["id"], "bureau",
                                           actor="human:proprio", cfg=self.cfg)
                work.set_external_executor(self.db, lot["id"], None,
                                           actor="human:proprio", cfg=self.cfg,
                                           clear=True)
                played.append(True)
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=reserve_and_clear):
            out = self.take()
        self.assertTrue(played)
        self.assertEqual(out, [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertFalse(any(e["note"].startswith(backlog.LOT_TAKE_NOTE)
                             for e in work.events(self.db, lot["id"])))
