# SPDX-License-Identifier: AGPL-3.0-only
"""#8040 — un arriéré de courrier passif ne bloque plus son propre écoulement.

Constat (staging, mesh-design, 22/11) : 893 passifs (`--info`, copies,
diffusions) en attente depuis le 17/11. La réserve d'aperçus de #7976 comptait
chaque passif en retard : 200 passifs réservaient plus que le budget, et un
tour ouvert par un actif frais ne servait plus que lui, tronqué au besoin.
Les passifs ne sortaient qu'en paquets, derrière un actif en retard.

Désormais seuls les actifs en retard réservent la place de leur aperçu ; un
passif prend la place qui reste, en entier ou en aperçu court.
"""
from __future__ import annotations

import time
import unittest

from ameesh import mail

from .test_l125_courrier_regroupe import _ligne


def _passifs(n, created_ts, body="compte rendu"):
    return [dict(_ligne(payload={"info": True}, body="%s %d" % (body, i)),
                 id=i + 1, created_ts=created_ts) for i in range(n)]


class PassifsServisTest(unittest.TestCase):
    def test_un_arriere_de_passifs_suit_un_actif_frais(self):
        maintenant = time.time()
        actif = dict(_ligne(body="le lot 12 attend ton avis " + "a" * 600),
                     id=1000, created_ts=maintenant)
        passifs = _passifs(200, maintenant - 5 * 86400, body="x" * 500)
        texte, retenus, reste = mail.prompt_for(
            None, [actif] + passifs, kind="mail", budget=20000,
            en_retard_avant=maintenant - 300)
        self.assertEqual(retenus[0]["id"], 1000)
        self.assertGreater(len(retenus), 10)
        self.assertEqual(reste, 201 - len(retenus))
        self.assertLessEqual(mail.octets(texte), 20000)

    def test_un_passif_lourd_entre_en_apercu_court(self):
        maintenant = time.time()
        actif = dict(_ligne(body="court"), id=1000, created_ts=maintenant)
        lourd = dict(_ligne(payload={"cc": "orchestrateur"}, body="y" * 30000),
                     id=7, created_ts=maintenant)
        texte, retenus, reste = mail.prompt_for(
            None, [actif, lourd], kind="mail", budget=20000,
            en_retard_avant=maintenant - 300)
        self.assertEqual(([m["id"] for m in retenus], reste), ([1000, 7], 0))
        self.assertIn("agent-mail show 7", texte)
        self.assertLessEqual(mail.octets(texte), 20000)

    def test_un_actif_en_retard_garde_sa_place_devant_un_passif(self):
        # le passif placé avant ne consomme pas la réserve de l'actif en retard
        maintenant = time.time()
        passif = dict(_ligne(payload={"info": True}, body="z" * 19000),
                      id=1, created_ts=maintenant)
        actif = dict(_ligne(body="avis " + "w" * 6000), id=2,
                     created_ts=maintenant - 3600)
        actif_frais = dict(_ligne(body="tête"), id=3, created_ts=maintenant)
        _t, retenus, _reste = mail.prompt_for(
            None, [actif_frais, passif, actif], kind="mail", budget=20000,
            en_retard_avant=maintenant - 300)
        self.assertEqual([m["id"] for m in retenus], [3, 1, 2])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
