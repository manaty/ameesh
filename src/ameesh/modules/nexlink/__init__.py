# SPDX-License-Identifier: AGPL-3.0-only
"""Nexlink tooling module (#7663, #7793 and #7813, #7637 tranches 3b, 4 and 6).

The Nexlink domain lives outside the generic core: the cutoff engine, its
closed workflow lists and the release vocabulary are in
:mod:`ameesh.modules.nexlink.release`; the `nexlink` alert channel, its crypto
envelope and its signed transport are in :mod:`ameesh.modules.nexlink.notify`
and :mod:`ameesh.modules.nexlink.alerts`; the train's dynamic packet CI
contract is in :mod:`ameesh.modules.nexlink.packet_ci`. The core reaches them
only through the Module interface (``registry.release_model``,
``registry.notify_channel`` and ``registry.train_contract``), the same way it
reaches the development tooling through ``registry.get("dev")``: no direct
import.

``nexlink`` is always-active tooling, like ``dev``: it declares no work item
type and gets no ``project_modules`` row. The bare profile
(``AMEESH_MODULES=bare``) has no built-in module and therefore no ``nexlink``
release model, no ``nexlink`` notification channel and no ``nexlink`` train
contract.

``RELEASE_MODELS``, ``NOTIFY_CHANNELS`` and ``TRAIN_CONTRACTS`` name the
providers this module supplies; each implementation is imported lazily by its getter, so discovery
does not load it.
"""
from __future__ import annotations

NAME = "nexlink"

#: Capabilities this module provides (see ``registry.CAPABILITIES``).
CAPABILITIES = ("release",)

#: Tooling is active on every host without a project selection.
ALWAYS_ACTIVE = True

#: Release model names this module provides, read by ``registry.release_model``.
RELEASE_MODELS = ("nexlink",)

#: Notification channel kinds this module provides, read by
#: ``registry.notify_channel`` (#7793).
NOTIFY_CHANNELS = ("nexlink",)

#: Train CI contract names this module provides, read by
#: ``registry.train_contract`` (#7794).
TRAIN_CONTRACTS = ("nexlink",)

#: #7813: page schemas the Nexlink side still serves under its historical
#: names. The core reads the generic schema and accepts these aliases, read by
#: ``registry.fingerprint_schemas`` and ``registry.dispatch_plan_schemas``: the
#: Nexlink app and its repository CI planner need no change.
FINGERPRINT_SCHEMAS = ("nexlink-fingerprints/1",)
DISPATCH_PLAN_SCHEMAS = ("nexlink-dispatch-due-parts/1",)

#: #6020: notes reserved to ``ameesh release attest``. They live here, not in
#: the core: ``work.note`` reads the list through ``registry``.
ATTEST_PREFIX = "release-attest:"
SKIP_PREFIX = "release-skip:"
RESERVED_PREFIXES = (ATTEST_PREFIX, SKIP_PREFIX)


def descriptor(options=None):
    """No work item type: release tooling lives outside the project lifecycle."""
    options = dict(options or {})
    if options:
        raise ValueError("unknown nexlink module option(s): %s"
                         % ", ".join(sorted(options)))
    return {"types": [], "schemas": {}, "rules": []}


def release_model(name):
    """The provider object of one release model, imported on first use.

    The core only knows the model name from its configuration; it never names
    this package at import time (``AMEESH_MODULES=bare`` has no such module)."""
    if name not in RELEASE_MODELS:
        raise ValueError("nexlink module does not provide release model %r" % (name,))
    from . import release
    return release


def notify_channel(kind):
    """The provider object of one notification channel, imported on first use.

    Same lazy contract as :func:`release_model`: discovery reads
    ``NOTIFY_CHANNELS`` without importing the channel implementation."""
    if kind not in NOTIFY_CHANNELS:
        raise ValueError("nexlink module does not provide notification channel %r"
                         % (kind,))
    from . import notify
    return notify


def train_contract(name):
    """The factory of one train CI contract, imported on first use (#7794)."""
    if name not in TRAIN_CONTRACTS:
        raise ValueError("nexlink module does not provide train contract %r" % (name,))
    from . import packet_ci
    return packet_ci.nexlink
