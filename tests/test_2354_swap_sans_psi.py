# SPDX-License-Identifier: AGPL-3.0-only
"""#2354 : sans PSI et sans plafond de swap déclaré, l'hôte garde le repli
prudent de 0028 (8 Gio) sur le stock de swap.

#2296 a retiré le plafond de swap par défaut : un stock n'est pas une
pression, et le défaut de 8 Gio faisait un faux « critical pressure » au
démarrage sur un hôte qui mesure PSI. Restait le cas d'un hôte SANS PSI et
SANS `max_swap_used` déclaré : plus que le plancher mémoire (1 Gio), alors
que la mémoire disponible peut rester au-dessus de son plancher pendant que
les pages partent en swap. Le repli de 0028 ne s'applique qu'à ce cas ; un
hôte qui mesure PSI n'a toujours pas de plafond de swap par défaut, et un
plafond déclaré l'emporte toujours (R1 delta #2296, mesh-design, 18/10).
"""
from __future__ import annotations

import unittest

from ameesh import exploitation, resources, storage

from . import test_canon
from .support import PgTestCase

GIB = 1024 ** 3


def limits(**declared):
    return resources.thresholds({"resources": declared})


# ==========================================================================
# le plafond effectif : déclaré, repli sans PSI, ou aucun
# ==========================================================================

class SwapCeilingTest(unittest.TestCase):
    def test_sans_psi_sans_plafond_le_repli_0028_s_applique(self):
        reading = {"swap_used_bytes": 10 * GIB}  # psi_mem_some absent
        self.assertEqual(resources.swap_ceiling(reading, limits()),
                         resources.DEFAULT_MAX_SWAP_USED_WITHOUT_PSI)
        self.assertEqual(resources.DEFAULT_MAX_SWAP_USED_WITHOUT_PSI, 8 * GIB)

    def test_un_plafond_declare_l_emporte_sur_le_repli(self):
        reading = {"swap_used_bytes": 17 * GIB}
        self.assertEqual(resources.swap_ceiling(reading, limits(max_swap_used="16GiB")),
                         16 * GIB)

    def test_un_hote_qui_mesure_psi_n_a_toujours_pas_de_plafond_par_defaut(self):
        # l'invariant de #2296 : un stock de swap n'est pas une pression
        reading = {"swap_used_bytes": 22 * GIB, "psi_mem_some": 0.4}
        self.assertIsNone(resources.swap_ceiling(reading, limits()))

    def test_psi_zero_est_une_mesure_pas_une_absence(self):
        # 0.0 % est une vraie mesure : pas de repli, pas de plafond par défaut
        reading = {"swap_used_bytes": 22 * GIB, "psi_mem_some": 0.0}
        self.assertIsNone(resources.swap_ceiling(reading, limits()))


# ==========================================================================
# le repli dans le verdict d'admission
# ==========================================================================

class SwapFallbackBreachTest(unittest.TestCase):
    def test_le_repli_franchit_le_plafond_de_0028(self):
        found = resources.breaches({"swap_used_bytes": 10 * GIB}, limits())
        self.assertEqual([(b["key"], b["limit"]) for b in found],
                         [("max_swap_used", 8 * GIB)])
        self.assertIn("no PSI", found[0]["label"])
        self.assertFalse(found[0]["critical"])

    def test_le_repli_reste_sous_le_plafond(self):
        self.assertEqual(resources.breaches({"swap_used_bytes": 7 * GIB}, limits()), [])

    def test_le_repli_est_critique_au_double(self):
        found = resources.breaches({"swap_used_bytes": 17 * GIB}, limits())
        self.assertEqual([b["key"] for b in found], ["max_swap_used"])
        self.assertTrue(found[0]["critical"])

    def test_un_plafond_declare_garde_son_libelle_et_sa_valeur(self):
        found = resources.breaches({"swap_used_bytes": 17 * GIB},
                                   limits(max_swap_used="16GiB"))
        self.assertEqual([(b["key"], b["label"], b["limit"]) for b in found],
                         [("max_swap_used", "swap used", 16 * GIB)])

    def test_un_hote_avec_psi_ne_plafonne_pas_le_stock(self):
        # #2296 tient toujours : ni repli ni plafond déclaré, le stock seul ne
        # retient pas les tours.
        reading = {"swap_used_bytes": 22 * GIB, "psi_mem_some": 0.4,
                   "mem_available_bytes": 6 * GIB}
        self.assertEqual(resources.breaches(reading, limits()), [])

    def test_le_repli_bloque_la_porte_sans_psi(self):
        verdict = resources.pressure({"swap_used_bytes": 9 * GIB}, limits=limits())
        self.assertTrue(verdict["blocked"])
        self.assertFalse(verdict["critical"])
        self.assertEqual([b["key"] for b in verdict["breaches"]], ["max_swap_used"])


# ==========================================================================
# l'alerte d'exploitation (base réelle)
# ==========================================================================

class HostPressureFallbackAlertTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_sans_psi_l_alerte_nomme_le_repli(self):
        storage.of(self.db).hosts.record({
            "host": "atelier", "mem_available_bytes": 8 * GIB,
            "swap_used_bytes": 10 * GIB,  # psi_mem_some absent
            "load1": 1.0, "cpu_count": 8, "disk_free_bytes": 100 * GIB,
            "disk_path": "/tmp", "turns_in_progress": 0})
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "host_pressure"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["breaches"], ["max_swap_used"])
        self.assertIn("no PSI", alertes[0]["detail"])
        self.assertFalse(alertes[0]["critical"])

    def test_avec_psi_l_alerte_ne_vient_pas_du_stock(self):
        storage.of(self.db).hosts.record({
            "host": "atelier", "mem_available_bytes": 8 * GIB,
            "swap_used_bytes": 10 * GIB, "psi_mem_some": 0.4,
            "load1": 1.0, "cpu_count": 8, "disk_free_bytes": 100 * GIB,
            "disk_path": "/tmp", "turns_in_progress": 0})
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "host_pressure"]
        self.assertEqual(alertes, [])


# ==========================================================================
# ce que `ameesh hosts` montre (base réelle)
# ==========================================================================

class HostsSwapFallbackTest(test_canon._CanonDbCase):
    def reading(self, **over):
        row = {"host": "atelier", "mem_available_bytes": 8 * GIB,
               "swap_used_bytes": 0, "load1": 0.1, "cpu_count": 8,
               "disk_free_bytes": 100 * GIB, "disk_path": "/tmp",
               "turns_in_progress": 0}
        row.update(over)
        return row

    def hosts(self):
        proc = self.mesh("hosts", "atelier", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_sans_psi_l_hote_affiche_le_repli(self):
        storage.of(self.db).hosts.record(self.reading())
        out = self.hosts()
        self.assertIn("swap ≤ 8.0 GiB [fallback: no PSI]", out)
        self.assertIn("counts alone, no PSI", out)

    def test_avec_psi_l_hote_n_a_pas_de_plafond_par_defaut(self):
        storage.of(self.db).hosts.record(self.reading(psi_mem_some=0.4))
        out = self.hosts()
        # « — [défaut] » prétendrait un défaut qui n'existe pas
        self.assertIn("swap ≤ — [no default]", out)
        self.assertIn("counts only under memory pressure", out)
        self.assertNotIn("[fallback: no PSI]", out)
