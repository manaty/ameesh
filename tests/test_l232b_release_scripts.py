# SPDX-License-Identifier: AGPL-3.0-only
"""Release pinning through the real dispatcher, with isolated fake migrations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import support  # noqa: F401 — environnement isolé du poste

# #3129 : le vrai cat, résolu avant les faux bancs (/usr/bin/cat sous Linux, /bin/cat sous macOS).
REAL_CAT = shutil.which("cat") or "/bin/cat"


ROOT = Path(__file__).resolve().parents[1]
# poste.sh « sauvegarder » exécute sauvegarder-prive.py, qui parle psycopg : la
# part psql de la CI n'en a pas, ces deux témoins y sont sautés et couverts par
# la part psycopg (même règle que test_653_db_connections).
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
FAKE_MODULE = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["RELEASE_CALLS"], "a") as out:
    out.write(json.dumps({"release": Path(__file__).with_name("release").read_text(),
                          "args": args}) + "\\n")
# Like cli.cmd_migrate, a dry-run reports nonadditivity without refusing.
if args[0] == "migrate" and "--dry-run" not in args and "--additive-only" in args and Path(__file__).with_name("nonadditive").exists():
    sys.exit(3)
'''
# The real bascule prepares a complete versioned venv with pip; the sequencing
# of poste.sh is what #2357 tests, so the fixture records the calls instead.
FAKE_BASCULE = '''import json, os, sys
with open(os.environ["RELEASE_CALLS"], "a") as out:
    out.write(json.dumps({"release": "bascule", "args": sys.argv[1:]}) + "\\n")
'''


class MigrationPinningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-l232b-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid",
                        RELEASE_CALLS=str(self.root / "calls"),
                        SYSTEMCTL_CALLS=str(self.root / "systemctl.calls"),
                        SYSTEMD_RUN_CALLS=str(self.root / "systemd-run.calls"),
                        BASCULE_ENV=str(self.root / "bascule.env"))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        (bin_dir / "cat").write_text(
            '#!/bin/sh\ncase "$1" in /proc/*/cgroup) printf "%s\\n" "${FAKE_CGROUP_TEXT:-0::/user.slice/session.scope}" ;; *) exec ' + REAL_CAT + ' "$@" ;; esac\n')
        (bin_dir / "cat").chmod(0o755)
        (bin_dir / "pg_dump").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "${PG_DUMP_CALLS:?}"\n'
            'while [ "$1" != --file ]; do shift; done\nprintf archive > "$2"\n')
        (bin_dir / "pg_dump").chmod(0o755)
        (bin_dir / "pg_restore").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "pg_restore").chmod(0o755)
        (bin_dir / "ssh").write_text(
            '#!/bin/sh\nfor arg do command=$arg; done\n'
            '/bin/sh -c "$command"\n')
        (bin_dir / "ssh").chmod(0o755)
        (bin_dir / "systemctl").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SYSTEMCTL_CALLS"\ncase "$1" in\n'
            '--user) case "$2" in\n'
            '  list-units) exit 0 ;;\n'
            '  restart|is-active) exit 0 ;;\n'
            '  *) exit 97 ;; esac ;;\n'
            'start) [ "${BACKUP_START_STATUS:-0}" = 0 ] || exit "${BACKUP_START_STATUS}";\n'
            '  d=${SAUVEGARDES_VM:?}; mkdir -p "$d";\n'
            '  if [ -n "${BACKUP_SAME_NAME:-}" ]; then\n'
            '    printf "server dump rewritten\\n" > "$d/$BACKUP_SAME_NAME"; exit 0; fi;\n'
            '  n=$(find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" | wc -l);\n'
            '  printf "server dump %s\\n" "$n" > "$d/ameesh-simulation-20261017-1500$(printf "%02d" "$n").dump";\n'
            '  exit 0 ;;\n'
            'show) case "$4" in\n'
            'Result) echo "${BACKUP_RESULT:-success}" ;;\n'
            'ExecMainStatus) echo "${BACKUP_EXIT_STATUS:-0}" ;;\n'
            '*) exit 97 ;; esac ;;\n'
            '*) exit 98 ;; esac\n')
        (bin_dir / "systemctl").chmod(0o755)
        # #3042 : macOS n'a pas /usr/bin/sha256sum (coreutils le fournit dans
        # libexec/gnubin). Résoudre le vrai binaire AVANT d'ajouter `bin_dir`
        # au PATH, sinon le faux se rappelle lui-même.
        vrai_sha256sum = shutil.which("sha256sum") or "/usr/bin/sha256sum"
        (bin_dir / "sha256sum").write_text(
            '#!/bin/sh\n'
            'if [ "${FAKE_SHA256_BAD:-0}" = 1 ]; then\n'
            '  printf "%064d  %s\\n" 0 "${2:-}"\n'
            '  exit 0\n'
            'fi\n'
            'exec ' + shlex.quote(vrai_sha256sum) + ' "$@"\n')
        (bin_dir / "sha256sum").chmod(0o755)
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "sleep").chmod(0o755)
        # The real host has systemd-run; the fixture controls whether the user
        # manager accepts the emergency transient restart.
        (bin_dir / "systemd-run").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SYSTEMD_RUN_CALLS"\n'
            'exit "${SYSTEMD_RUN_STATUS:-1}"\n')
        (bin_dir / "systemd-run").chmod(0o755)
        (bin_dir / "ls").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "ls").chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        self.remote_dumps = self.root / "remote-dumps"
        self.remote_dumps.mkdir()
        self.env["SAUVEGARDES_VM"] = str(self.remote_dumps)
        self.env["PG_DUMP_CALLS"] = str(self.root / "pg_dump-calls")
        self.git("init", "-q")
        self.script = self.repo / "deploy/mise-a-jour/poste.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copy(ROOT / "deploy/mise-a-jour/poste.sh", self.script)
        shutil.copy(ROOT / "deploy/mise-a-jour/sauvegarder-prive.py", self.script.with_name("sauvegarder-prive.py"))
        # #2357: a failed probe runs installer + redemarrer, which source
        # bail.sh and call bascule.py; both are stubbed for sequencing.
        shutil.copy(ROOT / "deploy/mise-a-jour/bail.sh", self.script.with_name("bail.sh"))
        self.script.with_name("bascule.py").write_text(FAKE_BASCULE)
        self.module = self.repo / "src/ameesh"
        self.module.mkdir(parents=True)
        (self.module / "__init__.py").write_text("")
        (self.module / "__main__.py").write_text(FAKE_MODULE)
        # poste.sh splits the DSN's password through the deployed commit's
        # src/ameesh/dsn.py (#518); a real snapshot carries it, so the fixture does.
        shutil.copy(ROOT / "src/ameesh/dsn.py", self.module / "dsn.py")
        self.a = self.commit("A")
        self.b = self.commit("B")
        (self.root / "bascule.env").write_text("VM=simulation\n")
        self.share = Path(self.env["HOME"]) / ".local/share/ameesh"
        venv = self.share / "venv/bin"
        venv.mkdir(parents=True)
        # The probe is run by the installed interpreter, outside PYTHONPATH.
        # Fake that installed entry here; the real probe has PostgreSQL tests.
        (venv / "python").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = -I ] && [ "${2##*/}" = auto-take-probe.py ]; then\n'
            "  printf '%s\\n' '{\"release\":\"installed\",\"args\":[\"auto-take-probe\"]}' >> \"$RELEASE_CALLS\"\n"
            '  exit "${AUTO_TAKE_PROBE_STATUS:-0}"\n'
            'fi\n'
            # installer's post-switch import control needs the real packages;
            # the fake venv has none, the sequencing is what matters here.
            'case "$2" in *"import psycopg"*) exit 0 ;; esac\n'
            'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
        (venv / "python").chmod(0o755)
        config = Path(self.env["HOME"]) / ".config/ameesh/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"dsn": "postgresql://simulation"}))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              check=True, text=True, capture_output=True).stdout.strip()

    def commit(self, release, nonadditive=False):
        (self.module / "release").write_text(release)
        if nonadditive:
            (self.module / "nonadditive").touch()
        self.git("add", ".")
        self.git("commit", "-qm", release)
        return self.git("rev-parse", "HEAD")

    def run_script(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args],
                              env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=20)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def systemctl_calls(self):
        path = self.root / "systemctl.calls"
        return path.read_text().splitlines() if path.exists() else []

    def systemd_run_calls(self):
        path = self.root / "systemd-run.calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_approved_a_with_head_b_only_runs_a(self):
        done = self.run_script("migrer", self.a)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([call["release"] for call in calls], ["A", "A", "A", "installed"])
        self.assertEqual(calls[0]["args"], ["migrate", "--dry-run", "--additive-only"])
        self.assertEqual(calls[1]["args"], ["migrate", "--additive-only"])
        self.assertEqual(calls[2]["args"], ["doctor", "--probe"])

    def test_incompatible_installed_auto_take_installs_and_restarts(self):
        # #2357: the database is already migrated, so stopping here would leave
        # the old code serving it. The target passed doctor --probe: migrer
        # installs that same commit and restarts the host at once.
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("ancien code incompatible avec le schéma migré", done.stderr)
        self.assertIn("installation et redémarrage immédiats", done.stdout)
        self.assertEqual([(call["release"], call["args"][0]) for call in self.calls()], [
            ("A", "migrate"), ("A", "migrate"), ("A", "doctor"),
            ("installed", "auto-take-probe"),
            ("A", "doctor"),
            ("bascule", "preparer"), ("bascule", "activer"),
        ])
        preparer = self.calls()[5]["args"]
        self.assertEqual(preparer[0], "preparer")
        self.assertIn("src-" + self.a[:7], " ".join(preparer))
        # The fake user manager refuses the transient unit, so the direct
        # restart runs (a plain human shell); the transient path is below.
        self.assertIn("unité transitoire indisponible", done.stderr)
        self.assertIn("--user restart ameesh-notify.service", self.systemctl_calls())

    def test_incompatible_probe_restarts_from_a_transient_unit(self):
        # #2357: launched from an agent's own runner, the restart must go
        # through a transient unit, or it would stop that runner before the
        # following ones (docs/AUDITEUR.md).
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1",
                               SYSTEMD_RUN_STATUS="0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("redémarrage terminé dans l'unité transitoire", done.stderr)
        lancements = self.systemd_run_calls()
        self.assertEqual(len(lancements), 1)
        self.assertIn("--user --collect --wait --unit poste-maj-urgence-", lancements[0])
        self.assertIn('/bin/sh -c "$0" redemarrer', lancements[0])
        self.assertNotIn("--user restart ameesh-notify.service", self.systemctl_calls())

    def test_emergency_fallback_in_agent_cgroup_refuses_without_stopping(self):
        done = self.run_script(
            "migrer", self.a, AUTO_TAKE_PROBE_STATUS="1", SYSTEMD_RUN_STATUS="1",
            FAKE_CGROUP_TEXT="0::/user.slice/ameesh-runner-agent@dev9.service")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("redémarrage refusé depuis une unité ameesh-*", done.stderr)
        self.assertIn("ÉCHEC du redémarrage immédiat", done.stderr)
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual(len(self.systemd_run_calls()), 1)

    def test_emergency_install_failure_stops_before_activation_and_restart(self):
        bascule = self.script.with_name("bascule.py")
        bascule.write_text(FAKE_BASCULE + '\nif sys.argv[1] == "preparer": sys.exit(42)\n')
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ÉCHEC de l'installation immédiate", done.stderr)
        self.assertEqual([c["args"][0] for c in self.calls() if c["release"] == "bascule"],
                         ["preparer"])
        self.assertFalse((self.share / "src-current").is_symlink())
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual(self.systemd_run_calls(), [])

    def test_emergency_direct_restart_failure_is_reported(self):
        systemctl = self.root / "bin/systemctl"
        systemctl.write_text('#!/bin/sh\nexit 42\n')
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ÉCHEC du redémarrage immédiat", done.stderr)

    def test_target_probe_failure_stops_before_installed_probe(self):
        snapshot = self.share / ("src-" + self.a[:7]) / "src/ameesh/__main__.py"
        self.run_script("migrer", self.a)
        snapshot.write_text(FAKE_MODULE + '\nif args[0] == "doctor": sys.exit(42)\n')
        (self.root / "calls").unlink()
        done = self.run_script("migrer", self.a, MIGRER_SANS_REDEMARRAGE="1")
        self.assertEqual(done.returncode, 42)
        self.assertEqual([c["args"][0] for c in self.calls()], ["migrate", "migrate", "doctor"])
        self.assertEqual(self.systemctl_calls(), [])

    def test_staging_defers_the_emergency_install_and_restart(self):
        # staging.sh runs migrer before its per-host sequence (VMs first, poste
        # last, canon §14). It must not be stopped, and the poste must not be
        # restarted here before the VMs: staging installs and restarts itself.
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1",
                               MIGRER_SANS_REDEMARRAGE="1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("la séquence staging poursuit sans s'arrêter", done.stderr)
        self.assertEqual([(call["release"], call["args"][0]) for call in self.calls()], [
            ("A", "migrate"), ("A", "migrate"), ("A", "doctor"),
            ("installed", "auto-take-probe"),
        ])
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual(self.systemd_run_calls(), [])

    def test_probe_failure_with_ref_already_installed_still_restarts(self):
        # Re-run after installer, or MIGRER_APRES=1: the venv already holds REF,
        # but the running processes may still be the old code. Skip the
        # reinstallation (which would overwrite the rollback precedent) and
        # restart at once.
        (self.share / "src-current").symlink_to("src-" + self.a[:7])
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("déjà installé : redémarrage immédiat", done.stderr)
        self.assertEqual([(call["release"], call["args"][0]) for call in self.calls()], [
            ("A", "migrate"), ("A", "migrate"), ("A", "doctor"),
            ("installed", "auto-take-probe"),
        ])
        self.assertIn("--user restart ameesh-notify.service", self.systemctl_calls())

    def test_unusable_probe_is_handled_like_a_failure_with_its_own_message(self):
        # #2359: exit 2 = probe unusable, not proven incompatible; #2357: like
        # a failure, it hands over to the install and restart (here deferred
        # to the staging sequence).
        done = self.run_script("migrer", self.a, AUTO_TAKE_PROBE_STATUS="2",
                               MIGRER_SANS_REDEMARRAGE="1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("sonde inutilisable", done.stderr)
        self.assertNotIn("ancien code incompatible", done.stderr)
        self.assertIn("la séquence staging poursuit sans s'arrêter", done.stderr)

    def test_missing_ref_takes_clone_head(self):
        # #1245 : sans REF, « migrer » migre le HEAD du dépôt de déploiement,
        # comme « installer » ; le contrôleur L232b migre, lui, son commit épinglé.
        done = self.run_script("migrer")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([call["release"] for call in calls], ["B", "B", "B", "installed"])
        self.assertEqual(calls[0]["args"], ["migrate", "--dry-run", "--additive-only"])
        self.assertEqual(calls[1]["args"], ["migrate", "--additive-only"])

    def test_installed_nonadditive_still_refused_with_override(self):
        c = self.commit("C", nonadditive=True)
        # Legacy installed state cannot be an implicit migration authorisation.
        (self.share / "src-current").symlink_to("src-" + c[:7])
        done = self.run_script("migrer", c, MIGRER_APRES="1")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.calls(), [
            {"release": "C", "args": ["migrate", "--dry-run", "--additive-only"]},
            {"release": "C", "args": ["migrate", "--additive-only"]},
        ])

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_failed_backup_start_is_propagated(self):
        done = self.run_script("sauvegarder", BACKUP_START_STATUS="42")
        self.assertEqual(done.returncode, 42, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_completed_backup_result_and_exit_status_are_required(self):
        for env in ({"BACKUP_RESULT": "exit-code"}, {"BACKUP_EXIT_STATUS": "1"}):
            with self.subTest(env=env):
                done = self.run_script("sauvegarder", **env)
                self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_successful_oneshot_does_not_require_active_state(self):
        done = self.run_script("sauvegarder")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_mesh_dump_is_pulled_from_the_server_not_dumped_from_the_poste(self):
        done = self.run_script("sauvegarder")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        calls = self.root / "pg_dump-calls"
        self.assertFalse(calls.exists() and calls.read_text().strip(),
                         "pg_dump ran on the poste for the mesh database")
        dumps = list((Path(self.env["HOME"]) / "backups/ameesh").glob(
            "mesh-*-avant-l60-l74.dump"))
        self.assertEqual(len(dumps), 1, dumps)
        self.assertEqual(dumps[0].read_bytes(), b"server dump 0\n")
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_dump_rewritten_under_the_same_name_is_pulled(self):
        # #2424 : le script serveur déployé réécrit le dump du jour sous le
        # même nom ; il reste reconnu comme nouveau.
        name = "ameesh-simulation-20261017.dump"
        self.remote_dumps.mkdir(parents=True, exist_ok=True)
        old = self.remote_dumps / name
        old.write_bytes(b"old dump\n")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        done = self.run_script("sauvegarder", BACKUP_SAME_NAME=name)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        dumps = list((Path(self.env["HOME"]) / "backups/ameesh").glob(
            "mesh-*-avant-l60-l74.dump"))
        self.assertEqual(len(dumps), 1, dumps)
        self.assertEqual(dumps[0].read_bytes(), b"server dump rewritten\n")

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_corrupt_retrieval_never_publishes_and_logs_bytes(self):
        done = self.run_script("sauvegarder", FAKE_SHA256_BAD="1")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("digest mismatch", done.stderr)
        self.assertIn("bytes written", done.stderr)
        self.assertIn("rapatriement en échec", done.stderr)
        self.assertFalse(list((Path(self.env["HOME"]) / "backups/ameesh").glob(
            "mesh-*-avant-l60-l74.dump")))
