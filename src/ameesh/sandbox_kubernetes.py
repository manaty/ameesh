# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in Kubernetes JobProvider; no runner hook or cluster discovery.

Kubeconfig is explicitly selected by Host.policy.sandbox. Static bearer tokens
and TLS client certificates are supported; executable credential plugins and
insecure TLS are refused. Secrets enter only through the per-turn callback,
never through JobSpec.argv. HTTP errors never include server bodies or URLs.
"""
from __future__ import annotations

import base64
import datetime
import json
import math
import re
import os
import ssl
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib import error, parse, request

from .canon import HostPolicy, SandboxPlacement, load_yaml
from .sandbox_provider import (
    InvalidJob, Job, JobAlreadyExists, JobIdentity, JobNotFound, JobProvider,
    JobSpec, JobUIDConflict, ProviderError, ProviderForbidden, ProviderUnavailable,
    phase_from_status,
)

MAX_LOG_BYTES = 65536
MAX_MESSAGE_BYTES = 4096
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FINALIZER = 'ameesh.dev/turn-secret'


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise InvalidJob('credential file must be private and regular')
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise InvalidJob('credential file exceeds limit')
        return raw


def _time(value):
    if value is None:
        return None
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class KubernetesJobProvider(JobProvider):
    """HTTP implementation. ``secrets(identity)`` returns prompt/relay_token.

    Requires Kubernetes >= 1.27 (batch.kubernetes.io/controller-uid labels).
    Embedded TLS keys briefly use 0600 files in a private TMPDIR directory
    for SSLContext.load_cert_chain; the directory is removed before return.

    The caller supplies the admitted turn duration separately because JobSpec
    deliberately contains no profile limits. A provider is scoped to one Host
    namespace/image/deadline/TTL; a mismatched spec is refused before any IO.
    No kubeconfig plugin is run and no ambient KUBECONFIG/proxy is consulted.

    Secret creation precedes Job creation, so a crash can leave an unowned
    Secret when no Job exists. Retrying create reuses that immutable Secret;
    the lifecycle integrator must call delete even for a cancelled intent
    with no observed Job. delete removes the named unowned Secret only on
    that explicit cancellation path. A Job finalizer protects the other crash
    window: get finishes ownership before TTL deletion can collect the Job.
    """

    def __init__(self, host, *, turn_seconds: int,
                 secrets: Callable[[JobIdentity], Mapping[str, str]], timeout=15):
        if (not isinstance(getattr(host, 'policy', None), HostPolicy)
                or not isinstance(host.policy.sandbox, SandboxPlacement)):
            raise InvalidJob('provider requires a typed Host sandbox placement')
        self.policy = host.policy.sandbox.to_dict()
        self.turn_seconds = turn_seconds
        if (isinstance(turn_seconds, bool) or not isinstance(turn_seconds, (int, float))
                or not math.isfinite(turn_seconds) or turn_seconds <= 0):
            raise InvalidJob('turn_seconds must be positive')
        self.secrets = secrets
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise InvalidJob('timeout must be finite and positive')
        self.timeout = timeout
        try:
            path = Path(self.policy['kubeconfig'])
            if not path.is_absolute():
                raise InvalidJob('kubeconfig must be absolute')
            raw = _private(path)
            try:
                config = json.loads(raw)
            except ValueError:
                config = load_yaml(raw.decode())
            context_name = config.get('current-context')
            if not isinstance(context_name, str) or not context_name.strip():
                raise ProviderUnavailable('kubeconfig current-context is missing')
            context = self._named(config.get('contexts', []), context_name, 'context')
            cluster = self._named(config.get('clusters', []), context.get('cluster'), 'cluster')
            user = self._named(config.get('users', []), context.get('user'), 'user')
            if any(key in user for key in ('exec', 'auth-provider', 'username', 'password')):
                raise InvalidJob('unsupported kubeconfig credential mechanism')
            server = parse.urlsplit(cluster['server'])
            if (server.scheme != 'https' or not server.hostname or server.username
                    or server.password or server.query or server.fragment):
                raise InvalidJob('kubeconfig server must use HTTPS')
            if cluster.get('insecure-skip-tls-verify') or cluster.get('proxy-url'):
                raise InvalidJob('insecure TLS and proxy-url are unsupported')
            self.server = cluster['server'].rstrip('/')
            tls = ssl.create_default_context()
            ca = self._material(cluster, 'certificate-authority', path.parent)
            if ca:
                tls.load_verify_locations(cadata=ca.decode())
            cert = self._material(user, 'client-certificate', path.parent)
            key = self._material(user, 'client-key', path.parent, private=True)
            if bool(cert) != bool(key):
                raise InvalidJob('client certificate and key must be paired')
            if cert:
                with tempfile.TemporaryDirectory(prefix='ameesh-kube-tls-') as directory:
                    cert_path, key_path = Path(directory) / 'cert', Path(directory) / 'key'
                    for target, content in ((cert_path, cert), (key_path, key)):
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'wb') as output:
                            output.write(content)
                    tls.load_cert_chain(cert_path, key_path)
            token = user.get('token', '')
            if user.get('tokenFile'):
                token = _private(path.parent / user['tokenFile']).decode().strip()
            if not isinstance(token, str) or '\n' in token or '\r' in token:
                raise InvalidJob('invalid bearer credential')
            if not token and not cert:
                raise InvalidJob('kubeconfig requires a token or client certificate')
            self._token = token
            self._opener = request.build_opener(request.ProxyHandler({}),
                                                request.HTTPSHandler(context=tls), _NoRedirect())
        except (InvalidJob, ProviderUnavailable):
            raise
        except Exception:
            raise InvalidJob('invalid or unreadable kubeconfig') from None

    @staticmethod
    def _named(entries, name, key):
        found = [entry[key] for entry in entries if entry.get('name') == name]
        if len(found) != 1:
            raise ProviderUnavailable('kubeconfig current-context references do not resolve uniquely')
        return found[0]

    @staticmethod
    def _material(value, field, directory, private=False):
        if field + '-data' in value:
            return base64.b64decode(value[field + '-data'], validate=True)
        if field in value:
            path = directory / value[field]
            return _private(path) if private else path.read_bytes()
        return None

    def _call(self, method, path, body=None, *, limit=MAX_RESPONSE_BYTES, raw=False):
        headers = {'Accept': 'application/json'}
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        if body is not None:
            headers['Content-Type'] = ('application/merge-patch+json' if method == 'PATCH'
                                       else 'application/json')
        req = request.Request(self.server + path,
                              data=None if body is None else json.dumps(body).encode(),
                              headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                content = response.read(limit if raw else limit + 1)
                if not raw and len(content) > limit:
                    raise ProviderUnavailable('Kubernetes response exceeds limit')
                if raw:
                    return content
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ProviderUnavailable('invalid Kubernetes response')
                return value
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            exception = {401: ProviderForbidden, 403: ProviderForbidden,
                         404: JobNotFound, 409: JobAlreadyExists}.get(code, ProviderUnavailable)
            raise exception('Kubernetes HTTP %d' % code) from None
        except (OSError, ValueError, error.URLError):
            raise ProviderUnavailable('Kubernetes request failed') from None

    def _path(self, namespace, resource='jobs', name=None):
        if namespace != self.policy['namespace']:
            raise InvalidJob('namespace differs from Host policy')
        if name is not None and (not isinstance(name, str) or not re.fullmatch(
                r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?', name)):
            raise InvalidJob('invalid Kubernetes resource name')
        prefix = '/apis/batch/v1' if resource == 'jobs' else '/api/v1'
        path = prefix + '/namespaces/' + parse.quote(namespace, safe='') + '/' + resource
        return path if name is None else path + '/' + parse.quote(name, safe='')

    def _pods(self, value):
        meta = value['metadata']
        path = self._path(meta['namespace'], 'pods')
        selector = 'batch.kubernetes.io/controller-uid=' + meta['uid']
        result = self._call('GET', path + '?' + parse.urlencode({'labelSelector': selector}))
        return [pod for pod in result['items'] if any(
            owner.get('uid') == meta['uid'] and owner.get('kind') == 'Job'
            for owner in pod['metadata'].get('ownerReferences', []))]

    def _safe_output(self, value, content):
        """Bad workload output is terminal data, never a transport failure.

        The prompt may legitimately be quoted in a result. Only the relay
        credential is withheld. An unreadable/malformed Secret neutralizes
        output too: it must not prevent observation or cancellation of a Job.
        """
        if not content:
            return ''
        meta = value['metadata']
        try:
            secret = self._call('GET', self._path(meta['namespace'], 'secrets', meta['name']))
            token = base64.b64decode(secret['data']['relay_token'], validate=True).decode()
        except (ProviderError, ValueError, KeyError, TypeError):
            return ''
        return '' if not token or token in content else content

    def _observe(self, value):
        meta, status = value['metadata'], value.get('status', {})
        message = ''
        if phase_from_status(status) in ('Succeeded', 'Failed'):
            pods = self._pods(value)
            if len(pods) == 1:
                for container in pods[0].get('status', {}).get('containerStatuses', []):
                    if container.get('name') == 'turn':
                        message = container.get('state', {}).get('terminated', {}).get('message', '')
            if not isinstance(message, str) or len(message.encode()) > MAX_MESSAGE_BYTES:
                message = ''
        message = self._safe_output(value, message)
        return Job(namespace=meta['namespace'], name=meta['name'], uid=meta['uid'],
                   labels=meta.get('labels', {}), phase=phase_from_status(status),
                   image=value['spec']['template']['spec']['containers'][0]['image'],
                   created_at=_time(meta['creationTimestamp']), started_at=_time(status.get('startTime')),
                   completed_at=_time(status.get('completionTime')), message=message,
                   deletion_timestamp=_time(meta.get('deletionTimestamp')))

    @staticmethod
    def _identity(namespace, labels):
        try:
            return JobIdentity(labels['ameesh.dev/turn-id'], labels['ameesh.dev/agent'],
                               int(labels['ameesh.dev/epoch']), labels['ameesh.dev/placement'],
                               labels['ameesh.dev/profile'], namespace)
        except (ValueError, KeyError, TypeError):
            raise JobUIDConflict('invalid turn Secret identity') from None

    def _attach_secret(self, value):
        """Finish the create crash window before removing our Job finalizer."""
        meta = value['metadata']
        if FINALIZER not in meta.get('finalizers', []):
            return
        path = self._path(meta['namespace'], 'secrets', meta['name'])
        secret = self._call('GET', path)
        smeta = secret['metadata']
        if smeta.get('labels') != meta.get('labels'):
            raise JobUIDConflict('Secret identity differs')
        owners = smeta.get('ownerReferences', [])
        if owners and not any(owner.get('uid') == meta['uid'] for owner in owners):
            raise JobUIDConflict('Secret belongs to another Job')
        self._call('PATCH', path, {'metadata': {'uid': smeta['uid'], 'resourceVersion': smeta['resourceVersion'],
            'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': meta['name'],
                                 'uid': meta['uid'], 'controller': True}]}})
        self._call('PATCH', self._path(meta['namespace'], name=meta['name']),
                   {'metadata': {'uid': meta['uid'], 'resourceVersion': meta['resourceVersion'],
                                 'finalizers': [f for f in meta.get('finalizers', []) if f != FINALIZER]}})

    def create(self, spec):
        if not isinstance(spec, JobSpec):
            raise InvalidJob('create requires JobSpec')
        identity = spec.identity
        defaults = self.policy['job']
        if (self.policy.get('provider') != 'kubernetes' or spec.image != self.policy['image']
                or spec.active_deadline_seconds != defaults['activeDeadlineSeconds']
                or spec.ttl_seconds_after_finished != defaults['ttlSecondsAfterFinished']
                or spec.active_deadline_seconds <= self.turn_seconds
                or defaults['backoffLimit'] != 0
                or defaults['automountServiceAccountToken'] is not False):
            raise InvalidJob('JobSpec differs from admitted Host limits')
        path = self._path(identity.namespace)
        if self.get(identity.namespace, identity.name) is not None:
            raise JobAlreadyExists(name=identity.name)
        material = self.secrets(identity)
        if (set(material) != {'prompt', 'relay_token'}
                or any(not isinstance(v, str) or not v for v in material.values())):
            raise InvalidJob('turn Secret requires prompt and relay_token')
        if any(value in argument for value in material.values() for argument in spec.argv):
            raise InvalidJob('turn secret appears in argv')
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'immutable': True, 'type': 'Opaque',
                  'metadata': {'name': identity.name, 'labels': identity.labels},
                  'data': {k: base64.b64encode(v.encode()).decode() for k, v in material.items()}}
        spath = self._path(identity.namespace, 'secrets')
        try:
            self._call('POST', spath, secret)
        except JobAlreadyExists:
            if self.get(identity.namespace, identity.name) is not None:
                raise JobAlreadyExists(name=identity.name) from None
            previous = self._call('GET', spath + '/' + identity.name)
            if (previous['metadata'].get('labels') != identity.labels
                    or previous.get('data') != secret['data']
                    or previous['metadata'].get('ownerReferences')):
                raise JobUIDConflict('turn Secret conflict') from None
        container = {'name': 'turn', 'image': spec.image,
                     'securityContext': {'readOnlyRootFilesystem': True, 'runAsNonRoot': True,
                                         'allowPrivilegeEscalation': False,
                                         'capabilities': {'drop': ['ALL']}},
                     'volumeMounts': [{'name': 'turn-secret', 'mountPath': '/run/ameesh/turn',
                                       'readOnly': True}, {'name': 'scratch', 'mountPath': '/tmp'}],
                     'terminationMessagePath': '/dev/termination-log',
                     'terminationMessagePolicy': 'File'}
        if spec.argv:
            container['command'] = list(spec.argv)
        value = {'apiVersion': 'batch/v1', 'kind': 'Job',
                 'metadata': {'name': identity.name, 'labels': identity.labels,
                              'finalizers': [FINALIZER]},
                 'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': spec.active_deadline_seconds,
                          'ttlSecondsAfterFinished': spec.ttl_seconds_after_finished,
                          'template': {'metadata': {'labels': identity.labels}, 'spec': {
                              'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                              'securityContext': {'seccompProfile': {'type': 'RuntimeDefault'}},
                              'containers': [container], 'volumes': [
                                  {'name': 'turn-secret', 'secret': {'secretName': identity.name,
                                                                   'defaultMode': 0o444}},
                                  {'name': 'scratch', 'emptyDir': {}}]}}}}
        created = self._call('POST', path, value)
        self._attach_secret(created)
        return self._observe(created)

    def get(self, namespace, name):
        try:
            value = self._call('GET', self._path(namespace, name=name))
        except JobNotFound:
            return None
        self._attach_secret(value)
        return self._observe(value)

    def list(self, namespace, labels=None):
        if labels is not None and (not isinstance(labels, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str)
                or not re.fullmatch(r'[A-Za-z0-9./_-]+', k)
                or not re.fullmatch(r'[A-Za-z0-9_.-]*', v)
                for k, v in labels.items())):
            raise InvalidJob('invalid label selector')
        selector = ','.join('%s=%s' % pair for pair in sorted((labels or {}).items()))
        path = self._path(namespace) + '?' + parse.urlencode({'labelSelector': selector})
        jobs = self._call('GET', path)['items']
        return sorted((self._observe(value) for value in jobs), key=lambda job: job.name)

    def logs(self, namespace, name):
        value = self._call('GET', self._path(namespace, name=name))
        pods = self._pods(value)
        if not pods:
            return ''
        if len(pods) != 1:
            return ''
        path = self._path(namespace, 'pods', pods[0]['metadata']['name']) + '/log?'
        path += parse.urlencode({'container': 'turn', 'limitBytes': MAX_LOG_BYTES + 1})
        content = self._call('GET', path, limit=MAX_LOG_BYTES + 1, raw=True)
        if len(content) > MAX_LOG_BYTES:
            # A bounded nonsecret sentinel follows the fake-provider contract:
            # S4b refuses it as 'missing' on this step, rather than retrying.
            return ' ' * (MAX_LOG_BYTES + 1)
        try:
            text = content.decode('utf-8')
        except UnicodeError:
            return ''
        return self._safe_output(value, text)

    def delete(self, namespace, name, *, uid=None):
        path = self._path(namespace, name=name)
        try:
            value = self._call('GET', path)
        except JobNotFound:
            # Explicit cancellation only: never sweep during get/list, because
            # another create may still be completing this exact turn.
            spath = self._path(namespace, 'secrets', name)
            try:
                secret = self._call('GET', spath)
                smeta = secret['metadata']
                labels = smeta.get('labels', {})
                identity = self._identity(namespace, labels)
                if identity.name != name:
                    raise JobUIDConflict('orphan Secret identity differs')
                if smeta.get('ownerReferences') or uid is not None:
                    return False
                self._call('DELETE', spath, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                    'preconditions': {'uid': smeta['uid'],
                                      'resourceVersion': smeta['resourceVersion']}})
            except JobNotFound:
                pass
            except JobAlreadyExists:
                raise JobUIDConflict(name=name) from None
            return False
        if uid is not None and uid != value['metadata']['uid']:
            raise JobUIDConflict(name=name)
        self._attach_secret(value)
        try:
            self._call('DELETE', path, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                       'propagationPolicy': 'Background',
                       'preconditions': {'uid': uid or value['metadata']['uid']}})
        except JobNotFound:
            return False
        except JobAlreadyExists:
            raise JobUIDConflict(name=name) from None
        return True
