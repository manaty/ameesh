# SPDX-License-Identifier: AGPL-3.0-only
"""Rotation de vrais fichiers : ctime, inodes ouverts, archives et échecs."""
import gzip
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from .test_5325_ci_disque import disque


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'Worker_20261101.log'
        self.log.write_bytes(b'journal\n' * 10000)
        self.now = time.time() + 73 * 3600
        self.open_patch = mock.patch.object(disque, '_journaux_ouverts', return_value=set())
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def rotate(self, **kw):
        return disque._rotation_diag([self.root], maintenant=self.now, **kw)

    def test_closed_log_compressed_losslessly(self):
        contenu = self.log.read_bytes()
        result = self.rotate()
        self.assertFalse(self.log.exists())
        archive = self.log.with_suffix('.log.gz')
        self.assertEqual(gzip.decompress(archive.read_bytes()), contenu)
        self.assertEqual(result['fichiers'], 1)
        self.assertGreater(result['octets'], 0)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_open_old_listener_is_kept(self):
        info = self.log.stat()
        with mock.patch.object(disque, '_journaux_ouverts',
                               return_value={(info.st_dev, info.st_ino)}):
            self.assertEqual(self.rotate()['fichiers'], 0)
        self.assertTrue(self.log.exists())

    def test_antidated_log_symlink_and_non_log_are_kept(self):
        os.utime(self.log, (100, 100))
        (self.root / 'Runner_link.log').symlink_to(self.log)
        (self.root / 'other.log').write_text('x')
        self.now = time.time()
        self.assertEqual(self.rotate()['fichiers'], 0)
        self.assertTrue(self.log.exists())

    def test_existing_archive_is_never_overwritten(self):
        archive = self.log.with_suffix('.log.gz')
        archive.write_bytes(b'preuve')
        self.rotate()
        self.assertEqual(archive.read_bytes(), b'preuve')
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.part')))

    def test_archive_has_its_own_seven_day_retention(self):
        self.rotate()
        archive = self.log.with_suffix('.log.gz')
        self.assertEqual(self.rotate()['fichiers'], 0)
        self.now = time.time() + 8 * 86400
        self.assertEqual(self.rotate()['fichiers'], 1)
        self.assertFalse(archive.exists())

    def test_dry_run_and_unreadable_process_preserve_source(self):
        self.assertEqual(self.rotate(dry_run=True)['fichiers'], 1)
        self.assertTrue(self.log.exists())
        with mock.patch.object(disque, '_journaux_ouverts', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.rotate()
        self.assertTrue(self.log.exists())

    def test_compression_failure_removes_partial_and_keeps_source(self):
        with mock.patch.object(disque.gzip.GzipFile, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertFalse(list(self.root.glob('*.gz')))

    def test_modified_source_is_not_removed(self):
        original = disque.gzip.GzipFile.write
        changed = []
        def write(gz, data):
            if not changed:
                changed.append(True)
                with self.log.open('ab') as f:
                    f.write(b'new')
            return original(gz, data)
        with mock.patch.object(disque.gzip.GzipFile, 'write', write):
            self.rotate()
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.gz')))

    def test_big_log_resumes_across_passes_instead_of_restarting(self):
        contenu = b'x' * 5000
        self.log.write_bytes(contenu)
        part = self.log.with_name(self.log.name + '.gz.part')
        etat = self.log.with_name(self.log.name + '.gz.state')
        premier = disque._rotation_diag([self.root], maintenant=self.now,
                                        budget_s=0, taille_bloc=1000)
        self.assertEqual(premier['fichiers'], 0)
        self.assertEqual(premier['partiels'], 1)
        self.assertTrue(part.exists() and etat.exists())
        self.assertTrue(self.log.exists())
        passes = 0
        while not self.log.with_suffix('.log.gz').exists() and passes < 10:
            disque._rotation_diag([self.root], maintenant=self.now,
                                  budget_s=0, taille_bloc=1000)
            passes += 1
        self.assertLess(passes, 10)
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)
        self.assertFalse(self.log.exists())
        self.assertFalse(part.exists())
        self.assertFalse(etat.exists())

    def test_stale_partial_without_state_is_discarded(self):
        contenu = b'journal\n' * 1000
        self.log.write_bytes(contenu)
        part = self.log.with_name(self.log.name + '.gz.part')
        part.write_bytes(b'residu illisible')
        resultat = self.rotate()
        self.assertEqual(resultat['fichiers'], 1)
        self.assertEqual(resultat['residus'], 1)
        self.assertFalse(part.exists())
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)

    def test_partial_of_a_changed_source_is_restarted(self):
        self.log.write_bytes(b'a' * 2000)
        premier = disque._rotation_diag([self.root], maintenant=self.now,
                                        budget_s=0, taille_bloc=1000)
        self.assertEqual(premier['partiels'], 1)
        with self.log.open('ab') as fichier:
            fichier.write(b'b' * 500)
        resultat = self.rotate()
        self.assertEqual(resultat['residus'], 1)
        self.assertEqual(resultat['fichiers'], 1)
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         b'a' * 2000 + b'b' * 500)

    def test_unrecorded_partial_suffix_is_truncated_then_resumed(self):
        contenu = b'z' * 3000
        self.log.write_bytes(contenu)
        disque._rotation_diag([self.root], maintenant=self.now, budget_s=0,
                              taille_bloc=1000)
        part = self.log.with_name(self.log.name + '.gz.part')
        with part.open('ab') as fichier:
            fichier.write(b'queue non enregistree')
        passes = 0
        while not self.log.with_suffix('.log.gz').exists() and passes < 10:
            disque._rotation_diag([self.root], maintenant=self.now,
                                  budget_s=0, taille_bloc=1000)
            passes += 1
        self.assertLess(passes, 10)
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)
        self.assertFalse(part.exists())

    def test_empty_log_is_archived(self):
        self.log.write_bytes(b'')
        resultat = self.rotate()
        self.assertEqual(resultat['fichiers'], 1)
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()), b'')

    def test_report_names_partial_residue_and_permission_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'r1'
            (root / '_diag').mkdir(parents=True)
            with mock.patch.object(disque, '_racine_runner', return_value=root), \
                    mock.patch.object(disque, '_rotation_diag', return_value={
                        'fichiers': 2, 'octets': 10, 'partiels': 1,
                        'residus': 1, 'interrompu': True}):
                resultat = disque._diag(False)
            self.assertEqual(resultat['libere'], 10)
            self.assertIn('résidu', resultat['sortie'])
            self.assertIn('en cours', resultat['sortie'])
            self.assertIn('reprise', resultat['sortie'])
            with mock.patch.object(disque, '_racine_runner', return_value=root), \
                    mock.patch.object(disque, '_journaux_ouverts',
                                      side_effect=PermissionError('refus fd')):
                resultat = disque._diag(False)
            self.assertIn('PermissionError', resultat['sortie'])
            self.assertIn('non tentée', resultat['sortie'])

    def test_linked_partial_is_not_followed(self):
        """Un `.gz.part` lié n'est jamais la sortie : sa cible reste intacte."""
        contenu = b'journal\n' * 1000
        self.log.write_bytes(contenu)
        cible = self.root / 'cible-du-partiel'
        cible.write_bytes(b'precieux' * 100)
        part = self.log.with_name(self.log.name + '.gz.part')
        etat = self.log.with_name(self.log.name + '.gz.state')
        part.symlink_to(cible)
        info = cible.stat()
        etat.write_text(json.dumps({
            'identite': disque._identite(self.log.lstat()),
            'offset': 100, 'part_size': info.st_size,
            'part_dev': info.st_dev, 'part_ino': info.st_ino}))
        resultat = self.rotate()
        self.assertEqual(cible.read_bytes(), b'precieux' * 100)
        self.assertEqual(resultat['fichiers'], 1)
        self.assertEqual(resultat['residus'], 1)
        self.assertFalse(part.exists())
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)

    def test_linked_state_tmp_is_not_written(self):
        """Un `.gz.state.tmp` lié n'est jamais écrit : sa cible reste intacte."""
        contenu = self.log.read_bytes()
        cible = self.root / 'cible-etat'
        cible.write_bytes(b'precieux')
        temporaire = self.log.with_name(self.log.name + '.gz.state.tmp')
        temporaire.symlink_to(cible)
        resultat = self.rotate()
        self.assertEqual(cible.read_bytes(), b'precieux')
        self.assertEqual(resultat['fichiers'], 1)
        self.assertTrue(temporaire.is_symlink())
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)

    def test_linked_state_is_not_read(self):
        """Un `.gz.state` lié n'est pas une source de confiance : résidu jeté."""
        contenu = b'journal\n' * 1000
        self.log.write_bytes(contenu)
        identite = disque._identite(self.log.lstat())
        part = self.log.with_name(self.log.name + '.gz.part')
        part.write_bytes(b'partiel')
        cible = self.root / 'cible-etat-lu'
        texte = json.dumps({'identite': identite, 'offset': 1, 'part_size': 7,
                            'part_dev': 1, 'part_ino': 1})
        cible.write_text(texte)
        etat = self.log.with_name(self.log.name + '.gz.state')
        etat.symlink_to(cible)
        resultat = self.rotate()
        self.assertEqual(resultat['fichiers'], 1)
        self.assertEqual(resultat['residus'], 1)
        self.assertEqual(cible.read_text(), texte)
        self.assertEqual(gzip.decompress(self.log.with_suffix('.log.gz').read_bytes()),
                         contenu)


