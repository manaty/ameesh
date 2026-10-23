# SPDX-License-Identifier: AGPL-3.0-only
"""L63/L66/L68 : macOS dans la CI — verdict court par PR, suite en nocturne.

#3198 : le job `platform-macos` de `tests.yml` exécutait la suite complète du
plan sous macOS (8 parts psycopg, PostgreSQL installé dans une étape) ; elle
durait jusqu'à 3 h 50 et retenait tout le rollup de fusion (`fire.ready_pr` et
`packet_automerge` attendent tous les checks). Ce job est désormais **court**
(L63/L66, sans base, 20 min au plus) et la suite complète vit dans
`.github/workflows/macos.yml` : nocturne sur main, à la main, et à la demande
par l'étiquette `ci-macos` sur une PR. Ce workflow utilise
`pull_request_target` pour ne pas entrer dans le workflow de gel `tests.yml`
(où `labeled` est déjà le déclencheur du check requis) ni dans le rollup de la
PR.

macOS n'a pas de conteneurs de service : la base est installée et démarrée
dans une étape, jamais par `services:`.

Depuis #3026 (L68b), le job court `platform-macos` est REQUIS : il entre dans
les `needs` de `tests`. La suite longue de `macos.yml` reste un nocturne hors
du chemin du paquet ; elle n'est pas encore verte sous macOS (les correctifs
de portabilité sont suivis par lots séparés).

Ces témoins lisent les workflows (sous-ensemble de texte, pas de PyYAML), les
tests `/proc` réécrits et les témoins Linux-only gardés par
`platform.is_linux()` (#3132) : une régression du job, un retour de la suite
longue dans le chemin du paquet, une lecture directe de `/proc`, ou un témoin
Linux-only non gardé rougit ici, sans attendre un runner macOS.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest

from ameesh import canon

from .support import REPO
from .test_1468_ci_prio import evaluate

WORKFLOW = os.path.join(REPO, ".github", "workflows", "tests.yml")
MACOS = os.path.join(REPO, ".github", "workflows", "macos.yml")

#: tests réécrits par L68 pour lire l'OS par `ameesh.platform`.
REWRITTEN = (
    "test_453_pause_pression.py",
    "test_l110_enrollment.py",
    "test_l131_suites_lourdes.py",
    "test_l76_resident_runner.py",
    "test_1651_ci_parts.py",
)


class MacosShortJobTest(unittest.TestCase):
    """Le job macOS des PR de paquet : la couche plateforme, sans la suite."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        cls.text = text
        debut = text.index("\n  platform-macos:\n")
        fin = text.index("\n  doc-readers:\n", debut)
        cls.job = text[debut:fin]

    def test_the_job_is_short_and_carries_no_suite(self):
        # #3198 : 20 min au plus, sans base ; la suite complète est partie
        # dans macos.yml, sinon elle retient tout le rollup du paquet.
        self.assertIn("timeout-minutes: 20", self.job)
        self.assertIn('AMEESH_REQUIRE_DB: "0"', self.job)
        self.assertNotIn("services:", self.job)
        self.assertNotIn("test-parts.py run", self.job)
        self.assertNotIn("psycopg[binary]", self.job)
        self.assertNotIn("actions/download-artifact", self.job)

    def test_the_platform_contract_still_runs_on_macos(self):
        for nom in ("tests.test_l63_platform.PlatformTest",
                    "tests.test_l63_platform.LintTest",
                    "tests.test_l63_platform.GuaranteeTest",
                    "tests.test_l66_desktop.PlatformNotifyTest"):
            self.assertIn(nom, self.job)
        self.assertIn("psutil", self.job)
        self.assertIn("osascript", self.job)

    def test_the_runner_is_the_standard_x64_image(self):
        # #987 : pas de `macos-latest` arm64, qui n'obtenait pas de capacité.
        self.assertIn("runs-on: macos-26-intel", self.job)

    def test_the_short_job_is_required(self):
        # L68b (#3026) : #3198 a sorti la suite longue du chemin du paquet ;
        # c'est le verdict court macOS qui est requis.
        debut = self.text.index("\n  tests:\n")
        aggregate = self.text[debut:]
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", aggregate)
        self.assertIsNotNone(needs, "job tests : ligne needs introuvable")
        self.assertIn("platform-macos", needs.group(1).split(", "))
        # #3251 : le résultat du job est lu par le classifieur de l'agrégat
        # (une annulation se consigne, un échec rougit), pas par une garde
        # bash `= success`.
        self.assertIn(
            '--plateforme-macos "${{ needs.platform-macos.result }}"', aggregate)
        self.assertNotIn("continue-on-error: true", self.job)


