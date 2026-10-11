# SPDX-License-Identifier: AGPL-3.0-only
"""Plafond mémoire par agent et slice dédiée (lot L159c).

Constat du 2026-10-10 : sur la VM du mesh, le `node` d'un harnais a atteint
4,1 Go et l'OOM du noyau l'a tué ; l'unité de l'exécuteur n'avait aucun
plafond. Désormais : `MemoryMax` (et `MemoryHigh` à 90 %) par unité d'agent,
`ameesh-agents.slice`, plafond réglable au canon (fiche Host), écart signalé
par `ameesh doctor`. Sans base.
"""
from __future__ import annotations

import configparser
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, hostcheck, resources

from . import test_canon
from .support import REPO
from .test_l31_pressure import set_host_resources

SYSTEMD = os.path.join(REPO, "deploy", "systemd")
GIB = 1024 ** 3


def _unit(*parts: str) -> configparser.ConfigParser:
    unite = configparser.ConfigParser(strict=False, interpolation=None)
    with open(os.path.join(SYSTEMD, *parts), encoding="utf-8") as fh:
        unite.read_string(fh.read())
    return unite


class UnitesTest(unittest.TestCase):
    def test_unite_d_agent_plafonnee_dans_la_slice(self):
        service = _unit("ameesh-runner-agent@.service")["Service"]
        self.assertEqual(service.get("Slice"), hostcheck.AGENTS_SLICE)
        self.assertEqual(resources.parse_bytes(service.get("MemoryMax")),
                         resources.DEFAULT_AGENT_MEMORY_MAX)
        self.assertEqual(resources.parse_bytes(service.get("MemoryHigh")),
                         int(resources.DEFAULT_AGENT_MEMORY_MAX
                             * resources.AGENT_MEMORY_HIGH_RATIO))
        self.assertGreater(int(service.get("OOMScoreAdjust")), 0)

    def test_complement_systeme_identique(self):
        agent = _unit("ameesh-runner-agent@.service")["Service"]
        systeme = _unit("systeme", "ameesh-runner@.service.d", "10-plafonds.conf")["Service"]
        for key in ("Slice", "MemoryMax", "MemoryHigh", "OOMScoreAdjust"):
            self.assertEqual(systeme.get(key), agent.get(key), key)

    def test_slices(self):
        for parts in (("ameesh-agents.slice",), ("systeme", "ameesh-agents.slice")):
            slice_ = _unit(*parts)["Slice"]
            self.assertTrue(slice_.get("MemoryMax"))
            self.assertTrue(slice_.get("MemoryHigh"))


class CanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def _load(self):
        loaded = canon.load(self.root, untrusted=True)
        return loaded, [f.code for f in canon.validate(loaded)]

    def test_plafonds_lus(self):
        set_host_resources(self.root, agent_memory_max="4G",
                           agent_memory_max_by_agent="{mesh-design: 8GiB}")
        loaded, codes = self._load()
        self.assertEqual(loaded.host("atelier").policy.resources, {
            "agent_memory_max": 4 * GIB,
            "agent_memory_max_by_agent": {"mesh-design": 8 * GIB},
        })
        self.assertNotIn("host-policy-invalid", codes)
        self.assertNotIn("host-resources-unknown", codes)

    def test_plafond_illisible(self):
        set_host_resources(self.root, agent_memory_max="beaucoup",
                           agent_memory_max_by_agent="{x: 0}")
        loaded, codes = self._load()
        self.assertIn("host-policy-invalid", codes)
        self.assertIsNone(loaded.host("atelier").policy.resources)


def _canon(cid: str, host: str, **raw):
    fiche = SimpleNamespace(policy=SimpleNamespace(resources=raw))
    return SimpleNamespace(id=cid, host=lambda name: fiche if name == host else None)


class PlafondEffectifTest(unittest.TestCase):
    def test_defaut(self):
        cap = resources.agent_memory_cap([], "vm", "a")
        self.assertEqual(cap, {"max": 6 * GIB, "high": int(6 * GIB * 0.9), "origin": "défaut"})

    def test_hote_agent_et_plus_strict(self):
        canons = [_canon("ameesh", "vm", agent_memory_max=4 * GIB,
                         agent_memory_max_by_agent={"gros": 8 * GIB}),
                  _canon("autre", "vm", agent_memory_max=3 * GIB)]
        self.assertEqual(resources.agent_memory_cap(canons, "vm", "a")["max"], 3 * GIB)
        self.assertEqual(resources.agent_memory_cap(canons, "vm", "a")["origin"], "autre")
        # le plafond propre à l'agent l'emporte sur celui de l'hôte
        self.assertEqual(resources.agent_memory_cap(canons, "vm", "gros")["max"], 8 * GIB)
        self.assertEqual(resources.agent_memory_cap(canons, "poste", "a")["max"], 6 * GIB)


class DoctorTest(unittest.TestCase):
    def _lines(self, units: dict, canons=(), controllers=lambda: {"memory", "cpu"}):
        def run(*args):
            if args[0] == "show":
                return units.get(args[1], "LoadState=not-found\n")
            return None
        agents = [{"name": name} for name in ("a", "b")]
        with mock.patch.object(hostcheck, "served_agents", return_value=agents):
            return hostcheck.memory_cap_lines(SimpleNamespace(host="vm"), None,
                                              canons=list(canons), run=run,
                                              controllers=controllers)

    @staticmethod
    def _show(memory_max, slice_="ameesh-agents.slice"):
        return "LoadState=loaded\nMemoryMax=%s\nSlice=%s\n" % (memory_max, slice_)

    def test_conforme(self):
        lines = self._lines({"ameesh-runner-agent@a.service": self._show(6 * GIB),
                             "ameesh-runner-agent@b.service": self._show(6 * GIB)})
        self.assertEqual(len(lines), 1)
        self.assertIn("2 agent unit(s)", lines[0])

    def test_sans_plafond_hors_slice(self):
        lines = self._lines({"ameesh-runner-agent@a.service":
                             self._show("infinity", "app.slice")})
        texte = "\n".join(lines)
        self.assertIn("no memory cap", texte)
        self.assertIn("slice app.slice", texte)
        self.assertIn("systemctl --user set-property ameesh-runner-agent@a.service "
                      "MemoryMax=6144M MemoryHigh=5529M", texte)

    def test_ecart_au_canon(self):
        canons = [_canon("ameesh", "vm", agent_memory_max_by_agent={"a": 8 * GIB})]
        lines = self._lines({"ameesh-runner-agent@a.service": self._show(6 * GIB)}, canons)
        self.assertIn("MemoryMax 6144M, the canon says 8192M (ameesh)", lines[0])
        self.assertIn("MemoryMax=8192M MemoryHigh=7372M", lines[1])

    def test_memoire_non_deleguee(self):
        lines = self._lines({"ameesh-runner-agent@a.service": self._show(6 * GIB)},
                            controllers=lambda: {"cpu", "pids"})
        self.assertTrue(any("not delegated" in line for line in lines))

    def test_sans_systemd(self):
        with mock.patch.object(hostcheck, "served_agents", return_value=[{"name": "a"}]):
            lines = hostcheck.memory_cap_lines(SimpleNamespace(host="vm"), None, canons=[],
                                               run=lambda *a: None)
        self.assertIn("not checked", lines[0])


if __name__ == "__main__":
    unittest.main()
