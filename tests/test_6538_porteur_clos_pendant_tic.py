# SPDX-License-Identifier: AGPL-3.0-only
"""#6538 — un porteur clos pendant le tic donne un arrêt propre.

Le pilote abandonne un porteur pendant que `paquet-auto.py` le compose ;
l'échec qui suit (rc 1, rc 5) n'est ni une panne du composeur ni un rouge à
reprendre : `after_compose` rend une ligne de journal, sans courrier (refusé
sur un lot clos), sans marque d'échec ni lot d'empreinte §16. Un porteur
ouvert, ou illisible, garde le chemin ordinaire.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hourly = _load("test_paquet_horaire_6538", ROOT / "scripts" / "paquet-horaire.py")


class FakeShell:
    def __init__(self, lot: dict | None) -> None:
        self.lot = lot
        self.calls: list[list[str]] = []

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        self.calls.append(list(cmd))
        if cmd[:3] == ["ameesh", "work", "show"] and self.lot is not None:
            return hourly.Result(0, json.dumps(self.lot), "")
        return hourly.Result(0)


class ClosedCarrierTest(unittest.TestCase):
    def compose(self, lot: dict | None, rc: int):
        self.shell = FakeShell(lot)
        ctx = hourly.Context(
            repo=ROOT, target="main", project="ameesh", pilot="mesh-design",
            actor="mesh-design", shell=self.shell, python="python3")
        return hourly.after_compose(
            ctx, 6531, "h-20261109-13",
            hourly.Result(rc, "", "message not delivered — lot #6531 already closed\n"),
            hourly.Attempt(hourly.COMPOSITION, 1, ""))

    def gestures(self) -> list[list[str]]:
        return [call for call in self.shell.calls
                if call[0] == "agent-mail"
                or call[:3] in (["ameesh", "work", "note"],
                                ["ameesh", "work", "fingerprint"])]

    def test_porteur_abandonne_rc_1_arret_propre(self):
        got = self.compose({"id": 6531, "state": "closed",
                            "close_reason": "abandoned"}, 1)
        self.assertIn("porteur clos (abandoned)", got)
        self.assertEqual(self.gestures(), [])

    def test_porteur_clos_rc_5_sans_rouge(self):
        got = self.compose({"id": 6531, "state": "closed",
                            "close_reason": "superseded"}, hourly.COMPOSITION_RED_RC)
        self.assertIn("arrêt propre", got)
        self.assertEqual(self.gestures(), [])

    def test_porteur_ouvert_reste_un_echec(self):
        with self.assertRaisesRegex(hourly.HoraireError, "paquet-auto rc 1"):
            self.compose({"id": 6531, "state": "frozen"}, 1)
        self.assertTrue([call for call in self.shell.calls
                         if call[0] == "agent-mail"])

    def test_porteur_illisible_reste_un_echec(self):
        with self.assertRaisesRegex(hourly.HoraireError, "paquet-auto rc 1"):
            self.compose(None, 1)


if __name__ == "__main__":
    unittest.main()