class MacosFullSuiteWorkflowTest(unittest.TestCase):
    """La suite complète sous macOS, hors du chemin du paquet (#3198)."""

    @classmethod
    def setUpClass(cls):
        with open(MACOS, encoding="utf-8") as fh:
            cls.text = fh.read()
        debut = cls.text.index("\n  suite:\n")
        cls.job = cls.text[debut:]

    def test_it_runs_nightly_on_main(self):
        self.assertRegex(self.text, r"(?m)^  schedule:\n")
        self.assertRegex(self.text, r"(?m)^    - cron: \"[0-9 ]+ \* \* \*\"$")

    def test_it_runs_by_hand_and_on_the_ci_macos_label(self):
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:\n")
        self.assertRegex(self.text, r"(?m)^  pull_request_target:\n")
        self.assertIn("types: [labeled]", self.text)
        self.assertIn("github.event.label.name == 'ci-macos'", self.job)

    def test_the_macos_workflow_is_not_the_freeze_workflow(self):
        # Le workflow macOS ne doit pas être un second `pull_request` : les
        # étiquettes de gel `ci-lot`/`ci-full` le réveilleraient pour rien.
        self.assertIn("pull_request_target:", self.text)
        self.assertNotRegex(self.text, r"(?m)^  pull_request:")
        with open(WORKFLOW, encoding="utf-8") as fh:
            tests = fh.read()
        self.assertIn("types: [labeled]", tests)

    def test_only_a_head_of_the_repository_runs(self):
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            self.job)

    def test_its_permissions_are_read_only(self):
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotRegex(self.text, r"(?m):\s+write(?:-only|-all)?\s*$")

    def test_it_runs_the_whole_plan_under_macos(self):
        self.assertIn("test-plan.py plan", self.job)
        self.assertIn("--mode full", self.job)
        self.assertIn("test-parts.py run --plan", self.job)
        self.assertIn("--driver psycopg", self.job)
        # N1 de la revue #393 : toutes les parts tournent avant l'échec.
        self.assertIn("rc=0", self.job)
        self.assertIn("|| rc=1", self.job)
        self.assertIn('exit "$rc"', self.job)

    def test_postgresql_is_installed_and_started_in_a_step(self):
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

    def test_the_runner_is_the_standard_x64_image(self):
        self.assertIn("runs-on: macos-26-intel", self.job)
        # La suite complète reste bornée (jusqu'à 3 h 50 mesuré), hors du
        # chemin du paquet : le plafond de 240 min ne bloque aucune fusion.
        self.assertIn("timeout-minutes: 240", self.job)


