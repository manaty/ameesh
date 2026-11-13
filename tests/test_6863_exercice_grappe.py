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


class _FausseSurveillance:
    """Surveillance sans réseau : un nouveau primaire et un retour immédiats."""

    def __init__(self, cible, intervalle=0.5):
        pass

    def start(self):
        pass

    def arreter(self):
        pass

    def attendre_nouveau_primaire(self, ancien, timeout):
        return "ameesh-agents-2", 12.0

    def attendre_role(self, nom, attendu, timeout):
        return "t", 5.0

    def primaires_simultanes(self):
        return 0.0, []


class PosteHorsScenarioTest(unittest.TestCase):
    """B de cx2 (#216056) : sans écrivain, une panne de grappe ne touche pas au
    poste. L'action du poste (ici « le PC ne répond pas ») n'appartient qu'au
    scénario pc-eteint ; l'appeler ailleurs ferait échouer l'exercice réel
    après la panne, PC allumé."""

    def _pilote(self, exercer):
        actions = []

        class FausseGrappe:
            nom = "grappe"
            noeuds = []
            noeuds_noms = ["ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1"]
            ecrivain_disponible = False
            table_jalon = "public.ep86_jalon"

            def versions(self):
                return ""

            def accessibilite(self):
                return ""

            def tuer(self, nom):
                actions.append(("perte_noeud", nom))

            def demarrer_noeud(self, nom):
                actions.append(("retour_noeud", nom))

            def couper(self, nom):
                actions.append(("coupure_reseau", nom))

            def retablir(self, nom):
                actions.append(("retablissement_reseau", nom))

            def arreter_pc(self):
                actions.append(("arret_pc", None))
                raise exercer.Erreur("le PC répond au ping")

            def demarrer_pc(self):
                actions.append(("demarrage_pc", None))

            def journal(self, nom, lignes=400):
                return "running pg_rewind\npg_rewind exit code=0"

        pilote = exercer.Pilote(FausseGrappe(), exercer.Options())
        pilote.preparer = lambda: "ameesh-agents-1"
        pilote.inserer_jalon = lambda primaire, scenario: 7
        pilote.jalon_present = lambda nom, jalon: True
        pilote.attendre_sain = lambda timeout=0: "ameesh-agents-2"
        return pilote, actions

    def _jouer(self, scenario):
        exercer = _charger_exercer()
        exercer.Surveillance = _FausseSurveillance
        pilote, actions = self._pilote(exercer)
        resultat = getattr(pilote, "_scenario_" + scenario)()
        return resultat, [a for a, _ in actions]

    def test_perte_primaire_sans_action_du_poste(self):
        resultat, actions = self._jouer("perte_primaire")
        self.assertEqual(actions, ["perte_noeud", "retour_noeud"])
        self.assertEqual(resultat["verdict"], "ok")
        self.assertFalse(resultat["preuves"]["fenetre_close_par_arret_du_poste"])

    def test_coupure_reseau_sans_action_du_poste(self):
        resultat, actions = self._jouer("coupure_reseau")
        self.assertEqual(actions, ["coupure_reseau", "retablissement_reseau"])
        self.assertNotEqual(resultat["verdict"], "blocked")
        self.assertFalse(resultat["preuves"]["fenetre_close_par_arret_du_poste"])
