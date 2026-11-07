# SPDX-License-Identifier: AGPL-3.0-only
"""#3854: the hourly packet tick of #3845 is posed by `ameesh role timers`.

Panier de #3845 (N/P2 de mty-amh-impl-04) : `ameesh-paquet-horaire` n'était
connu d'aucun chemin de déploiement. Témoin : un candidat qui déclare son clone
reçoit l'unité sous le nom de `deploy/systemd` (une copie manuelle est
remplacée, pas doublée) ; sans clone, la passe est un avertissement, jamais un
échec de `--check` ; un non-candidat ne reçoit rien.
"""
from __future__ import annotations

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ameesh import role_timers


def row(candidates=("agents-a", "agents-b")):
    return {"name": "mesh-design", "releve": "election", "host": "agents-a",
            "candidats": list(candidates)}


def plan(host="agents-a", paquet=("", "/srv/clone"), role="mesh-design"):
    return role_timers.plan(
        host=host, role=role, row=row(),
        settings=SimpleNamespace(projects={"ameesh": "manaty/ameesh"}, interval=900),
        watch_intervals=[900], notify_humans=["human:owner"], paquet=paquet)


class ReachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3854-reach-")
        self.root = Path(self.tmp.name)
        self.env = self.root / "paquet-horaire.env"
        self.clone = self.root / "clone"

    def tearDown(self):
        self.tmp.cleanup()

    def declare(self, text):
        self.env.write_text(text, encoding="utf-8")

    def test_without_the_env_file_the_pass_is_blocked(self):
        blocked, detail = role_timers.paquet_reach(str(self.env))
        self.assertIn("missing", blocked)
        self.assertIn("AMEESH_PAQUET_REPO", detail)

    def test_the_repo_is_read_from_the_file_not_the_shell(self):
        self.declare("# rien\nAMEESH_PAQUET_TARGET=main\n")
        blocked, _ = role_timers.paquet_reach(str(self.env))
        self.assertEqual(blocked, "AMEESH_PAQUET_REPO missing")

    def test_a_directory_that_is_not_a_clone_is_refused(self):
        self.clone.mkdir()
        self.declare("AMEESH_PAQUET_REPO=%s\n" % self.clone)
        blocked, _ = role_timers.paquet_reach(str(self.env))
        self.assertEqual(blocked, "AMEESH_PAQUET_REPO is not a git clone")

    def test_a_relative_or_tilde_path_is_refused(self):
        # systemd reads the EnvironmentFile literally (revue mty-amh-impl-04)
        for value in ("~/clone", "clone"):
            with self.subTest(value=value):
                self.declare("AMEESH_PAQUET_REPO=%s\n" % value)
                blocked, _ = role_timers.paquet_reach(str(self.env))
                self.assertEqual(blocked, "AMEESH_PAQUET_REPO is not an absolute path")

    def test_the_work_tree_answers_only_when_git_cannot(self):
        (self.clone / ".git").mkdir(parents=True)
        (self.clone / "scripts").mkdir()
        (self.clone / role_timers.PAQUET_SCRIPT).write_text("", encoding="utf-8")
        self.declare('AMEESH_PAQUET_REPO="%s"\n' % self.clone)

        def no_git(*args):
            raise FileNotFoundError("git")

        self.assertEqual(role_timers.paquet_reach(str(self.env), git=no_git),
                         ("", str(self.clone)))
        # origin/<target> is what the tick runs: a script left in the work
        # tree does not stand for it (revue mty-amh-impl-04)
        blocked, _ = role_timers.paquet_reach(
            str(self.env), git=lambda *args: subprocess.CompletedProcess(args, 128))
        self.assertIn("absent from the clone", blocked)

    def test_a_clone_behind_the_target_is_read_on_origin(self):
        (self.clone / ".git").mkdir(parents=True)
        self.declare("AMEESH_PAQUET_REPO=%s\nAMEESH_PAQUET_TARGET=develop\n" % self.clone)
        calls = []

        def git(*args, code=0):
            calls.append(args)
            return subprocess.CompletedProcess(args, code)

        self.assertEqual(role_timers.paquet_reach(str(self.env), git=git)[0], "")
        self.assertEqual(calls, [("cat-file", "-e",
                                  "origin/develop:" + role_timers.PAQUET_SCRIPT)])
        blocked, detail = role_timers.paquet_reach(
            str(self.env), git=lambda *args: git(*args, code=128))
        self.assertIn("absent from the clone", blocked)
        self.assertIn("#3845", detail)


