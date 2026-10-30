# SPDX-License-Identifier: AGPL-3.0-only
"""L243b step 1: alternate spellings never duplicate identity or authority."""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from ameesh import agent_names, authority, canon, canon_sync, exploitation, identity, mail, placement
from ameesh import fil, platform, registry, reprise, runner, session_bindings, work
from .support import PgTestCase
from .test_canon import _TmpMixin, codes, fiche, write
from .test_l39_adoption_reprise import SID, _dossier, _rollout


class AgentAliasTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('TRUNCATE agent_name_resolutions RESTART IDENTITY')
        registry.upsert(self.db, 'worker', host=self.cfg.host, harness='codex')
        registry.upsert(self.db, 'reviewer', host=self.cfg.host, harness='codex')
        # #4779 : les fiches de test portent `propose` et un rôle, comme les
        # agents du canon ; sans quoi la désignation manuelle est refusée.
        self.db.execute(
            "UPDATE agent_registry SET capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name IN ('worker', 'reviewer')")
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-worker', 'another-name'] "
                        "WHERE name = 'worker'")

    def test_primary_alias_and_unknown(self):
        for name in ('worker', 'old-worker', 'another-name'):
            self.assertEqual(agent_names.resolve(name, db=self.db), 'worker')
            self.assertEqual(registry.get(self.db, name)['name'], 'worker')
        self.assertEqual(agent_names.resolve('missing', db=self.db), 'missing')
        self.assertEqual(agent_names.resolve('human:old-worker', db=self.db), 'human:old-worker')
        self.assertIsNone(registry.get(self.db, 'missing'))
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM agent_registry')[0]['n'], 2)

    def test_resolution_logged_once_with_operation(self):
        self.assertEqual(agent_names.resolve('old-worker', db=self.db, operation='lease.test'), 'worker')
        events = self.db.query('SELECT * FROM agent_name_resolutions')
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['supplied_name'], events[0]['primary_name'], events[0]['operation']),
                         ('old-worker', 'worker', 'lease.test'))

    def test_collision_with_primary_alias_and_self_is_refused(self):
        for aliases in (['reviewer'], ['worker'], ['old-worker', 'old-worker'], ['bad/name'], ['x\n']):
            with self.subTest(aliases=aliases):
                with self.assertRaises(Exception):
                    self.db.execute('UPDATE agent_registry SET aliases = ARRAY(SELECT '
                                    'jsonb_array_elements_text(%s::jsonb)) WHERE name = %s',
                                    (json.dumps(aliases), 'worker'))
        with self.assertRaises(Exception):
            registry.upsert(self.db, 'third', host=self.cfg.host)
            self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-worker'] WHERE name='third'")
        # Registration under an existing alias updates the original, never creates a row.
        registry.upsert(self.db, 'old-worker', status_text='same identity')
        self.assertEqual(registry.get(self.db, 'worker')['status_text'], 'same identity')

    def test_human_key_collision_in_both_directions(self):
        with self.assertRaises(Exception):
            authority.register_human_key(self.db, 'old-worker', b'x' * 32)
        authority.register_human_key(self.db, 'alice', b'x' * 32)
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET aliases = ARRAY['alice'] WHERE name='worker'")
        authority.revoke_key(self.db, 'alice')
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET aliases = ARRAY['alice'] WHERE name='worker'")

    def test_the_key_holder_namespace_covers_the_primary_name_and_the_alias(self):
        """#897 N2: la 0364 garde le nom primaire (creation, renommage, cle),
        la 0418 garde l'alias ; l'exception stopgap du geste du proprietaire
        reste celle de 0364 (test_330 la pin)."""
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO key_holders (name, human, public_key, "
                            "public_key_fingerprint, key_role) "
                            "VALUES ('reviewer', 'human:reviewer', 'k', 'f', 'agent')")
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO key_holders (name, human, public_key, "
                            "public_key_fingerprint, key_role) "
                            "VALUES ('old-worker', 'human:old-worker', 'k', 'f', 'agent')")
        authority.register_human_key(self.db, 'alice', b'x' * 32)
        with self.assertRaises(Exception):
            registry.upsert(self.db, 'alice', host=self.cfg.host)
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET name = 'alice' WHERE name = 'worker'")
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET aliases = ARRAY['alice'] WHERE name = 'worker'")
        # une cle revoquee occupe toujours le nom
        authority.revoke_key(self.db, 'alice')
        with self.assertRaises(Exception):
            registry.upsert(self.db, 'alice', host=self.cfg.host)

    def test_same_lease_and_stale_epoch_remains_fenced(self):
        claimed = registry.claim(self.db, 'old-worker', 'runner-test', 120)
        self.assertEqual(claimed['name'], 'worker')
        self.assertIsNone(registry.claim(self.db, 'worker', 'other-runner', 120))
        self.assertTrue(registry.lease_matches(self.db, 'another-name', 'runner-test', claimed['lease_epoch'])[0])
        self.assertFalse(registry.lease_matches(self.db, 'old-worker', 'runner-test', claimed['lease_epoch'] - 1)[0])
        self.assertIsNotNone(registry.renew(self.db, 'old-worker', 'runner-test', claimed['lease_epoch'], 120))
        self.assertTrue(registry.release(self.db, 'another-name', 'runner-test', claimed['lease_epoch']))
        events = self.db.query('SELECT operation FROM agent_name_resolutions')
        self.assertIn('registry.claim', [e['operation'] for e in events])
        self.assertIn('registry.lease_matches', [e['operation'] for e in events])

    def test_human_registration_waits_for_an_uncommitted_alias(self):
        import threading
        attempted = threading.Event()
        outcome = []
        other = self.connect()
        def register_human():
            attempted.set()
            try:
                authority.register_human_key(other, 'reserved-alias', b'x' * 32)
                outcome.append('committed')
            except Exception as exc:
                outcome.append(str(exc))
        thread = threading.Thread(target=register_human, daemon=True)
        try:
            with self.db.transaction() as tx:
                tx.execute("UPDATE agent_registry SET aliases=aliases || ARRAY['reserved-alias'] "
                           "WHERE name='worker'")
                thread.start()
                self.assertTrue(attempted.wait(3))
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIn('alias', outcome[0])
            self.assertFalse(self.db.query("SELECT 1 FROM key_holders WHERE name='reserved-alias'"))
        finally:
            thread.join(10)
            other.close()

    def test_actual_namespace_cannot_be_shadowed_by_search_path(self):
        authority.register_human_key(self.db, 'alice', b'x' * 32)
        with self.assertRaises(Exception):
            with self.db.transaction() as tx:
                tx.execute('CREATE TEMP TABLE key_holders (name text)')
                tx.execute('CREATE TEMP TABLE agent_registry (name text, aliases text[])')
                tx.execute('UPDATE "' + self.schema + '".agent_registry '
                           "SET aliases=ARRAY['alice'] WHERE name='worker'")
        self.assertEqual(registry.get(self.db, 'old-worker')['name'], 'worker')

    def test_authority_trace_survives_a_later_transaction_rollback(self):
        with self.assertLogs('ameesh.identity', level='WARNING') as output:
            with self.assertRaises(RuntimeError):
                with self.db.transaction() as tx:
                    agent_names.resolve('old-worker', db=tx, operation='authority.denied')
                    raise RuntimeError('Denied')
        self.assertIn('old-worker -> worker (authority.denied)', output.output[0])

    def test_alias_audit_is_append_only(self):
        agent_names.resolve('old-worker', db=self.db, operation='lease.claim')
        for sql in ("UPDATE agent_name_resolutions SET primary_name='reviewer'",
                    'DELETE FROM agent_name_resolutions'):
            with self.assertRaises(Exception):
                self.db.execute(sql)

    def test_alias_lookup_on_a_read_only_connection(self):
        # GET /api/v1/agents/{a} reads through a read-only connection (serve.db_reader).
        with self.db.transaction() as tx:
            tx.execute('SET TRANSACTION READ ONLY')
            self.assertEqual(registry.get(tx, 'old-worker')['name'], 'worker')
        self.assertFalse(self.db.query('SELECT 1 FROM agent_name_resolutions'))

    def test_binding_resolves_alias_before_fencing(self):
        lease = registry.claim(self.db, 'worker', 'runner-test', 120)
        with mock.patch.dict(os.environ, {'AGENT_MAIL_NAME':'old-worker',
                            'AMEESH_RUNNER_ID':'runner-test',
                            'AMEESH_LEASE_EPOCH':str(lease['lease_epoch'])}):
            binding = identity.resolve_binding(self.cfg, self.db)
            self.assertTrue(binding.ok, binding.reason)
            self.assertEqual(binding.name, 'worker')

    def test_mail_aliases_share_inbox_and_thread(self):
        message = mail.send(self.db, 'old-worker', 'reviewer', 'Report from the worker')
        self.assertEqual(mail.get(self.db, message)['sender'], 'worker')
        message = mail.send(self.db, 'reviewer', 'old-worker', 'A useful review report')
        self.assertEqual(mail.unread(self.db, 'another-name')[0]['id'], message)
        p = self.cli('send', 'old-worker', 'Here is the review result', '--from', 'reviewer')
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.cli('inbox', 'old-worker')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Here is the review result', p.stdout)

    def test_session_alias_does_not_create_another_session(self):
        registry.set_mode(self.db, 'worker', 'externe')
        row = session_bindings.bind(self.cfg, self.db, 'old-worker', session_id='test-session',
                                    harness='codex', by='reviewer')
        self.assertEqual(row.row['agent'], 'worker')
        registry.set_session(self.db, 'another-name', 'resident-session')
        self.assertEqual(registry.get(self.db, 'worker')['session_id'], 'resident-session')

    def test_assignee_and_lot_filter_resolve_alias(self):
        item = work.add(self.db, title='Alias assigned work', assignee='old-worker',
                        estimate='1h', cfg=self.cfg)
        self.assertEqual(item['assignee'], 'worker')
        self.assertEqual(work.open_lots(self.db, 'another-name')[0]['id'], item['id'])
        self.assertEqual(work.list_items(self.db, assignee='old-worker')[0]['id'], item['id'])

    def test_reviewer_designation_resolves_alias(self):
        item = work.add(self.db, title='Alias reviewed work', assignee='worker',
                        estimate='1h', cfg=self.cfg)
        self.assertEqual(work.check_reviewer(self.db, 'old-worker', cfg=self.cfg), 'worker')
        with self.assertRaises(work.WorkError):
            work.check_reviewer_choice(self.db, item, 'another-name', cfg=self.cfg)
        self.assertEqual(work.check_reviewer_choice(self.db, item, 'reviewer', cfg=self.cfg),
                         'reviewer')

    def test_systemd_runner_filter_is_primary_and_deduplicated(self):
        instance = runner.Runner(self.cfg, self.db, agents=['old-worker', 'worker'])
        self.assertEqual(instance.agents_filter, ['worker'])
        self.assertEqual([r['name'] for r in registry.claimable(self.db, self.cfg.host, ['old-worker'])], ['worker'])

    def test_enabled_alias_units_cover_primary_in_both_systemd_scopes(self):
        from ameesh import hostcheck
        for scope, template in (((), 'ameesh-runner-agent@%s.service'),
                                ((hostcheck.SYSTEM,), 'ameesh-runner@%s.service')):
            for explicit_filter in (False, True):
                with self.subTest(scope=scope, explicit_filter=explicit_filter):
                    unit = template % 'old-worker'
                    commands = {
                        scope + ('list-unit-files', '--type=service', '--no-legend', '--plain'):
                            unit + ' enabled enabled\n',
                        scope + ('show', unit, '-p', 'ExecStart', '--value'):
                            'argv[]=/x/ameesh run' + (' --agents old-worker' if explicit_filter else ''),
                    }
                    coverage = hostcheck.runner_coverage(
                        ['worker', 'reviewer'], db=self.db,
                        run=lambda *args: commands.get(args, ''))
                    self.assertEqual(coverage, {'worker': unit, 'reviewer': None})
        self.assertTrue(self.db.query(
            "SELECT 1 FROM agent_name_resolutions WHERE operation='systemd.runner'"))

    def test_register_alias_keeps_worktree_marker_in_primary_state(self):
        import subprocess
        from pathlib import Path
        directory = Path(self.tmp) / 'registered-worktree'
        subprocess.run(['git', 'init', '-q', str(directory)], check=True)
        result = self.register('old-worker', 'codex', cwd=str(directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = Path(self.cfg.agent_dir('worker')) / 'worktree.json'
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads(marker.read_text())['cwd'], str(directory))
        self.assertFalse((Path(self.cfg.agent_dir('old-worker')) / 'worktree.json').exists())
        self.assertEqual(registry.get(self.db, 'worker')['cwd'], str(directory))

    def test_agent_key_alias_refers_to_the_same_key(self):
        authority.register_key(self.db, 'old-worker', b'x' * 32)
        self.assertEqual(authority.key_info(self.db, 'another-name')['public_key_fingerprint'],
                         authority.key_info(self.db, 'worker')['public_key_fingerprint'])
        self.assertTrue(authority.revoke_key(self.db, 'old-worker'))
        self.assertTrue(authority.key_info(self.db, 'worker')['key_revoked_ts'])

    def test_signed_alias_spelling_is_preserved_and_cannot_retarget(self):
        from ameesh import signing
        seed = b"s" * 32
        authority.register_key(self.db, 'worker', signing.backend().public_from_seed(seed))
        signed = authority.sign_message(seed, sender='old-worker', recipient='reviewer',
                                        body='A signed report')
        identifier = mail.send(self.db, 'old-worker', 'reviewer', 'A signed report', **signed)
        row = mail.normalize(mail.get(self.db, identifier))
        verdict = authority.verify_message(self.db, row)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertFalse(verdict.authority)
        row['to'] = 'worker'
        self.assertFalse(authority.verify_message(self.db, row).ok)
        row['to'] = 'reviewer'
        row['text'] += ' altered'
        self.assertFalse(authority.verify_message(self.db, row).ok)

    def test_permission_mode_uses_primary_local_state(self):
        registry.upsert(self.db, 'worker', harness='claude')
        p = self.mesh('set', 'old-worker', 'permission_mode=auto')
        self.assertEqual(p.returncode, 0, p.stderr)
        from ameesh.mesh_cli import _lit_etat
        self.assertEqual(json.loads(_lit_etat(self.cfg, 'worker', runner.PERMISSION_MODE_SETTING)), {
            'harness': 'claude', 'incarnation': runner.agent_incarnation(registry.get(self.db, 'worker')),
            'mode': 'auto',
        })
        self.assertFalse(_lit_etat(self.cfg, 'old-worker', runner.PERMISSION_MODE_SETTING))
        self.assertTrue(self.db.query("SELECT 1 FROM agent_name_resolutions WHERE operation='settings.permission_mode'"))


class SessionCommandAliasTest(PgTestCase):
    """B1 de la R2 : `adopt`, `resume` et `restart` reçus sous un alias
    écrivent sous le primaire (bail, NOTIFY, oubli local, marqueur), sans
    créer d'identité parallèle, et gardent les refus de bail existants."""

    def setUp(self):
        super().setUp()
        # #4589 : les détenteurs de session sont une observation de l'hôte,
        # pas le sujet de ces alias. Un processus de l'utilisateur aux fd
        # illisibles (gpg-agent, python3) fait lever HoldersUnreadable et
        # rougir resume/adopt ; la lecture réelle a ses témoins dans
        # test_l63_platform, test_1037 fait le même montage.
        holders = mock.patch.object(platform, "holders", lambda *a, **k: [])
        holders.start()
        self.addCleanup(holders.stop)
        self.travail = os.path.join(self.tmp, "travail")
        os.makedirs(self.travail, exist_ok=True)
        self.register("worker", "codex", cwd=self.travail)
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-worker', 'another-name'] "
                        "WHERE name = 'worker'")

    def _codex(self) -> dict:
        return {"codex": [
            {"name": "primaire",
             "path": _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")},
            {"name": "secondaire",
             "path": _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")},
        ]}

    def _cfg(self, comptes: dict | None = None):
        return dataclasses.replace(self.cfg, accounts=comptes or {})

    def _arrete(self, session: str, compte: str | None = None) -> None:
        registry.set_session(self.db, "worker", session, compte)
        proc = self.runner("stop", "worker")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _ligne(self) -> dict:
        rows = self.db.query("SELECT * FROM agent_registry")
        self.assertEqual([r["name"] for r in rows], ["worker"])
        return rows[0]

    def test_adopt_by_alias_writes_under_primary(self):
        comptes = self._codex()
        chemin = _rollout(comptes["codex"][1]["path"], SID, self.travail)
        registry.upsert(self.db, "coord", harness="codex", host=self.cfg.host, mode="externe")
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-coord'], "
                        "responsible = 'human:proprio' WHERE name = 'coord'")
        os.makedirs(self.cfg.agent_dir("coord"), exist_ok=True)
        with open(os.path.join(self.cfg.agent_dir("coord"), "session"), "w",
                  encoding="utf-8") as fh:
            fh.write("sess-externe\n")
        # bail vivant sur le primaire : le refus existant vaut aussi par alias
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-test', lease_epoch = 1, "
                        "lease_expires_at = now() + interval '1 hour' WHERE name = 'coord'")
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "old-coord", session_id=SID, harness="codex")
        self.assertIn("live lease", str(ctx.exception))
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
                        "WHERE name = 'coord'")
        out = reprise.adopt(self._cfg(comptes), self.db, "old-coord", session_id=SID, harness="codex")
        self.assertEqual((out["account"], out["path"]), ("secondaire", chemin))
        row = self.db.query("SELECT name, mode, status, session_id, session_account, cwd "
                            "FROM agent_registry WHERE name = 'coord'")[0]
        self.assertEqual((row["name"], row["mode"], row["status"], row["session_id"],
                          row["session_account"], row["cwd"]),
                         ("coord", "execute", "queued", SID, "secondaire", self.travail))
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("coord"), "session")))
        self.assertTrue(self.db.query(
            "SELECT 1 FROM agent_name_resolutions WHERE supplied_name = 'old-coord' "
            "AND primary_name = 'coord' AND operation = 'reprise.adopt'"))

    def test_resume_by_alias_covers_same_session_and_fresh(self):
        comptes = self._codex()
        self._arrete("sess-a", "primaire")
        # arrêté sous un bail vivant : le refus existant vaut aussi par alias
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-test', lease_epoch = 1, "
                        "lease_expires_at = now() + interval '1 hour' WHERE name = 'worker'")
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.resume(self._cfg(comptes), self.db, "old-worker")
        self.assertIn("lease is still live", str(ctx.exception))
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
                        "WHERE name = 'worker'")
        out = reprise.resume(self._cfg(comptes), self.db, "another-name")
        self.assertTrue(out["kept"])
        row = self._ligne()
        self.assertEqual((row["status"], row["session_id"]), ("queued", "sess-a"))
        self.assertTrue(self.db.query(
            "SELECT 1 FROM agent_name_resolutions WHERE supplied_name = 'another-name' "
            "AND primary_name = 'worker' AND operation = 'reprise.resume'"))
        # reprise fraîche sous l'autre alias : la session locale du primaire part
        proc = self.runner("stop", "worker")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        os.makedirs(self.cfg.agent_dir("worker"), exist_ok=True)
        with open(os.path.join(self.cfg.agent_dir("worker"), "session"), "w",
                  encoding="utf-8") as fh:
            fh.write("sess-a\n")
        out = reprise.resume(self.cfg, self.db, "old-worker", fresh=True)
        self.assertEqual(out["forgotten_session"], "sess-a")
        row = self._ligne()
        self.assertEqual((row["status"], row["session_id"]), ("queued", None))
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("worker"), "session")))

    def test_restart_by_alias_requests_on_primary(self):
        registry.set_session(self.db, "worker", "vieille")
        # bail vivant : la demande vise le primaire et l'exécuteur est réveillé
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-test', lease_epoch = 1, "
                        "lease_expires_at = now() + interval '1 hour' WHERE name = 'worker'")
        out = exploitation.restart(self.cfg, self.db, "old-worker", "Brief neuf.")
        self.assertFalse(out["applied"])
        self.assertTrue(out["notified"])
        row = self._ligne()
        self.assertEqual((row["restart_brief"], row["session_id"]), ("Brief neuf.", "vieille"))
        self.assertIsNotNone(row["restart_requested_at"])
        # sans bail vivant : l'oubli local porte sur le primaire, pas sur l'alias
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
                        "WHERE name = 'worker'")
        os.makedirs(self.cfg.agent_dir("worker"), exist_ok=True)
        with open(os.path.join(self.cfg.agent_dir("worker"), "session"), "w",
                  encoding="utf-8") as fh:
            fh.write("vieille\n")
        out = exploitation.restart(self.cfg, self.db, "another-name", "Brief deux.")
        self.assertTrue(out["applied"])
        self.assertEqual(out["forgotten_session"], "vieille")
        row = self._ligne()
        self.assertTrue(row["pending_prompt"].startswith("Brief deux."))
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["restart_requested_at"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("worker"), "session")))
        self.assertTrue(self.db.query(
            "SELECT 1 FROM agent_name_resolutions WHERE supplied_name = 'another-name' "
            "AND primary_name = 'worker' AND operation = 'exploitation.restart'"))

    def test_stop_by_alias_bad_reason_has_no_side_effects(self):
        """#2488 : un motif invalide ne produit même pas d'audit d'alias."""
        before = self._ligne()
        audit_before = self.db.query("SELECT * FROM agent_name_resolutions ORDER BY id")
        for reason in (None, "", " \t\n ", "x" * (exploitation.STOP_REASON_MAX + 1)):
            with self.subTest(reason=reason):
                with self.assertRaises(exploitation.StopRefused) as ctx:
                    exploitation.stop(self.cfg, self.db, "another-name", reason,
                                      actor="human:proprio")
                self.assertEqual(ctx.exception.code, "bad_reason")
                self.assertEqual(self.db.query(
                    "SELECT * FROM agent_name_resolutions ORDER BY id"), audit_before)
                self.assertEqual(self._ligne(), before)
                entries = fil.transport_for(self.cfg).read(
                    fil.ThreadRef(fil.project_for(self.cfg)))
                self.assertEqual(entries, [])

    def test_stop_by_alias_reports_and_traces_primary(self):
        """N2 de #423 (#1955) : `stop` résout l'alias à l'entrée ; le champ
        rendu et le texte du fil nomment le primaire, jamais l'orthographe
        fournie."""
        out = exploitation.stop(self.cfg, self.db, "another-name", "raison d'arrêt",
                                actor="human:proprio")
        self.assertEqual((out["agent"], out["changed"], out["stop_reason"]),
                         ("worker", True, "manuel"))
        row = self._ligne()
        self.assertEqual((row["status"], row["stop_reason"]), ("stopped", "manuel"))
        entries = fil.transport_for(self.cfg).read(fil.ThreadRef(fil.project_for(self.cfg)))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].text, "worker stopped by human:proprio: raison d'arrêt")
        self.assertNotIn("another-name", entries[0].text)
        self.assertTrue(self.db.query(
            "SELECT 1 FROM agent_name_resolutions WHERE supplied_name = 'another-name' "
            "AND primary_name = 'worker' AND operation = 'exploitation.stop'"))


