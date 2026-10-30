#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Delete a verified immutable Scaleway object VERSION, never an unqualified key.

No identity is enrolled or remote operation executed merely by importing this
module. The controller must first verify version-specific retrieval/restore and
pin that exact version in its recovery catalog. null versions are not immutable.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import tempfile
from urllib.parse import quote
import xml.etree.ElementTree as ET


class StorageError(ValueError):
    pass


# Signé sous ce nom par SigV4. curl 7.88 (Debian 12) ne l'ajoute pas seul, et
# Scaleway refuse alors la requête ; ces GET et DELETE n'ont jamais de corps.
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise StorageError('invalid storage reference')
    return value


def version(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or value.lower() == 'null' or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise StorageError('immutable non-null version required')
    return value


def private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 16384):
            raise StorageError('private owned retention credentials required')
        return json.load(stream)


def check_curl():
    """`--aws-sigv4` needs curl 7.75; the response size is bounded below.

    Debian 12 ships curl 7.88. curl only aborts an oversize transfer on its own
    (--max-filesize) when the length is known, and that for 8.4+; the child
    therefore carries its own kernel RLIMIT_FSIZE, which bounds the response on
    disk whatever the provider sends.
    """
    try:
        result = subprocess.run(['/usr/bin/curl', '--version'],
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=True, text=True, timeout=10)
        match = re.match(r'curl (\d+)\.(\d+)\.(\d+)', result.stdout)
        if not match or tuple(map(int, match.groups())) < (7, 75, 0):
            raise StorageError('curl 7.75 or newer required for signed transfers')
    except (OSError, subprocess.SubprocessError):
        raise StorageError('retention transport unavailable') from None


def response_version(path):
    # curl may receive informational/proxy responses. Only the FINAL block
    # attests the downloaded object, and duplicate version fields are refused.
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise StorageError('oversized storage headers')
    blocks = re.split(rb'\r?\n\r?\n', data)
    block = next((part for part in reversed(blocks) if part.startswith(b'HTTP/')), b'')
    values = []
    for line in block.splitlines()[1:]:
        key, sep, value = line.partition(b':')
        if sep and key.lower() in (b'x-amz-version-id', b'versionid'):
            values.append(value.strip().decode('ascii'))
    if len(values) != 1:
        raise StorageError('storage did not attest exact version')
    return values[0]


