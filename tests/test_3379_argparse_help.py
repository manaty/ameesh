# SPDX-License-Identifier: AGPL-3.0-only
"""Render every argparse entry point and its subcommands, without dispatching."""
import argparse
import ast
import importlib
import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import support  # noqa: F401 — isolate the host configuration
from .entrypoint import run as isolated_entrypoint


class _Parsed(BaseException):
    """Stop before command dispatch, including handlers catching Exception."""


def _render(parser):
    text = parser.format_help()
    if not text:
        raise AssertionError(f"empty help: {parser.prog}")
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _render(child)


class ArgparseHelpTests(unittest.TestCase):
    def test_all_parser_entry_points(self):
        original_spawn = subprocess.Popen.__init__
        self.addCleanup(setattr, subprocess.Popen, "__init__", original_spawn)
        root = Path(__file__).resolve().parents[1] / "src" / "ameesh"
        covered = set()

        def stop(parser, *args, **kwargs):
            _render(parser)
            raise _Parsed()

        # Discover constructors so a newly added entry point cannot silently
        # miss this check. Invoke the real code, including parser add-ons.
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not any(isinstance(call, ast.Call)
                           and ((isinstance(call.func, ast.Attribute)
                                 and call.func.attr == "ArgumentParser")
                                or (isinstance(call.func, ast.Name)
                                    and call.func.id == "HiddenAliasParser"))
                           for call in ast.walk(node)):
                    continue
                parts = path.relative_to(root.parent).with_suffix("").parts
                module_name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
                with self.subTest(module=module_name, function=node.name):
                    func = getattr(importlib.import_module(module_name), node.name)
                    kwargs = {}
                    for name, param in inspect.signature(func).parameters.items():
                        if name in ("argv", "args"):
                            kwargs[name] = ["backup", "--help"] if module_name == "ameesh.session_backup" else ["--help"]
                        elif name == "prog":
                            kwargs[name] = "ameesh"
                        elif name == "module" and module_name == "ameesh.pipeline_cli":
                            # Declarative CLI parsers need the discovered module's name.
                            kwargs[name] = SimpleNamespace(NAME="example")
                        elif param.default is inspect.Parameter.empty:
                            kwargs[name] = None
                    with patch.object(argparse.ArgumentParser, "parse_args", stop), \
                            patch.object(argparse.ArgumentParser, "parse_known_args", stop):
                        try:
                            result = isolated_entrypoint(func, **kwargs)
                        except _Parsed:
                            pass
                        else:
                            self.assertIsInstance(result, argparse.ArgumentParser)
                            _render(result)
                    covered.add((module_name, node.name))
                    self.assertIs(subprocess.Popen.__init__, original_spawn,
                                  (module_name, node.name))
        self.assertIn(("ameesh.mesh_cli", "build_parser"), covered)
        self.assertIn(("ameesh.role_timers", "main"), covered)
        self.assertIn(("ameesh.roadmap", "build_parser"), covered)


if __name__ == "__main__":
    unittest.main()
