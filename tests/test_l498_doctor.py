# SPDX-License-Identifier: AGPL-3.0-only
"""L498 : le contrôle lecture seule des cgroups de tour dans `ameesh doctor`.

Le lot #498 pose un cgroup délégué par tour ; `ameesh doctor` doit dire, sans
rien écrire, si l'unité délègue (`Delegate=yes` + `DelegateSubgroup=runner`) et
si la racine déléguée est utilisable (cgroup.kill/events, inscriptible, libre
de tout processus). Ces témoins n'ont besoin ni de systemd ni d'un vrai cgroup :
la commande `systemctl show` et la monture cgroup sont injectées."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import support  # isole l'environnement agent de l'hôte
from ameesh import hostcheck


def _props(**values) -> str:
    return "".join("%s=%s\n" % (key, value) for key, value in values.items())


def _fake_root(tmp: str, control: str, *, kill: bool = True, events: bool = True,
               procs: str = "") -> Path:
    root = Path(tmp) / control.lstrip("/")
    root.mkdir(parents=True)
    if kill:
        (root / "cgroup.kill").write_text("")
    if events:
        (root / "cgroup.events").write_text("populated 0\n")
    (root / "cgroup.procs").write_text(procs)
    return root


class TurnCgroupLinesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = mock.Mock(host="hôte-de-test")
        self.served = mock.patch.object(hostcheck, "served_agents",
                                        return_value=[{"name": "anna"}]).start()
        self.addCleanup(self.served.stop)

    def lines(self, run, root=None):
        patches = [mock.patch.object(hostcheck, "CGROUP_ROOT", root)] if root else []
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return hostcheck.turn_cgroup_lines(self.cfg, None, run=run)

    def test_pas_d_agent_ou_hors_linux(self):
        with mock.patch.object(hostcheck, "served_agents", return_value=[]):
            self.assertEqual(hostcheck.turn_cgroup_lines(self.cfg, None,
                                                         run=lambda *a: ""), [])
        with mock.patch.object(hostcheck.platform, "is_linux", return_value=False):
            self.assertEqual(hostcheck.turn_cgroup_lines(self.cfg, None,
                                                         run=lambda *a: ""), [])

    def test_systemd_indisponible(self):
        self.assertEqual(self.lines(run=lambda *a: None),
                         ["turn cgroups: user systemd unavailable — not checked"])

    def test_unite_non_deleguee_reste_au_repli(self):
        run = lambda *a: _props(LoadState="loaded", Delegate="no",
                                DelegateSubgroup="", ControlGroup="/u/a.service")
        self.assertEqual(self.lines(run=run),
                         ["turn cgroups: 1 runner unit(s) not delegated — PID/PGID "
                          "ownership (L498 canary pending)"])

    def test_unite_inconnue_ignoree(self):
        run = lambda *a: _props(LoadState="not-found", Delegate="no",
                                DelegateSubgroup="", ControlGroup="")
        self.assertEqual(self.lines(run=run), [])

    def test_racine_deleguee_utilisable(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp, "/u.slice/a.service")
            run = lambda *a: _props(LoadState="loaded", Delegate="yes",
                                    DelegateSubgroup="runner",
                                    ControlGroup="/u.slice/a.service")
            self.assertEqual(self.lines(run=run, root=tmp),
                             ["turn cgroups: 1 runner unit(s) delegated, root free "
                              "and writable (L498)"])

    def test_racine_avec_processus_signalee(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp, "/u.slice/a.service", procs="4242\n")
            run = lambda *a: _props(LoadState="loaded", Delegate="yes",
                                    DelegateSubgroup="runner",
                                    ControlGroup="/u.slice/a.service")
            lines = self.lines(run=run, root=tmp)
            self.assertEqual(len(lines), 1)
            self.assertIn("own process(es) in the delegated root", lines[0])
            self.assertIn("keeps PID/PGID ownership (L498)", lines[0])

    def test_cgroup_kill_absent_signale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp, "/u.slice/a.service", kill=False)
            run = lambda *a: _props(LoadState="loaded", Delegate="yes",
                                    DelegateSubgroup="runner",
                                    ControlGroup="/u.slice/a.service")
            lines = self.lines(run=run, root=tmp)
            self.assertEqual(len(lines), 1)
            self.assertIn("cgroup.kill missing", lines[0])

    def test_racine_absente_signalee(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: _props(LoadState="loaded", Delegate="yes",
                                    DelegateSubgroup="runner",
                                    ControlGroup="/u.slice/a.service")
            lines = self.lines(run=run, root=tmp)
            self.assertEqual(len(lines), 1)
            self.assertIn("delegated root", lines[0])
            self.assertIn("missing", lines[0])


if __name__ == "__main__":
    unittest.main()
