#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Enrolled SSH transport of a secret frame (L232c); no CLI or remote installation.

A dedicated account, distinct from L232b's restart account, reaches one fixed
root dispatcher (`hote-secrets.py`) whose stdin is the frame of
`coffre-release.py`. The frame carries the value: it crosses SSH stdin only,
never argv, the environment or a log. An SSH failure is ambiguous: the mesh
side keeps its delivery intent and replays the same version.
"""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('ssh_secret_release',
        Path(__file__).with_name('transport-release.py'))
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

ACCOUNT = 'ameesh-secrets'
TransportError = release.TransportError


class SSHSecretTransport:
    """The `transport` of `coffre-release.Livraison`: one frame in, one receipt out.

    Endpoint, host key and private key paths come from an independent
    enrollment only; the remote account has an admin-installed forced command.
    The receipt is returned as bytes and checked by `Livraison`.
    """
    def __init__(self, enrollment):
        self.enrollment = json.loads(json.dumps(enrollment))
        release.validate_enrollment(self.enrollment, ACCOUNT)

    def exchange(self, payload, timeout):
        try:
            # Revalidated per frame: a changed host key or key file refuses
            # before any connection.
            release.validate_enrollment(self.enrollment, ACCOUNT)
            if not isinstance(payload, (bytes, bytearray)) or not payload:
                raise TransportError('invalid secret frame')
            seconds = min(self.enrollment['seconds'], max(5, int(timeout)))
            return release.exchange(release.ssh_args(self.enrollment), bytes(payload), seconds)
        except TransportError:
            raise
        except Exception:
            raise TransportError('SSH action unavailable') from None
