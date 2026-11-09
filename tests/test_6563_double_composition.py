# SPDX-License-Identifier: AGPL-3.0-only
"""#6563 : un seul composeur par porteur, y compris quand l'agent désigné
reprend un porteur créé par le tic.

Le 09/11/2026 (h-17, #6562), l'alerte de créneau a désigné mty-amh-impl-01
pendant que le tic venait de créer le porteur. impl-01 a composé 4 unités dans
son clone et publié `cd5d5e859b57` à 16:50Z ; le tic, qui ne voyait que son
`flock` local (autre clone), a relancé une composition de 7 unités sur le même
porteur (`92f21b21f4db`, poussée refusée). Le verrou de #5941 ne couvrait que
« décider et créer ».

Témoins : un bail par porteur, distinct de la clé projet, est pris par
`paquet-auto` pour toute composition réelle et lu par le tic avant toute
reprise ; une composition seulement enregistrée (notes `packet composition
recorded`) ou une tête poussée sans gel n'est jamais réécrite par le tic ;
après publication, le tic demande la ci-full de la même tête et ne lance aucun
second composeur. Le détenteur ne se contente pas de la vie de son enfant
(vivant après SIGSTOP, ou renouvellement bloqué) : une preuve fraîche bornée,
propriétaire et époque relus en base, est exigée avant chaque mutation ; un
bail expiré puis repris est refusé.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from unittest import mock

from ameesh import packet_composition, work

from .support import PgTestCase
from .test_3807_paquet_auto import paquet_auto
from .test_3845_paquet_horaire import HEAD, HEAD2, NOW, Base, carrier, ph

BRANCH = "integration/paquet-h-20261027-04"
EARLY_PUSH = ("paquet h-20261027-04 poussé par scripts/paquet-auto.py dès la "
              "composition (canon §6, #4304) : 1 unité")


class TickClaimTest(Base):
    """Le tic ne relance pas une composition qu'un autre a commencée."""

    def item(self, **overrides) -> dict:
        item = carrier(lot=4000, sha="", created=NOW - 900)
        item.update(overrides)
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def autos(self) -> list[list[str]]:
        return [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)]

    def test_bail_du_porteur_tenu_par_un_autre_composeur(self):
        # impl-01 compose dans son clone : le bail par porteur est partagé,
        # le flock local ne le voit pas.
        self.item()
        self.ctx.carrier_claim = lambda carrier: {
            "owner": "paquet-auto:agents-1:42:abc", "left_s": 200.0}
        out = ph.tick(self.ctx)
        self.assertIn("composition en cours (paquet-auto:agents-1:42:abc)", out)
        self.assertEqual(self.autos(), [])
        self.assertEqual(self.shell.ran("work", "note"), [])

    def test_composition_enregistree_par_un_autre_composeur(self):
        # Avant son gel, une composition est visible par ses unités
        # enregistrées ; le tic ne la réécrit pas.
        self.item(events=[{
            "note": "packet composition recorded: 4 unit(s) 65c82625..cd5d5e85 "
                    "(branch %s)" % BRANCH,
            "created_ts": NOW - 120}])
        self.ctx.carrier_claim = lambda carrier: None
        out = ph.tick(self.ctx)
        self.assertIn("composition enregistrée par un autre composeur", out)
        self.assertEqual(self.autos(), [])

    def test_composition_enregistree_ancienne_ne_bloque_plus(self):
        # Une composition morte, sans poussée ni gel, rend la main au tic.
        self.item(events=[{
            "note": "packet composition recorded: 1 unit(s) 65c82625..abcdef01 "
                    "(branch %s)" % BRANCH,
            "created_ts": NOW - ph.ATTEMPT_STALE - 1}])
        self.ctx.carrier_claim = lambda carrier: None
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        self.assertEqual(len(self.autos()), 1)

    def test_tete_publiee_sans_gel_n_est_pas_reecrite(self):
        self.item(branch_head=HEAD2)
        self.ctx.carrier_claim = lambda carrier: None
        out = ph.tick(self.ctx)
        self.assertIn("publiée sans gel", out)
        self.assertEqual(self.autos(), [])

    def test_apres_publication_une_seule_tete_et_la_ci_full_de_cette_tete(self):
        # Le scénario du constat : l'agent désigné tient le bail, puis publie
        # et gèle sa tête. Le tic demande la ci-full de CETTE tête ; il ne
        # lance jamais un second `paquet-auto`.
        item = self.item()
        self.ctx.carrier_claim = lambda carrier: {
            "owner": "paquet-auto:agents-1:42:abc"}
        self.assertIn("composition en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

        item["freeze"] = {"declared": {"sha": HEAD2, "at_ts": NOW - 100, "id": 7}}
        item["milestones"] = [{
            "id": 7, "kind": "frozen", "sha": HEAD2, "at_ts": NOW - 100,
            "note": EARLY_PUSH}]
        item["branch_head"] = HEAD2
        item["events"] = [{
            "note": "packet composition recorded: 4 unit(s) 65c82625..cd5d5e85 "
                    "(branch %s)" % BRANCH,
            "created_ts": NOW - 200}]
        self.ctx.carrier_claim = lambda carrier: None
        out = ph.tick(self.ctx)
        self.assertIn("ci-full demandée", out)
        self.assertEqual(self.autos(), [])
        ci = self.shell.ran("ci", "run", "4000", "--full")[0]
        self.assertEqual(ci[ci.index("--sha") + 1], HEAD2)
        # Une seule tête publiée, et c'est celle du composeur désigné.
        self.assertEqual(item["freeze"]["declared"]["sha"], HEAD2)
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_bail_illisible_aucun_geste(self):
        self.item()
        self.ctx.carrier_claim = mock.Mock(side_effect=RuntimeError("base absente"))
        with self.assertRaisesRegex(ph.HoraireError, "illisible"):
            ph.tick(self.ctx)
        self.assertEqual(self.autos(), [])


class CarrierLeaseTest(PgTestCase):
    """Le bail par porteur, distinct du bail projet (#5941)."""

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

    def test_bail_par_porteur_distinct_et_exclusif(self):
        project = packet_composition.Lease(
            self.db, "ameesh", owner="projet:1", ttl_s=60)
        carrier = packet_composition.Lease(
            self.db, "ameesh", owner="porteur:1", ttl_s=60, carrier=4000)
        self.assertTrue(project.acquire())
        self.assertTrue(carrier.acquire())
        self.assertEqual(packet_composition.holder(self.db, "ameesh")["owner"],
                         "projet:1")
        held = packet_composition.holder(self.db, "ameesh", carrier=4000)
        self.assertEqual(held["owner"], "porteur:1")
        # Un second composeur du même porteur est refusé, même avec un autre
        # nom de paquet : la clé nomme le porteur, pas la composition.
        other = packet_composition.Lease(
            self.db, "ameesh", owner="porteur:2", ttl_s=60, carrier=4000)
        self.assertFalse(other.acquire())
        self.assertIn("porteur:1", other.describe())
        # La paire parallèle #3256 C garde deux porteurs indépendants.
        peer = packet_composition.Lease(
            self.db, "ameesh", owner="porteur:3", ttl_s=60, carrier=4001)
        self.assertTrue(peer.acquire())
        self.assertTrue(carrier.renew())
        self.assertTrue(carrier.release())
        self.assertIsNone(packet_composition.holder(self.db, "ameesh", carrier=4000))
        self.assertTrue(peer.release())
        self.assertTrue(project.release())

    def test_hold_cli_prend_et_rend_le_bail(self):
        item = work.add(
            self.db, title="Paquet horaire h-20261027-04 — composition",
            branch="integration/paquet-h-20261027-04", branch_target="main",
            app="ameesh", source="paquet-horaire (#3845), minuterie", cfg=self.cfg)
        carrier_id = int(item["id"])
        first = subprocess.Popen(
            [sys.executable, "-m", "ameesh.main", "work", "packet", "hold",
             str(carrier_id), "--json", "--ttl", "60"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=self.tmp, env=self.env())
        try:
            line = first.stdout.readline().strip()
            data = json.loads(line)
            self.assertEqual(data["key"],
                             "packet-compose:carrier:ameesh:%d" % carrier_id)
            self.assertGreater(data["epoch"], 0)
            # Un second composeur est refusé pendant que le premier tient.
            second = subprocess.run(
                [sys.executable, "-m", "ameesh.main", "work", "packet",
                 "hold", str(carrier_id), "--json", "--ttl", "60"],
                input="", capture_output=True, text=True, timeout=60,
                env=self.env(), cwd=self.tmp)
            self.assertEqual(second.returncode, 3, second.stderr)
            self.assertTrue(json.loads(second.stdout)["busy"])
        finally:
            first.stdin.close()
            self.assertEqual(first.wait(timeout=10), 0)
        self.assertIsNone(packet_composition.holder(
            self.db, "ameesh", carrier=carrier_id))

    def _carrier(self) -> int:
        item = work.add(
            self.db, title="Paquet horaire h-20261027-04 — composition",
            branch=BRANCH, branch_target="main", app="ameesh",
            source="paquet-horaire (#3845), minuterie", cfg=self.cfg)
        return int(item["id"])

    def _claim(self, carrier_id: int) -> "paquet_auto.CompositionClaim":
        return paquet_auto.CompositionClaim(
            self.tmp, carrier_id, "h-20261027-04",
            cli=[sys.executable, "-m", "ameesh.main"], env=self.env())

    @staticmethod
    def _idle_child() -> subprocess.Popen:
        """Un enfant vivant qui ne tient aucun bail (le SIGSTOP simulé)."""
        return subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

    def test_claim_start_prend_et_rend_le_bail(self):
        carrier_id = self._carrier()
        claim = self._claim(carrier_id).start()
        try:
            self.assertGreater(claim.epoch, 0)
            self.assertEqual(packet_composition.holder(
                self.db, "ameesh", carrier=carrier_id)["owner"], claim.owner)
            claim.require()
        finally:
            claim.stop()
        self.assertIsNone(packet_composition.holder(
            self.db, "ameesh", carrier=carrier_id))

    def test_preuve_fraiche_refuse_bail_repris_malgre_enfant_vivant(self):
        carrier_id = self._carrier()
        claim = self._claim(carrier_id)
        lease = packet_composition.Lease(
            self.db, "ameesh", owner=claim.owner, ttl_s=1.0, carrier=carrier_id)
        self.assertTrue(lease.acquire())
        claim.epoch = lease.epoch
        claim.proc = self._idle_child()
        try:
            # bail encore à nous, enfant vivant : la preuve passe
            self.assertIsNone(claim.require())
            # l'enfant vit toujours, mais le bail expire puis est repris
            time.sleep(1.3)
            other = packet_composition.Lease(
                self.db, "ameesh", owner="autre:composeur", ttl_s=60,
                carrier=carrier_id)
            self.assertTrue(other.acquire())
            with self.assertRaises(paquet_auto.EpochLost):
                claim.require()
            self.assertTrue(other.release())
        finally:
            claim.stop()

    def test_preuve_bornee_refuse_timeout_malgre_enfant_vivant(self):
        carrier_id = self._carrier()
        claim = self._claim(carrier_id)
        claim.epoch = 1
        claim.proc = self._idle_child()
        try:
            with mock.patch.object(
                    paquet_auto.subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("check", 30)):
                with self.assertRaises(paquet_auto.EpochLost):
                    claim.require()
        finally:
            claim.stop()

    def test_check_cli_prouve_ou_refuse_le_bail(self):
        carrier_id = self._carrier()
        lease = packet_composition.Lease(
            self.db, "ameesh", owner="preuve:1", ttl_s=60, carrier=carrier_id)
        self.assertTrue(lease.acquire())
        try:
            ok = self.mesh("work", "packet", "check", str(carrier_id),
                           "--json", "--owner", "preuve:1",
                           "--epoch", str(lease.epoch))
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            data = json.loads(ok.stdout)
            self.assertTrue(data["match"])
            self.assertEqual(data["owner"], "preuve:1")
            self.assertEqual(data["epoch"], lease.epoch)
            bad = self.mesh("work", "packet", "check", str(carrier_id),
                            "--json", "--owner", "autre:1",
                            "--epoch", str(lease.epoch))
            self.assertEqual(bad.returncode, 1)
            self.assertFalse(json.loads(bad.stdout)["match"])
        finally:
            self.assertTrue(lease.release())


if __name__ == "__main__":
    unittest.main()
