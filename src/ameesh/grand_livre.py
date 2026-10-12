# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.ledger` (L148), deleted in stage 7.

The module was renamed ``grand_livre`` -> ``ledger``; importers keep working
unchanged until the cleanup stage. New code imports ``ameesh.ledger``.
"""
from .ledger import *  # noqa: F401,F403
from .ledger import __all__  # noqa: F401
