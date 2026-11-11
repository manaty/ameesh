# SPDX-License-Identifier: AGPL-3.0-only
"""#6839 : la prise exclusive d'un geste d'exécutant externe.

Le constat du 11/11 : deux sessions du bureau (BG et PC) ont exécuté le même
feu d'achat de #6836. Le lot du geste est le point de sérialisation : une
session le prend avant l'effet externe, sous le verrou de sa ligne ; la
seconde reçoit un refus nommé avec la porteuse et l'heure. Quand le feu nomme
une session (`--external-session`), seule cette session PROUVÉE peut prendre ;
une autre session ne peut ni prendre son libellé, ni libérer sa prise.

Les témoins couvrent la prise, le refus nommé, deux prises concurrentes, la
levée par la porteuse (ou par un pilote en `--force`), l'exclusivité de la
preuve de session, la levée de la réservation qui efface la prise, la
compatibilité N-1 du `--clear` d'avant 0565 et la CLI (`--json`, `work show`).
"""
from __future__ import annotations

import json
import threading

from ameesh import work

from . import test_3447_prise_externe as prise_externe
from .test_l119_backlog import _Base


class PriseGesteExterneTest(_Base):
    external_agent = prise_externe.PriseExterneTest.external_agent

    def reserve(self, *, session=None, title="achat de l'hôte isolé"):
        self.external_agent()
        lot = work.add(self.db, title=title, app="nexlink", actor="human:proprio",
                       cfg=self.cfg, external_executor="bureau",
                       external_session=session)
        return work.get(self.db, lot["id"])

    def item(self, lot_id):
        return work.get(self.db, lot_id)

    def claim(self, lot_id, session, *, proof=None, actor="bureau", host=None, **kw):
        return work.claim_external(self.db, lot_id, session, actor=actor, host=host,
                                   proof=proof, cfg=self.cfg, **kw)

    def release(self, lot_id, session, *, proof=None, actor="bureau", host=None,
                force=False):
        return work.release_external(self.db, lot_id, session, actor=actor, host=host,
                                     proof=proof, cfg=self.cfg, force=force)

    def test_prise_exclusive_nomme_la_porteuse_et_l_heure(self):
        lot = self.reserve()
        out = self.claim(lot["id"], "BG")
        self.assertEqual(out["session"], "BG")
        self.assertIsNotNone(out["at"])
        stored = self.item(lot["id"])
        self.assertEqual(stored["external_claim_session"], "BG")
        self.assertEqual(stored["external_claimed_ts"], out["at"])
        # la seconde session est refusée en nommant la porteuse et son heure
        with self.assertRaises(work.WorkError) as raised:
            self.claim(lot["id"], "PC")
        message = str(raised.exception)
        self.assertIn("already taken by BG at", message)
        self.assertIn("claim refused, nothing changed", message)
        self.assertIn("20", message)          # une heure, pas un « ? »
        stored = self.item(lot["id"])
        self.assertEqual(stored["external_claim_session"], "BG")
        self.assertEqual(stored["state"], lot["state"])

    def test_le_feu_qui_nomme_bg_refuse_pc(self):
        lot = self.reserve(session="BG")
        self.assertEqual(self.item(lot["id"])["external_session"], "BG")
        with self.assertRaises(work.WorkError) as raised:
            self.claim(lot["id"], "PC", proof="PC")
        self.assertIn("reserved to session BG", str(raised.exception))
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])
        # la session nommée prend, avec sa preuve
        out = self.claim(lot["id"], "BG", proof="BG")
        self.assertEqual(out["session"], "BG")
        self.assertEqual(self.item(lot["id"])["external_claim_session"], "BG")

    def test_le_feu_nomme_exige_une_session_prouvee(self):
        lot = self.reserve(session="BG")
        with self.assertRaises(work.WorkError) as raised:
            self.claim(lot["id"], "BG")          # aucune preuve : libellé libre
        message = str(raised.exception)
        self.assertIn("the fire names session BG", message)
        self.assertIn("no proved session", message)
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_le_libelle_ne_peut_pas_etre_celui_d_une_autre_session(self):
        lot = self.reserve()
        with self.assertRaises(work.WorkError) as raised:
            self.claim(lot["id"], "BG", proof="PC")
        self.assertIn("proved session is PC, not BG", str(raised.exception))
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_deux_prises_concurrentes_une_seule_est_acceptee(self):
        lot = self.reserve()
        barrier = threading.Barrier(2, timeout=30)
        results: list[dict] = []
        errors: list[BaseException] = []

        def attempt(session):
            db = self.connect()
            try:
                barrier.wait()
                results.append(
                    work.claim_external(db, lot["id"], session, actor="bureau",
                                        proof=session, cfg=self.cfg))
            except BaseException as exc:  # noqa: BLE001 - remonté par le témoin
                errors.append(exc)
            finally:
                db.close()

        threads = [threading.Thread(target=attempt, args=(session,))
                   for session in ("BG", "PC")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse([t for t in threads if t.is_alive()], "claim thread survived")
        self.assertEqual(len(results), 1, (results, errors))
        self.assertEqual(len(errors), 1, (results, errors))
        self.assertIsInstance(errors[0], work.WorkError)
        self.assertIn("already taken by %s at" % results[0]["session"],
                      str(errors[0]))
        self.assertEqual(self.item(lot["id"])["external_claim_session"],
                         results[0]["session"])
        notes = [event for event in work.events(self.db, lot["id"])
                 if str(event.get("note") or "").startswith("external claim")]
        self.assertEqual(len(notes), 1)

    def test_la_levee_est_reservee_a_la_porteuse(self):
        lot = self.reserve()
        self.claim(lot["id"], "BG")
        with self.assertRaises(work.WorkError) as raised:
            self.release(lot["id"], "PC")
        self.assertIn("claimed by session BG, not PC", str(raised.exception))
        self.assertEqual(self.item(lot["id"])["external_claim_session"], "BG")
        out = self.release(lot["id"], "BG")
        self.assertEqual(out["session"], "BG")
        stored = self.item(lot["id"])
        self.assertIsNone(stored["external_claim_session"])
        self.assertIsNone(stored["external_claimed_ts"])
        # une autre session peut reprendre après la levée
        self.assertEqual(self.claim(lot["id"], "PC")["session"], "PC")

    def test_une_autre_session_ne_peut_pas_liberer_la_prise_puis_reprendre(self):
        lot = self.reserve()
        self.claim(lot["id"], "BG", proof="BG", actor="bureau", host="bg-host")
        # même libellé public, mais la preuve de PC : la levée est refusée
        with self.assertRaises(work.WorkError) as raised:
            self.release(lot["id"], "BG", proof="PC", actor="bureau", host="bg-host")
        self.assertIn("proved session is PC, not BG", str(raised.exception))
        # même preuve, mais un autre acteur : refus
        with self.assertRaises(work.WorkError) as raised:
            self.release(lot["id"], "BG", proof="BG", actor="autre", host="bg-host")
        self.assertIn("taken by actor bureau on bg-host", str(raised.exception))
        # même acteur et preuve, mais un autre hôte : refus
        with self.assertRaises(work.WorkError) as raised:
            self.release(lot["id"], "BG", proof="BG", actor="bureau", host="pc-host")
        self.assertIn("taken on host bg-host", str(raised.exception))
        self.assertEqual(self.item(lot["id"])["external_claim_session"], "BG")
        # la porteuse, elle, lève
        self.release(lot["id"], "BG", proof="BG", actor="bureau", host="bg-host")
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_un_pilote_force_la_levee_d_une_session_disparue(self):
        lot = self.reserve()
        self.claim(lot["id"], "BG")
        with self.assertRaises(work.WorkError):
            self.release(lot["id"], "PC", actor="claude1", force=True)
        out = self.release(lot["id"], "PC", actor="human:proprio", force=True)
        self.assertTrue(out["forced"])
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_un_lot_ordinaire_ou_ferme_n_a_pas_de_prise(self):
        ordinaire = work.add(self.db, title="lot ordinaire", app="nexlink",
                             actor="human:proprio", cfg=self.cfg)
        with self.assertRaises(work.WorkError) as raised:
            self.claim(ordinaire["id"], "BG")
        self.assertIn("has no external executor", str(raised.exception))
        closed = self.reserve(title="geste déjà fini")
        work.close(self.db, closed["id"], abandoned=True, actor="human:proprio")
        with self.assertRaises(work.WorkError) as raised:
            self.claim(closed["id"], "BG")
        self.assertIn("no external gesture to take", str(raised.exception))

    def test_la_prise_refuse_une_session_invalide_avant_ecriture(self):
        lot = self.reserve()
        for bad in ("", "  ", "deux mots", "a\nb", "x" * 201, True):
            with self.assertRaises(work.WorkError):
                self.claim(lot["id"], bad)
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_la_reservation_ne_se_change_pas_sous_une_prise(self):
        lot = self.reserve()
        self.claim(lot["id"], "BG")
        with self.assertRaises(work.WorkError) as raised:
            work.set_external_executor(self.db, lot["id"], "bureau",
                                       session="PC", actor="human:proprio",
                                       cfg=self.cfg)
        self.assertIn("is claimed by session BG", str(raised.exception))
        self.assertEqual(self.item(lot["id"])["external_session"], None)
        # --clear efface la réservation ET la prise, en une écriture
        cleared = work.set_external_executor(self.db, lot["id"], None,
                                             clear=True, actor="human:proprio",
                                             cfg=self.cfg)
        self.assertIsNone(cleared["item"]["external_executor"])
        stored = self.item(lot["id"])
        self.assertIsNone(stored["external_executor"])
        self.assertIsNone(stored["external_claim_session"])
        self.assertIsNone(stored["external_claimed_ts"])

    def test_le_clear_d_un_code_d_avant_ne_casse_pas_la_migration(self):
        """N-1 (#6839, B1) : le vieux `--clear` ne met à NULL que
        `external_executor` ; les contraintes 0565 ne portent que sur les
        colonnes de prise, donc la fenêtre de déploiement ne casse pas."""
        lot = self.reserve(session="BG")
        self.claim(lot["id"], "BG", proof="BG")
        self.db.execute("UPDATE work_items SET external_executor = NULL"
                        " WHERE id = %s", (lot["id"],))
        stored = self.item(lot["id"])
        self.assertIsNone(stored["external_executor"])
        self.assertEqual(stored["external_claim_session"], "BG")
        # la levée neuve efface la réservation et la prise d'un code d'avant
        work.set_external_executor(self.db, lot["id"], None, clear=True,
                                   actor="human:proprio", cfg=self.cfg)
        stored = self.item(lot["id"])
        self.assertIsNone(stored["external_claim_session"])
        self.assertIsNone(stored["external_claim_actor"])

    def test_la_session_du_feu_peut_etre_nommee_apres_la_reservation(self):
        lot = self.reserve()
        out = work.set_external_executor(self.db, lot["id"], "bureau",
                                         session="BG", actor="human:proprio",
                                         cfg=self.cfg)
        self.assertEqual(out["session"], "BG")
        self.assertEqual(self.item(lot["id"])["external_session"], "BG")
        with self.assertRaises(work.WorkError):
            self.claim(lot["id"], "PC", proof="PC")

    def test_cli_claim_json_et_show(self):
        lot = self.reserve()
        env = self.env(AMEESH_SESSION_ID="BG")
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "BG",
                         "--actor", "bureau", "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh.work.claim/1")
        self.assertEqual(out["lot"], lot["id"])
        self.assertEqual(out["session"], "BG")
        self.assertFalse(out["released"])
        self.assertTrue(out["proved"])
        # la seconde session est refusée, nommée, code 1
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "PC",
                         "--actor", "bureau",
                         env=self.env(AMEESH_SESSION_ID="PC"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("already taken by BG at", proc.stderr)
        # l'état se lit dans work show
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("claim    : BG at", proc.stdout)
        self.assertIn("by bureau on", proc.stdout)
        # la levée par la porteuse
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "BG",
                         "--release", "--actor", "bureau", "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["released"])
        self.assertIsNone(self.item(lot["id"])["external_claim_session"])

    def test_cli_add_et_reservation_nomment_la_session(self):
        self.external_agent()
        proc = self.mesh("work", "add", "--title", "geste avec feu nommé",
                         "--app", "nexlink", "--new-project",
                         "--external-executor", "bureau",
                         "--external-session", "BG", "--actor", "human:proprio",
                         "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = json.loads(proc.stdout)["lot"]
        self.assertEqual(lot["external_session"], "BG")
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "PC",
                         "--actor", "bureau",
                         env=self.env(AMEESH_SESSION_ID="PC"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("reserved to session BG", proc.stderr)
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("session  : BG", proc.stdout)

    def test_work_add_refuse_une_session_sans_executant(self):
        with self.assertRaises(work.WorkError) as raised:
            work.add(self.db, title="session seule", app="nexlink",
                     actor="human:proprio", cfg=self.cfg,
                     external_session="BG")
        self.assertIn("goes with --external-executor", str(raised.exception))
