# SPDX-License-Identifier: AGPL-3.0-only
"""#6862 (EP8.5) : `poste.sh copie-hebdo`, la copie hebdomadaire du poste.

Décision owner #207987, point 7 : une copie hebdomadaire sur pc-smichea si
son disque est sain, jamais votante. Le banc est celui de `poste.sh
sauvegarder` (test_l232b_release_scripts : faux ssh/systemctl, dump serveur
simulé) ; ici les outils PostgreSQL du poste annoncent leur version.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import unittest

from tests import test_l232b_release_scripts as l232b

HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None

FAKE_TOOL = '''#!/bin/sh
if [ "$1" = --version ]; then echo "%s (PostgreSQL) ${FAKE_PG_MAJOR:-17}.6"; exit 0; fi
%s
'''


@unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent : sauvegarder-prive.py non exécutable")
class CopieHebdoTest(unittest.TestCase):
    def setUp(self):
        self.bench = l232b.MigrationPinningTest("test_missing_ref_takes_clone_head")
        self.bench.setUp()
        self.addCleanup(self.bench.doCleanups)
        bin_dir = self.bench.root / "bin"
        dump = (bin_dir / "pg_dump").read_text().split("\n", 1)[1]
        (bin_dir / "pg_dump").write_text(FAKE_TOOL % ("pg_dump", dump))
        (bin_dir / "pg_restore").write_text(FAKE_TOOL % ("pg_restore", "exit 0"))
        # l'installation de la minuterie : daemon-reload, enable, list-timers
        (bin_dir / "systemctl").rename(bin_dir / "systemctl.banc")
        (bin_dir / "systemctl").write_text(
            '#!/bin/sh\ncase "$1 $2" in\n'
            '"--user daemon-reload"|"--user enable"|"--user list-timers")\n'
            '  printf \'%s\\n\' "$*" >> "$SYSTEMCTL_CALLS"; exit 0 ;;\n'
            'esac\nexec "$(dirname "$0")/systemctl.banc" "$@"\n')
        (bin_dir / "systemctl").chmod(0o755)
        self.backups = Path(self.bench.env["HOME"]) / "backups/ameesh"

    def run_copy(self, *args, **env):
        return self.bench.run_script("copie-hebdo", *args,
                                     **dict({"COPIE_LIBRE_MIN_GIO": "1"}, **env))

    def copies(self):
        return sorted(p.name for p in self.backups.glob("mesh-hebdo-*.dump"))

    def test_healthy_disk_pulls_a_weekly_copy(self):
        done = self.run_copy()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        [name] = self.copies()
        copy = self.backups / name
        self.assertEqual(copy.read_bytes(), b"server dump 0\n")
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        # la copie du poste n'est pas un pg_dump du poste
        calls = self.bench.root / "pg_dump-calls"
        self.assertFalse(calls.exists() and calls.read_text().strip())
        self.assertFalse(list(self.backups.glob(".sonde-*")))

    def test_full_disk_postpones_without_touching_the_server(self):
        done = self.run_copy(COPIE_LIBRE_MIN_GIO="999999999")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("disque du poste refusé", done.stderr)
        self.assertEqual(self.copies(), [])
        self.assertNotIn("start ameesh-sauvegarde.service", self.bench.systemctl_calls())

    def test_old_client_tools_are_refused(self):
        done = self.run_copy(FAKE_PG_MAJOR="16")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("17 ou plus requis", done.stderr)
        self.assertEqual(self.copies(), [])

    def test_only_the_last_copies_are_kept(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        old = ["mesh-hebdo-2026-10-%02d-0530.dump" % day for day in (4, 11, 18, 25)]
        for name in old:
            (self.backups / name).write_bytes(b"old\n")
        other = self.backups / "mesh-2026-10-01-1200-avant-l60-l74.dump"
        other.write_bytes(b"staging\n")
        done = self.run_copy(COPIE_GARDEES="4")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        kept = self.copies()
        self.assertEqual(len(kept), 4)
        self.assertNotIn(old[0], kept)
        self.assertIn(old[3], kept)
        self.assertTrue(other.exists(), "un dump de staging n'est pas une copie hebdomadaire")

    def test_invalid_settings_are_refused(self):
        for setting in ("COPIE_GARDEES", "COPIE_PG_MIN"):
            with self.subTest(setting=setting):
                done = self.run_copy(**{setting: "0"})
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(setting + " invalide", done.stderr)

    def test_missing_client_tool_is_refused_with_code_2(self):
        # Relecture impl-08 : sous set -e/pipefail, l'outil absent sortait en 127.
        # (un pg_restore système peut exister : le faux rend le 127 du shell)
        (self.bench.root / "bin/pg_restore").write_text("#!/bin/sh\nexit 127\n")
        done = self.run_copy()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("pg_restore absent", done.stderr)
        self.assertEqual(self.copies(), [])

    def test_installer_writes_the_weekly_timer_once(self):
        done = self.run_copy("--installer")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        units = Path(self.bench.env["HOME"]) / ".config/systemd/user"
        service = (units / "ameesh-copie-hebdo.service").read_text()
        self.assertIn("poste.sh copie-hebdo", service)
        self.assertIn("OnCalendar=Sun", (units / "ameesh-copie-hebdo.timer").read_text())
        self.assertIn("--user enable --now ameesh-copie-hebdo.timer",
                      self.bench.systemctl_calls())
        again = self.run_copy("--installer")
        self.assertEqual(again.returncode, 1, again.stdout + again.stderr)


if __name__ == "__main__":
    unittest.main()
