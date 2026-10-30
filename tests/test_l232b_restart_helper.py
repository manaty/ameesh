# SPDX-License-Identifier: AGPL-3.0-only
"""Restart effects outlive the SSH helper; exact receipts prevent repeat."""
import importlib.util
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('restart_helper',
        Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/helper-redemarrage.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class Authority:
    allowed = True
    def validate(self,*args):
        return self.allowed


class Fence:
    closed = True
    def fenced(self,request):
        return self.closed

    @contextmanager
    def guard(self,request):
        if not self.closed:
            raise helper.HelperError('live drained host boundary required')
        yield


class System:
    boot_id = 'fixture-boot'
    drained = True
    fail_reply = False
    action = 'absent'
    def __init__(self):
        self.submissions = []
    def boot(self):
        return self.boot_id
    def quiet(self,units, *, manager):
        return self.drained
    def state(self,job):
        return self.action
    def submit(self,job,units,seconds, *, manager):
        self.submissions.append((job,units,seconds))
        self.action = 'success'
        if self.fail_reply:
            raise RuntimeError('synthetic private canary must not escape')


class RestartHelperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = dict(host='enrolled-host',units=['ameesh-runner@test.service'],
                           controller_unit='ameesh-release-controller.service',manager={'kind':'system'},restart_seconds=5)
        manifest = dict(host='enrolled-host',commit='a'*40,release='a'*40+'-'+'b'*64,
                        database='mesh-test',units=self.policy['units'],
                        policy=helper.policy_id(self.policy),rollback_until=2000000000)
        self.request = dict(operation='test-release',manifest=manifest,
                            manifest_digest=helper.protocol.identity(manifest),
                            action_id='test-release:restart',step='restart')
        self.authority,self.fence,self.system = Authority(),Fence(),System()
        self.engine = helper.RestartHelper(Path(self.temp.name)/'journal',self.policy,
                           authority=self.authority,fence=self.fence,system=self.system)

    def perform(self):
        return self.engine.perform('restart',self.request)

    def test_success_replay_never_restarts_twice(self):
        expected = dict(self.request,ok=True)
        self.assertEqual(self.perform(),expected)
        self.assertEqual(self.perform(),expected)
        self.assertEqual(len(self.system.submissions),1)
        self.assertNotIn(self.policy['controller_unit'],self.system.submissions[0][1])

    def test_lost_submit_reply_reconciles_retained_action_unit(self):
        self.system.fail_reply = True
        with self.assertRaises(helper.HelperError) as failure:
            self.perform()
        self.assertNotIn('canary',str(failure.exception))
        self.assertEqual(self.perform(),dict(self.request,ok=True))
        self.assertEqual(len(self.system.submissions),1)

    def test_crash_between_completed_unit_and_receipt_is_reconciled(self):
        original = helper.protocol.save
        def lost(path,record):
            if record.get('receipt'):
                raise RuntimeError('lost durable reply')
            return original(path,record)
        with mock.patch.object(helper.protocol,'save',lost), self.assertRaises(helper.HelperError):
            self.perform()
        self.assertEqual(self.perform(),dict(self.request,ok=True))
        self.assertEqual(len(self.system.submissions),1)

    def test_missing_action_or_new_boot_never_resubmits_ambiguous_intent(self):
        self.system.fail_reply = True
        with self.assertRaises(helper.HelperError):
            self.perform()
        self.system.action = 'absent'
        with self.assertRaisesRegex(helper.HelperError,'uncertain'):
            self.perform()
        self.system.boot_id = 'different-boot'
        with self.assertRaisesRegex(helper.HelperError,'reboot'):
            self.perform()
        self.assertEqual(len(self.system.submissions),1)

    def test_db_fence_and_real_process_boundary_both_required(self):
        for boundary in ('fence','processes'):
            with self.subTest(boundary=boundary):
                self.fence.closed = boundary != 'fence'
                self.system.drained = boundary != 'processes'
                with self.assertRaisesRegex(helper.HelperError,'boundary'):
                    self.perform()
        self.assertEqual(self.system.submissions,[])

    def test_scope_change_unknown_unit_and_arbitrary_step_are_refused(self):
        self.request['manifest']['units'] = ['unrelated.service']
        self.request['manifest_digest'] = helper.protocol.identity(self.request['manifest'])
        with self.assertRaisesRegex(helper.HelperError,'scope'):
            self.perform()
        with self.assertRaisesRegex(helper.HelperError,'request'):
            self.engine.perform('shell',self.request)
        self.assertEqual(self.system.submissions,[])

    def test_revocation_after_slow_cgroup_observation_prevents_submission(self):
        def observe(units, *, manager):
            self.authority.allowed = False
            return True
        self.system.quiet = observe
        with self.assertRaisesRegex(helper.HelperError,'denied'):
            self.perform()
        self.assertEqual(self.system.submissions,[])

    def test_revoked_authority_blocks_receipt_replay(self):
        self.perform()
        self.authority.allowed = False
        with self.assertRaisesRegex(helper.HelperError,'denied'):
            self.perform()
        self.assertEqual(len(self.system.submissions),1)

    def test_cgroup_subtree_orphan_is_not_a_drained_runner(self):
        root = Path(self.temp.name)/'cgroup'
        group = root/'system.slice/ameesh-runner@test.service'
        group.mkdir(parents=True)
        (group/'cgroup.procs').write_text('42\n')
        system = helper.Systemd(cgroups=root)
        response = 'LoadState=loaded\nMainPID=42\nControlGroup=/system.slice/ameesh-runner@test.service\n'
        with mock.patch.object(system,'run',return_value=response):
            self.assertTrue(system.quiet(self.policy['units'], manager=self.policy['manager']))
            child = group/'child'
            child.mkdir()
            (child/'cgroup.procs').write_text('99\n')
            self.assertFalse(system.quiet(self.policy['units'], manager=self.policy['manager']))

    def test_manager_is_explicit_closed_and_account_is_strict(self):
        helper.validate_policy(self.policy)
        helper.validate_policy(dict(self.policy, manager={'kind':'user','account':'ameesh'}))
        missing = dict(self.policy)
        del missing['manager']
        with self.assertRaises(helper.HelperError):
            helper.validate_policy(missing)
        for manager in (None, 'system', {}, {'kind':'system','account':'ameesh'},
                        {'kind':'user'}, {'kind':'user','account':['ameesh']},
                        {'kind':'user','account':'ameesh','extra':True}):
            with self.subTest(manager=manager), self.assertRaises(helper.HelperError):
                helper.validate_policy(dict(self.policy,manager=manager))
        for account in ('', '-root', 'a@host', 'a/b', 'a b', 'a\n', 'Ameesh', 'é', 'a'*33):
            with self.subTest(account=account), self.assertRaises(helper.HelperError):
                helper.validate_policy(dict(self.policy,manager={'kind':'user','account':account}))

    def test_user_cgroup_subtree_and_fixed_manager_commands(self):
        manager = {'kind':'user','account':'ameesh'}
        unit = 'ameesh-runner-agent@test.service'
        root = Path(self.temp.name)/'user-cgroups'
        relative = 'user.slice/user-1000.slice/user@1000.service/app.slice/'+unit
        group = root/relative
        group.mkdir(parents=True)
        (group/'cgroup.procs').write_text('42\n')
        system = helper.Systemd(cgroups=root)
        response = 'LoadState=loaded\nMainPID=42\nControlGroup=/'+relative+'\n'
        with mock.patch.object(system,'run',return_value=response) as run:
            self.assertTrue(system.quiet([unit],manager=manager))
            run.assert_called_once_with(['/usr/bin/systemctl','--machine=ameesh@.host',
                '--user','show',unit,'--property=LoadState,MainPID,ControlGroup'])
            child = group/'orphan'
            child.mkdir()
            (child/'cgroup.procs').write_text('99\n')
            self.assertFalse(system.quiet([unit],manager=manager))
        for selected, target in ((manager, ['--machine=ameesh@.host','--user']),
                                 ({'kind':'system'}, [])):
            with mock.patch.object(system,'run') as run:
                system.submit('action.service',[unit],5,manager=selected)
                run.assert_called_once_with(['/usr/bin/systemd-run','--quiet','--no-block',
                    '--unit=action.service','--property=Type=oneshot',
                    '--property=RemainAfterExit=yes','--property=TimeoutStartSec=5s',
                    '--property=Slice=system.slice','--','/usr/bin/systemctl',
                    *target,'restart','--',unit])

    def test_manager_cannot_be_overridden_by_ssh_request(self):
        self.request['manager'] = {'kind':'user','account':'other'}
        with self.assertRaisesRegex(helper.HelperError,'request'):
            self.perform()
        self.assertEqual(self.system.submissions,[])

    def test_systemd_environment_stays_scrubbed(self):
        system = helper.Systemd()
        with mock.patch.object(helper.subprocess,'run',return_value=
                mock.Mock(stdout='',stderr='')) as run:
            system.submit('action.service',self.policy['units'],5,
                          manager={'kind':'user','account':'ameesh'})
            self.assertEqual(run.call_args.kwargs['env'],
                             {'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_enrolled_user_manager_reaches_boundary_and_submission(self):
        policy = dict(self.policy,manager={'kind':'user','account':'ameesh'})
        engine = helper.RestartHelper(Path(self.temp.name)/'user-journal',policy,
            authority=self.authority,fence=self.fence,system=self.system)
        request = dict(self.request,manifest=dict(self.request['manifest'],policy=helper.policy_id(policy)))
        request['manifest_digest'] = helper.protocol.identity(request['manifest'])
        with mock.patch.object(self.system,'quiet',return_value=True) as quiet, \
                mock.patch.object(self.system,'submit',wraps=self.system.submit) as submit:
            self.assertEqual(engine.perform('restart',request),dict(request,ok=True))
            self.assertTrue(quiet.called)
            self.assertEqual(quiet.call_args.kwargs,{'manager':policy['manager']})
            self.assertEqual(submit.call_args.kwargs,{'manager':policy['manager']})

    def test_transient_job_result_needs_invocation_and_successful_exit(self):
        system = helper.Systemd()
        success = 'LoadState=loaded\nActiveState=active\nResult=success\nExecMainStatus=0\nInvocationID='+'a'*32+'\n'
        with mock.patch.object(system,'run',return_value=success):
            self.assertEqual(system.state('synthetic.service'),'success')
        with mock.patch.object(system,'run',return_value=success.replace('a'*32,'')):
            with self.assertRaises(helper.HelperError):
                system.state('synthetic.service')
        with mock.patch.object(system,'run',return_value=success.replace('success','exit-code').replace('=0','=1')):
            self.assertEqual(system.state('synthetic.service'),'failure')

    def test_changed_policy_id_and_corrupt_receipt_are_refused(self):
        self.perform()
        root = self.engine.root
        path = next(root.glob('*.json'))
        record = helper.protocol.load(path)
        record['receipt']['ok'] = 1
        helper.protocol.save(path,record)
        with self.assertRaisesRegex(helper.HelperError,'receipt mismatch'):
            self.perform()
        self.request['manifest']['policy'] = 'unapproved-policy'
        self.request['manifest_digest'] = helper.protocol.identity(self.request['manifest'])
        with self.assertRaisesRegex(helper.HelperError,'scope'):
            self.perform()

    def test_host_dispatcher_refuses_unenrolled_runtime_without_echoing_stdin(self):
        import subprocess
        import sys
        dispatcher = Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/hote-release.py'
        result = subprocess.run([sys.executable,str(dispatcher)],input='{"secret":"private-canary"}',
                                capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout.strip(),'{"error":"host_action_unavailable"}')
        self.assertEqual(result.stderr,'')
        self.assertNotIn('canary',result.stdout+result.stderr)
