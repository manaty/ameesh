# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T2 (#4392): the CLI in the active module's vocabulary.

``mesh_cli.py``, ``cli.py`` and ``main.py`` render their help texts,
descriptions and work messages through :mod:`ameesh.vocab`: DEV ("lot") via
the dev module, CORE ("tâche") when no active module declares a vocabulary.
Commands, flags, exit codes and ``--json`` keys stay unchanged; only the
user-visible noun moves.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from unittest import mock

from ameesh import cli, main, mesh_cli, vocab
from ameesh.modules import dev

from tests import support  # noqa: F401 — isolate the host configuration


def _sub(parser, name):
    """The named subparser of ``parser``."""
    action = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    return action.choices[name]


def _choice_help(parser, name):
    """The help line argparse shows for the subcommand ``name`` (unwrapped)."""
    action = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    return next(choice.help for choice in action._choices_actions
                if choice.dest == name)


def _arg_help(parser, dest):
    """The help of the argument ``dest``, before argparse wraps it."""
    return next(action.help for action in parser._actions if action.dest == dest)


def _capture(function, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = function(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class VocabularyGrammarTest(unittest.TestCase):
    """The words and articles the CLI phrases rely on (CORE and DEV)."""

    def test_core_articles_and_contractions(self):
        self.assertEqual(vocab.CORE.definite, "la tâche")
        self.assertEqual(vocab.CORE.indefinite, "une tâche")
        self.assertEqual(vocab.CORE.of, "de la tâche")
        self.assertEqual(vocab.CORE.to, "à la tâche")
        self.assertEqual(vocab.CORE.definite_plural, "les tâches")
        self.assertEqual(vocab.CORE.agree("livré", "livrée", plural=True), "livrées")

    def test_dev_articles_and_contractions(self):
        self.assertEqual(dev.DEV.definite, "le lot")
        self.assertEqual(dev.DEV.indefinite, "un lot")
        self.assertEqual(dev.DEV.of, "du lot")
        self.assertEqual(dev.DEV.to, "au lot")
        self.assertEqual(dev.DEV.definite_plural, "les lots")
        self.assertEqual(dev.DEV.agree("livré", "livrée", plural=True), "livrés")

    def test_the_cli_words_are_the_vocabulary_nouns(self):
        self.assertEqual(mesh_cli._words(vocab.CORE),
                         {"work": "tâche", "works": "tâches"})
        self.assertEqual(mesh_cli._words(dev.DEV), {"work": "lot", "works": "lots"})


class HelpTextTest(unittest.TestCase):
    def test_the_work_command_names_the_work_item(self):
        dev_parser = mesh_cli.build_parser(dev.DEV)
        core_parser = mesh_cli.build_parser(vocab.CORE)
        self.assertIn("lots (work_items)", dev_parser.format_help())
        self.assertIn("tâches (work_items)", core_parser.format_help())

    def test_a_subcommand_help_follows_the_vocabulary(self):
        dev_neighbors = _sub(_sub(mesh_cli.build_parser(dev.DEV), "work"), "neighbors")
        core_neighbors = _sub(_sub(mesh_cli.build_parser(vocab.CORE), "work"), "neighbors")
        self.assertEqual(_arg_help(dev_neighbors, "app"), "the lot's project")
        self.assertEqual(_arg_help(core_neighbors, "app"), "the tâche's project")

    def test_an_argument_help_follows_the_vocabulary(self):
        dev_ci = _sub(_sub(mesh_cli.build_parser(dev.DEV), "ci"), "status")
        core_ci = _sub(_sub(mesh_cli.build_parser(vocab.CORE), "ci"), "status")
        self.assertIn("of a lot and their failing tests",
                      _choice_help(_sub(mesh_cli.build_parser(dev.DEV), "ci"),
                                   "status"))
        self.assertIn("of a tâche and their failing tests",
                      _choice_help(_sub(mesh_cli.build_parser(vocab.CORE), "ci"),
                                   "status"))
        self.assertEqual(_arg_help(dev_ci, "id"), "lot number")
        self.assertEqual(_arg_help(core_ci, "id"), "tâche number")

    def test_the_ci_verdict_help_agrees_with_the_vocabulary(self):
        self.assertIn("(lot léger)",
                      _arg_help(_sub(_sub(mesh_cli.build_parser(dev.DEV), "ci"),
                                     "run"), "avis"))
        self.assertIn("(tâche légère)",
                      _arg_help(_sub(_sub(mesh_cli.build_parser(vocab.CORE), "ci"),
                                     "run"), "avis"))

    def test_the_program_descriptions_follow_the_vocabulary(self):
        self.assertIn("lots", mesh_cli._doc(dev.DEV).splitlines()[0])
        self.assertIn("tâches", mesh_cli._doc(vocab.CORE).splitlines()[0])
        self.assertIn("unassigned lot", cli._doc(dev.DEV))
        self.assertIn("unassigned tâche", cli._doc(vocab.CORE))
        self.assertIn("latest CI runs and failing tests of a lot", main._doc(dev.DEV))
        self.assertIn("latest CI runs and failing tests of a tâche", main._doc(vocab.CORE))

    def test_the_french_prose_agrees(self):
        # main.py's docstring carries articles and a past participle.
        self.assertIn("les lots en qa/blocked", main._doc(dev.DEV))
        self.assertIn("les tâches en qa/blocked", main._doc(vocab.CORE))
        self.assertIn("trois lots livrés", main._doc(dev.DEV))
        self.assertIn("trois tâches livrées", main._doc(vocab.CORE))

    def test_the_dev_module_command_says_lot(self):
        from ameesh.modules import dev as dev_module
        code, out, _ = _capture(dev_module.main, None, ["--help"], actor="")
        self.assertEqual(code, 0)
        self.assertIn("close the lots", out)
        self.assertIn("one GitHub issue per lot", out)
        self.assertNotIn("work items", out)

    def test_the_default_vocabulary_is_the_dev_module(self):
        vocab.reset()
        self.addCleanup(vocab.reset)
        self.assertEqual(vocab.active(), dev.DEV)
        self.assertIn("lots (work_items)", mesh_cli.build_parser().format_help())


class MessageTest(unittest.TestCase):
    def test_an_external_session_waits_for_a_work_item(self):
        check = {"externe": True, "assignee": "ameesh-dev5",
                 "responsible": "human:alice"}
        with mock.patch.object(vocab, "active", return_value=dev.DEV):
            _, out, _ = _capture(mesh_cli._print_assignment, check)
        self.assertIn("the lot waits for human:alice", out)
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            _, out, _ = _capture(mesh_cli._print_assignment, check)
        self.assertIn("the tâche waits for human:alice", out)

    def test_the_deep_analysis_warning_follows_the_vocabulary(self):
        result = {"requested": True, "auditors": []}
        with mock.patch.object(vocab, "active", return_value=dev.DEV):
            _, _, err = _capture(mesh_cli._print_deep_analysis, result)
        self.assertIn("requested on the lot", err)
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            _, _, err = _capture(mesh_cli._print_deep_analysis, result)
        self.assertIn("requested on the tâche", err)

    def test_agent_mail_send_refuses_an_invalid_lot_in_the_vocabulary(self):
        with mock.patch.object(vocab, "active", return_value=dev.DEV):
            code, _, err = _capture(
                cli.cmd_send, None, None, ["dest", "body", "--lot", "bad!lot"])
        self.assertEqual(code, 2)
        self.assertIn("invalid lot", err)
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            code, _, err = _capture(
                cli.cmd_send, None, None, ["dest", "body", "--lot", "bad!lot"])
        self.assertEqual(code, 2)
        self.assertIn("invalid tâche", err)

    def test_agent_mail_send_names_the_lot_to_assign(self):
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            code, _, err = _capture(
                cli.cmd_send, None, None, ["dest", "body", "--assign"])
        self.assertEqual(code, 2)
        self.assertIn("say which tâche to assign", err)


class UnchangedCommandsTest(unittest.TestCase):
    """Only the noun moves: commands, flags and keys do not."""

    def test_the_flags_and_the_json_key_stay(self):
        text = cli._doc(vocab.CORE)
        for flag in ("--lot", "--new-lot", "--assign"):
            self.assertIn(flag, text)
        self.assertNotIn("--tâche", text)
        root = mesh_cli.build_parser(vocab.CORE)
        self.assertIn("tâches (work_items)", root.format_help())
        add = _sub(_sub(root, "work"), "add")
        flags = {option for action in add._actions
                 for option in action.option_strings}
        for flag in ("--parent-lot", "--branch", "--estimate"):
            self.assertIn(flag, flags)
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            code, _, err = _capture(
                cli.cmd_send, None, None,
                ["dest", "body", "--parent-lot", "4391"])
        self.assertEqual(code, 2)
        self.assertIn("--parent-lot goes with --new-lot", err)

    def test_the_parser_still_accepts_the_same_arguments(self):
        parser = mesh_cli.build_parser(vocab.CORE)
        args = parser.parse_args(["work", "show", "4392"])
        self.assertEqual(args.work_command, "show")
        self.assertEqual(args.id, 4392)
        args = parser.parse_args(["ci", "status", "4392"])
        self.assertEqual(args.ci_command, "status")
        self.assertEqual(args.id, 4392)


if __name__ == "__main__":
    unittest.main()
