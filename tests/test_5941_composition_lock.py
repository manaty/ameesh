# SPDX-License-Identifier: AGPL-3.0-only
"""#5941 : un seul composeur de paquet, verrou partagé entre les deux chemins.

Constat du 05/11/2026 : l'alerte « créneau 15:00 manqué » a désigné
mty-amh-impl-01 pour composer, et une minute plus tard la minuterie
`ameesh-paquet-horaire` a composé le même lot #5925 dans un second porteur
(h-21 et h-22), contre le §11 bis (une seule CI de paquet à la fois).

Les témoins portent sur la course réelle, avec Postgres : un seul porteur
pour deux `work add` simultanés ; un bail tenu refuse la création avant toute
écriture ; le tic peut créer les deux porteurs d'une paire parallèle (#3256 C)
tant qu'il tient le bail, pas un autre composeur.
"""
from __future__ import annotations

import threading

from ameesh import packet_composition, work

from .support import PgTestCase

SLOW_FUNCTION = "t5941_slow_carrier"
SLOW_TRIGGER = "t5941_slow_carrier"


class CompositionLockTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM merge_locks WHERE target LIKE 'packet-compose:%%'")
        self.addCleanup(self._clear)

    def _clear(self) -> None:
        try:
            self.db.execute("DELETE FROM merge_locks"
                            " WHERE target LIKE 'packet-compose:%%'")
        except Exception:  # noqa: BLE001 - nettoyage de banc
            pass

    def add(self, name: str, *, owner: str | None = None, db=None) -> dict:
        return work.add(
            db or self.db,
            title="Paquet horaire %s — composition" % name,
            branch="integration/paquet-%s" % name,
            branch_target="main", app="ameesh",
            source="paquet-horaire (#3845), minuterie",
            cfg=self.cfg, composition_owner=owner)

    def open_carriers(self) -> list[int]:
        return packet_composition.carriers(self.db, "ameesh", "main")

    def test_un_second_porteur_est_refuse_par_le_verrou(self):
        first = self.add("h-20261105-21")
        self.assertEqual(self.open_carriers(), [int(first["id"])])
        with self.assertRaises(work.WorkError) as caught:
            self.add("h-20261105-22")
        self.assertIn("already reserves", str(caught.exception))
        self.assertEqual(self.open_carriers(), [int(first["id"])])

    def test_un_autre_composeur_qui_tient_le_bail_bloque_avant_ecriture(self):
        lease = packet_composition.Lease(
            self.db, "ameesh", owner="paquet-horaire:hote:4242")
        self.assertTrue(lease.acquire())
        try:
            with self.assertRaises(work.WorkError) as caught:
                self.add("h-20261105-22")
            self.assertIn("composition in progress", str(caught.exception))
            self.assertIn("paquet-horaire:hote:4242", str(caught.exception))
            self.assertEqual(self.open_carriers(), [])
        finally:
            self.assertTrue(lease.release())

    def test_le_tic_tient_le_bail_pour_les_deux_porteurs_paralleles(self):
        owner = packet_composition.owner("paquet-horaire", "hote", 4242)
        lease = packet_composition.Lease(self.db, "ameesh", owner=owner)
        self.assertTrue(lease.acquire())
        try:
            first = self.add("h-20261105-04", owner=owner)
            second = self.add("h-20261105-05", owner=owner)
            # Un autre composeur ne peut pas profiter du bail du tic.
            with self.assertRaises(work.WorkError) as caught:
                self.add("h-20261105-06")
            self.assertIn("composition in progress", str(caught.exception))
            self.assertEqual(self.open_carriers(),
                             sorted([int(first["id"]), int(second["id"])]))
        finally:
            self.assertTrue(lease.release())

    def test_un_porteur_fusionne_ne_reserve_plus(self):
        first = self.add("h-20261105-07")
        for state in ("build", "qa", "merged"):
            work.move(self.db, int(first["id"]), state, actor="pilote")
        self.assertEqual(self.open_carriers(), [])
        second = self.add("h-20261105-08")
        self.assertEqual(self.open_carriers(), [int(second["id"])])

    def test_deux_compositions_simultanees_donnent_un_seul_porteur(self):
        # Le déclencheur ralentit l'insertion du porteur : sans le verrou,
        # les deux créateurs liraient « aucun porteur » puis inséreraient
        # tous les deux.
        self.db.execute(
            "CREATE OR REPLACE FUNCTION %s() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN PERFORM pg_sleep(0.4); RETURN NEW; END $$" % SLOW_FUNCTION)
        self.db.execute(
            "CREATE TRIGGER %s BEFORE INSERT ON work_items FOR EACH ROW"
            " WHEN (NEW.branch LIKE 'integration/paquet%%')"
            " EXECUTE FUNCTION %s()" % (SLOW_TRIGGER, SLOW_FUNCTION))
        self.addCleanup(self._drop_slow)

        barrier = threading.Barrier(2)
        results: list[tuple[str, str]] = []

        def create(name: str) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=15)
                item = self.add(name, db=db)
                results.append(("ok", str(item["id"])))
            except Exception as exc:  # noqa: BLE001 - le perdant est le témoin
                results.append(("error", "%s: %s" % (type(exc).__name__, exc)))
            finally:
                db.close()

        threads = [threading.Thread(target=create, args=(name,))
                   for name in ("h-20261105-31", "h-20261105-32")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        won = [row for row in results if row[0] == "ok"]
        lost = [row for row in results if row[0] == "error"]
        self.assertEqual(len(won), 1, results)
        self.assertEqual(len(lost), 1, results)
        self.assertTrue(
            "composition in progress" in lost[0][1]
            or "already reserves" in lost[0][1], lost)
        carriers = self.open_carriers()
        self.assertEqual(len(carriers), 1, carriers)
        self.assertEqual([int(won[0][1])], carriers)

    def _drop_slow(self) -> None:
        try:
            self.db.execute("DROP TRIGGER IF EXISTS %s ON work_items" % SLOW_TRIGGER)
            self.db.execute("DROP FUNCTION IF EXISTS %s()" % SLOW_FUNCTION)
        except Exception:  # noqa: BLE001 - nettoyage de banc
            pass
