# SPDX-License-Identifier: AGPL-3.0-only
"""Real HTTPS exchanges against a local Kubernetes API double, no cluster."""
import base64
import dataclasses
import http.server
import json
import shutil
from pathlib import Path
import ssl
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from ameesh.canon import HostPolicy, SandboxPlacement, SandboxJob
from ameesh.sandbox_supervisor import SandboxSupervisor
from ameesh.sandbox_kubernetes import KubernetesJobProvider, FINALIZER
from ameesh.sandbox_provider import (
    InvalidJob, JobAlreadyExists, JobIdentity, JobNotFound, JobSpec,
    JobUIDConflict, ProviderForbidden, ProviderUnavailable,
)

IMAGE = 'registry.invalid/reviewer@sha256:' + 'a' * 64
PROMPT, TOKEN = 'private review prompt', 'private-relay-token'


class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_api()

    do_POST = do_PATCH = do_DELETE = do_GET

    def handle_api(self):
        state = self.server.state
        url = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(body) if body else None
        state['calls'].append((self.command, url.path, parse_qs(url.query), body))
        if state['failures']:
            verb, suffix, code = state['failures'][0]
            if self.command == verb and url.path.endswith(suffix):
                if state.get('failure_skip', 0):
                    state['failure_skip'] -= 1
                else:
                    state['failures'].pop(0)
                    return self.reply(code, {'message': PROMPT + TOKEN})
        if self.headers.get('Authorization') != 'Bearer cluster-token':
            return self.reply(401, {})
        parts = url.path.split('/')
        resource = parts[5] if parts[1] == 'api' else parts[6]
        index = 6 if parts[1] == 'api' else 7
        name = parts[index] if len(parts) > index else None
        objects = state[resource]
        if self.command == 'GET':
            if url.path.endswith('/log'):
                return self.reply(200, state['log'], raw=True)
            if name is None:
                wanted = parse_qs(url.query).get('labelSelector', [''])[0]
                labels = dict(item.split('=', 1) for item in wanted.split(',') if item)
                items = [value for value in objects.values() if all(
                    value['metadata'].get('labels', {}).get(k) == v for k, v in labels.items())]
                return self.reply(200, {'items': items})
            return self.reply(200, objects[name]) if name in objects else self.reply(404, {})
        if self.command == 'POST':
            name = body['metadata']['name']
            if name in objects:
                return self.reply(409, {})
            body['metadata'].update(namespace='reviewers', uid=resource + '-uid',
                                    resourceVersion='1', creationTimestamp='2026-10-31T12:00:00Z')
            objects[name] = body
            return self.reply(201, body)
        if name not in objects:
            return self.reply(404, {})
        obj = objects[name]
        if self.command == 'PATCH':
            if (body['metadata'].get('uid') != obj['metadata']['uid']
                    or body['metadata']['resourceVersion'] != obj['metadata']['resourceVersion']):
                return self.reply(409, {})
            obj['metadata'].update(body['metadata'])
            obj['metadata']['resourceVersion'] = str(int(obj['metadata']['resourceVersion']) + 1)
            return self.reply(200, obj)
        if (body['preconditions']['uid'] != obj['metadata']['uid']
                or body['preconditions'].get('resourceVersion', obj['metadata']['resourceVersion'])
                != obj['metadata']['resourceVersion']):
            return self.reply(409, {})
        del objects[name]
        # Simulate Kubernetes garbage collection by UID owner reference.
        if resource == 'jobs':
            for secret_name, value in list(state['secrets'].items()):
                if any(o['uid'] == obj['metadata']['uid']
                       for o in value['metadata'].get('ownerReferences', [])):
                    del state['secrets'][secret_name]
        return self.reply(200, {'kind': 'Status'})

    def reply(self, code, body, raw=False):
        content = body.encode() if raw else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class KubernetesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('openssl') is None:
            raise unittest.SkipTest('openssl unavailable for local HTTPS fixture')
        cls.directory = tempfile.TemporaryDirectory(prefix='ameesh-kubernetes-')
        cls.root = Path(cls.directory.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.root / 'key'), '-out', str(cls.root / 'cert'),
                        '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cls.root / 'cert', cls.root / 'key')
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def setUp(self):
        self.server.state = {'jobs': {}, 'secrets': {}, 'pods': {}, 'calls': [],
                             'failures': [], 'log': '{"result": "ok"}'}
        self.config = {'current-context': 'chosen',
                       'clusters': [{'name': 'cluster', 'cluster': {
                           'server': 'https://localhost:%s' % self.server.server_port,
                           'certificate-authority': str(self.root / 'cert')}}],
                       'users': [{'name': 'user', 'user': {'token': 'cluster-token'}}],
                       'contexts': [{'name': 'chosen', 'context': {
                           'cluster': 'cluster', 'user': 'user'}}]}
        self.path = self.root / 'kubeconfig'
        self.write_config()
        self.host = SimpleNamespace(policy=HostPolicy(sandbox=SandboxPlacement(
            'kubernetes', str(self.path), 'reviewers', IMAGE, ['profile'], 1,
            'example/home#1', SandboxJob(1200, 0, 3600, False))))
        self.identity = JobIdentity('turn-1', 'reviewer', 1, 'host', 'profile', 'reviewers')
        self.spec = JobSpec(self.identity, IMAGE, 1200, 3600, ('review',))
        self.provider = self.make_provider()

    def write_config(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def make_provider(self):
        return KubernetesJobProvider(self.host, turn_seconds=900,
            secrets=lambda identity: {'prompt': PROMPT, 'relay_token': TOKEN})

    def create(self):
        return self.provider.create(self.spec)

    def pod(self, message='digest', uid='jobs-uid'):
        self.server.state['pods']['pod-1'] = {
            'metadata': {'name': 'pod-1', 'namespace': 'reviewers',
                         'labels': {'batch.kubernetes.io/controller-uid': uid},
                         'ownerReferences': [{'kind': 'Job', 'uid': uid}]},
            'status': {'containerStatuses': [{'name': 'turn', 'state': {
                'terminated': {'message': message}}}]}}

    def test_five_calls_hardening_secret_and_gc(self):
        job = self.create()
        state = self.server.state
        value = state['jobs'][job.name]
        spec = value['spec']
        self.assertEqual(spec['backoffLimit'], 0)
        self.assertEqual(spec['activeDeadlineSeconds'], 1200)
        self.assertEqual(spec['ttlSecondsAfterFinished'], 3600)
        pod = spec['template']['spec']
        self.assertIs(pod['automountServiceAccountToken'], False)
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(pod['containers'][0]['command'], ['review'])
        self.assertNotIn(PROMPT, json.dumps(value))
        self.assertNotIn(TOKEN, json.dumps(value))
        secret = state['secrets'][job.name]
        self.assertEqual(base64.b64decode(secret['data']['prompt']).decode(), PROMPT)
        self.assertEqual(secret['metadata']['ownerReferences'][0]['uid'], job.uid)
        self.assertNotIn(FINALIZER, value['metadata']['finalizers'])
        self.assertEqual(self.provider.get(job.namespace, job.name), job)
        self.assertEqual(self.provider.list(job.namespace, self.identity.labels), [job])
        self.assertEqual(self.provider.list(job.namespace, {'ameesh.dev/turn-id': 'other'}), [])
        self.pod()
        self.assertEqual(self.provider.logs(job.namespace, job.name), state['log'])
        self.assertTrue(self.provider.delete(job.namespace, job.name, uid=job.uid))
        self.assertEqual(state['secrets'], {})
        self.assertIsNone(self.provider.get(job.namespace, job.name))
        self.assertFalse(self.provider.delete(job.namespace, job.name))
        with self.assertRaises(JobNotFound):
            self.provider.logs(job.namespace, job.name)

    def test_replayed_create_and_uid_fence(self):
        job = self.create()
        with self.assertRaises(JobAlreadyExists):
            self.create()
        posts = [call for call in self.server.state['calls'] if call[0] == 'POST']
        self.assertEqual(len(posts), 2)  # one Secret, one Job
        with self.assertRaises(JobUIDConflict):
            self.provider.delete(job.namespace, job.name, uid='replacement')
        self.assertEqual(self.provider.get(job.namespace, job.name).uid, job.uid)
        self.server.state['failures'] = [('DELETE', job.name, 409)]
        with self.assertRaises(JobUIDConflict):
            self.provider.delete(job.namespace, job.name, uid=job.uid)
        self.assertIn(job.name, self.server.state['jobs'])

    def test_status_conditions_and_bounded_termination_message(self):
        job = self.create()
        value = self.server.state['jobs'][job.name]
        value['status'] = {'failed': 1}
        self.assertEqual(self.provider.get(job.namespace, job.name).phase, 'Pending')
        value['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}],
                           'completionTime': '2026-10-31T12:01:00Z'}
        self.pod('digest')
        seen = self.provider.get(job.namespace, job.name)
        self.assertEqual(seen.phase, 'Succeeded')
        self.assertEqual(seen.message, 'digest')
        self.assertEqual(seen.completed_at - seen.created_at, 60)
        self.pod('é' * 2049)
        self.assertEqual(self.provider.get(job.namespace, job.name).message, '')

    def test_bounded_logs(self):
        job = self.create()
        self.pod()
        self.server.state['log'] = 'x' * 65536
        self.assertEqual(len(self.provider.logs(job.namespace, job.name)), 65536)
        self.server.state['log'] += 'x'
        self.assertEqual(len(self.provider.logs(job.namespace, job.name)), 65537)
        calls = [c for c in self.server.state['calls'] if c[1].endswith('/log')]
        self.assertEqual(calls[-1][2]['limitBytes'], ['65537'])

    def test_http_errors_do_not_echo_server_body(self):
        for status, exception in ((401, ProviderForbidden), (403, ProviderForbidden),
                                  (429, ProviderUnavailable), (500, ProviderUnavailable),
                                  (409, JobAlreadyExists)):
            with self.subTest(status=status):
                self.server.state['failures'] = [('GET', self.identity.name, status)]
                with self.assertRaises(exception) as raised:
                    self.provider.get('reviewers', self.identity.name)
                self.assertNotIn(PROMPT, str(raised.exception))
                self.assertNotIn(TOKEN, str(raised.exception))

    def test_forbidden_and_unavailable_for_each_operation(self):
        job = self.create()
        operations = (
            ('GET', '/jobs', lambda: self.provider.list('reviewers')),
            ('GET', job.name, lambda: self.provider.get('reviewers', job.name)),
            ('GET', job.name, lambda: self.provider.logs('reviewers', job.name)),
            ('DELETE', job.name, lambda: self.provider.delete('reviewers', job.name)),
        )
        for status, exception in ((403, ProviderForbidden), (503, ProviderUnavailable)):
            for verb, suffix, operation in operations:
                with self.subTest(status=status, verb=verb, suffix=suffix):
                    self.server.state['failures'] = [(verb, suffix, status)]
                    with self.assertRaises(exception):
                        operation()
            other = dataclasses.replace(self.spec, identity=dataclasses.replace(
                self.identity, turn_id='turn-%s' % status))
            self.server.state['failures'] = [('POST', '/jobs', status)]
            with self.assertRaises(exception):
                self.provider.create(other)

    def test_recover_after_secret_created_job_create_failed(self):
        self.server.state['failures'] = [('POST', '/jobs', 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        self.assertEqual(len(self.server.state['secrets']), 1)
        job = self.create()
        self.assertEqual(len(self.server.state['secrets']), 1)
        self.assertEqual(len(self.server.state['jobs']), 1)
        self.assertEqual(self.server.state['secrets'][job.name]['metadata']['ownerReferences'][0]['uid'], job.uid)

    def test_recover_after_job_created_owner_patch_failed(self):
        self.server.state['failures'] = [('PATCH', self.identity.name, 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        value = self.server.state['jobs'][self.identity.name]
        self.assertIn(FINALIZER, value['metadata']['finalizers'])
        # A fresh instance has no memory of the failed create.
        provider = self.make_provider()
        job = provider.get('reviewers', self.identity.name)
        self.assertNotIn(FINALIZER, value['metadata']['finalizers'])
        self.assertTrue(provider.delete('reviewers', job.name, uid=job.uid))
        self.assertEqual(self.server.state['secrets'], {})

    def test_cancelled_intent_cleans_unowned_secret(self):
        self.server.state['failures'] = [('POST', '/jobs', 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        self.assertFalse(self.provider.delete('reviewers', self.identity.name))
        self.assertEqual(self.server.state['secrets'], {})
        self.assertEqual(self.server.state['jobs'], {})

    def test_orphan_cleanup_fences_secret_uid_and_resource_version(self):
        self.server.state['failures'] = [('POST', '/jobs', 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        self.server.state['failures'] = [('DELETE', self.identity.name, 409)]
        with self.assertRaises(JobUIDConflict):
            self.provider.delete('reviewers', self.identity.name)
        self.assertIn(self.identity.name, self.server.state['secrets'])
        call = self.server.state['calls'][-1]
        self.assertEqual(call[3]['preconditions'], {'uid': 'secrets-uid', 'resourceVersion': '1'})

    def test_conflicting_secret_is_not_overwritten(self):
        self.server.state['failures'] = [('POST', '/jobs', 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        secret = self.server.state['secrets'][self.identity.name]
        secret['data']['prompt'] = base64.b64encode(b'foreign').decode()
        with self.assertRaises(JobUIDConflict):
            self.create()
        self.assertEqual(self.server.state['jobs'], {})

    def test_policy_and_argv_refused_before_io(self):
        for spec in (dataclasses.replace(self.spec, image='other.invalid/image@sha256:' + 'b' * 64),
                     dataclasses.replace(self.spec, active_deadline_seconds=800),
                     dataclasses.replace(self.spec, argv=(TOKEN,))):
            with self.subTest(spec=spec), self.assertRaises(InvalidJob):
                self.provider.create(spec)
        self.assertFalse(any(c[0] == 'POST' for c in self.server.state['calls']))
        with self.assertRaises(InvalidJob):
            self.provider.list('another-namespace')

    def test_kubeconfig_refusals_and_context_selection(self):
        self.path.chmod(0o644)
        with self.assertRaises(InvalidJob):
            self.make_provider()
        self.write_config()
        for field, value in (('exec', {'command': 'false'}),
                             ('auth-provider', {'name': 'plugin'})):
            self.config['users'][0]['user'][field] = value
            self.write_config()
            with self.assertRaises(InvalidJob):
                self.make_provider()
            del self.config['users'][0]['user'][field]
        self.config['clusters'][0]['cluster']['insecure-skip-tls-verify'] = True
        self.write_config()
        with self.assertRaises(InvalidJob):
            self.make_provider()
        del self.config['clusters'][0]['cluster']['insecure-skip-tls-verify']
        self.config['current-context'] = 'missing'
        self.write_config()
        with self.assertRaises(ProviderUnavailable):
            self.make_provider()
        del self.config['current-context']
        self.write_config()
        with self.assertRaises(ProviderUnavailable):
            self.make_provider()
        self.config['current-context'] = 'chosen'
        self.config['contexts'][0]['context']['cluster'] = 'missing'
        self.write_config()
        with self.assertRaises(ProviderUnavailable):
            self.make_provider()

    def supervisor_row(self, job, *, cancelling=False):
        return dict(turn_id=self.identity.turn_id, agent=self.identity.agent,
            lease_epoch=self.identity.epoch, namespace=job.namespace, job_name=job.name,
            job_uid=job.uid, profile_ref=self.identity.profile,
            placement_ref=self.identity.placement, image_digest=IMAGE,
            state='cancelling' if cancelling else 'bound', deadline_ts=200,
            cancel_requested_at=100 if cancelling else None)

    def test_terminal_secret_message_remains_observable_and_cancellable(self):
        job = self.create()
        self.server.state['jobs'][job.name]['status'] = {
            'conditions': [{'type': 'Failed', 'status': 'True'}]}
        self.pod(TOKEN)
        observed = self.provider.get(job.namespace, job.name)
        self.assertEqual(observed.message, '')
        self.assertEqual(self.provider.list(job.namespace)[0].message, '')
        supervisor = SandboxSupervisor(self.provider, lambda event: None,
                                       orphan_after=10, clock=lambda: 100)
        event = supervisor.step(self.supervisor_row(job, cancelling=True), self.spec)
        self.assertEqual(event.kind, 'cancelled')
        # Explicit cancellation cleans the Secret even for a terminal Job.
        self.assertEqual(self.server.state['secrets'], {})

    def test_cancel_after_secret_creation_and_failed_job_post_cleans_secret(self):
        self.server.state['failures'] = [('POST', '/jobs', 503)]
        with self.assertRaises(ProviderUnavailable):
            self.create()
        self.assertTrue(self.server.state['secrets'])
        self.assertFalse(self.server.state['jobs'])
        missing = SimpleNamespace(namespace='reviewers', name=self.identity.name, uid=None)
        row = self.supervisor_row(missing, cancelling=True)
        supervisor = SandboxSupervisor(self.provider, lambda event: None, orphan_after=10, clock=lambda: 100)
        event = supervisor.step(row, self.spec)
        self.assertEqual(event.kind, 'cancelled')
        self.assertFalse(self.server.state['secrets'])

    def test_running_cancel_calls_delete_despite_malicious_message(self):
        job = self.create()
        self.pod(TOKEN)
        self.server.state['jobs'][job.name]['status'] = {'active': 1}
        supervisor = SandboxSupervisor(self.provider, lambda event: None,
                                       orphan_after=10, clock=lambda: 100)
        event = supervisor.step(self.supervisor_row(job, cancelling=True), self.spec)
        self.assertEqual(event.kind, 'cancelled')
        self.assertTrue(any(c[0] == 'DELETE' and '/jobs/' in c[1]
                            for c in self.server.state['calls']))
        self.assertEqual(self.server.state['secrets'], {})

    def test_invalid_log_is_failed_on_first_supervisor_step(self):
        job = self.create()
        self.server.state['jobs'][job.name]['status'] = {
            'conditions': [{'type': 'Complete', 'status': 'True'}]}
        self.pod('digest')
        supervisor = SandboxSupervisor(self.provider, lambda event: None,
                                       orphan_after=10, clock=lambda: 100)
        for content in ('x' * 65537, TOKEN):
            self.server.state['log'] = content
            event = supervisor.step(self.supervisor_row(job), self.spec)
            self.assertEqual(event.kind, 'failed')
            self.assertEqual(event.refusal, 'missing')
            self.assertNotIn(TOKEN, json.dumps(event.as_dict()))

    def test_secret_transport_failure_retries_then_returns_result(self):
        from ameesh.storage.postgres.sandbox import _canonical
        job = self.create()
        row = self.supervisor_row(job)
        row.update(scope={'repository': 'example/repo', 'pr': 1, 'head': 'a' * 40},
                   scope_digest='scope-digest', source={'contract': 'ameesh-sandbox/1'},
                   limits=dict(turns=1, tool_calls=10, seconds=60, model_calls=10,
                               session='none', tools=[], capabilities=[]))
        result = dict(schema='ameesh-sandbox-result/1', turn_id=row['turn_id'],
            epoch=row['lease_epoch'], scope=row['scope'], scope_digest=row['scope_digest'],
            result_id='result-1', outcome='accepted', verdict='green', findings=[], evidence=[],
            usage=dict(turns=1, tool_calls=1, seconds=2, model_calls=1),
            cost={'usd': 0.01}, sandbox_session=None, source=row['source'],
            finished_at='2026-10-31T12:00:00Z')
        self.server.state['jobs'][job.name]['status'] = {
            'conditions': [{'type': 'Complete', 'status': 'True'}]}
        self.server.state['log'] = json.dumps(result)
        self.pod(json.dumps({'result_sha256': _canonical(result)[1]}))
        supervisor = SandboxSupervisor(self.provider, lambda event: None,
                                       orphan_after=10, clock=lambda: 100)
        for status, refusal, skip in ((503, 'unavailable', 0), (503, 'unavailable', 2),
                                     (403, 'forbidden', 0), (403, 'forbidden', 2)):
            with self.subTest(status=status, earlier_secret_reads=skip):
                self.server.state['failure_skip'] = skip
                self.server.state['failures'] = [('GET', '/secrets/' + job.name, status)]
                event = supervisor.step(row, self.spec)
                self.assertEqual(event.kind, 'retry')
                self.assertEqual(event.refusal, refusal)
                event = supervisor.step(row, self.spec)
                self.assertEqual(event.kind, 'result')
                self.assertEqual(event.result, result)
                self.assertFalse(any(c[0] == 'DELETE' for c in self.server.state['calls']))

    def test_prompt_is_valid_output_but_relay_token_is_neutralized(self):
        job = self.create()
        self.pod()
        self.server.state['log'] = PROMPT
        self.assertEqual(self.provider.logs(job.namespace, job.name), PROMPT)
        self.server.state['log'] = TOKEN
        self.assertEqual(self.provider.logs(job.namespace, job.name), '')

    def test_typed_host_policy(self):
        placement = SandboxPlacement('kubernetes', str(self.path), 'reviewers', IMAGE,
            ['profile'], 1, 'example/home#1', SandboxJob(1200, 0, 3600, False))
        typed = SimpleNamespace(policy=HostPolicy(sandbox=placement))
        provider = KubernetesJobProvider(typed, turn_seconds=900,
            secrets=lambda identity: {'prompt': PROMPT, 'relay_token': TOKEN})
        self.assertEqual(provider.create(self.spec).name, self.identity.name)

    def test_kubeconfig_yaml_without_pyyaml_token_file_and_embedded_ca(self):
        from ameesh import canon
        token = self.root / 'token'
        token.write_text('cluster-token')
        token.chmod(0o600)
        ca = base64.b64encode((self.root / 'cert').read_bytes()).decode()
        self.path.write_text("""apiVersion: v1
kind: Config
current-context: chosen
clusters:
  - name: cluster
    cluster:
      server: https://localhost:%s
      certificate-authority-data: %s
users:
  - name: user
    user:
      tokenFile: token
contexts:
  - name: chosen
    context:
      cluster: cluster
      user: user
""" % (self.server.server_port, ca))
        with mock.patch.object(canon, '_yaml', None):
            provider = self.make_provider()
            self.assertIsNone(provider.get('reviewers', self.identity.name))

    def test_kubeconfig_client_certificate_data(self):
        user = self.config['users'][0]['user']
        user['client-certificate-data'] = base64.b64encode((self.root / 'cert').read_bytes()).decode()
        user['client-key-data'] = base64.b64encode((self.root / 'key').read_bytes()).decode()
        self.write_config()
        self.assertIsNone(self.make_provider().get('reviewers', self.identity.name))

    def test_multiple_pods_and_foreign_owner_are_not_used(self):
        job = self.create()
        self.pod()
        self.server.state['pods']['foreign'] = {
            'metadata': {'name': 'foreign',
                         'labels': {'batch.kubernetes.io/controller-uid': job.uid},
                         'ownerReferences': [{'kind': 'Job', 'uid': 'foreign'}]}}
        self.assertEqual(self.provider.logs(job.namespace, job.name), self.server.state['log'])
        self.server.state['pods']['foreign']['metadata']['ownerReferences'][0]['uid'] = job.uid
        self.assertEqual(self.provider.logs(job.namespace, job.name), '')

    def test_unreachable_server(self):
        self.provider.server = 'https://127.0.0.1:1'
        with self.assertRaises(ProviderUnavailable):
            self.provider.list('reviewers')


if __name__ == '__main__':
    unittest.main()
