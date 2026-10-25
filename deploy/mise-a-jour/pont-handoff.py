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

A local effect is dispatched to the same pinned ``HostEffects`` object without
SSH, so the host journal and the receipt shape are identical. A transport
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

from ameesh import handoff_contract

ENROLLMENT_ROOT = Path('/etc/ameesh-handoff')
TRANSPORT_NAME = 'transport-release.py'
MAX_REQUEST_BYTES = 65536


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
