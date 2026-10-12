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
from ameesh.deprecated import DeprecatedAlias, HiddenAliasParser


def work_parser() -> argparse.ArgumentParser:
    """The `ameesh work` parser, without the rest of the mesh CLI."""
    parser = argparse.ArgumentParser()
    # même classe de sous-parseurs que le CLI (mesh_cli) : sans elle, les
    # alias cachés capteraient les abréviations testées plus bas
    sub = parser.add_subparsers(dest="command", parser_class=HiddenAliasParser)
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

    def test_hidden_prefix_resolves_two_spellings_of_one_destination(self):
        """P3 : deux alias cachés vers la même destination se résolvent."""
        parser = HiddenAliasParser(prog="t")
        parser.add_argument("--delivery", dest="livraison", default=None)
        for alias in ("--livraison", "--livrer"):
            parser.add_argument(alias, dest="livraison", action=DeprecatedAlias,
                                canonical="--delivery", default=argparse.SUPPRESS,
                                help=argparse.SUPPRESS)
        for argv in (("--liv", "X"), ("--liv=X",)):
            with self.subTest(argv=argv):
                namespace, err = parse(parser, *argv)
                self.assertEqual(namespace.livraison, "X")
                self.assertIn("is deprecated, use --delivery", err)

    def test_hidden_prefix_resolves_one_action_with_two_spellings(self):
        """P3 : les deux graphies d'une même action se résolvent aussi."""
        parser = HiddenAliasParser(prog="t")
        parser.add_argument("--delivery", dest="livraison", default=None)
        parser.add_argument("--livraison", "--livrer", dest="livraison",
                            action=DeprecatedAlias, canonical="--delivery",
                            default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        for argv in (("--liv", "X"), ("--liv=X",)):
            with self.subTest(argv=argv):
                namespace, err = parse(parser, *argv)
                self.assertEqual(namespace.livraison, "X")
                self.assertIn("is deprecated, use --delivery", err)

    def test_hidden_prefix_to_two_destinations_is_dropped(self):
        """Deux destinations distinctes : aucune graphie cachée n'est résolue."""
        parser = HiddenAliasParser(prog="t")
        parser.add_argument("--due", dest="due", default=None)
        parser.add_argument("--owner", dest="owner", default=None)
        for alias, dest in (("--pour", "due"), ("--porteur", "owner")):
            parser.add_argument(alias, dest=dest, action=DeprecatedAlias,
                                canonical="--due" if dest == "due" else "--owner",
                                default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as case:
            parser.parse_args(["--po", "X"])
        self.assertEqual(case.exception.code, 2)

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
        # une ligne par graphie : --depend-de répété n'en écrit qu'une
        self.assertEqual(len(err.splitlines()), 5)

    def test_old_prefixes_resolve_on_the_visible_option(self):
        """P2 : `--dep` reste l'abréviation de `--depends-on`."""
        namespace, err = parse(self.parser, "add", "x", "--dep", "L1")
        self.assertEqual(namespace.depends_on, ["L1"])
        self.assertEqual(err, "")

    def test_ambiguous_prefix_does_not_name_a_hidden_alias(self):
        """P3 : le message d'ambiguïté ne cite que les options visibles."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.parser.parse_args(["add", "x", "--de", "L1"])
        self.assertIn("--decision", err.getvalue())
        self.assertNotIn("--depend-de", err.getvalue())

    def test_hidden_only_ambiguity_names_no_french_alias(self):
        """P3 : `--po` ne désigne que des alias cachés : aucun n'est suggéré."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as case:
            self.parser.parse_args(["add", "x", "--po", "2026-10-20"])
        self.assertEqual(case.exception.code, 2)
        message = err.getvalue()
        self.assertIn("--po", message)
        for _, french in self.ADD:
            self.assertNotIn(french, message)

    def test_hidden_only_prefix_still_resolves_when_unique(self):
        """L'ancienne abréviation d'un seul alias caché reste acceptée."""
        namespace, err = parse(self.parser, "add", "x", "--pou", "2026-10-20")
        self.assertEqual(namespace.due, "2026-10-20")
        self.assertIn("--pour is deprecated, use --due", err)

    def test_depends_on_and_depend_de_do_not_duplicate(self):
        """P2 : la même dépendance, dans les deux graphies, n'est gardée qu'une fois."""
        for argv in (("--depend-de", "L1", "--depends-on", "L1"),
                     ("--depends-on", "L1", "--depend-de", "L1")):
            namespace, _ = parse(self.parser, "add", "x", *argv)
            self.assertEqual(namespace.depends_on, ["L1"])

    def test_repeating_a_deprecated_alias_warns_once(self):
        """P3 : une seule ligne par graphie, même répétée."""
        namespace, err = parse(self.parser, "add", "x", "--depend-de", "L1",
                               "--depend-de", "L2")
        self.assertEqual(namespace.depends_on, ["L1", "L2"])
        self.assertEqual(err, "--depend-de is deprecated, use --depends-on\n")

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

    def test_hidden_aliases_do_not_capture_the_visible_prefix(self):
        """P3 : `--de` va sur `--delivery`, sans citer --debut/--deploiement."""
        namespace, err = parse(self.parser, "plan", "12", "--de", "2026-10-12")
        self.assertEqual(namespace.livraison, "2026-10-12")
        self.assertEqual(err, "")

    def test_help_shows_english_and_hides_french(self):
        text = help_text(self.parser, "plan")
        for english, french in self.PLAN:
            self.assertIn(english, text)
            self.assertNotIn(french, text)


if __name__ == "__main__":
    unittest.main()
