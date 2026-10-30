# SPDX-License-Identifier: AGPL-3.0-only
"""No SSH/network: exercise enrollment and a real bounded local pipe peer."""
from tests import support  # isolated environment, including libpq/agent settings
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
from tests import test_l232b_restart_helper as restart_fixture
from tests import test_l232b_release_controller as controller_fixture

SPEC = importlib.util.spec_from_file_location('ssh_release',
        Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/transport-release.py')
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class SSHTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        # The bench can use a shared /tmp; model trusted enrollment ancestors
        # without weakening production validation. The fixture root and files
        # keep their actual modes, ownership and symlink status.
        original_lstat = Path.lstat
        ancestors = set(root.parents)
        def enrolled_lstat(path):
            info = original_lstat(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields = list(info)
                fields[0] &= ~0o022
                fields[4] = os.geteuid()
                return os.stat_result(fields)
            return info
        patch = mock.patch.object(Path,'lstat',enrolled_lstat)
        patch.start()
        self.addCleanup(patch.stop)
        self.hosts = root/'known_hosts'
        self.key = root/'identity'
        self.hosts.write_text('fixture-host ssh-ed25519 synthetic-public-key\n')
        self.key.write_text('synthetic-private-key-canary')
        self.hosts.chmod(0o600)
        self.key.chmod(0o600)
        self.enrollment = dict(host='fixture-host', address='host.example.test',
                port=2222, user='ameesh-release', host_key_alias='fixture-host',
                known_hosts=str(self.hosts), identity_file=str(self.key),
                known_hosts_sha256=hashlib.sha256(self.hosts.read_bytes()).hexdigest(), seconds=5)
        self.manifest = dict(host='fixture-host',commit='a'*40,
                release='a'*40+'-'+'b'*64, database='fixture-db',
                units=['ameesh-runner@fixture.service'],policy='fixture-policy',rollback_until=2000000000)
        self.request = dict(operation='fixture-release', step='restart',
                action_id='fixture-release:restart', manifest=self.manifest,
                manifest_digest=transport.protocol.identity(self.manifest))

    def engine(self):
        return transport.SSHRestartTransport(self.enrollment)

    def test_only_typed_ids_cross_wire_and_pinned_ssh_options_are_fixed(self):
        receipt = dict(self.request, ok=True)
        with mock.patch.object(transport,'exchange',return_value=json.dumps(receipt).encode()) as call:
            self.assertEqual(self.engine().perform('restart',self.request), receipt)
        args, payload, seconds = call.call_args.args
        self.assertEqual(args[0], '/usr/bin/ssh')
        self.assertEqual(args[-1], 'host.example.test')
        self.assertEqual(args[1:3], ['-F','/dev/null'])
        for option in ('BatchMode=yes','StrictHostKeyChecking=yes','UpdateHostKeys=no',
                       'GlobalKnownHostsFile=/dev/null','ForwardAgent=no','IdentityAgent=none',
                       'ClearAllForwardings=yes','ControlPath=none','ProxyCommand=none'):
            self.assertIn(option,args)
        self.assertEqual(set(json.loads(payload)), {'operation','action_id','step','manifest_digest'})
        self.assertNotIn(b'private-key-canary',payload)
        self.assertNotIn('synthetic-private-key-canary',repr(args))
        self.assertEqual(seconds,5)

    def test_changed_host_key_is_refused_before_connection(self):
        engine = self.engine()
        self.hosts.write_text('changed host key')
        with mock.patch.object(transport,'exchange') as call:
            with self.assertRaisesRegex(transport.TransportError,'host key changed'):
                engine.perform('restart',self.request)
            call.assert_not_called()

    def test_private_key_permissions_and_symlinks_are_refused(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(transport.TransportError,'private'):
            self.engine()
        self.key.chmod(0o600)
        link = self.key.with_name('linked-key')
        link.symlink_to(self.key)
        self.enrollment['identity_file'] = str(link)
        with self.assertRaisesRegex(transport.TransportError,'untrusted'):
            self.engine()

    def test_writable_enrollment_directory_is_refused(self):
        root = Path(self.temp.name)
        root.chmod(0o777)
        try:
            with self.assertRaisesRegex(transport.TransportError,'untrusted'):
                self.engine()
        finally:
            root.chmod(0o700)

    def test_option_shell_and_path_injection_are_refused(self):
        for name, value in [('address','-oProxyCommand=evil'),('address','host;evil'),
                            ('user','root'),('port',True),('seconds',0),
                            ('known_hosts',str(self.hosts.parent/'../known_hosts')),
                            ('identity_file','/private/key with spaces')]:
            with self.subTest(name=name,value=value):
                enrollment = dict(self.enrollment, **{name:value})
                with self.assertRaises(transport.TransportError):
                    transport.SSHRestartTransport(enrollment)

    def test_action_scope_and_caller_transport_fields_are_refused(self):
        engine = self.engine()
        for request in (dict(self.request,address='attacker'),
                        dict(self.request,action_id='another:restart'),
                        dict(self.request,manifest_digest='c'*64),
                        dict(self.request,operation='../escape')):
            with mock.patch.object(transport,'exchange') as call:
                with self.assertRaises(transport.TransportError):
                    engine.perform('restart',request)
                call.assert_not_called()
        with self.assertRaises(transport.TransportError):
            engine.perform('configure',self.request)

    def test_wrong_extra_and_non_boolean_receipts_are_ambiguous(self):
        engine = self.engine()
        for receipt in (dict(self.request,ok=1),dict(self.request,ok=True,extra='canary'),
                        dict(self.request,ok=True,action_id='wrong:restart'),
                        {'error':'private-key-canary'}):
            with mock.patch.object(transport,'exchange',return_value=json.dumps(receipt).encode()):
                with self.assertRaises(transport.TransportError) as failure:
                    engine.perform('restart',self.request)
                self.assertNotIn('canary',str(failure.exception))
        with mock.patch.object(transport,'exchange',return_value=b'not JSON private-key-canary'):
            with self.assertRaises(transport.TransportError) as failure:
                engine.perform('restart',self.request)
            self.assertNotIn('canary',str(failure.exception))

    def test_duplicate_receipt_keys_and_nested_boolean_integer_alias_are_refused(self):
        receipt = dict(self.request,ok=True)
        nested = json.loads(json.dumps(receipt))
        nested['manifest']['rollback_until'] = True
        request = json.loads(json.dumps(self.request))
        request['manifest']['rollback_until'] = 1
        request['manifest_digest'] = transport.protocol.identity(request['manifest'])
        nested['manifest_digest'] = request['manifest_digest']
        for output, sent in ((json.dumps(receipt)[:-1]+', "ok": true}',self.request),
                             (json.dumps(nested),request)):
            with mock.patch.object(transport,'exchange',return_value=output.encode()):
                with self.assertRaises(transport.TransportError):
                    self.engine().perform('restart',sent)

    def test_controller_helper_lost_reply_reconciles_release_and_recovery(self):
        for lost_step in ('restart','recovery_restart'):
            with self.subTest(lost_step=lost_step):
                host = controller_fixture.Host()
                if lost_step == 'recovery_restart':
                    host.fail.add('health')
                authority = controller_fixture.Authority()
                policy = dict(host='fixture-host',units=self.manifest['units'],
                    controller_unit='ameesh-release-controller.service',manager={'kind':'system'},restart_seconds=5)
                manifest = dict(self.manifest,policy=restart_fixture.helper.policy_id(policy))
                system = restart_fixture.System()
                states = {}
                system.state = lambda job: states.get(job,'absent')
                def submit(job, units, seconds, *, manager):
                    system.submissions.append((job,units,seconds))
                    states[job] = 'success'
                system.submit = submit
                helper = restart_fixture.helper.RestartHelper(
                    Path(self.temp.name)/lost_step/'helper',policy,
                    authority=authority,fence=restart_fixture.Fence(),system=system)
                engine = self.engine()
                original = host.perform
                requests = {}
                lost = {lost_step}
                def perform(step, request):
                    if step not in ('restart','recovery_restart'):
                        return original(step,request)
                    requests[step] = request
                    return engine.perform(step,request)
                host.perform = perform
                def exchange(args,payload,seconds):
                    wire = json.loads(payload)
                    # Represents the enrolled operation/journal lookup at host.
                    self.assertEqual(wire['manifest_digest'],transport.protocol.identity(manifest))
                    receipt = helper.perform(wire['step'],requests[wire['step']])
                    if wire['step'] in lost:
                        lost.remove(wire['step'])
                        raise OSError('synthetic lost SSH response')
                    return json.dumps(receipt).encode()
                journal = Path(self.temp.name)/lost_step/'controller'
                def run():
                    return transport.protocol.run(journal,'fixture-release',manifest,
                                                  authority=authority,adapter=host)
                with mock.patch.object(transport,'exchange',side_effect=exchange):
                    with self.assertRaisesRegex(transport.protocol.ControllerError,'uncertain'):
                        run()
                    record = json.loads((journal/'fixture-release.json').read_text())
                    self.assertEqual(record['intent'],lost_step)
                    self.assertTrue(host.has_fence)
                    count = len(system.submissions)
                    self.assertEqual(run(),'accepted' if lost_step=='restart' else 'rolled_back')
                    self.assertEqual(len(system.submissions),count)
                    self.assertFalse(host.has_fence)

    def test_definitive_failure_receipt_is_preserved_for_controller_recovery(self):
        receipt = dict(self.request,ok=False)
        with mock.patch.object(transport,'exchange',return_value=json.dumps(receipt).encode()):
            self.assertEqual(self.engine().perform('restart',self.request),receipt)

    def test_ambiguous_lost_reply_never_retries_inside_transport(self):
        engine = self.engine()
        with mock.patch.object(transport,'exchange',side_effect=OSError('private-key-canary')) as call:
            with self.assertRaises(transport.TransportError) as failure:
                engine.perform('restart',self.request)
            self.assertNotIn('canary',str(failure.exception))
            self.assertEqual(call.call_count,1)

    def peer(self, code, *, seconds=2):
        original = subprocess.Popen
        def launch(args, **kwargs):
            self.assertEqual(kwargs['env'], {'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
            return original([sys.executable,'-I','-c',code],**kwargs)
        with mock.patch.object(transport.subprocess,'Popen',side_effect=launch):
            return transport.exchange(['/unused/ssh'],b'fixture-input',seconds)

    def test_real_pipe_peer_receives_eof_with_clean_environment(self):
        with mock.patch.dict(os.environ, {'SSH_AUTH_SOCK':'private-key-canary','PGPASSWORD':'private-key-canary'}):
            output = self.peer("import os,sys; data=sys.stdin.buffer.read(); assert 'PGPASSWORD' not in os.environ; assert 'SSH_AUTH_SOCK' not in os.environ; sys.stdout.buffer.write(data)")
        self.assertEqual(output,b'fixture-input')

    def test_oversized_real_pipe_output_is_killed_and_reaped(self):
        with self.assertRaisesRegex(transport.TransportError,'size'):
            self.peer("import sys; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush()")

    def test_deadline_covers_peer_that_never_reads_or_exits_after_eof(self):
        for code in ("import time; time.sleep(10)",
                     "import os,time; os.close(1); time.sleep(10)"):
            start = time.monotonic()
            with self.assertRaises(transport.TransportError):
                self.peer(code,seconds=0.15)
            self.assertLess(time.monotonic()-start,2)

    def test_nonzero_peer_status_redacts_diagnostic_output(self):
        with self.assertRaises(transport.TransportError) as failure:
            self.peer("import sys; print('private-key-canary'); print('private-key-canary',file=sys.stderr); sys.exit(1)")
        self.assertNotIn('canary',str(failure.exception))
