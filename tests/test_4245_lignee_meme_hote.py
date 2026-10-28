# SPDX-License-Identifier: AGPL-3.0-only
"""#4245 : un redémarrage d'exécuteur au même hôte ne tue plus la composition.

Le 28/10 (h-07, #4243), le redémarrage de l'exécuteur mesh-design d'agents-1
(réservation #4086) a fait passer le bail de 291 à 293 sans quitter l'hôte ;
le `packet record` de la composition d'époque 291 a été refusé et deux
tentatives sur trois perdues. Témoins : la garde EP4 accepte une commande de
rôle d'époque E à E' > E quand la lignée du bail est restée sur son hôte
(déclencheur de 0503), attend la reprise sous une réservation de relève à cet
hôte, et refuse toujours un bail passé par un autre hôte entre E et E'
(#4087) ; paquet-horaire ne décompte pas une tentative refusée par la garde.
"""
from __future__ import annotations

import contextlib
import os
import re
import unittest
from unittest import mock

from ameesh import pilot_gesture, registry, role_handover_pg as handover_pg

from .support import PgTestCase
from .test_3845_paquet_horaire import Base, carrier, ph
from .test_l165_ep4_pilot_gesture import IDENTITY_KEYS


class SameHostLineageTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        slug = re.sub(r"[^a-z0-9]+", "-", self._testMethodName.lower()).strip("-")
        self.name = ("l-" + slug)[:32]
        registry.upsert(self.db, self.name, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', candidats = ARRAY[%s, %s] "
            " WHERE name = %s", ("h1", "h2", self.name))
        self.db.execute("DELETE FROM host_holds")
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM role_handover")
        self.epoch = int(registry.claim(self.db, self.name, "runner-h1-a", 600,
                                        host="h1")["lease_epoch"])

    @contextlib.contextmanager
    def role_env(self, epoch: int, *, host: str | None = "h1",
                 owner: str | None = "runner-h1-a"):
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            os.environ["AMEESH_ROLE"] = self.name
            os.environ["AMEESH_LEASE_EPOCH"] = str(epoch)
            if host:
                os.environ["AMEESH_ROLE_HOST"] = host
            if owner:
                os.environ["AMEESH_LEASE_OWNER"] = owner
            yield

    def restart(self, owner: str, epoch: int, new_owner: str, host: str = "h1",
                *, cross: bool = False) -> int:
        """Retour du bail puis reprise (relève croisée : avec brief)."""
        self.assertTrue(registry.release(self.db, self.name, owner, epoch))
        brief = "brief-%s" % host if cross else None
        lease = registry.claim(self.db, self.name, new_owner, 600, host=host,
                               election_brief=brief)
        self.assertIsNotNone(lease)
        return int(lease["lease_epoch"])

    def events(self, ref: str) -> list[dict]:
        return self.db.query(
            "SELECT data FROM ameesh_events WHERE type = 'pilot.gesture'"
            " AND object = %s AND data->>'ref' = %s ORDER BY id",
            ("agent:" + self.name, ref))

    def expire(self) -> None:
        self.db.execute("UPDATE agent_registry SET lease_expires_at = clock_timestamp()"
                        " - interval '1 second' WHERE name = %s", (self.name,))

    def test_redemarrage_meme_hote_accepte_et_journalise_les_deux_epoques(self):
        now = self.restart("runner-h1-a", self.epoch, "runner-h1-b")
        self.assertEqual(now, self.epoch + 2)
        with self.role_env(self.epoch):
            gate = pilot_gesture.gate(self.cfg, self.db, "packet", "record #1")
            self.assertEqual((gate.epoch, gate.entered_epoch, gate.runner),
                             (now, self.epoch, "runner-h1-b"))
            gate.hold(self.db)
        data = self.events("record #1")[0]["data"]
        self.assertEqual((data["epoch"], data["lease_epoch"], data["lineage"]),
                         (self.epoch, now, "same-host"))

    def test_deux_redemarrages_et_bail_expire_reclame_restent_couverts(self):
        now = self.restart("runner-h1-a", self.epoch, "runner-h1-b")
        self.expire()
        now = int(registry.claim(self.db, self.name, "runner-h1-c", 600,
                                 host="h1")["lease_epoch"])
        now = self.restart("runner-h1-c", now, "runner-h1-d")
        with self.role_env(self.epoch):
            gate = pilot_gesture.gate(self.cfg, self.db, "packet", "record #2")
            gate.hold(self.db)
        self.assertEqual(gate.epoch, now)

    def test_bail_passe_par_un_autre_hote_toujours_refuse(self):
        other = self.restart("runner-h1-a", self.epoch, "runner-h2", host="h2", cross=True)
        self.restart("runner-h2", other, "runner-h1-b", host="h1", cross=True)
        with self.role_env(self.epoch):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #3")
        self.assertIn("fenced by the lease epoch", str(caught.exception))
        self.assertEqual(self.events("record #3"), [])

    def test_autre_hote_par_expiration_puis_retour_a_e_plus_2_refuse(self):
        # E (h1) expire, h2 réclame à E+1, expire, h1 réclame à E+2 : même
        # écart qu'un redémarrage, mais la lignée a quitté h1
        self.expire()
        registry.claim(self.db, self.name, "runner-h2", 600, host="h2",
                       election_brief="brief-h2")
        self.expire()
        lease = registry.claim(self.db, self.name, "runner-h1-b", 600, host="h1",
                               election_brief="brief-h1")
        self.assertEqual(int(lease["lease_epoch"]), self.epoch + 2)
        with self.role_env(self.epoch):
            with self.assertRaises(pilot_gesture.GestureRefused):
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #4")

    def test_session_d_un_autre_hote_refusee(self):
        self.restart("runner-h1-a", self.epoch, "runner-h1-b")
        with self.role_env(self.epoch, host="h2"):
            with self.assertRaises(pilot_gesture.GestureRefused):
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #5")
        with self.role_env(self.epoch, host=None):
            with self.assertRaises(pilot_gesture.GestureRefused):
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #5")

    def test_reservation_a_cet_hote_attend_la_reprise(self):
        self.assertTrue(handover_pg.reserve_restart(
            self.db, "h1", self.name, 90, owner="runner-h1-a", epoch=self.epoch))
        self.assertTrue(registry.release(self.db, self.name, "runner-h1-a", self.epoch))
        naps = []

        def nap(seconds):
            naps.append(seconds)
            if len(naps) == 2:  # l'exécuteur relancé reprend pendant l'attente
                registry.claim(self.db, self.name, "runner-h1-b", 600, host="h1")
                handover_pg.clear_restart(self.db, self.name)

        with self.role_env(self.epoch), mock.patch.object(pilot_gesture.time, "sleep", nap):
            gate = pilot_gesture.gate(self.cfg, self.db, "packet", "record #6")
            gate.hold(self.db)
        self.assertEqual(gate.epoch, self.epoch + 2)
        self.assertEqual(len(naps), 2)

    def test_reservation_echue_sans_reprise_refuse_borne(self):
        self.assertTrue(handover_pg.reserve_restart(
            self.db, "h1", self.name, 90, owner="runner-h1-a", epoch=self.epoch))
        self.assertTrue(registry.release(self.db, self.name, "runner-h1-a", self.epoch))
        ticks = iter(range(0, 1000, 30))
        with self.role_env(self.epoch), \
                mock.patch.object(pilot_gesture.time, "sleep", lambda s: None), \
                mock.patch.object(pilot_gesture.time, "monotonic", lambda: next(ticks)):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #7")
        self.assertIn("nobody", str(caught.exception))

    def test_reservation_a_un_autre_hote_n_attend_pas(self):
        self.assertTrue(handover_pg.reserve_restart(
            self.db, "h2", self.name, 90, owner="runner-h1-a", epoch=self.epoch))
        self.assertTrue(registry.release(self.db, self.name, "runner-h1-a", self.epoch))
        with self.role_env(self.epoch), \
                mock.patch.object(pilot_gesture.time, "sleep",
                                  mock.Mock(side_effect=AssertionError("attente"))):
            with self.assertRaises(pilot_gesture.GestureRefused):
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #8")

    def test_lignee_rompue_entre_la_garde_et_l_ecriture_refuse(self):
        self.restart("runner-h1-a", self.epoch, "runner-h1-b")
        with self.role_env(self.epoch):
            gate = pilot_gesture.gate(self.cfg, self.db, "packet", "record #9")
            # l'hôte de la ligne change sous le même détenteur : lignée repartie
            self.db.execute("UPDATE agent_registry SET host = 'h2' WHERE name = %s",
                            (self.name,))
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                gate.hold(self.db)
        self.assertIn("lineage broken", str(caught.exception))
        self.assertEqual(self.events("record #9"), [])

    def test_session_d_agent_garde_l_epoque_exacte(self):
        self.restart("runner-h1-a", self.epoch, "runner-h1-b")
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            os.environ.update(AGENT_MAIL_NAME=self.name, AMEESH_RUNNER_ID="runner-h1-a",
                              AMEESH_LEASE_EPOCH=str(self.epoch))
            with self.assertRaises(pilot_gesture.GestureRefused):
                pilot_gesture.gate(self.cfg, self.db, "packet", "record #10")