class PlanTest(unittest.TestCase):
    def test_without_the_reach_the_plan_is_unchanged(self):
        report = role_timers.plan(
            host="agents-a", role="mesh-design", row=row(),
            settings=SimpleNamespace(projects={"ameesh": "manaty/ameesh"}, interval=900),
            watch_intervals=[900], notify_humans=["human:owner"])
        self.assertNotIn(role_timers.PAQUET_PASS,
                         [item["name"] for item in report["timers"]])

    def test_a_candidate_gets_the_packet_tick_under_the_deploy_name(self):
        item = plan()["timers"][-1]
        self.assertEqual(item["name"], "paquet-horaire")
        self.assertEqual(item["unit"], "ameesh-paquet-horaire")
        self.assertEqual(item["interval_s"], 600)
        self.assertFalse(item["blocked"])

    def test_a_missing_clone_is_advisory(self):
        item = plan(paquet=("AMEESH_PAQUET_REPO missing", "declare it"))["timers"][-1]
        self.assertEqual(item["blocked"], "AMEESH_PAQUET_REPO missing")
        self.assertTrue(item["advisory"])

    def test_another_role_does_not_carry_the_tick(self):
        names = [item["name"] for item in plan(role="auditeur")["timers"]]
        self.assertNotIn(role_timers.PAQUET_PASS, names)

    def test_a_non_candidate_gets_nothing(self):
        self.assertEqual(plan(host="agents-c")["timers"], [])


class RenderTest(unittest.TestCase):
    spec = role_timers.TimerSpec(role_timers.PAQUET_PASS, role_timers.PAQUET_PASS,
                                 600, role_timers.PAQUET_DESCRIPTION)

    def test_the_service_ticks_the_clone_script_under_the_role_lease(self):
        text = role_timers.render_service(self.spec, role="mesh-design",
                                          bin_path="/opt/ameesh/bin/ameesh")
        self.assertIn("ExecStartPre=/usr/bin/git -C ${AMEESH_PAQUET_REPO} fetch "
                      "--tags --force --quiet origin ${AMEESH_PAQUET_TARGET}", text)
        self.assertIn("ExecStartPre=/usr/bin/git -C ${AMEESH_PAQUET_REPO} merge "
                      "--ff-only --quiet origin/${AMEESH_PAQUET_TARGET}", text)
        self.assertNotIn("pull --ff-only", text)
        self.assertIn("ExecStart=/opt/ameesh/bin/ameesh role run mesh-design -- "
                      "/usr/bin/env PYTHONPATH=${AMEESH_PAQUET_REPO}/src "
                      "/opt/ameesh/bin/python ${AMEESH_PAQUET_REPO}/scripts/"
                      "paquet-horaire.py tick --repo ${AMEESH_PAQUET_REPO} "
                      "--target ${AMEESH_PAQUET_TARGET} --pilot ${AMEESH_PAQUET_PILOT}",
                      text)
        self.assertIn("EnvironmentFile=%h/.config/ameesh/paquet-horaire.env\n", text)
        self.assertIn("Environment=AMEESH_PAQUET_PILOT=mesh-design", text)
        self.assertIn("TimeoutStartSec=4h", text)
        self.assertNotIn("notify --once", text)

    def test_the_timer_keeps_the_wall_clock_of_3845(self):
        text = role_timers.render_timer(self.spec)
        self.assertIn("OnCalendar=*:0/10", text)
        self.assertNotIn("OnUnitActiveSec", text)


