#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Enrolled SSH restart transport; no CLI, enrollment or remote installation.

An SSH failure is ambiguous. The controller retains its intent and retries the
same action id; only the host helper can reconcile its durable effect receipt.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

_spec = importlib.util.spec_from_file_location('ssh_release_protocol',
        Path(__file__).with_name('controleur-release.py'))
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)


class TransportError(ValueError):
    pass


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise TransportError('ambiguous SSH receipt')
        value[key] = item
    return value


def protected_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise TransportError('absolute enrolled file required')
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022):
            raise TransportError('untrusted transport file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise TransportError('private transport file required')
        data = stream.read(65537)
    if not data or len(data) > 65536:
        raise TransportError('transport file size refused')
    return data


def exchange(args, payload, seconds):
    """Bound memory and wall time even when the peer streams or never exits."""
    process = None
    try:
        process = subprocess.Popen(args, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        deadline = time.monotonic() + seconds
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            sent, output = 0, bytearray()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError('SSH action deadline exceeded')
                for key, _ in selector.select(remaining):
                    if key.data == 'input':
                        sent += os.write(process.stdin.fileno(), payload[sent:])
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        part = os.read(process.stdout.fileno(), 4096)
                        if not part:
                            selector.unregister(process.stdout)
                        output.extend(part)
                        if len(output) > 65536:
                            raise TransportError('SSH receipt size exceeded')
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise TransportError('SSH action unavailable')
            return bytes(output)
    except TransportError:
        raise
    except Exception:
        raise TransportError('SSH action unavailable') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


class SSHRestartTransport:
    """Endpoint and private key paths come from independent enrollment only.

    The dedicated remote account must have an admin-installed forced command
    invoking the fixed root dispatcher. No remote command is supplied here.
    This class does not implement other release steps or fence observation.
    """
    def __init__(self, enrollment):
        self.enrollment = json.loads(json.dumps(enrollment))
        self._validate()

    def _validate(self):
        value = self.enrollment
        if (not isinstance(value, dict) or set(value) != {'host', 'address',
                'port', 'user', 'host_key_alias', 'known_hosts',
                'known_hosts_sha256', 'identity_file', 'seconds'}):
            raise TransportError('invalid SSH enrollment')
        for name in ('host', 'address', 'host_key_alias'):
            if (not isinstance(value[name], str) or not re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value[name])):
                raise TransportError('invalid SSH endpoint')
        if (value['user'] != 'ameesh-release' or type(value['port']) is not int
                or not 1 <= value['port'] <= 65535
                or type(value['seconds']) is not int or not 5 <= value['seconds'] <= 360
                or not isinstance(value['known_hosts_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['known_hosts_sha256'])):
            raise TransportError('invalid SSH transport policy')
        for name in ('known_hosts', 'identity_file'):
            if (not isinstance(value[name], str) or not value[name].startswith('/')
                    or not re.fullmatch(r'/[A-Za-z0-9/_.~-]+', value[name])
                    or '..' in Path(value[name]).parts):
                raise TransportError('invalid enrolled SSH path')
        if hashlib.sha256(protected_file(value['known_hosts'])).hexdigest() != value['known_hosts_sha256']:
            raise TransportError('enrolled host key changed')
        # Check permissions/size; never retain, log or forward the key bytes.
        protected_file(value['identity_file'])

    def perform(self, step, request):
        try:
            self._validate()
            if (not isinstance(request, dict) or set(request) != {'operation',
                    'manifest', 'manifest_digest', 'action_id', 'step'}
                    or step not in ('restart', 'recovery_restart') or request['step'] != step
                    or not isinstance(request['operation'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', request['operation'])
                    or request['action_id'] != request['operation'] + ':' + step):
                raise TransportError('invalid SSH action')
            protocol.validate(request['manifest'])
            if (request['manifest']['host'] != self.enrollment['host']
                    or request['manifest_digest'] != protocol.identity(request['manifest'])):
                raise TransportError('SSH action scope mismatch')
            value = self.enrollment
            args = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
                    '-o', 'GlobalKnownHostsFile=/dev/null',
                    '-o', 'UserKnownHostsFile=' + value['known_hosts'],
                    '-o', 'HostKeyAlias=' + value['host_key_alias'],
                    '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                    '-o', 'CertificateFile=none', '-o', 'PreferredAuthentications=publickey',
                    '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                    '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no',
                    '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
                    '-o', 'ControlPath=none', '-o', 'ProxyCommand=none',
                    '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
                    '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
                    '-i', value['identity_file'], '-p', str(value['port']),
                    '-l', value['user'], value['address']]
            wire = {key: request[key] for key in ('operation', 'action_id', 'step', 'manifest_digest')}
            output = exchange(args, json.dumps(wire, sort_keys=True).encode(), value['seconds'])
            receipt = json.loads(output, object_pairs_hook=unique_object)
            expected = dict(request, ok=True)
            if (not isinstance(receipt, dict) or set(receipt) != set(expected)
                    or type(receipt.get('ok')) is not bool
                    or protocol.identity(receipt) != protocol.identity(dict(request, ok=receipt['ok']))):
                raise TransportError('SSH action receipt mismatch')
            return receipt
        except TransportError:
            raise
        except Exception:
            raise TransportError('SSH action unavailable') from None
