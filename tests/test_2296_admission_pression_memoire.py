# SPDX-License-Identifier: AGPL-3.0-only
"""#2296 : l'admission suit la pression mémoire réelle (PSI, mémoire
disponible) ; le swap utilisé n'est plus un critère à lui seul.

Constat (pc-smichea, 17/10 21:50) : swap utilisé 22.3 GiB au-dessus du seuil
de 16 GiB, mémoire disponible 6.2 GiB, PSI mémoire 2.4 % — la porte restait
en PRESSURE pendant des heures et retenait les tours Codex, alors que la
pression réelle était retombée. Le swap utilisé est un STOCK : les pages y
restent tant qu'on ne les touche pas, donc un plafond sur ce stock ne se
rouvre jamais de lui-même.

Le critère d'admission devient la pression mémoire (PSI `memory some` 60 s),
avec `min_mem_available` ; le plafond de swap ne compte plus que corroboré
par une pression réelle, et garde son sens d'avant sur un hôte qui ne sait
pas mesurer PSI. Le seuil `max_mem_pressure` a un défaut prudent : une fiche
qui ne le déclare pas ne retire pas la garde (`resources.thresholds`).
"""
from __future__ import annotations

import unittest

from ameesh import canon, exploitation, resources, storage

from . import test_canon
from .support import PgTestCase
from .test_l31_pressure import set_host_resources

GIB = 1024 ** 3


def limits(**declared):
    return resources.thresholds({"resources": declared})


# ==========================================================================
# le stock de swap n'est pas une pression
# ==========================================================================

class SwapStockTest(unittest.TestCase):
    def test_l_incident_pc_smichea_n_est_plus_une_pression(self):
        # le relevé du constat : swap au-dessus du plafond, pression réelle
        # basse (mémoire disponible et PSI)
        verdict = resources.pressure({
            "mem_available_bytes": int(6.2 * GIB),
            "swap_used_bytes": int(22.3 * GIB),
            "psi_mem_some": 2.4,
            "load1": 8.0,
            "disk_free_bytes": 100 * GIB,
        }, limits(max_swap_used="16GiB"))
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["breaches"], [])

    def test_le_plafond_de_swap_compte_avec_une_pression_reelle(self):
        lecture = {"swap_used_bytes": int(17.5 * GIB), "psi_mem_some": 6.0,
                   "mem_available_bytes": 8 * GIB}
        found = {b["key"]: b for b in resources.breaches(
            lecture, limits(max_swap_used="16GiB"))}
        self.assertEqual(set(found), {"max_swap_used"})
        self.assertFalse(found["max_swap_used"]["critical"])

    def test_le_plafond_de_swap_ignore_une_pression_sous_la_moitie(self):
        # 4 % < 10 % / 2 : le stock de swap ne retient pas
        lecture = {"swap_used_bytes": int(17.5 * GIB), "psi_mem_some": 4.0,
                   "mem_available_bytes": 8 * GIB}
        self.assertEqual(resources.breaches(lecture, limits(max_swap_used="16GiB")), [])

    def test_sans_psi_le_plafond_de_swap_garde_son_sens(self):
        # un hôte qui ne mesure pas PSI ne doit pas ouvrir la porte sur le
        # seul stock : le plafond garde son sens d'avant (pas de signal de
        # pression réelle, pas d'ouverture)
        found = resources.breaches({"swap_used_bytes": int(17.5 * GIB)},
                                   limits(max_swap_used="16GiB"))
        self.assertEqual([b["key"] for b in found], ["max_swap_used"])

    def test_un_seuil_absent_garde_le_defaut_prudent(self):
        # le défaut de swap (8 GiB) s'applique quand la fiche se tait, et une
        # pression réelle le fait compter : l'absence de seuil n'ouvre pas
        found = resources.breaches(
            {"swap_used_bytes": resources.DEFAULT_MAX_SWAP_USED + 1,
             "psi_mem_some": 20.0},
            resources.thresholds(None))
        self.assertIn("max_swap_used", [b["key"] for b in found])

    def test_le_plafond_de_swap_reste_critique_au_double(self):
        found = resources.breaches(
            {"swap_used_bytes": int(33 * GIB), "psi_mem_some": 6.0,
             "mem_available_bytes": 8 * GIB},
            limits(max_swap_used="16GiB"))
        self.assertTrue(found[0]["critical"])


# ==========================================================================
# le critère d'admission : PSI mémoire
# ==========================================================================

