# SPDX-License-Identifier: AGPL-3.0-only
"""L68 : la CI exécute la suite sous macOS, PostgreSQL installé dans une étape.

Le job `platform-macos` de `tests.yml` est le seul à tourner sous macOS. Avant
L68 il n'exécutait que la couche plateforme ; il porte désormais la sélection
du plan (toute la suite sur main/release/**/paquet, les impactés d'une PR de
lot) avec le pilote psycopg. macOS n'a pas de conteneurs de service : la base
est installée et démarrée dans une étape, jamais par `services:`.

Ces témoins lisent le workflow (sous-ensemble de texte, pas de PyYAML) et les
tests `/proc` réécrits : une régression du job ou un retour à une lecture
directe de `/proc` dans ces tests rougit ici, sans attendre un runner macOS.
"""
from __future__ import annotations

import os
import unittest

from .support import REPO

WORKFLOW = os.path.join(REPO, ".github", "workflows", "tests.yml")

#: tests réécrits par L68 pour lire l'OS par `ameesh.platform`.
REWRITTEN = (
    "test_453_pause_pression.py",
    "test_l110_enrollment.py",
    "test_l131_suites_lourdes.py",
    "test_l76_resident_runner.py",
    "test_1651_ci_parts.py",
)


class MacosSuiteJobTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        cls.text = text
        debut = text.index("\n  platform-macos:\n")
        fin = text.index("\n  doc-readers:\n", debut)
        cls.job = text[debut:fin]

    def test_the_full_suite_comes_from_the_plan(self):
        # same selection as the Linux psycopg parts: full on the full-suite
        # lanes, impacted + contract on a lot PR (#1211).
        self.assertIn("test-parts.py run --plan", self.job)
        self.assertIn("--driver psycopg", self.job)
        self.assertIn("plan.outputs.plan_sha256", self.job)
        self.assertIn("sha256sum -c -", self.job)

    def test_postgresql_is_installed_and_started_in_a_step(self):
        # `services:` runs Linux containers only: on macOS the database is a
        # step, and the suite must not silently skip its database tests.
        self.assertNotIn("services:", self.job)
        self.assertIn("brew install postgresql@17", self.job)
        self.assertIn("initdb -D", self.job)
        self.assertIn("pg_ctl", self.job)
        self.assertIn("createdb", self.job)
        self.assertIn('AMEESH_REQUIRE_DB: "1"', self.job)

    def test_the_platform_contract_still_runs_on_macos(self):
        for nom in ("tests.test_l63_platform.PlatformTest",
                    "tests.test_l63_platform.LintTest",
                    "tests.test_l63_platform.GuaranteeTest",
                    "tests.test_l66_desktop.PlatformNotifyTest"):
            self.assertIn(nom, self.job)
        self.assertIn("osascript", self.job)

    def test_the_runner_is_the_standard_x64_image(self):
        # #987 : pas de `macos-latest` arm64, qui n'obtenait pas de capacité.
        self.assertIn("runs-on: macos-26-intel", self.job)
        self.assertIn("timeout-minutes: 120", self.job)


class ProcTestsGoThroughTheLayerTest(unittest.TestCase):
    """L68 : ces tests ne lisent plus `/proc` eux-mêmes (psutil sous macOS)."""

    def test_no_direct_proc_read_remains(self):
        for nom in REWRITTEN:
            chemin = os.path.join(REPO, "tests", nom)
            with open(chemin, encoding="utf-8") as fh:
                source = fh.read()
            with self.subTest(test=nom):
                self.assertNotIn('open("/proc', source,
                                 "lecture directe de /proc : passez par ameesh.platform")
                self.assertNotIn('os.listdir("/proc"', source,
                                 "lecture directe de /proc : passez par ameesh.platform")
                self.assertNotIn('Path("/proc', source,
                                 "lecture directe de /proc : passez par ameesh.platform")


if __name__ == "__main__":
    unittest.main()
