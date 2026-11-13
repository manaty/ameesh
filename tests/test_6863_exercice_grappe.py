# SPDX-License-Identifier: AGPL-3.0-only
"""EP8.6 (#6863) : le fichier de grappe relu de l'exercice réel.

Le fichier se charge par la cible ``grappe`` d'``exercer.py`` (#6902), décrit
la topologie de ``topologie.json`` et ses commandes gardent les propriétés
relues : perte de nœud sans redémarrage par systemd, coupure réseau bornée
par un retour automatique posé avant la coupure, aucun geste sur le poste.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATRONI = ROOT / "deploy" / "serveur" / "patroni"
FICHIER = PATRONI / "exercice-grappe.json"
TOPOLOGIE = PATRONI / "topologie.json"
EXERCER = PATRONI / "banc" / "exercer.py"


def _charger_exercer():
    spec = importlib.util.spec_from_file_location("exercer_6863", EXERCER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExerciceGrappeTest(unittest.TestCase):
    def setUp(self):
        self.conf = json.loads(FICHIER.read_text(encoding="utf-8"))
        self.actions = self.conf["actions"]

    def test_charge_par_la_cible_grappe(self):
        exercer = _charger_exercer()
        cible = exercer.GrappeCible(FICHIER)
        self.assertEqual(set(cible.actions), set(exercer.GrappeCible.ACTIONS))
        self.assertEqual(cible.table_jalon, "public.ep86_jalon")

    def test_meme_topologie_que_patroni(self):
        topo = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        attendu = {(n["nom"], n["adresse"], n["nofailover"]) for n in topo["noeuds"]}
        lu = {(n["nom"], n["adresse"], n["nofailover"]) for n in self.conf["noeuds"]}
        self.assertEqual(lu, attendu)

    def test_perte_de_noeud_sans_redemarrage_systemd(self):
        # Restart=on-failure relancerait Patroni 5 s après le SIGKILL.
        perte = self.actions["perte_noeud"]
        self.assertIn("systemctl kill --signal=SIGKILL patroni.service etcd.service", perte)
        self.assertLess(perte.index("kill"), perte.index("systemctl stop patroni.service"))

    def test_coupure_bornee_par_un_retour_automatique(self):
        coupure = self.actions["coupure_reseau"]
        self.assertTrue(coupure.startswith("sudo -n systemd-run --unit=ep86-retablir --on-active="))
        self.assertLess(coupure.index("systemd-run"), coupure.index("iptables -I"))
        # Chaque règle posée a son retrait, à l'identique, dans le retour
        # automatique et dans le rétablissement.
        for regle in ("INPUT -s 10.78.0.0/24 -m comment --comment ep86 -j DROP",
                      "OUTPUT -d 10.78.0.0/24 -m comment --comment ep86 -j DROP"):
            self.assertIn("iptables -I " + regle.replace("INPUT", "INPUT 1").replace(
                "OUTPUT -d", "OUTPUT 1 -d"), coupure)
            self.assertIn("iptables -D " + regle, coupure)
            self.assertIn("iptables -D " + regle, self.actions["retablissement_reseau"])
        self.assertIn("! sudo -n iptables -S | grep -q ep86", self.actions["retablissement_reseau"])

    def test_aucun_geste_sur_le_poste(self):
        self.assertEqual(self.actions["demarrage_pc"], "true")
        self.assertTrue(self.actions["arret_pc"].startswith("! ping "))
        self.assertIn("10.77.0.2", self.actions["arret_pc"])


if __name__ == "__main__":
    unittest.main()
