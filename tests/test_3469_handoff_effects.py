# SPDX-License-Identifier: AGPL-3.0-only
"""H2: preparation and Git effects of the enrolled handoff, without a real host.

The factory, companion and bridge are loaded as pinned sources. Host effects
are exercised on disposable private files, local Git repositories, a simulated
systemd and a durable journal; no SSH, root operation, credential value or
production host is touched. A database-backed class covers the adapter's
durable-operation and preflight checks.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import handoff_contract, handoffs, registry, resources, storage
from .support import PgTestCase

REPO = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40
GIB = 1024 ** 3


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, REPO / 'deploy' / 'mise-a-jour' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


companion = _module('handoff_companion', 'hote-handoff.py')
bridge_module = _module('handoff_bridge', 'pont-handoff.py')
factory_module = _module('handoff_factory', 'fabrique-handoff.py')


def git_env(home):
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
            'HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid'}


def git(*args, cwd=None, home=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, env=git_env(home or cwd),
                          capture_output=True, text=True, check=check)


class FakeSystem:
    def __init__(self):
        self.fields = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                       'MemoryMax': str(4 * GIB), 'MemoryHigh': str(3 * GIB),
                       'Slice': 'ameesh-agents.slice'}
        self.stops = 0
        self.shows = 0
        self.quiet_value = True
        self.after_stop = None

    def show(self, scope, account, unit, properties):
        self.shows += 1
        return {name: self.fields.get(name, '') for name in properties}

    def stop(self, scope, account, unit):
        self.stops += 1
        if self.after_stop:
            self.fields.update(self.after_stop)

    def quiet(self, scope, account, unit):
        return self.quiet_value


def release_store(root, commit):
    """A completed release receipt the versioned-environment helper accepts."""
    store = Path(root) / 'store'
    store.mkdir(mode=0o711)
    store.chmod(0o711)
    manifest = {'commit': commit}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    release_id = commit + '-' + identity
    path = store / release_id
    path.mkdir()
    path.chmod(0o711)
    (path / 'source').mkdir()
    (path / 'source').chmod(0o700)
    (path / 'venv' / 'bin').mkdir(parents=True)
    (path / 'venv').chmod(0o711)
    (path / 'venv' / 'bin').chmod(0o711)
    (path / 'venv' / 'bin' / 'python').write_text('')
    (path / 'venv' / 'bin' / 'python').chmod(0o600)
    (path / 'ready.json').write_text(json.dumps(manifest))
    (path / 'ready.json').chmod(0o600)
    (store / 'current').symlink_to(release_id)
    return store


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user = pwd.getpwuid(os.geteuid()).pw_name
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.work = self.root / 'work'
        self.work.mkdir(mode=0o700)
        self.config = self.root / 'config.json'
        self.config.write_text('{}')
        self.config.chmod(0o600)
        self.store = release_store(self.root, COMMIT)
        self.execution = {
            'host': 'target',
            'state': str(self.root / 'state'),
            'config': str(self.config),
            'release_store': str(self.store),
            'accounts': {'dev': {'account': self.user, 'home': str(self.home),
                                 'unit': 'ameesh-runner@dev.service',
                                 'scope': 'system'}},
            'roots': [str(self.work)],
            'tools': ['git'],
            'secrets': ['codex/api'],
        }
        self.system = FakeSystem()
        self.effects = self._effects()
        self.origin = self.root / 'origin.git'
        self.source = self.work / 'source'
        git('init', '-q', '--bare', '-b', 'main', str(self.origin), home=self.root)
        git('init', '-q', '-b', 'main', str(self.source), home=self.root)
        (self.source / 'a.txt').write_text('one\n')
        git('add', 'a.txt', cwd=self.source, home=self.root)
        git('commit', '-q', '-m', 'one', cwd=self.source, home=self.root)
        git('remote', 'add', 'origin', str(self.origin), cwd=self.source, home=self.root)
        git('push', '-q', '-u', 'origin', 'main', cwd=self.source, home=self.root)
        self.commit = git('rev-parse', 'HEAD', cwd=self.source,
                          home=self.root).stdout.strip()

    def close(self):
        self.temp.cleanup()

    def _effects(self, **changes):
        value = dict(self.execution)
        value.update(changes)
        return companion.HostEffects(enrollment=value, system=self.system,
                                     work_dir=lambda agent, project: str(self.work / agent))


class RequestMixin:
    def request(self, fx, step, **changes):
        value = {'step': step, 'host': 'target', 'operation': 'a' * 32,
                 'action_id': 'a' * 32 + ':' + step, 'controller_epoch': 3,
                 'agent': 'dev'}
        if step in ('prepare', 'session'):
            value.update(harness='codex', account=fx.user)
        if step == 'prepare':
            value.update(tools=['git'], secrets=['codex/api'])
        if step == 'publish':
            value.update(repository=str(fx.origin), cwd=str(fx.source))
        if step == 'transfer':
            value.update(repository=str(fx.origin), branch='main',
                         commit=fx.commit, project='ameesh')
        if step == 'session':
            value.update(cwd=str(fx.work / 'dev'), commit=fx.commit)
        if step == 'revalidate':
            value.update(state='planned')
        value.update(changes)
        value['action_id'] = value['operation'] + ':' + step
        return handoff_contract.validate_effect_request(value)


class WireContractTest(unittest.TestCase):
    def endpoint(self, root):
        return {'host': 'target', 'address': '10.0.0.3', 'port': 22,
                'user': 'ameesh-handoff', 'host_key_alias': 'target',
                'known_hosts': str(Path(root) / 'known_hosts'),
                'known_hosts_sha256': 'a' * 64,
                'identity_file': str(Path(root) / 'id'), 'seconds': 30}

    def test_transport_is_exact_and_private(self):
        root = Path('/etc/ameesh-handoff')
        value = self.endpoint(root)
        self.assertEqual(handoff_contract.validate_transport(value, 'target', root=root),
                         value)
        cases = [
            dict(value, user='ameesh-release'),
            dict(value, host='other'),
            dict(value, port=0),
            dict(value, seconds=4),
            dict(value, known_hosts_sha256='f' * 63),
            dict(value, known_hosts='/tmp/known_hosts'),
            dict(value, identity_file='/etc/ameesh-handoff/../id'),
            dict(value, extra='x'),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(
                    handoff_contract.ContractError):
                handoff_contract.validate_transport(case, 'target', root=root)

    def test_effect_request_is_narrow_and_normalised(self):
        value = {'step': 'prepare', 'host': 'target', 'operation': 'a' * 32,
                 'action_id': 'a' * 32 + ':prepare', 'controller_epoch': 3,
                 'agent': 'dev', 'harness': 'deepseek', 'account': 'main',
                 'tools': ['gh', 'git'], 'secrets': ['z/api', 'a/api']}
        checked = handoff_contract.validate_effect_request(value)
        self.assertEqual(checked['tools'], ('gh', 'git'))
        self.assertEqual(checked['secrets'], ('a/api', 'z/api'))
        for field in ('operation', 'controller_epoch', 'host', 'agent'):
            with self.subTest(field=field):
                broken = dict(value)
                broken.pop(field)
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_effect_request(broken)
        for changes in ({'action_id': 'a' * 32 + ':publish'},
                        {'step': 'admit'},
                        {'tools': ['gh', 'gh']},
                        {'tools': ['/bin/sh']},
                        {'account': 'bad account'},
                        {'account': 'a@b'},
                        {'controller_epoch': 0}):
            with self.subTest(changes=changes):
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_effect_request(dict(value, **changes))

    def test_each_effect_has_its_own_fields_and_receipt(self):
        base = {'host': 'target', 'operation': 'a' * 32, 'controller_epoch': 3,
                'agent': 'dev'}
        requests = {
            'revalidate': {'state': 'planned'},
            'stop_source': {},
            'publish': {'repository': 'git@forge:r.git', 'cwd': '/w/source'},
            'transfer': {'repository': 'git@forge:r.git', 'branch': 'main',
                         'commit': COMMIT, 'project': 'ameesh'},
            'session': {'harness': 'codex', 'account': '', 'cwd': '/w/dev',
                        'commit': COMMIT},
        }
        for step, fields in requests.items():
            with self.subTest(step=step):
                request = dict(base, step=step,
                               action_id='a' * 32 + ':' + step, **fields)
                checked = handoff_contract.validate_effect_request(request)
                receipt = {'host': 'target', 'action_id': checked['action_id'],
                           'controller_epoch': 3, 'ok': True}
                if step == 'prepare':
                    continue
                if step == 'revalidate':
                    pass
                elif step == 'stop_source':
                    receipt.update(unit='ameesh-runner@dev.service', stopped=True,
                                   quiet=True, executor_enabled=False)
                elif step == 'publish':
                    receipt.update(repository=fields['repository'], branch='main',
                                   commit=COMMIT, remote_commit=COMMIT, clean=True)
                elif step == 'transfer':
                    receipt.update(repository=fields['repository'], branch='main',
                                   commit=COMMIT, clean=True, cwd='/w/dev')
                elif step == 'session':
                    receipt.update(mode='brief', executor_enabled=False,
                                   oauth_cache_transferred=False)
                self.assertEqual(
                    handoff_contract.validate_effect_receipt(receipt, checked)['ok'],
                    True)

    def test_prepare_receipt_refuses_a_target_executor_or_missing_caps(self):
        request = handoff_contract.validate_effect_request({
            'step': 'prepare', 'host': 'target', 'operation': 'a' * 32,
            'action_id': 'a' * 32 + ':prepare', 'controller_epoch': 3,
            'agent': 'dev', 'harness': 'deepseek', 'account': 'main',
            'tools': ['git'], 'secrets': []})
        receipt = {'host': 'target', 'action_id': 'a' * 32 + ':prepare',
                   'controller_epoch': 3, 'ok': True, 'release_revision': COMMIT,
                   'credentials_ready': True, 'caps_ready': True,
                   'tooling_ready': True, 'dsh_profile_ready': True,
                   'executor_enabled': False}
        self.assertTrue(handoff_contract.validate_effect_receipt(receipt, request)['ok'])
        for changes in ({'executor_enabled': True}, {'caps_ready': 1},
                        {'release_revision': 'nope'}):
            with self.subTest(changes=changes):
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_effect_receipt(
                        dict(receipt, **changes), request)

    def test_unknown_receipt_fields_are_refused(self):
        request = handoff_contract.validate_effect_request({
            'step': 'stop_source', 'host': 'target', 'operation': 'a' * 32,
            'action_id': 'a' * 32 + ':stop_source', 'controller_epoch': 3,
            'agent': 'dev'})
        receipt = {'host': 'target', 'action_id': 'a' * 32 + ':stop_source',
                   'controller_epoch': 3, 'ok': True,
                   'unit': 'ameesh-runner@dev.service', 'stopped': True,
                   'quiet': True, 'executor_enabled': False, 'diagnostic': 'x'}
        with self.assertRaises(handoff_contract.ContractError):
            handoff_contract.validate_effect_receipt(receipt, request)


class EffectsTest(RequestMixin, unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_inventory_is_boolean_and_fresh(self):
        receipt = self.fx.effects.inventory({
            'step': 'inventory', 'host': 'target',
            'tools': ['git', 'absent'], 'secrets': ['codex/api', 'other/api']})
        self.assertEqual(receipt['host'], 'target')
        self.assertIs(receipt['connected'], True)
        self.assertEqual(receipt['tools'], {'git': True, 'absent': False})
        self.assertEqual(receipt['secrets'], {'codex/api': True, 'other/api': False})
        self.assertLess(abs(receipt['checked_ts'] - time.time()), 5)
        self.assertNotIn('PATH', json.dumps(receipt))

    def test_prepare_reads_release_and_caps_without_starting_anything(self):
        receipt = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['release_revision'], COMMIT)
        self.assertIs(receipt['executor_enabled'], False)
        self.assertEqual(self.fx.system.stops, 0)

    def test_prepare_refuses_a_missing_release(self):
        (self.fx.store / 'current').unlink()
        receipt = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertIs(receipt['ok'], False)
        self.assertEqual(receipt['reason'], 'release')

    def test_prepare_refuses_missing_caps_tooling_or_secret(self):
        for field, value in (('MemoryMax', 'infinity'), ('LoadState', 'not-found')):
            with self.subTest(field=field):
                fx = Fixture()
                self.addCleanup(fx.close)
                fx.system.fields[field] = value
                receipt = fx.effects.perform(self.request(fx, 'prepare'))
                self.assertEqual(receipt['reason'], 'caps')
        for change, reason in (({'tools': []}, 'tooling'),
                               ({'secrets': []}, 'credentials')):
            with self.subTest(change=change):
                fx = Fixture()
                self.addCleanup(fx.close)
                fx.effects = fx._effects(**change)
                receipt = fx.effects.perform(self.request(fx, 'prepare'))
                self.assertEqual(receipt['reason'], reason)

    def test_prepare_refuses_an_active_destination_executor(self):
        self.fx.system.fields['ActiveState'] = 'active'
        receipt = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertIs(receipt['ok'], False)
        self.assertEqual(receipt['reason'], 'target_active')

    def test_dsh_profile_is_private_and_credential_free(self):
        receipt = self.fx.effects.perform(self.request(
            self.fx, 'prepare', harness='deepseek'))
        self.assertIs(receipt['ok'], True)
        profile = self.fx.home / '.ameesh-handoff' / 'dsh' / 'profile.json'
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertFalse((profile.parent / '.credentials.yaml').exists())
        other = Fixture()
        self.addCleanup(other.close)
        base = other.home / '.ameesh-handoff'
        base.mkdir(mode=0o700)
        profile = base / 'dsh'
        profile.mkdir(mode=0o700)
        (profile / '.credentials.yaml').write_text('token')
        refused = other.effects.perform(self.request(
            other, 'prepare', harness='deepseek'))
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'dsh_credentials')

    def test_private_chains_refuse_a_pre_created_link(self):
        outside = self.fx.root / 'outside'
        outside.mkdir()
        (self.fx.home / '.ameesh-handoff').symlink_to(outside)
        refused = self.fx.effects.perform(self.request(
            self.fx, 'prepare', harness='deepseek'))
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'state')
        self.assertEqual(list(outside.iterdir()), [])
        linked = Fixture()
        self.addCleanup(linked.close)
        linked.effects.perform(self.request(linked, 'transfer'))
        (linked.home / '.ameesh-handoff').symlink_to(linked.root / 'outside')
        (linked.root / 'outside').mkdir()
        refused = linked.effects.perform(self.request(linked, 'session'))
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'state')
        self.assertEqual(list((linked.root / 'outside').iterdir()), [])

    def test_stop_source_waits_for_quiescence_and_replays(self):
        self.fx.system.fields['ActiveState'] = 'active'
        self.fx.system.after_stop = {'ActiveState': 'inactive'}
        receipt = self.fx.effects.perform(self.request(self.fx, 'stop_source'))
        self.assertIs(receipt['ok'], True)
        self.assertIs(receipt['stopped'], True)
        self.assertIs(receipt['quiet'], True)
        self.assertEqual(self.fx.system.stops, 1)
        replay = self.fx.effects.perform(self.request(self.fx, 'stop_source'))
        self.assertEqual(replay, receipt)
        self.assertEqual(self.fx.system.stops, 1)

    def test_a_refused_step_still_revalidates_and_retries(self):
        self.fx.system.fields['MemoryMax'] = 'infinity'
        refused = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertEqual(refused['reason'], 'caps')
        checked = self.fx.effects.perform(
            self.request(self.fx, 'revalidate', state='planned'))
        self.assertIs(checked['ok'], True)
        self.fx.system.fields['MemoryMax'] = str(4 * GIB)
        accepted = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertIs(accepted['ok'], True)

    def test_stop_source_timeout_is_uncertain(self):
        self.fx.system.fields['ActiveState'] = 'active'
        with mock.patch.object(companion, 'STOP_SECONDS', 0.05), \
                self.assertRaises(companion.Uncertain):
            self.fx.effects.perform(self.request(self.fx, 'stop_source'))

    def test_publish_is_the_exact_clean_pushed_commit(self):
        receipt = self.fx.effects.perform(self.request(self.fx, 'publish'))
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['commit'], self.fx.commit)
        self.assertEqual(receipt['remote_commit'], self.fx.commit)
        (self.fx.source / 'dirty.txt').write_text('x')
        refused = self.fx.effects.perform(self.request(self.fx, 'publish',
                                                       operation='b' * 32,
                                                       action_id='b' * 32 + ':publish'))
        self.assertEqual(refused['reason'], 'dirty')

    def test_publish_refuses_unpushed_or_detached_source(self):
        (self.fx.source / 'b.txt').write_text('two\n')
        git('add', 'b.txt', cwd=self.fx.source, home=self.fx.root)
        git('commit', '-q', '-m', 'two', cwd=self.fx.source, home=self.fx.root)
        refused = self.fx.effects.perform(self.request(self.fx, 'publish'))
        self.assertEqual(refused['reason'], 'unpublished')
        git('checkout', '-q', '--detach', cwd=self.fx.source, home=self.fx.root)
        detached = self.fx.effects.perform(self.request(
            self.fx, 'publish', operation='b' * 32,
            action_id='b' * 32 + ':publish'))
        self.assertIn(detached['reason'], ('branch', 'unpublished'))

    def test_transfer_checks_out_the_exact_commit_and_never_copies_a_cache(self):
        (self.fx.home / '.credentials.yaml').write_text('source token')
        receipt = self.fx.effects.perform(self.request(self.fx, 'transfer'))
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['cwd'], str(self.fx.work / 'dev'))
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.fx.work / 'dev',
                             home=self.fx.root).stdout.strip(), self.fx.commit)
        self.assertFalse((self.fx.work / 'dev' / '.credentials.yaml').exists())
        self.assertFalse((self.fx.work / 'dev' / '.ameesh-handoff').exists())
        calls = []
        real = self.fx.effects.git.run

        def spy(account, home, cwd, args, **kwargs):
            calls.append(tuple(args))
            return real(account, home, cwd, args, **kwargs)
        with mock.patch.object(self.fx.effects.git, 'run', side_effect=spy):
            replay = self.fx.effects.perform(self.request(self.fx, 'transfer'))
        self.assertEqual(replay, receipt)
        self.assertEqual(calls, [])

    def test_transfer_refuses_a_linked_work_root(self):
        outside = self.fx.root / 'outside'
        outside.mkdir()
        (self.fx.work / 'linked').symlink_to(outside)
        effects = companion.HostEffects(
            enrollment=self.fx.execution, system=self.fx.system,
            work_dir=lambda agent, project: str(self.fx.work / 'linked' / agent))
        receipt = effects.perform(self.request(self.fx, 'transfer'))
        self.assertIs(receipt['ok'], False)
        self.assertEqual(receipt['reason'], 'cwd')
        self.assertEqual(list(outside.iterdir()), [])

    def test_transfer_refuses_to_clobber_local_work(self):
        target = self.fx.work / 'dev'
        target.mkdir()
        (target / 'keep.txt').write_text('mine')
        receipt = self.fx.effects.perform(self.request(self.fx, 'transfer'))
        self.assertIs(receipt['ok'], False)
        self.assertEqual((target / 'keep.txt').read_text(), 'mine')

    def test_transfer_resumes_its_own_interrupted_temporary(self):
        real = self.fx.effects.git.run
        calls = {'n': 0}

        def fail_checkout(account, home, cwd, args, **kwargs):
            if args and args[0] == 'checkout' and calls['n'] == 0:
                calls['n'] += 1
                raise companion.Uncertain('test interruption')
            return real(account, home, cwd, args, **kwargs)
        with mock.patch.object(self.fx.effects.git, 'run',
                               side_effect=fail_checkout):
            with self.assertRaises(companion.Uncertain):
                self.fx.effects.perform(self.request(self.fx, 'transfer'))
        temporary = list((self.fx.work).glob('.dev.handoff-*'))
        self.assertEqual(len(temporary), 1)
        receipt = self.fx.effects.perform(self.request(self.fx, 'transfer'))
        self.assertIs(receipt['ok'], True)
        self.assertEqual(list(self.fx.work.glob('.dev.handoff-*')), [])

    def test_session_brief_and_fresh_have_no_oauth_cache(self):
        for harness, mode in (('codex', 'brief'), ('deepseek', 'fresh')):
            with self.subTest(harness=harness):
                fx = Fixture()
                self.addCleanup(fx.close)
                transfer = fx.effects.perform(self.request(fx, 'transfer'))
                session = fx.effects.perform(
                    self.request(fx, 'session', harness=harness))
                self.assertIs(session['ok'], True)
                self.assertEqual(session['mode'], mode)
                marker = (fx.home / '.ameesh-handoff' / 'sessions' / ('a' * 32)
                          / 'session.json')
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                self.assertIs(json.loads(marker.read_text())['oauth_cache'], False)
                self.assertEqual(transfer['commit'], fx.commit)

    def test_session_refuses_an_active_target_and_an_unknown_transfer(self):
        self.fx.effects.perform(self.request(self.fx, 'transfer'))
        self.fx.system.fields['ActiveState'] = 'active'
        active = self.fx.effects.perform(self.request(self.fx, 'session'))
        self.assertEqual(active['reason'], 'target_active')
        self.fx.system.fields['ActiveState'] = 'inactive'
        orphan = self.fx.effects.perform(self.request(
            self.fx, 'session', operation='c' * 32))
        self.assertEqual(orphan['reason'], 'order')

    def test_stale_epoch_and_out_of_order_effect_are_refused(self):
        self.assertIs(self.fx.effects.perform(
            self.request(self.fx, 'prepare'))['ok'], True)
        # A replay under a newer epoch is idempotent but advances the durable
        # epoch, so a delayed older request can never perform afterwards.
        replay = self.fx.effects.perform(
            self.request(self.fx, 'prepare', controller_epoch=5))
        self.assertIs(replay['ok'], True)
        stale = self.fx.effects.perform(
            self.request(self.fx, 'publish', controller_epoch=4))
        self.assertEqual(stale['reason'], 'revoked_epoch')
        publish = self.fx.effects.perform(
            self.request(self.fx, 'publish', controller_epoch=6))
        self.assertIs(publish['ok'], True)
        replay = self.fx.effects.perform(
            self.request(self.fx, 'prepare', controller_epoch=7))
        self.assertIs(replay['ok'], True)
        self.assertEqual(replay['release_revision'], COMMIT)

    def test_stop_source_replays_after_a_later_publication(self):
        self.fx.system.fields['ActiveState'] = 'active'
        self.fx.system.after_stop = {'ActiveState': 'inactive'}
        stop = self.fx.effects.perform(self.request(self.fx, 'stop_source'))
        self.assertIs(stop['ok'], True)
        self.assertIs(self.fx.effects.perform(
            self.request(self.fx, 'publish'))['ok'], True)
        replay = self.fx.effects.perform(
            self.request(self.fx, 'stop_source', controller_epoch=8))
        self.assertIs(replay['ok'], True)
        self.assertEqual(self.fx.system.stops, 1)

    def test_a_new_earlier_effect_is_out_of_order(self):
        publish = self.fx.effects.perform(self.request(self.fx, 'publish'))
        self.assertIs(publish['ok'], True)
        late = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertEqual(late['reason'], 'order')

    def test_receipt_survives_a_restart_and_a_refusal_is_not_cached(self):
        self.fx.system.fields['MemoryMax'] = 'infinity'
        refused = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertEqual(refused['reason'], 'caps')
        self.fx.system.fields['MemoryMax'] = str(4 * GIB)
        accepted = self.fx.effects.perform(self.request(self.fx, 'prepare'))
        self.assertIs(accepted['ok'], True)
        shown = self.fx.system.shows
        restarted = companion.HostEffects(
            enrollment=self.fx.execution, system=self.fx.system,
            work_dir=lambda agent, project: str(self.fx.work / agent))
        replay = restarted.perform(self.request(self.fx, 'prepare'))
        self.assertEqual(replay, accepted)
        self.assertEqual(self.fx.system.shows, shown)


class DispatcherTest(unittest.TestCase):
    def test_executor_loads_a_private_enrollment_and_answers_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.chmod(0o700)
            home = root / 'home'
            home.mkdir(mode=0o700)
            work = root / 'work'
            work.mkdir(mode=0o700)
            config = root / 'config.json'
            config.write_text('{}')
            config.chmod(0o600)
            store = release_store(root, COMMIT)
            execution = {
                'host': 'target', 'state': str(root / 'state'),
                'config': str(config), 'release_store': str(store),
                'accounts': {'dev': {'account': pwd.getpwuid(os.geteuid()).pw_name,
                                     'home': str(home),
                                     'unit': 'ameesh-runner@dev.service',
                                     'scope': 'system'}},
                'roots': [str(work)], 'tools': ['git'], 'secrets': [],
            }
            document = root / 'execution.json'
            document.write_text(json.dumps(execution))
            document.chmod(0o600)
            receipt = companion.execute(
                {'step': 'inventory', 'host': 'target',
                 'tools': ['git', 'absent'], 'secrets': []}, root=root)
            self.assertEqual(receipt['tools'], {'git': True, 'absent': False})
            with self.assertRaises(companion.Refused):
                companion.execute(
                    {'step': 'inventory', 'host': 'other',
                     'tools': [], 'secrets': []}, root=root)

    def test_dispatcher_refuses_arguments_and_hides_every_detail(self):
        output = __import__('io').StringIO()
        with mock.patch('sys.stdout', output):
            self.assertEqual(companion.main(['unexpected']), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {'error': 'host_action_unavailable'})


class BridgeTest(unittest.TestCase):
    class Transport:
        def __init__(self, reply=None, error=None):
            self.reply = reply
            self.error = error
            self.payloads = []

        def protected_file(self, path):
            return Path(path).read_bytes()

        def ssh_args(self, endpoint):
            return ['ssh', endpoint['host']]

        def unique_object(self, pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate')
                value[key] = item
            return value

        def exchange(self, args, payload, seconds):
            self.payloads.append((args, json.loads(payload), seconds))
            if self.error:
                raise self.error
            return json.dumps(self.reply).encode()

    def test_remote_request_is_typed_and_receipt_is_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'known_hosts').write_text('host')
            (root / 'id').write_text('key')
            endpoint = {'host': 'target', 'address': '10.0.0.3', 'port': 22,
                        'user': 'ameesh-handoff', 'host_key_alias': 'target',
                        'known_hosts': str(root / 'known_hosts'),
                        'known_hosts_sha256': hashlib.sha256(b'host').hexdigest(),
                        'identity_file': str(root / 'id'), 'seconds': 30}
            transport = self.Transport()
            bridge = bridge_module.Bridge(local_host='source',
                                          transports={'target': endpoint},
                                          transport=transport,
                                          enrollment_root=root)
            inventory = {'host': 'target', 'checked_ts': time.time(),
                         'connected': True, 'tools': {'git': True},
                         'secrets': {'codex/api': True}}
            transport.reply = inventory
            receipt = bridge.inventory('target', ['git'], ['codex/api'])
            self.assertEqual(receipt['tools'], {'git': True})
            request = handoff_contract.validate_effect_request({
                'step': 'stop_source', 'host': 'target',
                'operation': 'a' * 32,
                'action_id': 'a' * 32 + ':stop_source',
                'controller_epoch': 3, 'agent': 'dev'})
            transport.reply = {'host': 'target',
                               'action_id': 'a' * 32 + ':stop_source',
                               'controller_epoch': 3, 'ok': True,
                               'unit': 'ameesh-runner@dev.service',
                               'stopped': True, 'quiet': True,
                               'executor_enabled': False}
            self.assertIs(bridge.perform('target', request)['ok'], True)
            transport.reply = dict(transport.reply, diagnostic='leak')
            with self.assertRaises(bridge_module.BridgeError):
                bridge.perform('target', request)

    def test_local_dispatch_never_uses_the_transport(self):
        class Local:
            def __init__(self):
                self.calls = []

            def inventory(self, request):
                return {'host': 'target', 'checked_ts': time.time(),
                        'connected': True,
                        'tools': {name: True for name in request['tools']},
                        'secrets': {name: True for name in request['secrets']}}

            def perform(self, request):
                self.calls.append(request)
                return {'host': 'target', 'action_id': request['action_id'],
                        'controller_epoch': request['controller_epoch'], 'ok': True}

        local = Local()
        transport = self.Transport(error=AssertionError('must not be used'))
        bridge = bridge_module.Bridge(local_host='target', transports={},
                                      local=local, transport=transport)
        request = handoff_contract.validate_effect_request({
            'step': 'revalidate', 'host': 'target', 'operation': 'a' * 32,
            'action_id': 'a' * 32 + ':revalidate', 'controller_epoch': 3,
            'agent': 'dev', 'state': 'planned'})
        self.assertIs(bridge.perform('target', request)['ok'], True)
        self.assertEqual(local.calls[0]['state'], 'planned')
        self.assertEqual(transport.payloads, [])

    def test_a_lost_reply_raises_and_a_retry_can_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'known_hosts').write_text('host')
            (root / 'id').write_text('key')
            endpoint = {'host': 'target', 'address': '10.0.0.3', 'port': 22,
                        'user': 'ameesh-handoff', 'host_key_alias': 'target',
                        'known_hosts': str(root / 'known_hosts'),
                        'known_hosts_sha256': hashlib.sha256(b'host').hexdigest(),
                        'identity_file': str(root / 'id'), 'seconds': 30}
            transport = self.Transport(error=OSError('network'))
            bridge = bridge_module.Bridge(local_host='source',
                                          transports={'target': endpoint},
                                          transport=transport,
                                          enrollment_root=root)
            with self.assertRaises(bridge_module.BridgeError):
                bridge.inventory('target', [], [])


if __name__ == '__main__':
    unittest.main()


class FakeBridge:
    """Controller-side bridge double: exact receipts, recorded requests."""

    def __init__(self):
        self.requests = []
        self.revalidate_ok = True
        self.fail = set()

    def inventory(self, host, tools, secrets):
        return {'host': host, 'checked_ts': time.time(), 'connected': True,
                'tools': {name: True for name in tools},
                'secrets': {name: True for name in secrets}}

    def perform(self, host, request):
        self.requests.append(request)
        step = request['step']
        base = {'host': host, 'action_id': request['action_id'],
                'controller_epoch': request['controller_epoch']}
        if step in self.fail:
            return dict(base, ok=False, reason='refused')
        if step == 'revalidate':
            return dict(base, ok=self.revalidate_ok)
        if step == 'prepare':
            return dict(base, ok=True, release_revision=COMMIT,
                        credentials_ready=True, caps_ready=True,
                        tooling_ready=True, dsh_profile_ready=True,
                        executor_enabled=False)
        if step == 'stop_source':
            return dict(base, ok=True, unit='ameesh-runner@dev.service',
                        stopped=True, quiet=True, executor_enabled=False)
        if step == 'publish':
            return dict(base, ok=True, repository=request['repository'],
                        branch='main', commit=COMMIT, remote_commit=COMMIT,
                        clean=True)
        if step == 'transfer':
            return dict(base, ok=True, repository=request['repository'],
                        branch=request['branch'], commit=request['commit'],
                        clean=True, cwd='/w/dev')
        if step == 'session':
            return dict(base, ok=True,
                        mode='fresh' if request['harness'] in ('deepseek', 'dsh') else 'brief',
                        executor_enabled=False, oauth_cache_transferred=False)
        raise AssertionError('unexpected bridge step: ' + step)


class AdapterTest(PgTestCase):
    """The companion against a durable operation, a real schema and fake hosts."""

    def setUp(self):
        super().setUp()
        self.fake = FakeBridge()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.host = 'ameesh-agents-2'
        for name, value in self._documents().items():
            path = self.root / (name + '.json')
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.factory = factory_module.HandoffFactory(
            db=self.db, root=self.root, bridge=self.fake)
        self.adapter = self.factory.adapter
        registry.upsert(self.db, 'dev', chantier='ameesh', harness='deepseek',
                        host='source', cwd='/work/source')
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target']"
                        " WHERE name='dev'")
        registry.claim(self.db, 'dev', 'runner', ttl_seconds=300)
        self.api = storage.of(self.db)
        for host in ('source', 'target'):
            self.api.canon.record_state(
                host, 'ok', root='/canon', source='local', commit=COMMIT,
                good=True, diagnostic='')
            for _ in range(3):
                self.reading(host, memory=3 * GIB if host == 'source' else 24 * GIB)
        self.limits = resources.thresholds(
            {'resources': {'min_mem_available': '4GiB', 'min_disk_free': '1GiB'}})
        self.limits['max_agents'] = 4
        for harness, account in (('deepseek', ''), ('gh', '')):
            self.api.credentials.report('target', harness, account, 'healthy',
                                        source='manual', expires_at=time.time() + 3600)
        self.row = self.reserve()

    def _documents(self):
        def endpoint(host, number):
            return {'host': host, 'address': '10.0.0.%d' % number, 'port': 22,
                    'user': 'ameesh-handoff', 'host_key_alias': host,
                    'known_hosts': str(self.root / (host + '.known')),
                    'known_hosts_sha256': 'a' * 64,
                    'identity_file': str(self.root / (host + '.id')),
                    'seconds': 30}
        return {
            'enrollment': {'version': 1, 'host': self.host},
            'hosts': {'hosts': {
                self.host: {'deepseek': {'accounts': [''], 'tools': ['gh'],
                                         'secrets': []}},
                'source': {'deepseek': {'accounts': [''], 'tools': ['gh'],
                                        'secrets': []}},
                'target': {'deepseek': {'accounts': [''], 'tools': ['gh'],
                                        'secrets': []}},
            }},
            'repositories': {'repositories': {
                'ameesh': 'git@forge:manaty/app.git'}},
            'secrets': {'references': {}},
            'transports': {'transports': {
                'source': endpoint('source', 1),
                'target': endpoint('target', 2),
            }},
        }

    def reading(self, host, memory, **fields):
        return self.api.hosts.record({
            'host': host, 'mem_available_bytes': memory, 'swap_used_bytes': 0,
            'cpu_count': 8, 'load1': 0.0, 'disk_free_bytes': 100 * GIB,
            'turns_in_progress': 0, **fields})

    def candidate(self):
        return handoffs.plan(self.db, registry.get(self.db, 'dev'),
                             limits_for=lambda host: self.limits)

    def reserve(self):
        candidate = self.candidate()
        self.assertIsNotNone(candidate)
        return handoffs.request(self.db, candidate,
                                repository='git@forge:manaty/app.git')

    def intent(self, step):
        row = self.api.handoffs.get(self.row['id'])
        with self.api.handoffs.controller(row['id']) as tx:
            self.assertIsNotNone(tx)
            value = storage.of(tx).handoffs.begin_effect(row['id'], row['state'], step)
        self.assertIsNotNone(value)
        value = dict(value)
        value['controller_epoch'] = value['evidence']['controller_epoch']
        return value

    def finish(self, operation):
        try:
            self.api.handoffs.finish_effect(operation['id'],
                                            operation['controller_epoch'])
        except Exception:
            pass

    def plain(self, step=None, **changes):
        row = self.api.handoffs.get(self.row['id'])
        evidence = ({'pending_effect': {'name': step, 'epoch': 1}}
                    if step else {})
        evidence.update(changes.pop('evidence', {}))
        value = {'id': row['id'], 'agent': row['agent'], 'source': row['source'],
                 'target': row['target'], 'state': row['state'],
                 'plan': row['plan'], 'controller_epoch': 1,
                 'evidence': evidence}
        value.update(changes)
        return value

    def test_prepare_proves_the_exact_destination_without_a_start(self):
        operation = self.intent('prepare')
        try:
            result = self.adapter.prepare(operation)
        finally:
            self.finish(operation)
        self.assertIs(result['ok'], True)
        self.assertEqual(result['canon_revision'], COMMIT)
        self.assertEqual(result['release_revision'], COMMIT)
        self.assertTrue(result['credentials_ready'])
        self.assertEqual(self.fake.requests[-1]['step'], 'prepare')
        self.assertNotIn('enable', [request['step'] for request in self.fake.requests])
        inventory = self.adapter.inventory('target')
        self.assertEqual(inventory['tools'], {'gh': True})
        self.assertEqual(inventory['secrets'], {})

    def test_prepare_refuses_a_changed_canon_or_a_closed_gate(self):
        operation = self.intent('prepare')
        try:
            self.db.execute("UPDATE canon_state SET last_good_commit='other'"
                            " WHERE host='target' AND canon=''")
            result = self.adapter.prepare(operation)
        finally:
            self.finish(operation)
        self.assertEqual(result, {'ok': False, 'reason': 'prerequisites'})
        self.db.execute("UPDATE canon_state SET last_good_commit=%s"
                        " WHERE host='target' AND canon=''", (COMMIT,))
        operation = self.intent('prepare')
        try:
            self.reading('target', memory=24 * GIB, gate_state='draining')
            result = self.adapter.prepare(operation)
        finally:
            self.finish(operation)
        self.assertEqual(result, {'ok': False, 'reason': 'prerequisites'})

    def test_prepare_refuses_a_failing_destination_login(self):
        self.api.credentials.report('target', 'deepseek', '', 'failing',
                                    source='manual')
        operation = self.intent('prepare')
        try:
            result = self.adapter.prepare(operation)
        finally:
            self.finish(operation)
        self.assertEqual(result, {'ok': False, 'reason': 'prerequisites'})

    def test_revalidate_requires_a_current_placement_canon_and_login(self):
        operation = self.intent('revalidate')
        try:
            self.assertIs(self.adapter.revalidate(operation), True)
        finally:
            self.finish(operation)
        self.fake.revalidate_ok = False
        operation = self.intent('revalidate')
        try:
            self.assertIs(self.adapter.revalidate(operation), False)
        finally:
            self.finish(operation)
        self.fake.revalidate_ok = True
        self.db.execute("UPDATE canon_state SET status='invalid'"
                        " WHERE host='target' AND canon=''")
        operation = self.intent('revalidate')
        try:
            self.assertIs(self.adapter.revalidate(operation), False)
        finally:
            self.finish(operation)

    def test_stop_publish_transfer_and_session_return_only_proof_fields(self):
        self.assertIs(self.adapter.stop_source(self.plain('stop_source')), True)
        publish = self.adapter.publish(self.plain('publish'))
        self.assertEqual(set(publish), {'repository', 'branch', 'commit',
                                        'remote_commit', 'clean'})
        transfer = self.adapter.transfer(self.plain(
            'transfer', evidence={'branch': 'main', 'commit': COMMIT}))
        self.assertEqual(transfer['cwd'], '/w/dev')
        session = self.adapter.session(self.plain(
            'session', evidence={'branch': 'main', 'commit': COMMIT,
                                 'cwd': '/w/dev'}))
        self.assertEqual(session, {'ok': True, 'mode': 'fresh',
                                   'executor_enabled': False})
        steps = [request['step'] for request in self.fake.requests]
        self.assertEqual(steps, ['stop_source', 'publish', 'transfer', 'session'])

    def test_the_controller_proof_rejects_a_mismatched_receipt(self):
        operation = self.plain('publish')
        receipt = self.adapter.publish(operation)
        with self.assertRaises(handoffs.HandoffError):
            handoffs._proof('publish', dict(receipt, commit='b' * 40), operation)
        receipt = self.adapter.transfer(self.plain(
            'transfer', evidence={'branch': 'main', 'commit': COMMIT}))
        with self.assertRaises(handoffs.HandoffError):
            handoffs._proof('transfer', dict(receipt, cwd='relative'), operation)
        receipt = self.adapter.session(self.plain(
            'session', evidence={'branch': 'main', 'commit': COMMIT,
                                 'cwd': '/w/dev'}))
        with self.assertRaises(handoffs.HandoffError):
            handoffs._proof('session', dict(receipt, mode='brief'), operation)

    def test_a_refused_host_effect_is_not_a_simulated_success(self):
        self.fake.fail = {'prepare'}
        operation = self.intent('prepare')
        try:
            result = self.adapter.prepare(operation)
        finally:
            self.finish(operation)
        self.assertIs(result['ok'], False)
        self.fake.fail = {'stop_source', 'publish', 'transfer', 'session'}
        self.assertIs(self.adapter.stop_source(self.plain('stop_source')), False)
        for method in ('publish', 'transfer', 'session'):
            evidence = ({'branch': 'main', 'commit': COMMIT, 'cwd': '/w/dev'}
                        if method == 'session' else
                        {'branch': 'main', 'commit': COMMIT})
            result = getattr(self.adapter, method)(
                self.plain(method, evidence=evidence))
            self.assertIs(result['ok'], False)

    def test_each_effect_requires_its_durable_pending_intent(self):
        self.assertIs(self.adapter.stop_source(self.plain()), False)
        mismatched = self.plain('publish')
        mismatched['evidence']['pending_effect']['name'] = 'transfer'
        self.assertIs(self.adapter.publish(mismatched)['ok'], False)
        wrong_epoch = self.plain('publish')
        wrong_epoch['evidence']['pending_effect']['epoch'] = 9
        self.assertIs(self.adapter.publish(wrong_epoch)['ok'], False)

    def test_repository_userinfo_is_refused_before_reservation(self):
        self.db.execute("UPDATE host_handoffs SET state='cancelled' WHERE id=%s",
                        (self.row['id'],))
        candidate = self.candidate()
        self.assertIsNotNone(candidate)
        for repository in ('http://TOKEN@forge.example/r.git',
                           'git+https://TOKEN@forge.example/r.git',
                           'ssh://TOKEN@forge.example/r.git',
                           'ssh://git:secret@forge.example/r.git',
                           'https://git@forge.example/r.git'):
            with self.subTest(repository=repository), self.assertRaises(
                    handoffs.HandoffError):
                handoffs.request(self.db, candidate, repository=repository)
        for repository in ('git@forge:manaty/ameesh.git',
                           'ssh://git@forge.example/manaty/ameesh.git',
                           'https://forge.example/manaty/ameesh.git'):
            with self.subTest(repository=repository):
                row = handoffs.request(self.db, candidate, repository=repository)
                self.assertEqual(row['plan']['repository'], repository)
                self.db.execute("UPDATE host_handoffs SET state='cancelled'"
                                " WHERE id=%s", (row['id'],))

    def test_plan_carries_the_exact_session_account(self):
        self.db.execute("UPDATE host_handoffs SET state='cancelled' WHERE id=%s",
                        (self.row['id'],))
        self.db.execute("UPDATE agent_registry SET session_id='session-1',"
                        " session_account='main' WHERE name='dev'")
        candidate = handoffs.plan(
            self.db, registry.get(self.db, 'dev'),
            limits_for=lambda host: self.limits)
        self.assertEqual(candidate['session_account'], 'main')
