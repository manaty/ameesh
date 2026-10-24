# SPDX-License-Identifier: AGPL-3.0-only
"""Staging must never undo a fix already active on the mesh (#3291)."""
import json
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import mechanics, release_cutoff, staging_pending
from tests.test_2448_release_cutoff import MECHANICS, ReleaseFixtureMixin, _paris


class ActiveFloorTest(ReleaseFixtureMixin, unittest.TestCase):
    def attest(self, sha):
        path = Path(release_cutoff.attestation_path(sha))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sha": sha, "date": "2026-10-24T04:00:00Z",
                                   "hotes": {"vm": {"actif": sha}}}))

    def test_cutoff_keeps_active_when_only_green_is_older(self):
        self.attest(self.head)
        old = self.git(self.clone, "rev-parse", "v1.11.7^{commit}")
        with mock.patch.object(release_cutoff, "ci_reading", return_value={"green": False}), \
             mock.patch.object(release_cutoff, "last_green", return_value=old), \
             mock.patch.object(release_cutoff, "tree_green", return_value=None), \
             mock.patch.object(release_cutoff, "run_staging") as stage:
            result = release_cutoff.execute(self.cfg, None, self.project,
                                            now=_paris(23, 5))
        self.assertEqual(result["result"], "kept-active")
        self.assertEqual(result["active"], {"vm": self.head})
        stage.assert_not_called()
        self.assertEqual(self.git(self.clone, "tag", "-l", "v1.11.8"), "")

    def test_guard_also_covers_reconciliation_and_direct_staging(self):
        self.attest(self.head)
        old = self.git(self.clone, "rev-parse", "v1.11.7^{commit}")
        with mock.patch.object(release_cutoff, "recent_tag", return_value={
                "commit": old, "version": "1.11.7"}), \
             mock.patch.object(release_cutoff, "ensure_github_release") as publish:
            report = release_cutoff.execute(self.cfg, None, self.project, now=_paris(23, 5))
        self.assertEqual(report["result"], "kept-active")
        publish.assert_not_called()
        with self.assertRaisesRegex(release_cutoff.ReleaseError, "roll back"):
            release_cutoff._staging_step(self.project, old, [], self.cfg.state_dir)

    def test_descendant_allowed_and_unresolvable_active_ref_refused(self):
        old = self.git(self.clone, "rev-parse", "v1.11.7^{commit}")
        self.assertEqual(staging_pending.regressions(
            self.project, self.head, {"vm": {"sha": old}}), {})
        with self.assertRaises(release_cutoff.ReleaseError):
            staging_pending.regressions(self.project, self.head,
                                        {"vm": {"sha": "a" * 40}})


