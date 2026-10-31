# SPDX-License-Identifier: AGPL-3.0-only
"""#2212 : le mode du feu suit la configuration de l'hôte au moment du feu.

Constat (17/10, paquet Nexlink 48) : un exécuteur démarré avant que le projet
déclare `land` dans `radar.projects` gardait sa configuration de démarrage et
fusionnait en GitHub direct au lieu de la file. `radar` est désormais relu à
chaque passage du feu et du radar ; un fichier devenu illisible fait échouer
le passage (retenté) au lieu de retomber en silence sur des conditions sans
file.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from dataclasses import replace
from unittest import mock

from ameesh import config, fire, radar, runner

NEXLINK = {"projects": {"nexlink": {"repo": "/srv/nexlink", "github": "manaty/nexlink",
                                    "land": "local", "gates": ["true"]}}}


class _ConfigFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="ameesh-2212-")
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "config.json")
        self.write({})

    def write(self, raw) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))

    def load(self):
        return config.load({"AMEESH_CONFIG": self.path, "HOME": self.dir.name})


class FreshRadarTest(_ConfigFile):
    def test_load_records_the_file_it_read(self):
        self.assertEqual(self.load().config_file, self.path)
        self.assertEqual(config.Config().config_file, "")

    def test_config_file_is_never_read_from_the_file(self):
        self.write({"config_file": "/ailleurs.json"})
        self.assertEqual(self.load().config_file, self.path)

    def test_land_declared_after_start_switches_the_fire_to_the_queue(self):
        cfg = self.load()
        self.assertIsNone(fire._project(cfg, None, "manaty/nexlink"))
        self.write({"radar": NEXLINK})
        project = fire._project(config.fresh_radar(cfg), None, "manaty/nexlink")
        self.assertIsNotNone(project)
        self.assertEqual(project.land, "local")

    def test_radar_removed_from_the_file_is_removed(self):
        self.write({"radar": NEXLINK})
        cfg = self.load()
        self.write({})
        self.assertEqual(config.fresh_radar(cfg).radar, {})

    def test_unreadable_or_vanished_file_fails_closed(self):
        self.write({"radar": NEXLINK})
        cfg = self.load()
        self.write("{tronqué")
        with self.assertRaises(ValueError):
            config.fresh_radar(cfg)
        self.write("[]")
        with self.assertRaises(ValueError):
            config.fresh_radar(cfg)
        os.unlink(self.path)
        with self.assertRaises(ValueError):
            config.fresh_radar(cfg)

    def test_without_a_file_at_start_the_config_is_kept(self):
        cfg = replace(config.Config(), radar=NEXLINK)
        self.assertIs(config.fresh_radar(cfg), cfg)


class RunnerPassesTest(_ConfigFile):
    """Les passages de l'exécuteur reçoivent la configuration relue."""

    def runner(self, cfg):
        return types.SimpleNamespace(cfg=cfg, dry_run=False, runner_id="r-2212", db=None)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_fire_pass_reads_the_current_radar(self):
        fake = self.runner(self.load())
        self.write({"radar": NEXLINK})
        with mock.patch.object(fire, "run_pass", return_value=[]) as run_pass:
            runner.Runner.fire_once(fake, None)
        self.assertEqual(run_pass.call_args.args[0].radar, NEXLINK)
        self.assertEqual(fake.cfg.radar, {})

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_radar_pass_reads_the_current_radar(self):
        fake = self.runner(self.load())
        self.write({"radar": NEXLINK})
        with mock.patch.object(radar, "run_pass", return_value=[]) as run_pass:
            runner.Runner.radar_once(fake, None)
        self.assertEqual(run_pass.call_args.args[0].radar, NEXLINK)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_unreadable_config_skips_the_fire_pass(self):
        self.write({"radar": NEXLINK})
        fake = self.runner(self.load())
        self.write("{tronqué")
        with mock.patch.object(fire, "run_pass") as run_pass, \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.fire_once(fake, None)
        run_pass.assert_not_called()
        self.assertIn("fire pass failed", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
