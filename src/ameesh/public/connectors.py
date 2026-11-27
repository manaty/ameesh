# SPDX-License-Identifier: AGPL-3.0-only
"""Public action-connector surface, contract v1.8 (#8342 / #7628 / #5818).

An installed external module declares the action connectors it supplies as a
module-level mapping under :data:`ACTION_CONNECTORS`; the core registry reads
it on discovery and the governed action gate is the only caller.  This module
re-exports the exact protocol objects the core uses, so a connector module
imports neither ``ameesh.connectors`` nor another internal path:

* :class:`Action` — the frozen identity of one action, as handed to
  ``execute`` and ``reconcile``;
* :class:`Outcome` — the closed issue of an attempt or a reconciliation
  (:data:`OUTCOMES`), built through ``Outcome.confirmed``, ``.failed`` or
  ``.unknown``;
* :class:`NotSentError` — the one certain pre-send failure a connector may
  raise; the gate settles it ``failed`` (``not sent``) and admits a retry under
  the same ``action_id``.

The connector object itself is duck-typed: ``name``, ``dedupe`` (one of
:data:`DEDUPE`), optional positive ``timeout`` seconds, and callable
``classify(operation, args)``, ``execute(action, idempotency_key)`` and
``reconcile(action)``.  :data:`ACTION_CLASSES` is the closed vocabulary of
operation classes returned by ``classify``; the class declared in the trusted
canon is authoritative.

The written boundary is ``docs/api/module-contract-v1.md``.  This surface does
not grant activation: declaring a connector is code, and the trusted canon's
``action_classes`` policy is what authorizes an operation in a project.  The
gate remains the only writer of the action row, approval, reservation and
ledger; a connector never writes core tables or invokes itself outside it.
"""
from __future__ import annotations

from ..connectors import (ACTION_CLASSES, DEFAULT_TIMEOUT, DEDUPE, OUTCOMES,
                          Action, NotSentError, Outcome)

#: revision of the written contract served by this module
CONTRACT_VERSION = "1.8"

#: module attribute naming the ``{connector name: constructor}`` declaration
ACTION_CONNECTORS = "ACTION_CONNECTORS"

__all__ = [
    "CONTRACT_VERSION", "ACTION_CONNECTORS", "OUTCOMES", "ACTION_CLASSES",
    "DEDUPE", "DEFAULT_TIMEOUT", "Action", "Outcome", "NotSentError",
]
