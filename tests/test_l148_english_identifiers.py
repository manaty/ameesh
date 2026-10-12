# SPDX-License-Identifier: AGPL-3.0-only
"""L148 : les anciennes graphies françaises des options restent acceptées.

Stage 2a of the L148a plan (`docs/design/l148-plan.md`, §4) makes the English
option the visible one and keeps the French spelling as a hidden deprecated
alias: same destination, one line on stderr, absent from `--help`. These
tests run without a database: they build the parsers and read the namespace.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from ameesh import plan_cli, roadmap
from ameesh.deprecated import DeprecatedAlias


def work_parser() -> argparse.ArgumentParser:
    """The `ameesh work` parser, without the rest of the mesh CLI."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    plan_cli.add_parsers(sub, lambda **kwargs: 0)
    return parser


def help_text(parser: argparse.ArgumentParser, *argv: str) -> str:
    """The `--help` text of a subcommand, captured."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        parser.parse_args([*argv, "--help"])
    return out.getvalue()


def parse(parser: argparse.ArgumentParser, *argv: str) -> tuple[argparse.Namespace, str]:
    """The namespace and what the deprecated spellings wrote on stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        namespace = parser.parse_args(list(argv))
    return namespace, err.getvalue()


class DeprecatedAliasTest(unittest.TestCase):

    def test_store_warns_and_names_the_canonical_option(self):
        parser = argparse.ArgumentParser(prog="t")
        parser.add_argument("--new", dest="value", default=None)
        parser.add_argument("--old", dest="value", action=DeprecatedAlias,
                            canonical="--new", default=argparse.SUPPRESS,
                            help=argparse.SUPPRESS)
        namespace, err = parse(parser, "--old", "x")
        self.assertEqual(namespace.value, "x")
        self.assertEqual(err, "--old is deprecated, use --new\n")
        self.assertNotIn("--old", parser.format_help())

    def test_append_collects_in_the_canonical_list(self):
        parser = argparse.ArgumentParser(prog="t")
        parser.add_argument("--new", dest="items", action="append", default=[])
        parser.add_argument("--old", dest="items", action=DeprecatedAlias,
                            canonical="--new", append=True, default=argparse.SUPPRESS,
                            help=argparse.SUPPRESS)
        self.assertEqual(parse(parser, "--old", "a", "--old", "b")[0].items, ["a", "b"])
        self.assertEqual(parse(parser, "--new", "a", "--old", "b")[0].items, ["a", "b"])
        self.assertEqual(parse(parser, "--old", "a", "--new", "b")[0].items, ["a", "b"])


class RoadmapAliasesTest(unittest.TestCase):

    #: English spelling → French spelling, for `ameesh plan add`
    ADD = (("--due", "--pour"), ("--project", "--projet"), ("--owner", "--porteur"),
           ("--card", "--fiche"), ("--depends-on", "--depend-de"))

    def setUp(self):
        self.parser = roadmap.build_parser()

    def test_french_spellings_keep_working(self):
        namespace, err = parse(
            self.parser, "add", "x", "--pour", "2026-10-20", "--projet", "p",
            "--porteur", "o", "--fiche", "F", "--depend-de", "L1", "--depend-de", "L2")
        self.assertEqual((namespace.due, namespace.project, namespace.owner,
                          namespace.package, namespace.depends_on),
                         ("2026-10-20", "p", "o", "F", ["L1", "L2"]))
        self.assertEqual(len(err.splitlines()), 6)  # one line per deprecated spelling

    def test_english_spellings_are_silent(self):
        namespace, err = parse(
            self.parser, "add", "x", "--due", "2026-10-20", "--project", "p",
            "--owner", "o", "--card", "F", "--depends-on", "L1")
        self.assertEqual((namespace.due, namespace.project, namespace.owner,
                          namespace.package, namespace.depends_on),
                         ("2026-10-20", "p", "o", "F", ["L1"]))
        self.assertEqual(err, "")

    def test_help_shows_english_and_hides_french(self):
        text = help_text(self.parser, "add")
        for english, french in self.ADD:
            self.assertIn(english, text)
            self.assertNotIn(french, text)
        accept = help_text(self.parser, "accept")
        self.assertIn("--due", accept)
        self.assertNotIn("--pour", accept)

    def test_accept_keeps_the_french_spelling(self):
        namespace, err = parse(self.parser, "accept", "7", "--pour", "2026-10-30")
        self.assertEqual(namespace.due, "2026-10-30")
        self.assertIn("--pour is deprecated, use --due", err)


class WorkPlanAliasesTest(unittest.TestCase):

    #: English spelling → French spelling, for `ameesh work plan`
    PLAN = (("--start", "--debut"), ("--end", "--fin"), ("--delivery", "--livraison"),
            ("--delivery", "--deploiement"))

    def setUp(self):
        self.parser = work_parser()

    def test_french_spellings_keep_working(self):
        namespace, err = parse(self.parser, "plan", "12", "--debut", "2026-10-10",
                               "--fin", "2026-10-11", "--livraison", "2026-10-12")
        self.assertEqual((namespace.debut, namespace.fin, namespace.livraison),
                         ("2026-10-10", "2026-10-11", "2026-10-12"))
        self.assertEqual(len(err.splitlines()), 3)

    def test_deprecated_deployment_alias_still_names_delivery(self):
        namespace, err = parse(self.parser, "plan", "12", "--deploiement", "2026-10-12")
        self.assertEqual(namespace.livraison, "2026-10-12")
        self.assertIn("--deploiement is deprecated, use --delivery", err)

    def test_help_shows_english_and_hides_french(self):
        text = help_text(self.parser, "plan")
        for english, french in self.PLAN:
            self.assertIn(english, text)
            self.assertNotIn(french, text)


if __name__ == "__main__":
    unittest.main()