class MemoryPressureTest(unittest.TestCase):
    def test_un_seuil_absent_n_ouvre_pas_la_porte(self):
        # politique incomplète ou seuil illisible : le défaut prudent reste
        for policy in (None, {}, {"resources": {}},
                       {"resources": {"max_mem_pressure": "nope"}}):
            with self.subTest(policy=policy):
                self.assertEqual(resources.thresholds(policy)["max_mem_pressure"],
                                 resources.DEFAULT_MAX_MEM_PRESSURE)
        # et une pression mémoire au-dessus du défaut bloque sans fiche
        verdict = resources.pressure(
            {"psi_mem_some": resources.DEFAULT_MAX_MEM_PRESSURE + 1})
        self.assertTrue(verdict["blocked"])
        self.assertEqual([b["key"] for b in verdict["breaches"]], ["max_mem_pressure"])

    def test_le_seuil_declare_est_lu(self):
        self.assertEqual(limits(max_mem_pressure="20%")["max_mem_pressure"], 20.0)
        found = resources.breaches({"psi_mem_some": 21.0}, limits(max_mem_pressure="20%"))
        self.assertEqual([(b["key"], b["critical"]) for b in found],
                         [("max_mem_pressure", False)])

    def test_une_mesure_absente_ne_declenche_rien(self):
        # PSI non mesuré : on ne devine pas (même règle que les autres seuils)
        self.assertEqual(resources.breaches({"psi_mem_some": None},
                                            limits(max_mem_pressure="1%")), [])

    def test_critique_a_mi_chemin_du_plein(self):
        found = resources.breaches({"psi_mem_some": 54.9},
                                   limits(max_mem_pressure="10%"))
        self.assertFalse(found[0]["critical"])
        found = resources.breaches({"psi_mem_some": 55.0},
                                   limits(max_mem_pressure="10%"))
        self.assertTrue(found[0]["critical"])

    def test_la_pression_memoire_ne_remplace_pas_le_plancher(self):
        verdict = resources.pressure({"mem_available_bytes": 1, "psi_mem_some": 0.0})
        self.assertEqual([b["key"] for b in verdict["breaches"]], ["min_mem_available"])

    def test_l_ancien_swap_seul_ne_bloque_plus_le_runner(self):
        # le verdict que lit `AgentWorker._held_by_pressure` : ce qui compte
        # est `blocked`, et il reste faux sur le relevé du constat
        verdict = resources.pressure({
            "mem_available_bytes": int(6.9 * GIB),
            "swap_used_bytes": int(22.3 * GIB),
            "psi_mem_some": 2.4,
        }, limits(max_swap_used="16GiB"))
        self.assertFalse(verdict["blocked"])
        self.assertFalse(verdict["critical"])


# ==========================================================================
# limites physiques : le plus strict des plafonds déclarés
# ==========================================================================

class LimitsTest(unittest.TestCase):
    class Fiche:
        def __init__(self, value):
            self.policy = type("P", (), {"resources": {"max_mem_pressure": value},
                                         "max_agents": None})()

    class Canon:
        def __init__(self, ident, value):
            self.id, self.fiche = ident, LimitsTest.Fiche(value)

        def host(self, name):
            return self.fiche

    def test_le_plus_strict_des_plafonds_declares(self):
        got = resources.host_limits([self.Canon("a", "20%"), self.Canon("t", "5%")],
                                    "atelier")
        self.assertEqual(got["limits"]["max_mem_pressure"], 5.0)
        self.assertEqual(got["origin"]["max_mem_pressure"], "t")

    def test_un_seuil_declare_plus_large_n_est_pas_ecrase_par_le_defaut(self):
        got = resources.host_limits([self.Canon("a", "30%")], "atelier")
        self.assertEqual(got["limits"]["max_mem_pressure"], 30.0)
        self.assertEqual(got["origin"]["max_mem_pressure"], "a")


class CanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def test_le_seuil_memoire_est_lu_et_valide(self):
        set_host_resources(self.root, max_mem_pressure="20%")
        loaded = canon.load(self.root, untrusted=True)
        self.assertEqual(loaded.host("atelier").policy.resources,
                         {"max_mem_pressure": 20.0})
        self.assertNotIn("host-policy-invalid",
                         [f.code for f in canon.validate(loaded)])

    def test_un_pourcentage_hors_bornes_est_refuse(self):
        set_host_resources(self.root, max_mem_pressure="150%")
        loaded = canon.load(self.root, untrusted=True)
        self.assertIn("host-policy-invalid",
                      [f.code for f in canon.validate(loaded)])


# ==========================================================================
# alertes et affichage (base réelle)
# ==========================================================================

class HostPressureAlertTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def reading(self, **over):
        # 10 GiB dépasse le plafond par défaut (8 GiB) sans en atteindre le
        # double : le stock seul ne doit plus alerter
        row = {"host": "atelier", "mem_available_bytes": 6 * GIB,
               "swap_used_bytes": 10 * GIB, "psi_mem_some": 2.4,
               "load1": 1.0, "cpu_count": 8, "disk_free_bytes": 100 * GIB,
               "disk_path": "/tmp", "turns_in_progress": 0}
        row.update(over)
        return row

    def test_un_swap_haut_sans_pression_ne_leve_plus_d_alerte(self):
        storage.of(self.db).hosts.record(self.reading())
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "host_pressure"]
        self.assertEqual(alertes, [])

    def test_une_pression_memoire_reelle_leve_l_alerte(self):
        storage.of(self.db).hosts.record(self.reading(psi_mem_some=12.0,
                                                      swap_used_bytes=0))
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "host_pressure"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["breaches"], ["max_mem_pressure"])
        self.assertFalse(alertes[0]["critical"])


class HostsThresholdsTest(test_canon._CanonDbCase):
    def test_les_seuils_affichent_la_pression_memoire(self):
        storage.of(self.db).hosts.record({
            "host": "atelier", "mem_available_bytes": 8 * GIB,
            "swap_used_bytes": 0, "psi_mem_some": 0.0, "load1": 0.1,
            "cpu_count": 8, "disk_free_bytes": 100 * GIB, "disk_path": "/tmp",
            "turns_in_progress": 0})
        proc = self.mesh("hosts", "atelier", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("memory pressure ≤ 10.0 %", proc.stdout)
        self.assertIn("counts only under memory pressure", proc.stdout)