class ProcessReadingTest(unittest.TestCase):
    def test_only_runner_descriptors_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'log'
            log.write_text('x')
            for pid, comm in [('1', 'Runner.Listener'), ('2', 'other')]:
                p = root / pid
                (p / 'fd').mkdir(parents=True)
                (p / 'comm').write_text(comm)
                (p / 'fd' / '1').symlink_to(log)
            original = disque._journaux_ouverts
            self.assertIn((log.stat().st_dev, log.stat().st_ino), original(root))


class InstallationTest(unittest.TestCase):
    def test_install_verify_remove_without_system_changes(self):
        import subprocess
        repo = Path(__file__).resolve().parent.parent
        installer = repo / 'deploy/ci-flotte/install-disque.sh'
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CI_FLOTTE_TEST_ROOT=tmp)
            def run(mode):
                return subprocess.run(['bash', str(installer), mode], env=env,
                                      text=True, capture_output=True)
            self.assertNotEqual(run('--check').returncode, 0)
            installed = run('--install')
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            tool = Path(tmp) / 'usr/local/lib/ameesh-ci/ci-disque.py'
            self.assertTrue(os.access(tool, os.X_OK))
            self.assertEqual(run('--check').returncode, 0)
            tool.write_text('drift')
            self.assertNotEqual(run('--check').returncode, 0)
            self.assertEqual(run('--remove').returncode, 0)
            self.assertFalse(tool.exists())


