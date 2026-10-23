# SPDX-License-Identifier: AGPL-3.0-only
"""#3123 : un geste ne garde pas un pilote volatil 300 s après coup.

#3115 a rendu le renouvellement non raccourcissant (`GREATEST`) et fait
reprendre à `Gate.hold` la durée du bail de l'hôte. Mais `hold` gardait aussi
son minimum générique de 300 s (`ttl_s`) : sur un hôte volatil (règle de
90 s), un geste posait donc 300 s, et si l'hôte disparaissait juste après, la
bascule du rôle attendait 300 s au lieu de 90 s.

Le correctif : quand une fiche Host règle la durée, c'est elle qui s'applique
(seule la marge d'un appel externe peut l'allonger) ; `ttl_s` ne couvre que
l'hôte dont aucune fiche ne règle le bail.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import time
import types
import unittest
from unittest import mock

from ameesh import canon, pilot_gesture, registry
from ameesh.mediated_executor import host_gate as host_gate_mod

from .support import PgTestCase

HOST = "h1"
NAME = "p3123"
RUNNER = "runner-h1"
IDENTITY_KEYS = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                 "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                 "AMEESH_ROLE", "AMEESH_ROLE_HOST", "AMEESH_LEASE_OWNER")


def host_canon(*, lease_ttl: float | None = None, volatile: bool = False,
               name: str = HOST) -> types.SimpleNamespace:
    """Le strict nécessaire de `Canon.host` pour `lease_ttl_rule`."""
    policy = types.SimpleNamespace(lease_ttl=lease_ttl, volatile=volatile)
    fiche = types.SimpleNamespace(policy=policy)
    return types.SimpleNamespace(
        host=lambda host: fiche if host == name else None)


class LeaseTtlRuleTest(unittest.TestCase):
    """La règle d'hôte se distingue de l'absence de règle (#3123)."""

    def test_aucune_fiche_rend_none(self):
        self.assertIsNone(host_gate_mod.lease_ttl_rule([], HOST))

    def test_fiche_sans_policy_rend_none(self):
        canon = types.SimpleNamespace(host=lambda host: None)
        self.assertIsNone(host_gate_mod.lease_ttl_rule([canon], HOST))

    def test_fiche_volatile_rend_90_s(self):
        self.assertEqual(
            host_gate_mod.lease_ttl_rule([host_canon(volatile=True)], HOST), 90.0)

    def test_fiche_declaree_rend_sa_duree(self):
        self.assertEqual(
            host_gate_mod.lease_ttl_rule([host_canon(lease_ttl=1200.0)], HOST),
            1200.0)

    def test_le_plus_court_des_canons_gagne(self):
        canons = [host_canon(lease_ttl=1200.0), host_canon(lease_ttl=45.0)]
        self.assertEqual(host_gate_mod.lease_ttl_rule(canons, HOST), 45.0)

    def test_la_fiche_d_un_autre_hote_ne_regle_rien(self):
        self.assertIsNone(
            host_gate_mod.lease_ttl_rule([host_canon(volatile=True)], "autre"))

    def test_lease_ttl_for_garde_son_contrat(self):
        # Le runner continue de lire la même durée, règle d'hôte ou défaut.
        self.assertEqual(
            host_gate_mod.lease_ttl_for([host_canon(volatile=True)], HOST, 300.0),
            90.0)
        self.assertEqual(host_gate_mod.lease_ttl_for([], HOST, 300.0), 300.0)


class VolatileHoldTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, NAME, harness="deepseek", host=HOST,
                        cwd=self.tmp, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', candidats = ARRAY[%s, %s] "
            " WHERE name = %s", (HOST, "h2", NAME))
        lease = registry.claim(self.db, NAME, RUNNER, 300, host=HOST)
        self.assertIsNotNone(lease)
        self.epoch = int(lease["lease_epoch"])

    # -- helpers ---------------------------------------------------------
    @contextlib.contextmanager
    def live(self, **extra: str):
        """Pose l'identité d'une session, sans hériter celle du lanceur."""
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            os.environ.update({
                "AGENT_MAIL_NAME": NAME,
                "AMEESH_RUNNER_ID": RUNNER,
                "AMEESH_LEASE_EPOCH": str(self.epoch),
            })
            os.environ.update(extra)
            yield

    def remaining(self) -> float:
        """Secondes de bail restantes, selon l'horloge de la base."""
        row = self.db.query(
            "SELECT extract(epoch from lease_expires_at)::float8 AS ts "
            "FROM agent_registry WHERE name = %s", (NAME,))[0]
        return float(row["ts"]) - time.time()

    def shrink(self, seconds: float) -> None:
        """Pose l'échéance comme le battement du runner sur cet hôte.

        `renew` ne raccourcit jamais (#3115) : le bail de `setUp` (300 s) doit
        être ramené à la règle de l'hôte avant de mesurer ce que `hold` pose.
        """
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = clock_timestamp() "
            "+ make_interval(secs => %s) WHERE name = %s", (seconds, NAME))

    @contextlib.contextmanager
    def host_rule(self, *, lease_ttl: float | None = None,
                  volatile: bool = False):
        with mock.patch.object(
                canon, "load_configured",
                return_value=[host_canon(lease_ttl=lease_ttl,
                                         volatile=volatile)]):
            yield

    def gate(self, gesture: str = "assign", ref: str = "#3123",
             cfg=None) -> pilot_gesture.Gate:
        with self.live():
            fence = pilot_gesture.gate(cfg or self.cfg, self.db, gesture, ref)
        self.assertIsNotNone(fence)
        return fence

    def heartbeat(self, ttl: float) -> None:
        """Le battement du runner, qui renouvelle la règle de l'hôte."""
        self.assertIsNotNone(registry.renew(self.db, NAME, RUNNER, self.epoch, ttl))

    # -- l'hôte volatil n'est pas gardé 300 s ------------------------------
    def test_hold_volatil_ne_prend_pas_le_defaut_de_300_s(self):
        with self.host_rule(volatile=True):
            fence = self.gate()
        self.assertEqual(fence.lease_ttl, 90.0)
        self.assertEqual(fence.host_lease_ttl, 90.0)
        self.shrink(90.0)
        with self.live():
            fence.hold(self.db)
        self.assertGreater(self.remaining(), 60.0)
        self.assertLess(self.remaining(), 120.0)

    def test_hold_volatil_ignore_un_ttl_s_plus_long(self):
        # #3123 : `ttl_s` ne couvre que l'hôte sans règle connue.
        with self.host_rule(volatile=True):
            fence = self.gate()
        self.shrink(90.0)
        with self.live():
            fence.hold(self.db, ttl_s=600.0)
        self.assertGreater(self.remaining(), 60.0)
        self.assertLess(self.remaining(), 120.0)

    def test_hold_volatil_puis_battement_reste_sous_90_s(self):
        # Témoin du lot : hold puis heartbeat, l'échéance reste ≤ 90 s (+ marge
        # de mesure), elle n'est pas remontée à 300 s par le GREATEST.
        with self.host_rule(volatile=True):
            fence = self.gate()
        self.shrink(90.0)
        with self.live():
            fence.hold(self.db)
        self.heartbeat(90.0)
        self.assertLess(self.remaining(), 90.0 + 30.0)

    def test_la_marge_d_un_appel_externe_etend_au_dela_du_bail_volatil(self):
        # Un appel dont le pire cas dépasse la règle de l'hôte garde sa marge.
        with self.host_rule(volatile=True):
            fence = self.gate(gesture="merge", ref="#3123 merge")
        self.shrink(90.0)
        with self.live():
            fence.hold(self.db, margin_s=120.0)
        self.assertGreater(self.remaining(), 120.0)
        self.assertLess(self.remaining(), 90.0 + 120.0 + 30.0)

    def test_hold_volatil_du_timer_de_role(self):
        # Le chemin EP5 (`AMEESH_ROLE`) applique la même règle.
        with self.host_rule(volatile=True):
            with self.live(AMEESH_ROLE=NAME):
                fence = pilot_gesture.gate(self.cfg, self.db, "decision", "#3123")
                self.assertIsNotNone(fence)
                self.assertEqual(fence.host_lease_ttl, 90.0)
                self.shrink(90.0)
                fence.hold(self.db)
        self.assertGreater(self.remaining(), 60.0)
        self.assertLess(self.remaining(), 120.0)

    # -- les cas de #3115 restent tenus ------------------------------------
    def test_hold_sur_fiche_hote_de_1200_s_garde_1200_s(self):
        with self.host_rule(lease_ttl=1200.0):
            fence = self.gate(gesture="merge", ref="#3123 merge")
        self.assertEqual(fence.lease_ttl, 1200.0)
        self.assertEqual(fence.host_lease_ttl, 1200.0)
        with self.live():
            fence.hold(self.db, ttl_s=300.0)
        self.assertGreater(self.remaining(), 1170.0)
        self.assertLess(self.remaining(), 1230.0)

    def test_hold_sans_regle_hote_garde_le_defaut(self):
        # Aucune fiche Host : le geste garde le comportement d'avant, 300 s.
        cfg = dataclasses.replace(self.cfg, lease_ttl=300.0)
        with mock.patch.object(canon, "load_configured", return_value=[]):
            fence = self.gate(cfg=cfg)
        self.assertIsNone(fence.host_lease_ttl)
        with self.live():
            fence.hold(self.db)
        self.assertGreater(self.remaining(), 270.0)
        self.assertLess(self.remaining(), 330.0)

    def test_hold_sans_regle_hote_prend_la_configuration(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=600.0)
        with mock.patch.object(canon, "load_configured", return_value=[]):
            fence = self.gate(cfg=cfg)
        self.assertEqual(fence.lease_ttl, 600.0)
        self.assertIsNone(fence.host_lease_ttl)
        with self.live():
            fence.hold(self.db)
        self.assertGreater(self.remaining(), 570.0)
        self.assertLess(self.remaining(), 630.0)

    def test_hold_avec_canon_illisible_garde_la_configuration(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=600.0)
        with mock.patch.object(canon, "load_configured",
                               side_effect=RuntimeError("canon illisible")):
            fence = self.gate(cfg=cfg)
        self.assertIsNone(fence.host_lease_ttl)
        with self.live():
            fence.hold(self.db)
        self.assertGreater(self.remaining(), 570.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
