# SPDX-License-Identifier: AGPL-3.0-only
"""Readiness refuses cross-host credentials, stale probes and missing references."""
import copy
import unittest

from ameesh.host_preflight import Requirements, check


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.required = Requirements("vm-1", "codex", "mesh", secret_refs=("mesh/api",))
        self.inventory = {"host": "vm-1", "checked_ts": 995, "connected": True,
                          "tools": {"gh": True, "gcloud": True}, "secrets": {"mesh/api": True}}
        self.credentials = [{"host": "vm-1", "harness": name, "account": account,
                             "state": "healthy", "checked_ts": 995, "expires_ts": 2000}
                            for name, account in (("codex", "mesh"), ("gh", ""), ("gcloud", ""))]

    def check(self):
        return check(self.required, self.inventory, self.credentials, now=1000)

    def test_complete_enrolled_host_is_ready(self):
        self.assertEqual(self.check(), {"host": "vm-1", "ready": True, "missing": []})

    def test_other_host_or_account_does_not_supply_login(self):
        for field, value in (("host", "vm-2"), ("account", "another")):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.credentials)
                rows[0][field] = value
                result = check(self.required, self.inventory, rows, now=1000)
                self.assertEqual(result["missing"], ["login:codex/mesh"])

    def test_inventory_must_be_fresh_and_from_the_receiving_host(self):
        for changes in ({"host": "source"}, {"checked_ts": 600}, {"checked_ts": 1001},
                        {"checked_ts": True}, {"checked_ts": float("nan")}):
            with self.subTest(changes=changes):
                result = check(self.required, dict(self.inventory, **changes), self.credentials, now=1000)
                self.assertFalse(result["ready"])
                self.assertIn("host_inventory:vm-1", result["missing"])
                self.assertIn("host_connection:vm-1", result["missing"])
                self.assertIn("secret:mesh/api", result["missing"])

    def test_stale_expired_or_unknown_login_refuses_placement(self):
        for changes in ({"checked_ts": 600}, {"checked_ts": 1001}, {"expires_ts": 1000},
                        {"expires_ts": float("nan")}, {"state": "failing"}, {"state": "unknown"}):
            with self.subTest(changes=changes):
                rows = copy.deepcopy(self.credentials)
                rows[0].update(changes)
                result = check(self.required, self.inventory, rows, now=1000)
                self.assertEqual(result["missing"], ["login:codex/mesh"])

    def test_tools_need_both_availability_and_authentication(self):
        self.inventory["tools"]["gh"] = False
        self.credentials[2]["state"] = "failing"
        self.assertEqual(self.check()["missing"], ["tool:gh", "login:gcloud/default"])

    def test_missing_secret_and_connection_are_named_without_values(self):
        self.inventory["connected"] = False
        self.inventory["secrets"]["mesh/api"] = "private-value"
        self.credentials[0].update(state="failing", reason="private-diagnostic", failing_fingerprint="private-value")
        result = self.check()
        self.assertEqual(result["missing"], ["host_connection:vm-1", "secret:mesh/api", "login:codex/mesh"])
        self.assertNotIn("private", str(result))

    def test_newer_failed_login_is_not_hidden_by_old_healthy_report(self):
        self.credentials.append(dict(self.credentials[0], checked_ts=999, state="failing"))
        self.assertEqual(self.check()["missing"], ["login:codex/mesh"])

    def test_ambiguous_simultaneous_reports_refuse_login(self):
        self.credentials.append(dict(self.credentials[0], state="failing"))
        self.assertEqual(self.check()["missing"], ["login:codex/mesh"])

    def test_invalid_requirements_or_freshness_are_refused(self):
        for kwargs in ({"account": None}, {"tools": "gh"}, {"secret_refs": ["mesh/api"]},
                       {"secret_refs": ("secret\nvalue",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Requirements("vm-1", "codex", **kwargs)
        for max_age in (0, -1, True, float("nan")):
            with self.subTest(max_age=max_age), self.assertRaises(ValueError):
                check(self.required, self.inventory, self.credentials, now=1000, max_age=max_age)

    def test_absent_probes_name_all_prerequisites(self):
        result = check(self.required, None, None, now=1000)
        self.assertEqual(set(result["missing"]), {
            "host_inventory:vm-1", "host_connection:vm-1", "tool:gh", "tool:gcloud",
            "secret:mesh/api", "login:codex/mesh", "login:gh/default", "login:gcloud/default"})
