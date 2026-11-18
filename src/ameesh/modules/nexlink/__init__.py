# SPDX-License-Identifier: AGPL-3.0-only
"""Nexlink release module (#7663, #7637 tranche 4).

The Nexlink release domain lives outside the generic core: the cutoff engine,
its closed workflow lists and the release vocabulary are in
:mod:`ameesh.modules.nexlink.release`. The core reaches them only through the
Module interface (``registry.release_model``), the same way it reaches the
development tooling through ``registry.get("dev")``: no direct import.

``nexlink`` is always-active tooling, like ``dev``: it declares no work item
type and gets no ``project_modules`` row. The bare profile
(``AMEESH_MODULES=bare``) has no built-in module and therefore no ``nexlink``
release model.

``RELEASE_MODELS`` names the release models this module provides; the engine
is imported lazily by :func:`release_model`, so discovery does not load it.
"""
from __future__ import annotations

NAME = "nexlink"

#: Capabilities this module provides (see ``registry.CAPABILITIES``).
CAPABILITIES = ("release",)

#: Tooling is active on every host without a project selection.
ALWAYS_ACTIVE = True

#: Release model names this module provides, read by ``registry.release_model``.
RELEASE_MODELS = ("nexlink",)

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
