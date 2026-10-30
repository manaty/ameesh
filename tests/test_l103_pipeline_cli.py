# SPDX-License-Identifier: AGPL-3.0-only
"""L103 T3: exact git admission and real native commands on PostgreSQL."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
from unittest import mock
import uuid

from tests.support import PgTestCase
from tests.test_l103_pipeline_card import carte
from ameesh import canon, modules, pipeline_cli, process_projection, process_registry
from ameesh import work
from ameesh.modules.registry import PipelineModule
from ameesh.modules.storage import ModuleStore


class PipelineCliTest(PgTestCase):
    def setUp(self):
        super().setUp()
        installed = dict(process_registry.INSTALLED_VERIFIERS)
        def restore_verifiers():
            process_registry.INSTALLED_VERIFIERS.clear()
            process_registry.INSTALLED_VERIFIERS.update(installed)
        self.addCleanup(restore_verifiers)
        self.db.execute('TRUNCATE project_modules CASCADE')
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        (self.repo / 'pipelines').mkdir()
        self.path = self.repo / 'pipelines' / 'delivery.md'
        self.path.write_text(carte(name='delivery'))
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'card')
        self.sha = self.git('rev-parse', 'HEAD').strip()
        self.ref = 'acme:pipelines/delivery.md@' + self.sha
        self.loaded = canon.Canon(root=str(self.repo), readable=True, id='test',
            sources=[canon.Source('acme', str(self.repo), repo=str(self.repo),
                                  commit=self.sha)])
        patch = mock.patch.object(canon, 'load_configured', return_value=[self.loaded])
        patch.start()
        self.addCleanup(patch.stop)
        self.name, self.spec, self.encoding = pipeline_cli.pipeline_at(self.ref, cfg=self.cfg)
        self.module = PipelineModule(canon.Pipeline(
            canon.Fiche('Pipeline', 'delivery', 'acme', 'pipelines/delivery.md', self.ref, {}),
            name='delivery', artifact=self.spec))
        self.loaded.pipelines = [self.module.pipeline]
        self.db.execute("INSERT INTO agent_registry (name, status) VALUES ('author', 'idle')")
        self.lot = self.db.query("INSERT INTO work_items (title, type, app, state, assignee) "
            "VALUES ('pipeline test', 'evolution', 'acme', 'intake', 'author') RETURNING id")[0]['id']
        ModuleStore(self.db).replace('', [{'project': 'acme', 'module': 'delivery',
                                         'types': [], 'schemas': {}, 'rules': []}])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def run_cli(self, *args, actor='author', expected=0):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = pipeline_cli.main(self.db, list(map(str, args)), actor=actor,
                                     module=self.module, cfg=self.cfg)
        self.assertEqual(code, expected, errors.getvalue())
        return json.loads(output.getvalue()) if code == 0 else errors.getvalue()

    def test_adopt_pins_and_journals_without_receipt_then_refuses_second_pin(self):
        out = self.run_cli('adopt', self.lot)
        state = process_registry.registry_state(self.db, self.lot)
        self.assertEqual((state.workflow_ref, state.workflow_stage, state.workflow_version,
                          state.receipts), (self.ref, 'draft', 0, 0))
        self.assertEqual(out['workflow_ref'], self.ref)
        event = self.db.query('SELECT actor, command_id FROM work_item_events '
                              'WHERE work_item_id = %s', (self.lot,))
        self.assertEqual(event, [{'actor': 'author', 'command_id': None}])
        self.assertIn('already-pinned', self.run_cli('adopt', self.lot, expected=1))
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot, 'build', actor='author')

    def test_commands_receipts_replay_hold_release_and_projection(self):
        self.run_cli('adopt', self.lot)
        key = str(uuid.uuid4())
        args = ('advance', self.lot, '--to', 'backlog', '--guard', 'planning',
                '--command-id', key)
        receipt = self.run_cli(*args)
        self.assertFalse(receipt['replayed'])
        self.assertTrue(self.run_cli(*args)['replayed'])
        self.assertIn('version-conflict', self.run_cli('advance', self.lot,
            '--to', 'sprint', '--guard', 'planning', '--expected-version', 0, expected=1))
        self.run_cli('hold', self.lot, '--hold', 'blocked')
        held = work.get(self.db, self.lot)
        self.assertEqual((held['state'], held['workflow_stage']), ('blocked', 'backlog'))
        self.assertIn('held', self.run_cli('advance', self.lot, '--to', 'sprint',
                                         '--guard', 'planning', expected=1))
        self.run_cli('release', self.lot)
        self.run_cli('advance', self.lot, '--to', 'sprint', '--guard', 'planning')
        self.assertEqual(process_registry.registry_state(self.db, self.lot).receipts, 4)
        mapping = process_projection.ProcessMapping('acme', self.ref, 0, self.spec,
                                                    self.encoding.stage_labels)
        projected = process_projection.project_issue(work.get(self.db, self.lot), mapping)
        self.assertEqual(projected.label, 'Sprint')

    def test_undeclared_edge_or_stage_writes_no_receipt(self):
        self.run_cli('adopt', self.lot)
        for stage in ('missing', 'done'):
            self.run_cli('advance', self.lot, '--to', stage, '--guard', 'planning', expected=1)
        self.assertEqual(process_registry.registry_state(self.db, self.lot).receipts, 0)

    def test_adoption_refusals_leave_pin_and_journal_empty(self):
        self.db.execute("INSERT INTO agent_registry (name, status) VALUES ('other', 'idle')")
        self.assertIn('authority', self.run_cli('adopt', self.lot, actor='other', expected=1))
        self.db.execute("UPDATE work_items SET state='build' WHERE id=%s", (self.lot,))
        self.assertIn('stage-mismatch', self.run_cli('adopt', self.lot, expected=1))
        self.db.execute("UPDATE work_items SET state='closed', close_reason='abandoned' WHERE id=%s", (self.lot,))
        self.assertIn('terminal', self.run_cli('adopt', self.lot, expected=1))
        self.db.execute("UPDATE work_items SET state='intake', close_reason=NULL WHERE id=%s", (self.lot,))
        self.db.execute("DELETE FROM project_modules")
        self.assertIn('inactive', self.run_cli('adopt', self.lot, expected=1))
        self.assertFalse(work.get(self.db, self.lot)['workflow_ref'])
        self.assertEqual(process_registry.registry_state(self.db, self.lot).version, 0)

    def test_resolution_uses_pinned_git_object_after_local_and_head_changes(self):
        pipeline_cli._compiled.cache_clear()
        self.path.write_text('broken local card')
        self.git('add', '.')
        self.git('-c', 'user.name=test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'new broken revision')
        self.loaded.sources[0].commit = self.git('rev-parse', 'HEAD').strip()
        self.assertEqual(pipeline_cli.pipeline_at(self.ref, cfg=self.cfg)[1], self.spec)
        self.run_cli('adopt', self.lot)
        self.run_cli('advance', self.lot, '--to', 'backlog', '--guard', 'planning')

    def test_module_dispatch_discovers_card(self):
        with mock.patch('ameesh.config.load', return_value=self.cfg), \
             mock.patch('ameesh.db.connect', return_value=self.db), \
             mock.patch.object(self.db, 'close'), \
             mock.patch.object(work, 'resolve_actor', return_value=('author', None)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(modules.main(['delivery', 'adopt', str(self.lot)]), 0)

    def test_pilot_can_adopt_another_assignees_item(self):
        self.db.execute("INSERT INTO agent_registry(name, status) VALUES ('pilot', 'idle')")
        with mock.patch.dict('os.environ', {'AMEESH_ALERT_ORCHESTRATORS': 'pilot'}):
            self.run_cli('adopt', self.lot, actor='pilot')
        self.assertEqual(self.db.query('SELECT actor FROM work_item_events '
                         'WHERE work_item_id=%s', (self.lot,))[0]['actor'], 'pilot')

    def test_stopped_actor_and_delivered_lot_refused(self):
        self.db.execute("UPDATE agent_registry SET status='stopped' WHERE name='author'")
        self.assertIn('authority-revoked', self.run_cli('adopt', self.lot, expected=1))
        self.db.execute("UPDATE agent_registry SET status='idle' WHERE name='author'")
        self.db.execute("UPDATE work_items SET state='merged' WHERE id=%s", (self.lot,))
        self.assertIn('terminal', self.run_cli('adopt', self.lot, expected=1))

    def test_adoption_rollback_if_journal_fails(self):
        from ameesh.db import DbError
        self.db.script("""
            CREATE FUNCTION refuse_adoption_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test journal failure'; END $$;
            CREATE TRIGGER refuse_adoption BEFORE INSERT ON work_item_events
            FOR EACH ROW EXECUTE FUNCTION refuse_adoption_event();
        """)
        try:
            with self.assertRaises(DbError):
                self.run_cli('adopt', self.lot)
            self.assertFalse(work.get(self.db, self.lot)['workflow_ref'])
        finally:
            self.db.execute('DROP TRIGGER refuse_adoption ON work_item_events')
            self.db.execute('DROP FUNCTION refuse_adoption_event()')

    def test_other_module_and_changed_intent_cannot_reuse_command(self):
        self.run_cli('adopt', self.lot)
        key = str(uuid.uuid4())
        self.run_cli('advance', self.lot, '--to', 'backlog', '--guard', 'planning',
                     '--command-id', key)
        self.assertIn('intent-conflict', self.run_cli('advance', self.lot,
            '--to', 'sprint', '--guard', 'planning', '--command-id', key, expected=1))
        self.module.NAME = 'another'
        self.assertIn('pipeline-module', self.run_cli('release', self.lot, expected=1))

    def test_broken_or_missing_pinned_card_and_untrusted_adoption_refuse(self):
        self.module.pipeline.fiche.untrusted = True
        self.assertIn('untrusted', self.run_cli('adopt', self.lot, expected=1))
        for ref in ('acme:pipelines/missing.md@' + self.sha,
                    'acme:pipelines/delivery.md@HEAD'):
            with self.assertRaises(work.WorkError):
                pipeline_cli.pipeline_at(ref, cfg=self.cfg)

    def test_concurrent_adoptions_commit_one_pin_and_one_journal_line(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from ameesh import db as db_mod
        barrier = Barrier(2)

        def adopt():
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(timeout=10)
                try:
                    work.adopt_workflow(connection, self.lot, module='delivery',
                                        spec=self.spec, actor='author')
                    return 'accepted'
                except work.WorkError as exc:
                    return str(exc)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: adopt(), range(2)))
        self.assertEqual(results.count('accepted'), 1, results)
        self.assertTrue(any('already-pinned' in result for result in results), results)
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_item_events '
                         'WHERE work_item_id=%s', (self.lot,))[0]['n'], 1)

    def test_guard_reads_real_freeze_and_cli_references(self):
        self.run_cli('adopt', self.lot)
        self.run_cli('advance', self.lot, '--to', 'implementation', '--guard', 'start_work')
        head = 'b' * 40
        args = ('advance', self.lot, '--to', 'review', '--guard', 'review_requested',
                '--ref', 'head=' + head)
        self.assertIn('verifier-refused', self.run_cli(*args, expected=1))
        self.assertEqual(process_registry.registry_state(self.db, self.lot).receipts, 1)
        self.db.execute("INSERT INTO work_item_milestones(work_item_id, kind, sha, actor) "
                        "VALUES(%s, 'frozen', %s, 'author')", (self.lot, head))
        self.db.execute("INSERT INTO agent_registry(name, status) VALUES ('reviewer', 'idle')")
        self.db.execute("UPDATE work_items SET reviewer='reviewer' WHERE id=%s", (self.lot,))
        self.assertEqual(self.run_cli(*args)['workflow_stage'], 'review')
