# SPDX-License-Identifier: AGPL-3.0-only
"""#8186 : une portée systemd illisible ne prouve pas l'absence du timer."""
from __future__ import annotations

import unittest
from unittest import mock

from . import support  # noqa: F401 — isole la configuration de l'hôte
from ameesh import hostcheck


class CredentialsTimerScopeTest(unittest.TestCase):
    def check(self, user, system):
        def run(*args):
            scope = system if args[0] == hostcheck.SYSTEM else user
            if scope is None:
                return None
            if "LoadState" in args:
                return scope + "\n"
            if "ActiveState" in args:
                return "active\n"
            if "is-enabled" in args:
                return "enabled\n"
            self.fail("unexpected systemctl call: %r" % (args,))

        injected = mock.Mock(side_effect=run)
        result = hostcheck.credentials_timer_lines(run=injected)
        self.assertEqual(sum("LoadState" in call.args
                             for call in injected.call_args_list), 2)
        return result

    def test_no_user_bus_and_absent_system_unit_is_not_checked(self):
        ko, lines = self.check(None, "not-found")
        self.assertFalse(ko)
        self.assertIn("not checked", lines[0])
        self.assertIn("user scope unreadable", lines[0])
        self.assertNotIn("not installed", "\n".join(lines))
        self.assertIn("in the ameesh user session", lines[1])
        self.assertIn("XDG_RUNTIME_DIR=/run/user/$(id -u)", lines[1])
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus",
                      lines[1])
        self.assertIn("systemctl --user show ameesh-credentials.timer", lines[1])

    def test_absent_user_and_unreadable_system_is_not_checked(self):
        ko, lines = self.check("not-found", None)
        self.assertFalse(ko)
        self.assertIn("system scope unreadable", lines[0])
        self.assertNotIn("not installed", "\n".join(lines))

    def test_both_scopes_absent_is_not_installed(self):
        ko, lines = self.check("not-found", "not-found")
        self.assertTrue(ko)
        self.assertIn("not installed", lines[0])

    def test_active_system_unit_with_unreadable_user_scope_is_ok(self):
        ko, lines = self.check(None, "loaded")
        self.assertFalse(ko)
        self.assertEqual(lines, ["ameesh-credentials.timer active (system unit)"])

    def test_load_error_does_not_prove_absence(self):
        ko, lines = self.check("error", "not-found")
        self.assertFalse(ko)
        self.assertIn("not checked", lines[0])

    def test_both_scopes_unreadable_are_named(self):
        ko, lines = self.check(None, None)
        self.assertFalse(ko)
        self.assertIn("user, system scope unreadable", lines[0])