class VersionRetention:
    def __init__(self, credentials, staging_root, *, max_bytes):
        check_curl()
        settings = private_json(credentials)
        if (set(settings) != {'access_key', 'secret_key', 'bucket', 'region', 'purpose', 'allowed_hosts'}
                or settings['purpose'] != 'retention-versions'):
            raise StorageError('separate version-retention identity required')
        for key in ('access_key', 'secret_key'):
            if (not isinstance(settings[key], str) or not settings[key]
                    or any(c in settings[key] for c in '\r\n\x00')):
                raise StorageError('invalid retention credential')
        for key in ('bucket', 'region'):
            if not isinstance(settings[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', settings[key]):
                raise StorageError('invalid storage origin')
        hosts = settings['allowed_hosts']
        if not isinstance(hosts, list) or not hosts:
            raise StorageError('enrolled host allowlist required')
        self.hosts = {identifier(host) for host in hosts}
        if type(max_bytes) is not int or max_bytes <= 0:
            raise StorageError('explicit staging size budget required')
        self.max_bytes = max_bytes
        self.settings = settings
        self.staging = Path(staging_root).absolute()
        if self.staging.resolve() != self.staging:
            raise StorageError('linked storage staging path')
        self.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.staging.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StorageError('private owned on-disk staging required')

    def location(self, object_id, version_id):
        if not isinstance(object_id, str) or object_id.count('/') != 1:
            raise StorageError('invalid object reference')
        host, name = object_id.split('/')
        identifier(host)
        identifier(name)
        if host not in self.hosts:
            raise StorageError('host outside enrolled retention scope')
        version(version_id)
        return ('https://' + self.settings['bucket'] + '.s3.' + self.settings['region']
                + '.scw.cloud/' + host + '/' + name + '?versionId=' + quote(version_id, safe=''))

    def request(self, method, object_id, version_id):
        url = self.location(object_id, version_id)
        # An enrolled staging volume and admission budget are required. This
        # check is not a concurrent disk reservation; the controller owns that.
        capacity = os.statvfs(self.staging)
        if capacity.f_bavail * capacity.f_frsize < self.max_bytes + 65536:
            raise StorageError('insufficient storage staging capacity')
        with tempfile.TemporaryDirectory(prefix='retention-scw-', dir=self.staging) as directory:
            root = Path(directory)
            config, body, headers = (root / name for name in ('curl.conf', 'body', 'headers'))
            for path in (config, body, headers):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            config.write_text('user = ' + json.dumps(self.settings['access_key'] + ':' + self.settings['secret_key']) + '\n')
            args = ['/usr/bin/curl', '--disable', '--config', str(config), '--silent',
                    '--proto', '=https', '--connect-timeout', '10', '--max-time', '120',
                    '--max-filesize', str(self.max_bytes),
                    '-H', 'x-amz-content-sha256: ' + EMPTY_SHA256,
                    '--aws-sigv4',
                    'aws:amz:' + self.settings['region'] + ':s3', '--request', method,
                    '--dump-header', str(headers), '--output', str(body),
                    '--write-out', '%{http_code}', url]
            # The kernel bound is what makes an unknown-length transfer safe on
            # curl < 8.4: no file the child writes can pass this size.
            limit = self.max_bytes + 65537
            def bound_child():
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
            try:
                result = subprocess.run(args, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                                        preexec_fn=bound_child,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        check=True, text=True, timeout=130)
            except (OSError, subprocess.SubprocessError):
                raise StorageError('storage request failed') from None
            if body.stat().st_size > self.max_bytes or headers.stat().st_size > 65536:
                raise StorageError('storage response exceeds staging budget')
            status = result.stdout.strip()
            if status == '404':
                try:
                    # NoSuchBucket, proxy errors and arbitrary 404 pages are not
                    # proof that this particular immutable version is absent.
                    with body.open('rb') as stream:
                        payload = stream.read(65537)
                    if len(payload) > 65536:
                        raise StorageError('unverified storage absence')
                    document = ET.fromstring(payload)
                    codes = [child.text for child in document if child.tag.split('}')[-1] == 'Code']
                    code = codes[0] if len(codes) == 1 and document.tag.split('}')[-1] == 'Error' else None
                except ET.ParseError:
                    raise StorageError('unverified storage absence') from None
                if code not in ('NoSuchVersion', 'NoSuchKey'):
                    raise StorageError('unverified storage absence')
                return None
            if method == 'DELETE':
                if status not in ('200', '204'):
                    raise StorageError('storage deletion failed')
                return True
            if status != '200' or response_version(headers) != version_id or body.stat().st_size == 0:
                raise StorageError('storage version retrieval failed')
            sha = hashlib.sha256()
            with body.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    sha.update(chunk)
            return sha.hexdigest()

    def inspect(self, object_id, *, version_id=None):
        return self.request('GET', object_id, version_id)

    def delete_if_digest(self, object_id, sha256, *, version_id=None):
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise StorageError('pinned storage digest required')
        actual = self.inspect(object_id, version_id=version_id)
        if actual is not None:
            if actual != sha256:
                raise StorageError('immutable version digest mismatch')
            self.request('DELETE', object_id, version_id)
            if self.inspect(object_id, version_id=version_id) is not None:
                raise StorageError('version deletion not confirmed')
        # Caller accepts an already absent version only with a durable catalog
        # deletion intent. Provider version immutability protects GET→DELETE;
        # a newer version at the same key is never addressed by these requests.
        return {'object_id': object_id, 'sha256': sha256, 'version_id': version_id, 'absent': True}
