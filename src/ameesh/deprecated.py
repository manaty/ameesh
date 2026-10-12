# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated option spellings: accepted, hidden from --help, warned (L148).

Renaming a code identifier keeps the old spelling as an alias for at least
one release (L148a plan). Removing an option string is a breaking change for
``scripts/version-level.py`` (L158), so the alias must stay in the syntax
tree until the cleanup stage, which is bundled with the next major version.
The alias stays functional and is hidden from ``--help``; using it prints
one line on stderr naming the canonical spelling.
"""
from __future__ import annotations

import argparse
import sys


class DeprecatedAlias(argparse.Action):  # type: ignore[type-arg]
    """Store the value and warn on stderr, naming the canonical option.

    Give the alias its own ``add_argument`` call, with ``help`` and ``default``
    suppressed so ``--help`` and the default stay with the canonical option::

        p.add_argument("--deploiement", dest="livraison", action=DeprecatedAlias,
                       canonical="--delivery", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)

    ``append=True`` reproduces ``action="append"`` for a repeatable alias
    (``--depend-de``): the values collect in the canonical option's list,
    whatever the order, and the canonical argument keeps the ``default=[]``.
    """

    def __init__(self, option_strings, dest, canonical="", append=False, **kwargs):
        super().__init__(option_strings, dest, **kwargs)
        self.canonical = canonical
        self.append = append

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stderr.write("%s is deprecated, use %s\n" % (option_string, self.canonical))
        if self.nargs == 0:
            setattr(namespace, self.dest, self.const if self.const is not None else True)
        elif self.append:
            items = getattr(namespace, self.dest, None)
            items = [] if items is None else list(items)
            items.append(values)
            setattr(namespace, self.dest, items)
        else:
            setattr(namespace, self.dest, values)
