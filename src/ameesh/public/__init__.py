# SPDX-License-Identifier: AGPL-3.0-only
"""Public contract surfaces for an installed external module (contract v1).

The names below are the versioned boundary described in
``docs/api/module-contract-v1.md``.  Internal modules of this package are not
part of it, even when they are importable: only the surfaces listed there and
exported here carry the compatibility promise.

Contract 1.2 adds :mod:`ameesh.public.mail` (tranche E2a, #7636/#5827) and,
with #7706, reserves the core's sender identities and confines module data to
the imposed ``payload["module"]`` namespace. Contract 1.3 adds its lease-fenced
pending requests and correlated replies (tranche E2b, #7672); it is additive
to 1.2, and the mail surface stays source-compatible for a caller that already
used that namespace. Contract 1.4 adds no name: the core itself puts back in
the queue the requests of a turn it interrupted, under the captured lease
(tranche E2c, #7673). Contract 1.5 adds :mod:`ameesh.public.fixture`, the
consumer-owned disposable-database witness used by an installed module to
qualify its own Process, mail and observation paths (tranche F2, #7701).
"""
from __future__ import annotations

#: revision of the written contract served by this package
CONTRACT_VERSION = "1.5"

from . import fixture, mail  # noqa: E402  (the version is defined before the surface)

__all__ = ["CONTRACT_VERSION", "fixture", "mail"]
