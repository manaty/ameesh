# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated option spellings: accepted, hidden from --help, warned (L148).

Renaming a code identifier keeps the old spelling as an alias for at least
one release (L148a plan). Removing an option string is a breaking change for
``scripts/version-level.py`` (L158), so the alias must stay in the syntax
tree until the cleanup stage, which is bundled with the next major version.
The alias stays functional and is hidden from ``--help``; using it prints one
line on stderr naming the canonical spelling, once per spelling and per parse.
"""
from __future__ import annotations

import argparse
import sys


class HiddenAliasParser(argparse.ArgumentParser):
    """Parser whose hidden aliases never shadow a visible option (L148).

    ``--depends-on`` and its hidden alias ``--depend-de`` broke the old
    abbreviation ``--dep``: argparse saw two candidates and refused it. Here a
    prefix that matches at least one visible option resolves on the visible
    one (``--dep`` -> ``--depends-on``), and when several visible options
    remain the ambiguity message names those only: a hidden alias never shows
    up in an error suggestion.

    A prefix that matches hidden aliases only keeps the old spelling usable
    when it designates exactly one destination (``--pou`` -> ``--pour``), even
    when several hidden spellings lead there (``--livraison`` and ``--livrer``
    both store ``livraison``). When it designates several destinations,
    argparse's ambiguity message would name them (``--po could match --pour,
    --porteur``): the match is dropped instead, so no hidden alias leaks into
    a suggestion.
    """

    def _get_option_tuples(self, option_string):  # type: ignore[override]
        tuples = super()._get_option_tuples(option_string)
        visible = [t for t in tuples if t[0].help is not argparse.SUPPRESS]
        if visible:
            return visible
        # Hidden aliases only: the prefix resolves as soon as every match
        # leads to the same destination, however many spellings lead there
        # (two hidden aliases of one option, or one action with both).
        destinations = {t[0].dest for t in tuples}
        return tuples[:1] if len(destinations) == 1 else []


def _append_unique(namespace, dest: str, value) -> None:
    """Append without duplicate, keeping the order of first appearance."""
    items = getattr(namespace, dest, None)
    items = [] if items is None else list(items)
    if value not in items:
        items.append(value)
    setattr(namespace, dest, items)


class UniqueAppend(argparse.Action):  # type: ignore[type-arg]
    """``action="append"`` without duplicates, order kept (L148).

    The canonical ``--depends-on`` and its old spelling ``--depend-de``
    collect in the same list; a value given twice, in any order, is stored
    once.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        _append_unique(namespace, self.dest, values)


class DeprecatedAlias(argparse.Action):  # type: ignore[type-arg]
    """Store the value and warn on stderr, naming the canonical option.

    Give the alias its own ``add_argument`` call, with ``help`` and ``default``
    suppressed so ``--help`` and the default stay with the canonical option::

        p.add_argument("--deploiement", dest="livraison", action=DeprecatedAlias,
                       canonical="--delivery", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)

    ``append=True`` reproduces ``action="append"`` for a repeatable alias
    (``--depend-de``): the values collect in the canonical option's list,
    whatever the order, without duplicates, and the canonical argument keeps
    the ``default=[]``.

    The warning is written once per option spelling and per parse: repeating
    ``--depend-de`` no longer prints the line twice.
    """

    def __init__(self, option_strings, dest, canonical="", append=False, **kwargs):
        super().__init__(option_strings, dest, **kwargs)
        self.canonical = canonical
        self.append = append

    def __call__(self, parser, namespace, values, option_string=None):
        self._warn_once(namespace, option_string)
        if self.nargs == 0:
            setattr(namespace, self.dest, self.const if self.const is not None else True)
        elif self.append:
            _append_unique(namespace, self.dest, values)
        else:
            setattr(namespace, self.dest, values)

    def _warn_once(self, namespace, option_string) -> None:
        """One line per spelling: the same alias repeated warns once."""
        seen = getattr(namespace, "_deprecated_spellings", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_deprecated_spellings", seen)
        if option_string in seen:
            return
        seen.add(option_string)
        sys.stderr.write("%s is deprecated, use %s\n" % (option_string, self.canonical))
