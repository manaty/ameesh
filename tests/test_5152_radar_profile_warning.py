# SPDX-License-Identifier: AGPL-3.0-only
"""#5152: a runner profile must expose a lost radar declaration in doctor."""
import io
import os
from contextlib import redirect_stdout
from unittest import mock

from ameesh import cli, radar
from .test_3345_profils_runner import Banc


class RadarProfileWarningTest(Banc):
    def setup_radar(self, overlay):
        self._write(self.host, {"radar": {"projects": {"product": {
            "repo": "/checkout", "github": "example/product", "required_checks": []}}}})
        self._write(self.critiques, overlay)

    def test_doctor_warns_without_changing_inheritance_or_exit_status(self):
        self.setup_radar({"radar": {"projects": {"product": {
            "repo": "/checkout", "github": "example/product"}}}})
        cfg = self.load(self.critiques)
        self.assertFalse(radar.settings_of(cfg).projects["product"].required_checks_declared)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.home}), redirect_stdout(output):
            code = cli.cmd_doctor(cfg, notify_test=False, profiles=True)
        self.assertEqual(code, 0)
        self.assertIn("WARNING critiques.json: radar.projects.product.required_checks absent",
                      output.getvalue())

    def test_an_inherited_or_explicit_empty_declaration_has_no_warning(self):
        for overlay in ({}, {"radar": {"projects": {"product": {
                "repo": "/checkout", "github": "example/product", "required_checks": []}}}}):
            with self.subTest(overlay=overlay):
                self.setup_radar(overlay)
                self.assertEqual(self.audit().warnings, ())

    def test_warning_reports_lost_keys_without_secret_values(self):
        self._write(self.host, {"radar": {"token": "secret-host-value"}})
        self._write(self.critiques, {"radar": {}})
        audit = self.audit()
        self.assertTrue(audit.ok)
        self.assertEqual(len(audit.warnings), 1)
        self.assertIn("radar.token absent", "\n".join(audit.lines()))
        self.assertNotIn("secret-host-value", "\n".join(audit.lines()))

    def test_an_independent_profile_is_not_compared_to_the_host(self):
        self.setup_radar({"inherit": False, "radar": {}})
        self.assertEqual(self.audit().warnings, ())
