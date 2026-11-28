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
Contract 1.6 adds :mod:`ameesh.public.database` and its ``connect()`` re-export:
the production connection for the same public surfaces, opened from the usual
configuration with an immediate schema check (#6573); it is additive to 1.5.
Contract 1.7 adds the fixture's agent qualification states
(``Fixture.agent``/``Fixture.agent_state``, #8341): a module test puts an agent
in a lease, canon, placement, spend or budget state without writing a core
table; it is additive to 1.6. Contract 1.8 adds :mod:`ameesh.public.connectors`
(#8342): an installed module declares its action connectors under
``ACTION_CONNECTORS`` and imports ``Action``, ``Outcome`` and ``NotSentError``
from the public surface instead of ``ameesh.connectors``; it is additive to
1.7 and changes no connector protocol behavior. Contract 1.9 adds the CLI
``ameesh accounts profiles --json`` (#8337), the redacted read of the declared
account profiles; it is additive to 1.8 and adds no Python name. Contract 1.10
adds the machine-readable migration dry-run report `ameesh migrate --dry-run
--json` (#8339, manaty/ameesh#6579), schema ``ameesh-migrate-dry-run/1``; it is
additive to 1.9 and changes no migration application path. Contract 1.11 adds
the bounded, read-only fleet view of the account reservations still counted
(``ameesh reservations --json`` and ``GET /api/v1/reservations``, #8343); it is
additive to 1.10 and adds no Python name. Contract 1.12 adds no Python name: it
makes the governed canon-refusal write public as the CLI ``ameesh canon
refuse`` (#8340), so an installed module no longer imports ``ameesh.canon_sync``;
it is additive to 1.11 and changes no other state.
"""
from __future__ import annotations

#: revision of the written contract served by this package
CONTRACT_VERSION = "1.12"

from . import connectors, database, fixture, mail  # noqa: E402  (the version is defined before the surface)
from .database import connect  # noqa: E402

__all__ = ["CONTRACT_VERSION", "connect", "connectors", "database", "fixture", "mail"]
