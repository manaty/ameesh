# SPDX-License-Identifier: AGPL-3.0-only
"""#4378 — le footprint d'hôte (#4150) n'est lu que sur demande.

Constat du 28/10 : `ameesh show --json` calculait `host_idle.snapshot` à chaque
appel (120 à 145 requêtes, 12 à 48 s depuis agents-1 et agents-2). La lecture
`show --json` de vm.sh (DRAIN_LECTURE_DELAI=30 s) expirait, et le staging de
8f7b2fb est revenu en arrière deux fois. `show`, `GET /api/v1/agents/{a}` et
`doctor` ne le lisent plus que sur demande (`--footprint`, `?footprint=1`).
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import cli, db_capacity, host_idle, hostcheck, mesh_cli, registry, venv_guard

from .support import PgTestCase


class FootprintOnDemandTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")

    def _snapshot(self):
        return mock.patch.object(host_idle, "snapshot",
                                 return_value={"h1": {"state": "busy"}})

    def test_show_does_not_read_the_footprint_by_default(self):
        with self._snapshot() as snap:
            row = mesh_cli.agent_detail(self.db, "a1", cfg=self.cfg)
        snap.assert_not_called()
        self.assertEqual(row["name"], "a1")
        # les clés restent dans `show --json`, à null
        self.assertIn("footprint", row)
        self.assertIsNone(row["footprint"])
        self.assertIsNone(row["footprint_unavailable"])

    def test_footprint_on_demand(self):
        with self._snapshot() as snap:
            row = mesh_cli.agent_detail(self.db, "a1", cfg=self.cfg, footprint=True)
        snap.assert_called_once()
        self.assertEqual(row["footprint"], {"state": "busy"})

    def test_show_option_reaches_agent_detail(self):
        for argv, expected in ((["show", "a1", "--json"], False),
                               (["show", "a1", "--json", "--footprint"], True)):
            with self.subTest(argv=argv), \
                    mock.patch.object(mesh_cli, "_open", return_value=self.db), \
                    mock.patch.object(self.db, "close", create=True), \
                    mock.patch.object(mesh_cli, "agent_detail",
                                      wraps=mesh_cli.agent_detail) as detail, \
                    self._snapshot() as snap, redirect_stdout(io.StringIO()):
                args = mesh_cli.build_parser().parse_args(argv)
                self.assertEqual(mesh_cli.cmd_show(self.cfg, args), 0)
                self.assertEqual(detail.call_args.kwargs.get("footprint"), expected)
                self.assertEqual(snap.called, expected)

    def _doctor(self, **kw):
        out = io.StringIO()
        with mock.patch.object(hostcheck, "jit_lines", return_value=[]), \
                mock.patch.object(cli, "_profile_audit", return_value=None), \
                mock.patch.object(venv_guard, "installation_problems", return_value=[]), \
                mock.patch.object(db_capacity, "read", return_value=None), \
                self._snapshot() as snap, redirect_stdout(out):
            cli.cmd_doctor(self.cfg, notify_test=False, **kw)
        return snap

    def test_doctor_reads_the_footprint_only_on_demand(self):
        self.assertFalse(self._doctor().called)
        self.assertTrue(self._doctor(footprint=True).called)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
