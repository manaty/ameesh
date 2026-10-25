#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H2: the explicit bridge from the handoff companion to one admitted host.

This module owns the channel, not the effect authority. It validates the
controller's private ``transports.json`` endpoint again, reuses only the
hardened SSH substrate of ``transport-release.py`` (fixed client options,
bounded pipe exchange, duplicate-key refusal, private key/known-hosts files)
and sends one typed handoff request to that host's pinned
``hote-handoff.py`` executor. The release controller, its operations, its
database and its restart authority are never loaded.

A local effect crosses the fixed H8 Unix socket to the root-pinned
``HostEffects`` executor; the runner never acquires its privileges. A transport
failure is ambiguous and raises: the controller keeps its durable intent and
the next pass reconciles the same effect instead of repeating it blind.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import socket
import struct

from ameesh import handoff_contract

ENROLLMENT_ROOT = Path('/etc/ameesh-handoff')
TRANSPORT_NAME = 'transport-release.py'
MAX_REQUEST_BYTES = 65536
LOCAL_SOCKET = "/run/ameesh-handoff/local.sock"
LOCAL_SECONDS = 90  # includes seal lock + full measurement + request + systemd stop


class BridgeError(ValueError):
    """The bridge is absent, closed or did not return an exact typed receipt."""


def _trusted_file(path):
    """A fixed pinned source next to this bridge, never linked or writable."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise BridgeError('source') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022):
        raise BridgeError('source')
    return path


class LocalDispatcher:
    """Kernel-authenticated fixed Unix endpoint; no local in-process effects."""

    def inventory(self, request):
        return self._exchange(request)

    def perform(self, request):
        return self._exchange(request)

    def _exchange(self, request):
        payload = json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise BridgeError('request')
        try:
            # The path and its parent must belong to root, never the runner.
            for path in (Path('/run'), Path(LOCAL_SOCKET).parent):
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                        or info.st_mode & 0o022):
                    raise BridgeError('local')
            info = Path(LOCAL_SOCKET).lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise BridgeError('local')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(LOCAL_SECONDS)
                channel.connect(LOCAL_SOCKET)
                _, uid, _ = struct.unpack('3i', channel.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid != 0:
                    raise BridgeError('local_peer')
                channel.sendall(payload)
                channel.shutdown(socket.SHUT_WR)
                data = bytearray()
                while True:
                    chunk = channel.recv(min(8192, MAX_REQUEST_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_REQUEST_BYTES:
                        raise BridgeError('receipt')
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise BridgeError('receipt')
                        result[key] = value
                    return result
                receipt = json.loads(data, object_pairs_hook=unique)
                if not isinstance(receipt, dict):
                    raise BridgeError('receipt')
                return receipt
        except BridgeError:
            raise
        except Exception:
            # A broken channel can follow a completed effect: never retry here.
            raise BridgeError('local') from None


class Bridge:
    """One controller's typed channel to an admitted host's pinned executor."""

    def __init__(self, *, local_host, transports, local=None, transport=None,
                 enrollment_root=ENROLLMENT_ROOT):
        self.local_host = local_host
        self.transports = dict(transports or {})
        self.local = local
        self.enrollment_root = Path(enrollment_root)
        self._transport = transport

    def inventory(self, host, tools, secrets):
        try:
            request = handoff_contract.validate_inventory_request({
                'step': 'inventory', 'host': host,
                'tools': sorted(tools), 'secrets': sorted(secrets)})
        except handoff_contract.ContractError:
            raise BridgeError('request') from None
        receipt = self._dispatch(host, request)
        try:
            return handoff_contract.validate_inventory_receipt(receipt, request)
        except handoff_contract.ContractError:
            raise BridgeError('receipt') from None

    def perform(self, host, request):
        try:
            request = handoff_contract.validate_effect_request(request)
        except handoff_contract.ContractError:
            raise BridgeError('request') from None
        if request['host'] != host:
            raise BridgeError('host')
        receipt = self._dispatch(host, request)
        try:
            return handoff_contract.validate_effect_receipt(receipt, request)
        except handoff_contract.ContractError:
            raise BridgeError('receipt') from None

    def _dispatch(self, host, request):
        if host == self.local_host:
            if self.local is None:
                raise BridgeError('local')
            if request['step'] == 'inventory':
                return self.local.inventory(request)
            return self.local.perform(request)
        return self._remote(host, request)

    def _remote(self, host, request):
        endpoint = self.transports.get(host)
        if endpoint is None:
            raise BridgeError('transport')
        module = self._module()
        try:
            endpoint = handoff_contract.validate_transport(
                endpoint, host, root=self.enrollment_root)
            self._files(module, endpoint)
            payload = json.dumps(request, sort_keys=True,
                                 separators=(',', ':')).encode()
            if len(payload) > MAX_REQUEST_BYTES:
                raise BridgeError('request')
            output = module.exchange(module.ssh_args(endpoint), payload,
                                     endpoint['seconds'])
            receipt = json.loads(output, object_pairs_hook=module.unique_object)
        except BridgeError:
            raise
        except Exception:
            raise BridgeError('transport') from None
        if not isinstance(receipt, dict):
            raise BridgeError('receipt')
        return receipt

    @staticmethod
    def _files(module, endpoint):
        """Re-read the exact private files; the host key digest must match."""
        known = module.protected_file(endpoint['known_hosts'])
        if hashlib.sha256(known).hexdigest() != endpoint['known_hosts_sha256']:
            raise BridgeError('host_key')
        module.protected_file(endpoint['identity_file'])

    def _module(self):
        if self._transport is not None:
            return self._transport
        path = _trusted_file(Path(__file__).absolute().parent / TRANSPORT_NAME)
        try:
            spec = importlib.util.spec_from_file_location(
                'ameesh_handoff_transport', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except BridgeError:
            raise
        except Exception:
            raise BridgeError('source') from None
        self._transport = module
        return module