class PacketFetchTest(unittest.TestCase):
    """#6128: a pilot fetch in the same clone must not lose the packet tick.

    `git pull` merges `FETCH_HEAD` after its own fetch. A pilot composition
    fetching in the same clone at that instant leaves two heads there, and
    the merge dies with ``fatal: Cannot fast-forward to multiple branches``.
    The generated pre-step fetches, then fast-forwards on the named remote
    ref, so `FETCH_HEAD` is never read.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="6128-fetch-")
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.clone = self.root / "clone"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(self.origin))
        self.git("init", "-q", "--initial-branch=main", str(self.seed))
        (self.seed / "file.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "file.txt", cwd=self.seed)
        self.git("commit", "-q", "-m", "one", cwd=self.seed)
        self.git("branch", "pilot", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        self.git("push", "-q", "origin", "main", "pilot", cwd=self.seed)
        self.git("clone", "-q", str(self.origin), str(self.clone))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None, check=True):
        proc = subprocess.run(
            ["git", "-c", "user.name=6128", "-c", "user.email=6128@ameesh.invalid",
             *args], cwd=str(cwd or self.root), capture_output=True, text=True)
        if check and proc.returncode != 0:
            self.fail("git %s: %s" % (" ".join(args), proc.stderr.strip()))
        return proc

    def pre_steps(self):
        """The unit's fetch and merge `ExecStartPre`, variables resolved."""
        text = role_timers.render_paquet_service(role="mesh-design")
        lines = [line[len("ExecStartPre="):] for line in text.splitlines()
                 if line.startswith("ExecStartPre=")]
        self.assertTrue(lines, "the generated unit has no pre-step")
        for line in lines:
            self.assertNotIn(" pull ", line)
            self.assertNotIn("FETCH_HEAD", line)
        commands = [shlex.split(line.replace("${AMEESH_PAQUET_REPO}", str(self.clone))
                                .replace("${AMEESH_PAQUET_TARGET}", "main"))
                    for line in lines]
        fetch = [i for i, command in enumerate(commands) if "fetch" in command]
        merge = [i for i, command in enumerate(commands) if "merge" in command]
        self.assertEqual(len(fetch), 1, "one fetch pre-step: %r" % commands)
        self.assertEqual(len(merge), 1, "one merge pre-step: %r" % commands)
        self.assertLess(fetch[0], merge[0], "the fetch runs before the merge")
        self.assertIn("--tags", commands[fetch[0]])
        self.assertIn("--ff-only", commands[merge[0]])
        self.assertEqual(commands[merge[0]][-1], "origin/main")
        return commands[fetch[0]], commands[merge[0]]

    def run_step(self, command):
        return subprocess.run(command, capture_output=True, text=True)

    def test_a_concurrent_pilot_fetch_does_not_lose_the_tick(self):
        fetch, merge = self.pre_steps()
        # a commit lands on origin/main while pilot advances elsewhere
        (self.seed / "file.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", "two", cwd=self.seed)
        self.git("push", "-q", "origin", "main", cwd=self.seed)
        self.git("switch", "-q", "pilot", cwd=self.seed)
        (self.seed / "pilot.txt").write_text("three\n", encoding="utf-8")
        self.git("add", "pilot.txt", cwd=self.seed)
        self.git("commit", "-q", "-m", "three", cwd=self.seed)
        self.git("push", "-q", "origin", "pilot", cwd=self.seed)
        self.git("fetch", "-q", "origin", "main", "pilot", cwd=self.clone)
        main = self.git("rev-parse", "origin/main", cwd=self.clone).stdout.strip()
        pilot = self.git("rev-parse", "origin/pilot", cwd=self.clone).stdout.strip()
        self.assertNotEqual(main, pilot)
        step = self.run_step(fetch)
        self.assertEqual(step.returncode, 0, step.stderr)
        # the pilot's fetch lands between the two steps: two heads, the state
        # `git pull` used to read
        (self.clone / ".git" / "FETCH_HEAD").write_text(
            "%s\t\tbranch 'main' of origin\n%s\t\tbranch 'pilot' of origin\n"
            % (main, pilot), encoding="utf-8")
        step = self.run_step(merge)
        self.assertEqual(step.returncode, 0, step.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.clone).stdout.strip(),
                         main)
        self.assertEqual((self.clone / "file.txt").read_text(encoding="utf-8"), "two\n")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3854-timers-")
        self.dir = Path(self.tmp.name)
        (self.dir / "ameesh-notify.service").write_text("[Service]\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_replaces_a_hand_copy_and_check_is_green(self):
        hand = self.dir / "ameesh-paquet-horaire.service"
        hand.write_text("[Service]\nExecStart=/old\n", encoding="utf-8")
        report = plan()
        result = role_timers.install(report, directory=self.dir, apply=False)
        self.assertIn("ameesh-paquet-horaire.service", result["installed"])
        self.assertIn("ameesh-paquet-horaire.timer", result["installed"])
        self.assertFalse((self.dir / "ameesh-role-paquet-horaire.service").exists())
        self.assertIn("paquet-horaire.py tick", hand.read_text(encoding="utf-8"))
        # the packet tick is not a notify pass: the drop-in is unchanged
        dropin = self.dir / "ameesh-notify.service.d" / "10-role-timers.conf"
        self.assertIn("--skip issues,github-watch,ci,alerts\n",
                      dropin.read_text(encoding="utf-8"))
        self.assertEqual(role_timers.check(report, directory=self.dir, apply=False), [])

    def test_a_blocked_tick_is_a_warning_and_loses_its_unit(self):
        role_timers.install(plan(), directory=self.dir, apply=False)
        report = plan(paquet=("paquet-horaire.env missing", "declare it"))
        result = role_timers.install(report, directory=self.dir, apply=False)
        self.assertIn("ameesh-paquet-horaire.timer", result["removed"])
        self.assertEqual(role_timers.check(report, directory=self.dir, apply=False), [])
        self.assertEqual(role_timers.warnings(report),
                         ["paquet-horaire: paquet-horaire.env missing — declare it"])

    def test_check_names_a_stale_packet_unit(self):
        report = plan()
        role_timers.install(report, directory=self.dir, apply=False)
        (self.dir / "ameesh-paquet-horaire.timer").write_text("old", encoding="utf-8")
        problems = role_timers.check(report, directory=self.dir, apply=False)
        self.assertTrue(any("ameesh-paquet-horaire.timer is out of date" in text
                            for text in problems))


if __name__ == "__main__":
    unittest.main()
