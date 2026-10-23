# SPDX-License-Identifier: AGPL-3.0-only
"""L68 : la CI exécute la suite sous macOS, PostgreSQL installé dans une étape.

Le job `platform-macos` de `tests.yml` est le seul à exécuter la suite sous
macOS. Avant L68 il n'exécutait que la couche plateforme ; il porte désormais
la sélection du plan (toute la suite sur main/release/**/paquet, les impactés
d'une PR de lot) avec le pilote psycopg. macOS n'a pas de conteneurs de
service : la base est installée et démarrée dans une étape, jamais par
`services:`.

Décision de la revue #393 (23/10) : tant que la suite complète n'a pas tourné
verte sous macOS, ce job reste **indépendant** du check requis `tests`.
L68b (#3026) a fourni cette preuve (URL du run dans la note du lot) : le job
entre dans les `needs` de `tests` et n'est plus toléré par `continue-on-error`.
Le job court `platform-macos-short` reste requis à côté (N2 de la revue) : la
couche plateforme L63/L66, sans base, rend un verdict macOS en quelques
minutes.

Ces témoins lisent le workflow (sous-ensemble de texte, pas de PyYAML), les
tests `/proc` réécrits et les témoins Linux-only gardés par
`platform.is_linux()` (#3132) : une régression du job, un retour à une lecture
directe de `/proc`, ou un témoin Linux-only non gardé rougit ici, sans
attendre un runner macOS.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
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
        fin = text.index("\n  platform-macos-short:\n", debut)
        cls.job = text[debut:fin]

    def test_the_full_suite_comes_from_the_plan(self):
        # same selection as the Linux psycopg parts: full on the full-suite
        # lanes, impacted + contract on a lot PR (#1211).
        self.assertIn("test-parts.py run --plan", self.job)
        self.assertIn("--driver psycopg", self.job)
        self.assertIn("plan.outputs.plan_sha256", self.job)
        self.assertIn("sha256sum -c -", self.job)

    def test_every_part_runs_before_the_job_fails(self):
        # N1 de la revue #393 : sous `set -e`, une première part rouge
        # arrêtait la boucle et masquait les suivantes.
        self.assertIn("rc=0", self.job)
        self.assertIn("|| rc=1", self.job)
        self.assertIn('exit "$rc"', self.job)

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

    def test_the_suite_job_is_required_after_its_first_full_green_run(self):
        # L68b (#3026) : la preuve d'un run complet vert existe (note du lot) ;
        # le job entre dans les `needs` et n'est plus toléré.
        debut = self.text.index("\n  tests:\n")
        aggregate = self.text[debut:]
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", aggregate)
        self.assertIsNotNone(needs, "job tests : ligne needs introuvable")
        self.assertIn("platform-macos", needs.group(1).split(", "))
        self.assertIn('test "${{ needs.platform-macos.result }}" = success',
                      aggregate)
        self.assertNotIn("platform-macos n'est pas encore requis", aggregate)

    def test_the_suite_is_not_tolerated_any_more(self):
        # #3042 avait posé `continue-on-error` le temps du correctif ; L68b le
        # retire : une part macOS rouge rougit le check requis.
        self.assertNotIn("continue-on-error: true", self.job)


class MacosShortJobTest(unittest.TestCase):
    """L68b (#3026) : la couche plateforme L63/L66 reste requise sous macOS.

    N2 de la revue #393 : le basculement garde un job macOS requis court, à
    côté de la suite longue ; il rend un verdict sans base en quelques minutes.
    """

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        cls.text = text
        debut = text.index("\n  platform-macos-short:\n")
        fin = text.index("\n  doc-readers:\n", debut)
        cls.job = text[debut:fin]

    def test_it_is_the_short_platform_contract(self):
        self.assertIn("timeout-minutes: 10", self.job)
        self.assertIn('AMEESH_REQUIRE_DB: "0"', self.job)
        for nom in ("tests.test_l63_platform.PlatformTest",
                    "tests.test_l63_platform.LintTest",
                    "tests.test_l63_platform.GuaranteeTest",
                    "tests.test_l66_desktop.PlatformNotifyTest"):
            self.assertIn(nom, self.job)
        self.assertIn("osascript", self.job)
        # pas de base ni de suite : c'est le verdict court, pas le doublon.
        self.assertNotIn("test-parts.py", self.job)
        self.assertNotIn("PostgreSQL", self.job)

    def test_it_is_required(self):
        debut = self.text.index("\n  tests:\n")
        aggregate = self.text[debut:]
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", aggregate)
        self.assertIsNotNone(needs, "job tests : ligne needs introuvable")
        self.assertIn("platform-macos-short", needs.group(1).split(", "))
        self.assertIn(
            'test "${{ needs.platform-macos-short.result }}" = success',
            aggregate)
        self.assertIn("runs-on: macos-26-intel", self.job)

    def test_the_plan_guard_is_outside_the_tolerated_step(self):
        # #3048 (N de la revue #3030) : `continue-on-error` de #3042 ne
        # tolère que la portabilité de la suite ; la garde « aucune part
        # psycopg » est une étape à part, sans tolérance, avant la suite.
        garde = self.job.index("- name: le plan donne des parts psycopg (L68)")
        suite = self.job.index("- name: suite du plan (L68, pilote psycopg)")
        self.assertLess(garde, suite)
        etape = self.job[garde:suite]
        self.assertIn("aucune part psycopg", etape)
        self.assertIn("exit 1", etape)
        self.assertNotIn("continue-on-error", etape)


class BenchTmpdirTest(unittest.TestCase):
    """#3042 : le banc résout TMPDIR avant de créer ses bacs jetables."""

    def test_a_symlinked_tmpdir_is_resolved_before_the_fixtures(self):
        # Sous macOS /tmp et /var sont des liens : un bac créé sous le TMPDIR
        # lexical fait refuser ses ancêtres symboliques par les gardes de
        # déploiement. Le sous-processus importe le paquet `tests` (qui résout)
        # puis rend le répertoire que tempfile utilisera.
        with tempfile.TemporaryDirectory(prefix="ameesh-l68-tmp-") as base:
            real = os.path.join(base, "real")
            link = os.path.join(base, "link")
            os.mkdir(real)
            os.symlink(real, link)
            env = dict(os.environ, TMPDIR=link,
                       PYTHONPATH=os.path.join(REPO, "src"))
            done = subprocess.run(
                [sys.executable, "-c",
                 "import tempfile, tests; print(tempfile.gettempdir())"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), os.path.realpath(link))


class LinuxOnlyWitnessesAreBoundedTest(unittest.TestCase):
    """Les lectures Linux qui restent (pas de couche possible) sont bornées."""

    #: #3132 : témoins Linux-only de la suite macOS (cgroup, PSI, /proc,
    #: waitid, inotify). Chacun est gardé par la couche plateforme
    #: (`platform.is_linux()`, ou la capacité `waitid`) : sous macOS il est
    #: sauté proprement, et sous Linux il continue de prouver.
    BOUNDED = (
        ("test_l73_housekeeping.py",
         "test_nouvelle_session_ne_vide_pas_un_dossier_tmp_tenu"),
        ("test_l73_housekeeping.py",
         "test_dossier_tmp_d_un_agent_absent_tenu_par_un_processus_est_garde"),
        ("test_l73_housekeeping.py",
         "test_dossier_tmp_tenu_par_un_processus_n_est_pas_evince"),
        ("test_762_menage_dryrun_episode.py",
         "test_truncated_after_mounts_unreadable_keeps_its_since"),
        ("test_530_housekeeping_followups.py",
         "test_mount_table_unreadable_is_journaled_and_alerted"),
        ("test_530_housekeeping_followups.py",
         "test_mount_table_unreadable_never_resolves_an_unremovable_episode"),
        ("test_530_housekeeping_followups.py",
         "test_mount_table_readable_again_lifts_the_unscanned_alert"),
        ("test_l231_deps.py", "test_6_holders_see_mapped_files"),
        ("test_476_background_signals.py",
         "test_capture_refuses_a_harness_already_reaped_by_another_poller",
         "waitid"),
        ("test_turn_grace.py", "test_groupe_disparu_ferme_non_orphelin"),
        ("test_turn_grace.py", "test_shutdown_vanished_process_closes_without_orphan"),
        ("test_turn_grace.py",
         "test_failed_handoff_preserves_child_migrated_to_another_turn"),
        ("test_turn_grace.py", "test_echeance_de_la_grace_reglee_par_agent"),
        ("test_turn_grace.py", "test_tour_clos_au_point_sur_garde_le_travail_de_fond"),
        ("test_l131_suites_lourdes.py",
         "test_a_worker_ignoring_sigterm_keeps_the_slot_until_sigkill"),
        ("test_l131_suites_lourdes.py", "test_a_suite_on_a_terminal_can_read_it"),
        ("test_l131_suites_lourdes.py", "test_reads_dev_tty_with_stdin_redirected"),
        ("test_l131_suites_lourdes.py",
         "test_ctrl_z_suspends_the_wrapper_and_fg_resumes"),
        ("test_l131_suites_lourdes.py",
         "test_a_fork_chain_and_setsid_cannot_outlive_the_slot"),
        # garde de classe : tout le contrat de préchargement du garde cgroup
        ("test_l490_deepseek_cgroup.py", "PreloadTest"),
        ("test_l112_host_gate.py", "test_changement_vu_par_inotify"),
    )

    @staticmethod
    def _guarded(tree: ast.AST, name: str, needle: str = "is_linux") -> bool:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) or node.name != name:
                continue
            for decorator in node.decorator_list:
                dumped = ast.dump(decorator)
                if "skipUnless" in dumped and needle in dumped:
                    return True
        return False

    def test_every_linux_only_witness_is_skipped_off_linux(self):
        for entry in self.BOUNDED:
            nom, temoin = entry[:2]
            garde = entry[2] if len(entry) > 2 else "is_linux"
            chemin = os.path.join(REPO, "tests", nom)
            with open(chemin, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=chemin)
            with self.subTest(module=nom, witness=temoin):
                self.assertTrue(
                    self._guarded(tree, temoin, garde),
                    "%s : %s doit rester gardé par %s (#3132)"
                    % (nom, temoin, garde))

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