class WaitingTest(ReleaseFixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.old = self.git(self.clone, "rev-parse", "v1.11.7^{commit}")
        self.active = {"vm": {"sha": self.old, "at": 1}}
        self.rows = [{"id": 3111, "app": "ameesh", "state": "merged",
                      "migrations": [{"number": 477, "repo": "manaty/ameesh"}]}]
        self.events = []
        self.milestones = {3111: [{"id": 10, "kind": "merged", "sha": self.head,
                                  "at_ts": 5, "at": "2026-10-24T04:00:00Z"}]}
        self.st = types.SimpleNamespace(
            work=types.SimpleNamespace(items=lambda **kw: self.rows,
                                       milestones=lambda ident, **kw: self.milestones.get(ident, [])),
            progress=types.SimpleNamespace(lot_hold_events=lambda *a: self.events))
        patch = mock.patch.object(staging_pending.storage, "of", return_value=self.st)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(staging_pending.planned_waits, "_pilot_allowed",
                                  return_value=lambda actor: actor == "pilot")
        patch.start()
        self.addCleanup(patch.stop)

    def hold(self, ident, note, *, app="ameesh", actor="author"):
        if not any(r["id"] == ident for r in self.rows):
            self.rows.append({"id": ident, "app": app, "state": "qa"})
        self.events.append({"id": len(self.events) + 1, "work_item_id": ident,
                            "state": "qa", "note": note, "actor": actor,
                            "created_ts": len(self.events) + 10})

    def scan(self):
        return staging_pending.waiting(None, self.project, active=self.active)

    def test_incident_eight_lots_including_nexlink_and_migration(self):
        for ident in range(1, 9):
            self.hold(ident, "hold: attendre #3111 et migration 0477 en staging",
                      app="nexlink" if ident <= 3 else "ameesh")
        result = self.scan()
        self.assertEqual(result["lots"], list(range(1, 9)))
        self.assertEqual(len(result["waiting"]), 8)  # duplicate mentions do not add lots
        self.assertEqual(result["waiting"][0]["app"], "nexlink")
        self.assertEqual(result["waiting"][0]["merged_at"], 5)
        self.active["vm"]["sha"] = self.head
        self.assertEqual(self.scan()["lots"], [])

    def test_lifted_hold_column_reviewer_and_body_do_not_create_wait(self):
        self.hold(1, "hold: #3111 non actif")
        self.hold(1, "unhold: livré", actor="pilot")
        self.rows.append({"id": 2, "app": "ameesh", "state": "qa",
                          "body": "#3111", "reviewer": "someone"})
        self.assertEqual(self.scan()["lots"], [])

    def test_annulled_merge_is_not_a_fix_to_deploy(self):
        self.hold(1, "hold: #3111 non actif")
        self.milestones[3111][0]["annulled"] = True
        self.assertEqual(self.scan()["lots"], [])

    def test_satisfied_dependency_and_migration_only_hold(self):
        # #3297: an explicit dependency is satisfied at merge and is not a
        # deployment wait; the storage graph is not even read. Only an active
        # hold that states the wait counts.
        self.rows.append({"id": 1, "app": "nexlink", "state": "qa"})
        self.st.work_dependencies = types.SimpleNamespace(for_items=mock.Mock(
            return_value=[{"work_item_id": 1, "blocker_id": 3111, "blocking": False}]))
        self.hold(2, "hold: migration 0477 pas encore active")
        self.assertEqual(self.scan()["lots"], [2])
        self.st.work_dependencies.for_items.assert_not_called()
        self.active = {}
        result = self.scan()
        self.assertEqual(result["lots"], [])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["unknown"][0]["blocker"], 3111)
        self.assertIn("no active host", result["unknown"][0]["reason"])

    def test_conflict_pr_package_freeze_and_review_are_not_deployment_waits(self):
        # #3297: the reviewer's real examples — "conflit avec #3266", a PR
        # number, a package, a freeze id and a review reference. Each clause
        # also carries a deployment word, so only the context filter rejects
        # them.
        for ident, note in enumerate((
                "hold: écarté de h-03 : conflit sémantique avec #3111, pas déployé",
                "hold: PR #3111 pas déployée",
                "hold: paquet #3111 pas déployé",
                "hold: gel #3111 pas déployé",
                "hold: relu par #3111, pas déployé",
                "hold: ticket IMA #3111, pas déployé",
                "hold: pas le lot ameesh #3111, pas déployé"), start=1):
            self.hold(ident, note)
        self.assertEqual(self.scan()["lots"], [])
        self.hold(9, "hold: #3111 n'est pas déployé sur les hôtes")
        self.assertEqual(self.scan()["lots"], [9])

    def test_explicit_wait_words_count_without_a_deployment_word(self):
        self.hold(1, "hold: attendre #3111")
        self.hold(2, "hold: la version 1.21.0 doit porter #3111")
        self.assertEqual(self.scan()["lots"], [1, 2])

    def test_missing_merged_commit_goes_to_unknown_without_hiding_others(self):
        # #3297 N2: one merged head absent from the release clone used to make
        # the whole report unreadable at every tick.
        self.hold(1, "hold: #3111 pas encore déployé")
        self.rows.append({"id": 3112, "app": "ameesh", "state": "merged"})
        self.milestones[3112] = [{"id": 11, "kind": "merged", "sha": "b" * 40,
                                  "at_ts": 6}]
        self.hold(2, "hold: #3112 pas encore déployé")
        result = self.scan()
        self.assertEqual(result["lots"], [1])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["unknown"][0]["lot"], 2)
        self.assertEqual(result["unknown"][0]["blocker"], 3112)
        self.assertIn("merged commit", result["unknown"][0]["reason"])

    def test_missing_active_commit_is_unknown_and_other_hosts_still_read(self):
        # #3297 N2: an attestation whose commit is not in this clone is
        # unknown evidence for that host, not an unreadable report.
        self.hold(1, "hold: #3111 pas encore déployé")
        self.active = {"good": {"sha": self.old, "at": 1},
                       "lost": {"sha": "c" * 40, "at": 1}}
        result = self.scan()
        self.assertEqual(result["lots"], [1])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["unknown"][0]["host"], "lost")
        self.assertIn("active commit", result["unknown"][0]["reason"])

    def test_transient_release_error_is_not_swallowed_as_unknown(self):
        # A transient git failure is retried at the next tick, not recorded as
        # a durable unknown.
        self.hold(1, "hold: #3111 pas encore déployé")
        with mock.patch.object(release_cutoff, "commit_of",
                               side_effect=release_cutoff.TransientReleaseError("retry")):
            with self.assertRaises(release_cutoff.TransientReleaseError):
                self.scan()

    def test_unresolvable_active_host_blocks_the_candidate_without_failing(self):
        # #3297 N2: the candidate check must not turn a partial report into an
        # unreadable tick; it refuses the candidate instead.
        for ident in range(1, 4):
            self.hold(ident, "hold: #3111 non actif")
        self.active = {"good": {"sha": self.old, "at": 1},
                       "lost": {"sha": "c" * 40, "at": 1}}
        rules = types.SimpleNamespace(staging=mechanics.Staging(3, 1200, 600),
                                      next_cutoff=lambda now: _paris(23),
                                      next_packet=lambda now: _paris(23))
        with mock.patch.object(release_cutoff, "compiled_rules", return_value=(rules, None)), \
             mock.patch.object(staging_pending, "active_hosts", return_value=self.active), \
             mock.patch.object(release_cutoff, "commit_is_green", return_value=True):
            report = staging_pending.inspect(self.cfg, None, self.project, now=_paris(22, 30))
        self.assertEqual(report["result"], "needed")
        self.assertFalse(report["candidate_ready"])
        self.assertTrue(any(item.get("host") == "lost" for item in report["unknown"]))

    def test_threshold_and_cutoff_margin_are_compiled(self):
        rules = types.SimpleNamespace(staging=mechanics.Staging(3, 1200, 600),
                                      next_cutoff=lambda now: _paris(23),
                                      next_packet=lambda now: _paris(23))
        for ident in range(1, 4):
            self.hold(ident, "hold: #3111 non actif")
        with mock.patch.object(release_cutoff, "compiled_rules", return_value=(rules, None)), \
             mock.patch.object(staging_pending, "active_hosts", return_value=self.active), \
             mock.patch.object(release_cutoff, "commit_is_green", return_value=True):
            report = staging_pending.inspect(self.cfg, None, self.project, now=_paris(22, 40))
            self.assertEqual(report["result"], "needed")
            self.assertTrue(report["window_ok"])
            self.assertEqual(report["candidate"], self.head)
            report = staging_pending.inspect(self.cfg, None, self.project, now=_paris(22, 41))
            self.assertFalse(report["window_ok"])
            with mock.patch.object(release_cutoff, "commit_is_green", return_value=False), \
                 mock.patch.object(release_cutoff, "last_green", return_value=self.old):
                report = staging_pending.inspect(self.cfg, None, self.project, now=_paris(22, 30))
                self.assertFalse(report["candidate_ready"])
            self.rows.pop()
            self.events.pop()
            report = staging_pending.inspect(self.cfg, None, self.project, now=_paris(22, 30))
            self.assertEqual(report["result"], "below-threshold")

    def test_apply_uses_lease_and_attests_the_fixes_not_the_waiters(self):
        self.st.work.land_lock = mock.Mock(return_value=7)
        self.st.work.land_unlock = mock.Mock()
        report = {"result": "needed", "candidate": self.head, "candidate_ready": True,
                  "regressions": {}, "candidate_unblocks": [1, 2, 3],
                  "waiting": [{"lot": n, "blocker": 3111} for n in (1, 2, 3)]}
        rules = types.SimpleNamespace(staging=mechanics.Staging(3, 1200, 600),
                                      next_cutoff=lambda now: _paris(23),
                                      next_packet=lambda now: _paris(23))
        with mock.patch("ameesh.work.corrector", return_value="pilot"), \
             mock.patch.object(staging_pending, "inspect", return_value=report), \
             mock.patch.object(release_cutoff, "compiled_rules", return_value=(rules, None)), \
             mock.patch.object(release_cutoff, "_staging_step", return_value={"exit": 0}) as stage:
            result = staging_pending.apply(self.cfg, None, self.project, now=_paris(22, 30))
            self.assertEqual(result["result"], "staged")
            stage.assert_called_once_with(self.project, self.head, [3111], self.cfg.state_dir)
            self.assertEqual(self.st.work.land_lock.call_count, 2)
            self.st.work.land_unlock.assert_called_once()
            stage.reset_mock()
            report["result"] = "needed"
            self.st.work.land_lock.side_effect = [7, None]
            with self.assertRaises(release_cutoff.ReleaseBusy):
                staging_pending.apply(self.cfg, None, self.project, now=_paris(22, 30))
            stage.assert_not_called()

    def test_occupied_lease_or_near_packet_never_stages(self):
        self.st.work.land_lock = mock.Mock(return_value=None)
        self.st.work.land_unlock = mock.Mock()
        with mock.patch("ameesh.work.corrector", return_value="pilot"), \
             mock.patch.object(staging_pending, "inspect") as scan:
            with self.assertRaises(release_cutoff.ReleaseBusy):
                staging_pending.apply(self.cfg, None, self.project, now=_paris(22, 30))
            scan.assert_not_called()
            self.st.work.land_unlock.assert_not_called()
        rules = types.SimpleNamespace(staging=mechanics.Staging(3, 1200, 600),
                                      next_cutoff=lambda now: _paris(23),
                                      next_packet=lambda now: _paris(22))
        self.assertFalse(staging_pending._window(rules, _paris(21, 50))["window_ok"])
        self.assertTrue(staging_pending._window(rules, _paris(21, 49))["window_ok"])

    def test_unauthorized_apply_has_no_effect(self):
        from ameesh import work
        self.st.work.land_lock = mock.Mock()
        with mock.patch.object(work, "corrector", side_effect=work.WorkError("refused")), \
             mock.patch.object(staging_pending, "inspect") as scan:
            with self.assertRaises(work.WorkError):
                staging_pending.apply(self.cfg, None, self.project, now=_paris(22, 30))
            scan.assert_not_called()
            self.st.work.land_lock.assert_not_called()

    def test_timer_alert_is_deduplicated_for_each_recipient_and_rearmed(self):
        report = {"result": "needed", "lots": [1, 2, 3], "candidate": self.head,
                  "active": self.active}
        cfg = types.SimpleNamespace(state_dir=self.cfg.state_dir,
                                    packet_watch={"projects": {"ameesh": {
                                        "notify": ["pilot", "bureau"]}}})
        with mock.patch.object(staging_pending, "inspect", return_value=report), \
             mock.patch("ameesh.mail.send") as send:
            staging_pending.tick(cfg, None, self.project, now=_paris(22, 30))
            staging_pending.tick(cfg, None, self.project, now=_paris(22, 31))
            self.assertEqual(send.call_count, 2)
            self.assertEqual({c.args[2] for c in send.call_args_list}, {"pilot", "bureau"})
            with mock.patch.object(staging_pending, "inspect", return_value={
                    "result": "below-threshold"}):
                staging_pending.tick(cfg, None, self.project, now=_paris(22, 32))
            staging_pending.tick(cfg, None, self.project, now=_paris(22, 33))
            self.assertEqual(send.call_count, 4)


class CanonStagingTest(unittest.TestCase):
    def test_absent_has_no_assumed_policy_and_present_is_strict(self):
        self.assertIsNone(mechanics.compile_document(MECHANICS).staging)
        valid = MECHANICS + "\nstaging:\n  waiting_lots_min: 3\n  before_cutoff_seconds: 1200\n  before_packet_seconds: 600\n"
        self.assertEqual(mechanics.compile_document(valid).to_dict()["staging"],
                         {"waiting_lots_min": 3, "before_cutoff_seconds": 1200,
                          "before_packet_seconds": 600})
        for text in (valid.replace("1200", "true"), valid.replace("1200", "0"),
                     valid.replace("  before_cutoff_seconds: 1200\n  before_packet_seconds: 600\n", ""),
                     valid + "  unknown: 1\n"):
            with self.subTest(text=text), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(text)


if __name__ == "__main__":
    unittest.main()