class FencedAttemptTest(Base):
    """paquet-horaire : un refus de garde d'époque ne consomme pas de tentative."""

    REFUSAL = ("composition interrompue avant la prochaine opération : "
               "GestureRefused: gesture packet refused for mesh-design: this session "
               "is fenced by the lease epoch (lease expired); the role is now held "
               "by nobody on ameesh-agents-1 at epoch 292")

    def unfrozen(self, *notes):
        item = carrier(sha="", created=ph.time.time() - 3600)
        item["events"] = [{"note": n, "created_ts": self.shell.clock - 3000 + i}
                          for i, n in enumerate(notes)]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def autos(self):
        return [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)]

    def test_refus_de_garde_non_decompte_puis_reprise_au_meme_numero(self):
        self.ctx.alive = lambda pid: False
        self.ctx.epoch = 291
        item = self.unfrozen(
            ph.MARK_ATTEMPT + "composition n=1 base=- par=%s:99 epoque=291" % self.ctx.host)
        self.shell.auto_rc, self.shell.auto_out = 1, self.REFUSAL
        self.assertIn("non concluante", ph.tick(self.ctx))
        self.assertIn(ph.MARK_FENCED + "composition n=2 base=- époque=291", ph.marks(item))
        self.assertFalse(any(n.startswith(ph.MARK_FAILED) for n in ph.marks(item)))
        self.assertIn("non décomptée", self.shell.mails[-1][1])
        # tic suivant : même numéro (2), aucun --force de plus qu'avant
        self.shell.auto_rc, self.shell.auto_out = 1, "autre panne"
        with self.assertRaises(ph.HoraireError):
            ph.tick(self.ctx)
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertIn("composition n=2", tried[-1])
        self.assertEqual(len([n for n in tried if "n=2" in n]), 2)

    def test_refus_de_garde_au_dernier_numero_n_epuise_pas(self):
        self.ctx.alive = lambda pid: False
        notes = []
        for n in range(1, ph.MAX_ATTEMPTS):
            notes += [ph.MARK_ATTEMPT + "composition n=%d base=- par=autre:9" % n,
                      ph.MARK_FAILED + "composition n=%d base=- rc=1" % n]
        notes += [ph.MARK_ATTEMPT + "composition n=%d base=- par=autre:9" % ph.MAX_ATTEMPTS,
                  ph.MARK_FENCED + "composition n=%d base=- époque=291" % ph.MAX_ATTEMPTS]
        self.unfrozen(*notes)
        self.tick_ok()
        tried = [c for c in self.autos()]
        self.assertEqual(len(tried), 1)

    def tick_ok(self):
        self.shell.auto_rc, self.shell.auto_out = 0, "aucun lot prêt\n"
        ph.tick(self.ctx)

    def test_refus_de_garde_bornes_puis_signale(self):
        notes = []
        for _ in range(ph.MAX_FENCED):
            notes += [ph.MARK_ATTEMPT + "composition n=1 base=- par=autre:9",
                      ph.MARK_FENCED + "composition n=1 base=- époque=291"]
        self.unfrozen(*notes)
        self.assertIn("épuisée (garde)", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])
        self.assertIn("garde d'époque", self.shell.mails[-1][1])


if __name__ == "__main__":
    unittest.main()
