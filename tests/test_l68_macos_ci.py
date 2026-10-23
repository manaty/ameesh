# SPDX-License-Identifier: AGPL-3.0-only
"""L68 : la CI exécute la suite sous macOS, PostgreSQL installé dans une étape.

Le job `platform-macos` de `tests.yml` est le seul à tourner sous macOS. Avant
L68 il n'exécutait que la couche plateforme ; il porte désormais la sélection
du plan (toute la suite sur main/release/**/paquet, les impactés d'une PR de
lot) avec le pilote psycopg. macOS n'a pas de conteneurs de service : la base
est installée et démarrée dans une étape, jamais par `services:`.

Décision de la revue #393 (23/10) : tant que la suite complète n'a pas tourné
verte sous macOS, ce job reste **indépendant** du check requis `tests` —
l'exiger ferait rougir le premier paquet pour tous les lots (§11 bis). Il
entre dans les `needs` de `tests` quand cette preuve existe (lot de suite).

Ces témoins lisent le workflow (sous-ensemble de texte, pas de PyYAML) et les
tests `/proc` réécrits : une régression du job ou un retour à une lecture
directe de `/proc` dans ces tests rougit ici, sans attendre un runner macOS.
"""
from __future__ import annotations

import os
import re
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

    def test_the_gnu_find_of_the_deployment_scripts_is_installed(self):
        # #3042 : poste.sh utilise `find -printf` (GNU) ; le faux ssh du banc
        # l'exécute sous macOS, où le find BSD refuse cette primaire.
        self.assertIn("brew install postgresql@17 bash coreutils gnu-sed findutils",
                      self.job)
        self.assertIn("$(brew --prefix findutils)/libexec/gnubin", self.job)

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
        # N1 de la revue #393 : 8 parts en série sous macOS, ~90 min mesuré
        # sous Linux ; plafond provisoire, resserré après la première mesure.
        self.assertIn("timeout-minutes: 240", self.job)

    def test_the_git_identity_step_is_taken_from_the_linux_jobs(self):
        # B2 de la revue #393 : un runner hébergé macOS n'a pas d'identité git
        # globale ; les dépôts temporaires des tests en ont besoin.
        self.assertIn("- *git", self.job)
        self.assertIn("GIT_CONFIG_GLOBAL=", self.text)

    def test_the_job_is_not_required_before_its_first_full_green_run(self):
        # Décision de conception de la revue #393 : aucun paquet ne doit
        # rougir pour un job macOS qui n'a jamais exécuté le plan complet.
        debut = self.text.index("\n  tests:\n")
        aggregate = self.text[debut:]
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", aggregate)
        self.assertIsNotNone(needs, "job tests : ligne needs introuvable")
        self.assertNotIn("platform-macos", needs.group(1))
        self.assertIn("platform-macos n'est pas encore requis", aggregate)


class LinuxOnlyWitnessesAreBoundedTest(unittest.TestCase):
    """Les lectures Linux qui restent (pas de couche possible) sont bornées."""

    def test_the_poste_proc_exe_witness_is_linux_only(self):
        chemin = os.path.join(REPO, "tests", "test_2950_poste_processus.py")
        with open(chemin, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("skipUnless(platform.is_linux()", source)

    def test_the_mount_witness_uses_a_fixture_not_the_host_proc(self):
        chemin = os.path.join(REPO, "tests", "test_l159g_pilot.py")
        with open(chemin, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("MOUNTINFO=/proc/self/mountinfo", source)
        self.assertIn("self.mountinfo(tmp, mount)", source)


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
