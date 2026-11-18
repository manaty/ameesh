# SPDX-License-Identifier: AGPL-3.0-only
"""Public contract surfaces for an installed external module (contract v1).

The names below are the versioned boundary described in
``docs/api/module-contract-v1.md``.  Internal modules of this package are not
part of it, even when they are importable: only the surfaces listed there and
exported here carry the compatibility promise.

Contract 1.2 adds :mod:`ameesh.public.mail` (tranche E2a, #7636/#5827) and,
with #7706, reserves the core's sender identities and confines module data to
the imposed ``payload["module"]`` namespace. The mail surface stays
source-compatible for a caller that already used that namespace.
"""
from __future__ import annotations

#: revision of the written contract served by this package
CONTRACT_VERSION = "1.2"

from . import mail  # noqa: E402  (the version is defined before the surface)

__all__ = ["CONTRACT_VERSION", "mail"]