class CiMacosCoexistenceTest(unittest.TestCase):
    """#3198 : poser `ci-macos` ne publie pas le check requis `tests`.

    L'évènement `labeled` réveille les **deux** workflows (tests.yml écoute
    tous les labels, macos.yml seulement `ci-macos`). Ce témoin évalue les
    `if:` et le `name:` réellement livrés avec l'interpréteur d'expressions de
    #1468, sous deux contextes : étiquette `ci-macos` (tests.yml doit être
    entièrement silencieux et son agrégat renommé) et gel `ci-lot-<sha>`
    (tests.yml publie bien `tests`).
    """

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.tests_doc = canon.load_yaml(fh.read())
        with open(MACOS, encoding="utf-8") as fh:
            cls.macos_doc = canon.load_yaml(fh.read())
        cls.tests = cls.tests_doc["jobs"]
        cls.macos = cls.macos_doc["jobs"]

    @staticmethod
    def _eval(field, ctx):
        text = str(field).strip()
        if not text.startswith("${{"):
            text = "${{ %s }}" % text
        # l'interpréteur de #1468 ne connaît que les fonctions de routage
        text = text.replace("always()", "true").replace("cancelled()", "false")
        return evaluate(text, ctx)

    @classmethod
    def _template(cls, field, ctx):
        """Un scalaire mêlant littéral et un ou plusieurs `${{ … }}`."""
        def repl(match):
            return str(cls._eval("${{ %s }}" % match.group(1), ctx))
        return re.sub(r"\$\{\{(.*?)\}\}", repl, str(field))

    @staticmethod
    def _context(event, label, *, plan_result, outputs):
        return {
            "github": {
                "event_name": event,
                "repository": REPO,
                "ref": "refs/heads/main",
                "ref_name": "main",
                "base_ref": "",
                "head_ref": "",
                "event": {
                    "label": {"name": label} if label else {},
                    "pull_request": {
                        "number": 42,
                        "head": {"repo": {"full_name": REPO}},
                    },
                },
            },
            "needs": {"plan": {"result": plan_result, "outputs": outputs}},
            "vars": {},
        }

    def _ci_macos(self):
        return self._context("pull_request", "ci-macos",
                             plan_result="skipped", outputs={})

    def _freeze(self):
        return self._context(
            "pull_request", "ci-lot-" + "a" * 40, plan_result="success",
            outputs={"psycopg": "[1]", "psql": "[1]", "code": "true",
                     "docs_only": "false", "version_only": "false",
                     "release": "false"})

    def test_the_label_really_wakes_both_workflows(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            tests = fh.read()
        self.assertIn("pull_request:\n    types: [labeled]", tests)
        self.assertIn("pull_request_target:\n    types: [labeled]", self.text_macos())

    def text_macos(self):
        with open(MACOS, encoding="utf-8") as fh:
            return fh.read()

    def test_ci_macos_silences_every_tests_job(self):
        ctx = self._ci_macos()
        for job, spec in self.tests.items():
            with self.subTest(job=job):
                self.assertFalse(
                    bool(self._eval(spec.get("if", "true"), ctx)),
                    "tests.yml/%s tournerait sur l'étiquette ci-macos" % job)

    def test_ci_macos_renames_the_required_aggregate(self):
        ctx = self._ci_macos()
        nom = self._eval(self.tests["tests"]["name"], ctx)
        self.assertNotEqual(nom, "tests",
                            "le check requis tests serait publié sans tests")
        self.assertIn("ci-macos", str(nom))

    def test_a_freeze_still_publishes_the_required_tests_check(self):
        ctx = self._freeze()
        self.assertTrue(bool(self._eval(self.tests["plan"].get("if", "true"), ctx)))
        self.assertTrue(bool(self._eval(self.tests["tests"]["if"], ctx)))
        self.assertEqual(self._eval(self.tests["tests"]["name"], ctx), "tests")

    def test_the_macos_suite_follows_only_ci_macos(self):
        ci = self._context("pull_request_target", "ci-macos",
                           plan_result="skipped", outputs={})
        gel = self._context("pull_request_target", "ci-lot-" + "a" * 40,
                            plan_result="skipped", outputs={})
        condition = self.macos["suite"]["if"]
        self.assertTrue(bool(self._eval(condition, ci)))
        self.assertFalse(bool(self._eval(condition, gel)))

    def test_a_freeze_label_cannot_cancel_a_running_macos_suite(self):
        # Une étiquette ci-lot/ci-full arrive souvent pendant que la suite
        # ci-macos tourne : son groupe de concurrence doit être distinct et
        # elle ne doit rien annuler.
        ci = self._context("pull_request_target", "ci-macos",
                           plan_result="skipped", outputs={})
        gel = self._context("pull_request_target", "ci-lot-" + "a" * 40,
                            plan_result="skipped", outputs={})
        concurrence = self.macos_doc["concurrency"]
        self.assertNotEqual(self._template(concurrence["group"], ci),
                            self._template(concurrence["group"], gel))
        self.assertTrue(bool(self._eval(concurrence["cancel-in-progress"], ci)))
        self.assertFalse(bool(self._eval(concurrence["cancel-in-progress"], gel)))


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