class CanonAliasTest(_TmpMixin, unittest.TestCase):
    def test_optional_alias_field_in_mixed_card_lookup(self):
        from types import SimpleNamespace
        loaded = canon.Canon(root='/unused', agents=[
            SimpleNamespace(title='legacy-worker'),
            SimpleNamespace(title='worker', aliases=['old-worker']),
        ])
        for name, primary in (('legacy-worker', 'legacy-worker'), ('worker', 'worker'),
                              ('old-worker', 'worker'), ('unknown', 'unknown')):
            with self.subTest(name=name):
                self.assertEqual(agent_names.resolve(name, canon=loaded), primary)
        self.assertEqual(loaded.agent('old-worker').title, 'worker')
        self.assertIsNone(loaded.agent('unknown'))
        loaded.agents.append(SimpleNamespace(title='other-worker', aliases=['old-worker']))
        with self.assertRaises(ValueError):
            agent_names.resolve('old-worker', canon=loaded)
        self.assertIsNone(loaded.agent('old-worker'))

    def load_aliases(self, aliases):
        root = self.example_copy()
        path = 'agents/ouvrier.md'
        with open(os.path.join(root, path)) as handle:
            text = handle.read()
        write(root, path, text.replace('title: ouvrier', 'title: ouvrier\naliases: ' + json.dumps(aliases)))
        return root, canon.load(root, untrusted=True)

    def test_card_lookup_and_placement_accept_alias(self):
        root, loaded = self.load_aliases(['old-worker'])
        self.assertEqual(loaded.agent('old-worker').title, 'ouvrier')
        self.assertEqual(canon.errors(canon.validate(loaded)), [])
        primary = placement.evaluate(loaded, 'ouvrier', 'local')
        alias = placement.evaluate(loaded, 'old-worker', 'local')
        self.assertEqual(alias.to_dict(), primary.to_dict())
        self.assertEqual(loaded.to_dict()['agents'][1]['aliases'], ['old-worker'])

    def test_member_primary_and_duplicate_aliases_are_invalid(self):
        for aliases in (['alice'], ['orchestre'], ['same', 'same'], ['bad/name']):
            with self.subTest(aliases=aliases):
                root, loaded = self.load_aliases(aliases)
                findings = canon.validate(loaded)
                self.assertTrue(codes(findings, canon.ERROR), findings)

    def test_alias_field_requires_a_list_of_names(self):
        for value in ('old-worker', [17], ['all'], ['x\n']):
            with self.subTest(value=value):
                _root, loaded = self.load_aliases(value)
                self.assertTrue(canon.errors(canon.validate(loaded)))

    def test_primary_and_alias_placements_are_duplicate_admissions(self):
        from pathlib import Path
        root, loaded = self.load_aliases(['old-worker'])
        original = Path(root) / 'placements/ouvrier-banc.md'
        write(root, 'placements/alias-banc.md', original.read_text().replace(
            'title: ouvrier-banc', 'title: alias-banc').replace('agent: ouvrier', 'agent: old-worker'))
        self.assertIn('placement-duplicate', codes(canon.validate(canon.load(root, untrusted=True))))



