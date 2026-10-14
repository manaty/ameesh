# SPDX-License-Identifier: AGPL-3.0-only
"""#1060 : `ameesh alerts` ne tombe plus sur un type d'alerte non enregistre.

`exploitation.alerts()` triait avec `ALERT_TYPES.index(a["type"])` : les deux
types produits par `_housekeeping_alerts` (#500/#718), `tmp_unremovable` et
`tmp_unscanned`, absents de `ALERT_TYPES`, faisaient lever ValueError a toute la
commande des qu'une de ces alertes etait presente (constat de premiere main,
pc-smichea 13/10 22:50). Temoins : tri stable avec un type inconnu en fin,
`alerts()` qui rend la liste avec une telle alerte, affichage qui la signale, et
garde statique : tout litteral passe a `_alert` dans `src/ameesh` est declare.
"""
from __future__ import annotations

import ast
import os
import unittest
from unittest import mock

from ameesh import exploitation

from .support import SRC
from .test_l94_underuse import _Base

INCONNU = "type_inconnu_1060"


def _literal_alert_types(source: str) -> list[tuple[str, int]]:
    """Les (type, ligne) des appels `_alert("<litteral>", …)` d'un source."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "_alert":
            continue
        first = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "kind"), None)
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((first.value, first.lineno))
    return out


class AlertTypesDeclaredTest(unittest.TestCase):
    def test_chaque_litteral_est_declare(self):
        """Garde qui aurait attrape #1060 (tmp_unremovable/tmp_unscanned)."""
        non_declares: dict[str, list[str]] = {}
        for root, _dirs, files in os.walk(SRC):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    trouves = _literal_alert_types(fh.read())
                for kind, line in trouves:
                    if kind not in exploitation.ALERT_TYPES:
                        non_declares.setdefault(kind, []).append(
                            "%s:%d" % (os.path.relpath(path, SRC), line))
        self.assertEqual(non_declares, {},
                         "types d'alerte non declares dans ALERT_TYPES")

    def test_les_types_du_menage_sont_declares(self):
        self.assertIn("tmp_unremovable", exploitation.ALERT_TYPES)
        self.assertIn("tmp_unscanned", exploitation.ALERT_TYPES)


class TriTypeInconnuTest(unittest.TestCase):
    def test_type_inconnu_en_fin_sans_exception(self):
        inconnue_b = exploitation._alert(INCONNU, "b", None, 1, 0, "temoin")
        inconnue_a = exploitation._alert(INCONNU, "a", None, 1, 0, "temoin")
        connue_tard = exploitation._alert("owner_key_registered", None, None, 1, 0, "d")
        connue_tot = exploitation._alert("long_turn", "zoe", None, 1, 0, "d")
        out = sorted([inconnue_b, connue_tard, inconnue_a, connue_tot],
                     key=exploitation._alert_sort_key)
        self.assertEqual([a["type"] for a in out],
                         ["long_turn", "owner_key_registered", INCONNU, INCONNU])
        # ordre stable entre inconnus (agent), les connus d'abord
        self.assertEqual([a["agent"] for a in out], ["zoe", None, "a", "b"])
        self.assertEqual(exploitation._alert_rank(INCONNU),
                         len(exploitation.ALERT_TYPES))

    def test_cle_totale_meme_sans_type(self):
        self.assertIsInstance(exploitation._alert_sort_key({}), tuple)
        self.assertEqual(exploitation._alert_rank(None), len(exploitation.ALERT_TYPES))

    def test_affichage_signale_le_type_inconnu(self):
        texte = exploitation._format_alert(
            exploitation._alert(INCONNU, None, None, 1, 0, "detail"))
        self.assertIn("[%s (unregistered type)]" % INCONNU, texte)
        connu = exploitation._format_alert(
            exploitation._alert("long_turn", None, None, 1, 0, "detail"))
        self.assertNotIn("unregistered", connu)


class AlerteInconnueDansAlertsTest(_Base):
    """Le temoin du lot : une alerte de type inconnu injectee dans `alerts()`
    ressort dans la liste, en fin, sans exception."""

    def test_alerts_rend_la_liste_avec_un_type_inconnu(self):
        inconnue = exploitation._alert(INCONNU, "zoe", None, 1, 0, "temoin")
        with mock.patch.object(exploitation, "_gate_alerts", return_value=[inconnue]):
            out = exploitation.alerts(self.cfg, self.db, now=self.now)
        self.assertIn(inconnue, out)
        self.assertEqual(out[-1], inconnue, "un type inconnu passe en fin de tri")
        self.assertEqual(out[-1]["type"], INCONNU)


if __name__ == "__main__":
    unittest.main()
