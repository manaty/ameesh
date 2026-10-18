# SPDX-License-Identifier: AGPL-3.0-only
"""#2457 : runners shell/systemd/workflows, périmètre Git et faux positifs."""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import unittest

from tests.test_2449_garde_mecanique import garde


class RunnerTest(unittest.TestCase):
    def test_shell_cutoffs(self):
        for body in ('HOURS=(11 23)', 'HOURS="23,11"',
                     'AT="11:00 23:00"', 'AT="11:00:00 23:00:00"',
                     'CUTOFF_HOUR=11', 'RELEASE_HOUR="23"',
                     'CRON="0 11,23 * * *"', 'CRON="0 23,11 * * *"',
                     'CRON="0 11 * * *"', 'CRON="0 11,23 * * 1-5"',
                     'if [ "$hour" -eq 11 ]; then release; fi'):
            with self.subTest(body=body):
                self.assertTrue(garde.inspect_runner('bin/release.sh', body))
        # Le contexte peut venir du code actif, avec un nom français.
        self.assertTrue(garde.inspect_runner('bin/run.sh',
                                            'COUPURES="11:00 23:00"'))

    def test_timer_calendars(self):
        for value in ('*-*-* 11,23:00', '*-*-* 23,11:00:00 Europe/Paris',
                      '11:00', '*-*-* 23:00:00', 'Mon..Fri *-*-* 11:00'):
            with self.subTest(value=value):
                found = garde.inspect_runner('deploy/release.timer',
                                             '[Timer]\nOnCalendar=' + value)
                self.assertTrue(found)
                self.assertEqual(found[0].line, 2)
        found = garde.inspect_runner('deploy/run.timer',
                                     '[Unit]\nDescription=Release\n[Timer]\n'
                                     'OnCalendar=11:00\nOnCalendar=23:00')
        self.assertEqual([f.line for f in found], [4, 5])

    def test_workflow_cron_and_inline_shell(self):
        for body in ("on:\n  schedule:\n    - cron: '0 11,23 * * *'",
                     'on: {schedule: [{cron: "0 23,11 * * *"}]}',
                     "on:\n  schedule:\n    - cron: '0 11 * * *'\n"
                     "    - cron: '0 23 * * *'",
                     'jobs:\n  release:\n    steps:\n      - run: |\n'
                     '          HOURS=(11 23)'):
            with self.subTest(body=body):
                self.assertTrue(garde.inspect_runner(
                    '.github/workflows/release.yml', body))

    def test_unrelated_values_comments_and_computed_parameters(self):
        cases = [
            ('bin/run.sh', '# release\nHOURS=(11 23)'),
            ('bin/release.sh', '# AT="11:00"\nHOURS=(9 17)'),
            ('bin/release.sh', 'AT="11:00:01 23:00:30"'),
            ('bin/release.sh', 'CRON="5 11,23 * * *"'),
            ('bin/release.sh', 'AT="111:00 123:00"'),
            ('bin/release.sh', 'HOURS=$(ameesh module mechanics cutoffs)'),
            ('bin/backup.sh', 'AT="11:00 23:00"'),
            ('deploy/backup.timer', '[Timer]\nOnCalendar=11:00'),
            ('deploy/release.timer', '[Timer]\n; OnCalendar=11:00\n'
             '# OnCalendar=23:00\nOnCalendar=09:00'),
            ('deploy/release.timer', '[Unit]\nDescription=11:00\n'
             '[Timer]\nOnCalendar=23:30'),
            ('.github/workflows/backup.yml', "cron: '0 11,23 * * *'"),
            ('.github/workflows/release.yml', "# cron: '0 11,23 * * *'"),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(garde.inspect_runner(path, body), [])

    def test_comments_respect_quotes_and_escapes(self):
        self.assertEqual(garde._active_line('X="a # b" # c'), 'X="a # b" ')
        self.assertTrue(garde.inspect_runner('bin/run.sh',
                                            'X="release # 11:00"'))

    def test_exemptions_keep_path_parameter_and_literal_scope(self):
        found = garde.inspect_runner('bin/release.sh', 'AT="11:00"')
        exemption = garde.Exemption('bin/release.sh', 'cutoffs', '11:00',
                                    'test #2457', '2026-10-18', 'fixture')
        self.assertEqual(garde.apply_exemptions(found, (exemption,))[0], [])
        other = garde.inspect_runner('bin/other-release.sh', 'AT="11:00"')
        self.assertEqual(garde.apply_exemptions(other, (exemption,))[0], other)


class FilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True)

    def test_supported_formats_exclusions_and_external_symlink(self):
        body = 'AT="11:00"\n'
        for name in ('bin/release.sh', 'bin/release.bash',
                     '.github/workflows/release.yml',
                     '.github/workflows/release.yaml'):
            self.write(name, body)
        self.write('deploy/release.timer', '[Timer]\nOnCalendar=11,23:00')
        self.write('bin/release', '#!/usr/bin/env bash\n' + body)
        self.write('bin/release-dash', '#!/bin/sh\n' + body)
        for name in ('tests/release.sh', 'reviews/release.sh',
                     'docs/release.md', 'other/release.yaml', 'bin/release.txt'):
            self.write(name, body)
        self.write('bin/plain', body)
        (self.root / 'bin/release-link.sh').symlink_to(self.root / 'bin/release.sh')
        files = garde.source_files(self.root)
        self.assertEqual(len(files), 7, [rel for rel, _ in files])
        self.assertEqual(len(garde.scan_repository(self.root, ()).findings), 7)

    def test_git_empty_index_and_untracked_files_are_not_scanned(self):
        self.git('init', '-q')
        self.write('bin/release.sh', 'AT="11:00"')
        self.assertEqual(garde.source_files(self.root), [])
        self.git('add', 'bin/release.sh')
        self.write('bin/release-other.sh', 'AT="23:00"')
        self.assertEqual([rel for rel, _ in garde.source_files(self.root)],
                         ['bin/release.sh'])

    def test_cli_reports_file_line_and_rejects_non_python_runner(self):
        self.write('.github/workflows/release.yaml',
                   "on:\n  schedule:\n    - cron: '0 11,23 * * *'\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = garde.main(['--root', str(self.root), '--json'])
        self.assertEqual(status, 1)
        finding = json.loads(output.getvalue())['findings'][0]
        self.assertEqual((finding['path'], finding['line'], finding['parameter']),
                         ('.github/workflows/release.yaml', 3, 'cutoffs'))


if __name__ == '__main__':
    unittest.main()
