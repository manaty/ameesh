# SPDX-License-Identifier: AGPL-3.0-only
"""#4036 : un client périmé refuse le check requis, même avec les parts vertes."""
from __future__ import annotations

from pathlib import Path
import unittest

from . import test_940_agregat_bash as aggregate


class RequiredClientTypesTest(unittest.TestCase):
    def test_freshness_is_required_before_test_parts(self):
        workflow = (Path(__file__).resolve().parents[1]
                    / ".github/workflows/tests.yml").read_text()
        plan = workflow.split("\n  plan:\n", 1)[1].split("\n  psycopg:\n", 1)[0]
        steps = plan.split("\n      - ")
        [check] = [step for step in steps if "id: client_types\n" in step]
        self.assertIn("run: node ui/scripts/gen-api.mjs --check\n", check)
        self.assertNotIn("if:", check)
        self.assertNotIn("continue-on-error:", check)
        self.assertLess(plan.index("id: client_types"), plan.index("id: plan\n"))
        [install] = [step for step in steps if "run: npm ci " in step]
        self.assertIn("working-directory: ui", install)
        self.assertIn("--ignore-scripts", install)
        self.assertNotIn("if:", install)
        self.assertNotIn("continue-on-error:", install)
        self.assertIn("node-version: \"22\"", plan)
        self.assertIn("client_types: ${{ steps.client_types.outcome }}", plan)
        required = workflow.split("\n  tests:\n", 1)[1].split("\n  python314:\n", 1)[0]
        self.assertIn("needs: [plan,", required)
        self.assertIn("--plan \"${{ needs.plan.result }}\"", required)
        self.assertIn("CLIENT_TYPES: ${{ needs.plan.outputs.client_types }}", required)
        self.assertIn("Types clients à jour (gen-api --check, requis via plan)", required)

    def test_failed_plan_refuses_green_even_when_every_part_succeeded(self):
        # Le contrôle échoue dans plan. Les autres parts ne peuvent masquer
        # cet échec, y compris sur les voies documentation/version de main.
        for event in ("pull_request", "push", "workflow_call"):
            for lane in ({}, {"docs_only": "true"}, {"version_only": "true"}):
                with self.subTest(event=event, lane=lane):
                    flags = dict(version_only="false", docs_only="false")
                    flags.update(lane)
                    result = aggregate.agregat.classify(
                        event=event, plan="failure", version="success",
                        doc_readers="success", psycopg="success",
                        psycopg_parts="[1]", psql="success", psql_parts="[1]",
                        platform_macos="success", fs_class="success", **flags)
                    self.assertEqual(result["state"], aggregate.agregat.RED)
                    self.assertIn("plan (failure)", result["red"])
