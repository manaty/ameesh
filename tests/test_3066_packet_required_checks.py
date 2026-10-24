# SPDX-License-Identifier: AGPL-3.0-only
"""#3066 : un check facultatif ne retient pas un paquet aux requis verts."""
import json
import types

from ameesh import packet_automerge
from . import test_2813_packet_auto_merge as banc


class RequiredPacketChecksTest(banc.Journal):
    def forge(self, *, tests="success", extra=(), **kwargs):
        gh = banc.FakeGh(tests=tests, **kwargs)
        answer = gh.answer

        def respond(args):
            code, out, err = answer(args)
            if args[:2] == ["pr", "view"] and not code:
                data = json.loads(out)
                data["statusCheckRollup"].extend(extra)
                out = json.dumps(data)
            return code, out, err
        gh.answer = respond
        return gh

    def check(self, gh):
        return packet_automerge.check(None, None, banc.LOT, gh=gh)

    def optional(self, state):
        status, conclusion = banc.RUNS[state]
        return {"__typename": "CheckRun", "name": "macOS", "workflowName": "platforms",
                "status": status, "conclusion": conclusion}

    def test_optional_pending_or_failed_check_does_not_hold_the_packet(self):
        for state in ("pending", "failure"):
            with self.subTest(state=state):
                self.armed()
                gh = self.forge(extra=[self.optional(state)])
                out = self.check(gh)
                self.assertEqual(out["result"], "merged", out)
                self.assertEqual(gh.landed, [("main", banc.HEAD)])

    def test_required_missing_pending_failed_or_wrong_source_still_blocks(self):
        self.armed()
        for args in ({"tests": None}, {"tests": "pending"}, {"tests": "failure"},
                     {"app": 1}):
            with self.subTest(args=args):
                gh = self.forge(extra=[self.optional("pending")], **args)
                out = self.check(gh)
                self.assertEqual(out["result"], "pending", out)
                self.assertEqual(gh.merges(), [])

    def test_optional_context_does_not_hold_the_packet(self):
        self.armed()
        gh = self.forge(extra=[{"__typename": "StatusContext", "context": "optional",
                                "state": "PENDING"}])
        self.assertEqual(self.check(gh)["result"], "merged")

    def test_a_project_required_check_is_never_optional(self):
        self.armed()
        self.gate = (True, "", types.SimpleNamespace(
            required_checks=("macOS",), required_statuses=(), ci_required=False))
        for extra in ([], [self.optional("pending")], [self.optional("failure")]):
            with self.subTest(extra=extra):
                gh = self.forge(extra=extra)
                self.assertEqual(self.check(gh)["result"], "pending")
                self.assertEqual(gh.merges(), [])

    def test_a_forge_required_check_is_never_optional(self):
        self.armed()
        for extra in ([], [self.optional("pending")], [self.optional("failure")]):
            with self.subTest(extra=extra):
                gh = self.forge(extra=extra, protection={"required_status_checks": {
                    "contexts": ["tests", "macOS"]}})
                self.assertEqual(self.check(gh)["result"], "pending")
                self.assertEqual(gh.merges(), [])

    def test_project_required_status_is_checked_without_optional_checks(self):
        self.gate = (True, "", types.SimpleNamespace(
            required_checks=(), required_statuses=(("integration/**", "full/"),),
            ci_required=False))
        for state in (None, "PENDING", "FAILURE", "SUCCESS"):
            with self.subTest(state=state):
                self.armed()
                extra = [self.optional("pending")]
                if state:
                    extra.append({"__typename": "StatusContext", "context": "full/packet",
                                  "state": state})
                gh = self.forge(extra=extra)
                out = self.check(gh)
                self.assertEqual(out["result"], "merged" if state == "SUCCESS" else "pending",
                                 out)
                self.assertEqual(bool(gh.merges()), state == "SUCCESS")

    def test_missing_packet_run_proof_remains_blocking(self):
        self.armed()
        gh = self.forge(extra=[self.optional("pending")])
        answer = gh.answer

        def unavailable(args):
            if args[:1] == ["api"] and "/actions/runs?" in args[-1]:
                return 1, "", "unavailable"
            return answer(args)
        gh.answer = unavailable
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.merges(), [])

    def test_failed_packet_gate_is_not_treated_as_optional(self):
        self.armed()
        failed = dict(self.optional("failure"), workflowName="paquet", name="security")
        gh = self.forge(extra=[failed])
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.merges(), [])