class DiskTransportTest(unittest.TestCase):
    def setUp(self):
        from ameesh import ci_disk
        self.disk = ci_disk
        self.now = 1793700000
        self.repo = 'acme/widget'
        self.host = 'ci-a'
        self.payload = dict(schema=ci_disk.SCHEMA, host=self.host, volume='/',
                            free_bytes=1024**3, total_bytes=100 * 1024**3)

    def row(self, **over):
        out = dict(self.payload, repo=self.repo, run_id=42, sampled_ts=self.now)
        out.pop('schema')
        out.update(over)
        return out

    def annotation(self, **over):
        import json
        return dict(title=self.disk.SCHEMA, message=json.dumps(dict(self.payload, **over)))

    def test_parser_rejects_wrong_host_schema_and_invalid_sizes(self):
        for over in [dict(host='other'), dict(schema='other'), dict(free_bytes=-1),
                     dict(total_bytes=0), dict(free_bytes=True), dict(volume='/other'),
                     dict(free_bytes=2**64), dict(total_bytes=2**64)]:
            self.assertIsNone(self.disk.annotation(self.annotation(**over), self.host))
        self.assertEqual(self.disk.annotation(self.annotation(), self.host)['free_bytes'], 1024**3)

    def test_low_and_healthy_follow_native_thresholds(self):
        from ameesh import exploitation, notify
        low = self.disk.findings([self.row()], [], self.now)
        self.assertEqual([r['type'] for r in low], ['ci_disk_low'])
        self.assertIn('ci_disk_low', notify.DEFAULT_TYPES)
        self.assertEqual(exploitation.alert_key(low[0]),
                         exploitation.alert_key(dict(low[0], run_id=43, since=self.now + 3600)))
        self.assertEqual(self.disk.findings([self.row(free_bytes=80 * 1024**3)], [], self.now), [])

    def test_stale_low_stays_low_and_reports_missing_sampling(self):
        out = self.disk.findings([self.row(sampled_ts=self.now - 2 * 3600 - 1)], [], self.now)
        self.assertEqual({r['type'] for r in out}, {'ci_disk_low', 'ci_disk_unreadable'})
        self.assertEqual([r['type'] for r in self.disk.findings(
            [self.row(free_bytes=None, total_bytes=None, sampled_ts=None)], [], self.now)],
                         ['ci_disk_unreadable'])

    def test_first_measure_grace_uses_first_seen_not_last(self):
        healthy = dict(free_bytes=80 * 1024**3, total_bytes=100 * 1024**3)
        first = self.row(sampled_ts=None, first_seen_ts=self.now, seen_ts=self.now,
                         **healthy)
        self.assertEqual(self.disk.findings([first], [], self.now), [])
        self.assertEqual(self.disk.findings([first], [], self.now + self.disk.MAX_AGE), [])
        late = self.row(sampled_ts=None,
                        first_seen_ts=self.now - self.disk.MAX_AGE - 1,
                        seen_ts=self.now, **healthy)
        self.assertEqual([r['type'] for r in self.disk.findings([late], [], self.now)],
                         ['ci_disk_unreadable'])
        # B1 de la revue : une collecte qui repousse seen_ts ne renouvelle pas
        # la grâce ; seule first_seen_ts la porte.
        refreshed = dict(late, seen_ts=self.now + 1800)
        self.assertEqual([r['type'] for r in self.disk.findings([refreshed], [], self.now)],
                         ['ci_disk_unreadable'])
        # Une mesure déjà reçue puis vieillie n'a pas de grâce : la sonde ne
        # remonte plus, même si la VM est encore dans les jobs.
        old = self.row(sampled_ts=self.now - self.disk.MAX_AGE - 1,
                       first_seen_ts=self.now, seen_ts=self.now, **healthy)
        self.assertEqual([r['type'] for r in self.disk.findings([old], [], self.now)],
                         ['ci_disk_unreadable'])

    def test_collector_binds_annotation_to_base_workflow_job_and_timestamp(self):
        from types import SimpleNamespace
        calls, measures, expected, forgotten = [], [], [], []
        def api(path):
            calls.append(path)
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=44, head_branch='evil', event='pull_request'),
                                          dict(id=42, head_branch='main', event='schedule')]}
            if '/jobs?' in path:
                return {'jobs': [dict(name='disque ci-a', conclusion='success',
                    completed_at='2026-11-03T10:00:00Z',
                    check_run_url='https://api.github.com/repos/acme/widget/check-runs/99')]}
            return [self.annotation(host='wrong'), self.annotation()]
        store = SimpleNamespace(expect_disk=lambda *a, **kw: expected.append(kw),
                                observe_disk=lambda *a, **kw: measures.append(kw),
                                forget_disks=lambda repo, hosts, **kw: forgotten.append(
                                    (repo, hosts, kw)))
        self.disk.collect(api, self.repo, 'main', store)
        self.assertEqual(len(measures), 1)
        self.assertEqual(measures[0]['run_id'], 42)
        self.assertEqual(measures[0]['host'], self.host)
        self.assertEqual(len(expected), 1)
        self.assertFalse(any('/runs/44/' in call for call in calls))
        self.assertEqual(measures[0]['sampled_ts'], 1793700000)
        self.assertEqual(forgotten, [(self.repo, {self.host},
                                      {'older_than_s': self.disk.FORGET_AGE})])

    def test_no_job_seen_forgets_nothing(self):
        from types import SimpleNamespace
        forgotten = []
        def api(path):
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=42, head_branch='main',
                                               event='schedule')]}
            return {'jobs': []}
        store = SimpleNamespace(expect_disk=lambda *a, **kw: None,
                                observe_disk=lambda *a, **kw: None,
                                forget_disks=lambda *a, **kw: forgotten.append(kw))
        self.disk.collect(api, self.repo, 'main', store)
        self.assertEqual(forgotten, [])

    def test_probe_emits_parseable_notice_without_docker(self):
        import contextlib
        import io
        import json
        output = io.StringIO()
        with mock.patch.object(disque, 'espace', return_value=dict(chemin='/', libre=1,
                                total=100, utilise_pct=99)), contextlib.redirect_stdout(output):
            disque.main(['mesure', '--host', 'ci-a'])
        data = json.loads(output.getvalue().strip().split('::')[-1])
        self.assertEqual(data['host'], 'ci-a')
        self.assertEqual((data['free_bytes'], data['used_pct']), (1, 99))

    def test_native_host_threshold_overrides_default(self):
        from types import SimpleNamespace
        canon = SimpleNamespace(id='test', host=lambda name: SimpleNamespace(
            policy={'resources': {'warn_disk_free': '90 GiB', 'warn_disk_used': '95%'}}))
        rows = self.disk.findings([self.row(free_bytes=80 * 1024**3)], [canon], self.now)
        self.assertEqual([r['type'] for r in rows], ['ci_disk_low'])

    def test_watcher_collects_on_a_bounded_cadence(self):
        from ameesh import ci
        from types import SimpleNamespace
        watcher = ci.Watcher(SimpleNamespace(), api=lambda path: None, clock=lambda: self.now)
        with mock.patch.object(watcher, 'discovered_workflows', return_value=(self.disk.WORKFLOW,)), \
                mock.patch.object(ci.storage, 'of', return_value=SimpleNamespace(ci=object())), \
                mock.patch.object(self.disk, 'collect') as collect:
            watcher._disks(object(), self.repo, ('main',))
            watcher._disks(object(), self.repo, ('main',))
            collect.assert_called_once()
            self.assertEqual(collect.call_args.args[2], 'main')