from .test_canon import _CanonDbCase
from . import test_l108_executor_server as _server
from .test_l108_executor_server import TOKEN, SESSION, OTHER_TOKEN, OWNER


class CanonAliasSyncTest(_CanonDbCase):
    def add_aliases(self, names):
        from pathlib import Path
        path = Path(self.root) / 'agents/ouvrier.md'
        path.write_text(path.read_text().replace('title: ouvrier',
                        'title: ouvrier\naliases: ' + json.dumps(names)))

    def test_sync_aliases_without_changing_session_or_lease(self):
        self.sync('banc')
        registry.set_session(self.db, 'ouvrier', 'resident')
        lease = registry.claim(self.db, 'ouvrier', 'keeper', 120)
        self.add_aliases(['old-worker'])
        self.sync('banc')
        primary = registry.get(self.db, 'ouvrier')
        alias = registry.get(self.db, 'old-worker')
        self.assertEqual(alias, primary)
        self.assertEqual(primary['session_id'], 'resident')
        self.assertEqual(primary['lease_epoch'], lease['lease_epoch'])
        self.assertEqual(primary['lease_owner'], 'keeper')

    def test_cross_registry_collision_is_a_finding_without_alias_write(self):
        registry.upsert(self.db, 'other-identity', host='other-host')
        self.add_aliases(['other-identity'])
        report = self.sync('banc')
        self.assertIn('agent-alias-conflict', codes(report.findings))
        self.assertEqual(registry.get(self.db, 'ouvrier')['aliases'], [])
        self.assertIsNone(registry.get(self.db, 'ouvrier')['responsible'])
        self.assertEqual(registry.get(self.db, 'other-identity')['host'], 'other-host')

    def test_placement_and_work_directory_references_by_alias(self):
        from pathlib import Path
        self.add_aliases(['old-worker'])
        admission = Path(self.root) / 'placements/ouvrier-banc.md'
        admission.write_text(admission.read_text().replace('agent: ouvrier', 'agent: old-worker'))
        host = Path(self.root) / 'hotes/banc.md'
        host.write_text(host.read_text().replace('  max_agents: 2',
                         '  work_dirs: {old-worker: /srv/acme/worker}\n  max_agents: 2'))
        loaded = self.load()
        self.assertEqual(canon.errors(canon.validate(loaded)), [])
        self.sync('banc')
        self.assertEqual(registry.get(self.db, 'old-worker')['cwd'], '/srv/acme/worker')

    def test_conflicting_primary_and_alias_work_directories_close_admission(self):
        from pathlib import Path
        self.add_aliases(['old-worker'])
        host = Path(self.root) / 'hotes/banc.md'
        host.write_text(host.read_text().replace('  max_agents: 2',
                        '  work_dirs: {ouvrier: /srv/primary, old-worker: /srv/alias}\n'
                        '  max_agents: 2'))
        findings = canon.validate(self.load())
        self.assertIn('host-work-dir-alias-conflict', codes(findings))
        self.sync('banc')
        self.assertIsNone(registry.get(self.db, 'old-worker')['responsible'])

    def test_declarative_swap_resolves_both_directions(self):
        from pathlib import Path
        self.add_aliases(['new-worker'])
        loaded = self.load()
        self.assertEqual(agent_names.resolve('new-worker', canon=loaded), 'ouvrier')
        path = Path(self.root) / 'agents/ouvrier.md'
        text = path.read_text().replace('title: ouvrier', 'title: new-worker').replace(
                                        'aliases: ["new-worker"]', 'aliases: ["ouvrier"]')
        path.write_text(text)
        renamed = self.load()
        self.assertEqual(agent_names.resolve('ouvrier', canon=renamed), 'new-worker')
        path.write_text(text.replace('title: new-worker', 'title: ouvrier').replace(
                                    'aliases: ["ouvrier"]', 'aliases: ["new-worker"]'))
        self.assertEqual(agent_names.resolve('new-worker', canon=self.load()), 'ouvrier')


class MediatedAgentAliasTest(_server.ServeurExecTest):
    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE agent_registry SET aliases=ARRAY['old-worker'] WHERE name='inge-front'")

    def test_reduced_resolution_is_available_to_a_session(self):
        response = self.op('agents.resolve_name', ['old-worker'], token=SESSION, route='/session/op')
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.body['value'], 'inge-front')

    def test_scope_is_checked_after_resolution(self):
        response = self.op('agents.get', ['old-worker'])
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.body['value']['name'], 'inge-front')
        response = self.op('agents.get', ['old-worker'], token=OTHER_TOKEN)
        self.assertEqual(response.status, 403, response.body)

    def test_alias_envelope_uses_same_epoch(self):
        response = self.op('leases.renew', ['old-worker', OWNER, 42, 90],
                           fence=('old-worker', OWNER, 42))
        self.assertEqual(response.status, 200, response.body)
        self.assertFalse(response.body.get('fenced'))
        self.assertTrue(response.body['value'])
        response = self.op('leases.renew', ['old-worker', OWNER, 41, 90],
                           fence=('old-worker', OWNER, 41))
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(response.body.get('fenced'))
