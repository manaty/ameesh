# SPDX-License-Identifier: AGPL-3.0-only
"""#2256 : la voie du feu (file ou direct) ne dépend pas de la config de l'hôte.

Le paquet Nexlink 54 (PR manaty/nexlink#1511) a été fusionné **en direct** par
le passage du feu : l'hôte qui avait pris le bail `fire:<lot>` ne déclarait pas
`radar.projects.nexlink` et retombait sur `_merge_direct`. Le même lot, servi
par un hôte qui déclare le projet (`land: local` ou `pr`), entre dans la file.

Le témoin demande deux hôtes de configuration différente pour le **même
projet** : la voie attendue est la file sur les deux, et l'hôte qui ne déclare
pas le projet ne doit jamais fusionner en direct. Un hôte qui ne peut établir
ni la déclaration du projet ni l'usage de sa file s'abstient.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import fire

A = "a" * 40
B = "b" * 40

FAM = {"author": "deepseek", "rev1": "codex", "rev2": "claude"}


def _mark(kind, sha="", **extra):
    row = {"kind": kind, "sha": sha, "actor": "pilote", "at_ts": 0.0, "id": 0}
    row.update(extra)
    return row


def _marks(*rows):
    return [dict(row, at_ts=float(i), id=i) for i, row in enumerate(rows, start=1)]


def _status(**extra):
    row = {"number": 7, "state": "OPEN", "head": A, "base": "develop", "draft": False,
           "ci": "green", "merged": False, "merge_commit": "", "armed": False,
           "url": "https://forge/pr/7"}
    row.update(extra)
    return row


def _info(**extra):
    row = {"slug": "manaty/nexlink", "allow_auto_merge": True, "default_branch": "develop",
           "private": True, "merge": True, "squash": True, "rebase": True}
    row.update(extra)
    return row


class HostConfigWitnessTest(unittest.TestCase):
    """Le cas du paquet 54 : deux hôtes, un projet, une seule voie attendue."""

    ROW = {"id": 42, "title": "paquet 54", "branch": "integration/paquet-54-nexlink",
           "assignee": "author", "app": "nexlink", "branch_target": "develop",
           "state": "qa"}

    def _act(self, cfg, project, *, shared_rows=(), repo=None, status=None, dry_run=False):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        ops = mock.Mock()
        ops.queue.return_value = list(shared_rows)
        names = ("_repo_of", "_slug", "_project", "_pr_status", "_repo_info", "_target",
                 "ready_verdicts", "ready_pr", "_merge_pr", "_reconcile", "_queue",
                 "_open_pr", "_leave_queue", "_fresh", "_renew", "_target_lock", "_unlock",
                 "_passive", "_wake", "_disarm", "_invalidate")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched, \
                mock.patch.object(fire.storage, "of",
                                  return_value=mock.Mock(work=ops)), \
                mock.patch.object(fire.packet_guard, "check_merge",
                                  return_value={"allowed": True}):
            patched["_repo_of"].return_value = repo
            patched["_slug"].return_value = "manaty/nexlink"
            patched["_project"].return_value = project
            patched["_pr_status"].return_value = status if status is not None else _status()
            patched["_repo_info"].return_value = _info()
            patched["_target"].return_value = "develop"
            patched["ready_verdicts"].return_value = (True, "", {"class": "light"})
            patched["ready_pr"].return_value = (True, "")
            patched["_queue"].return_value = {"lot": 42, "result": "queued"}
            patched["_merge_pr"].return_value = (True, "PR merged")
            patched["_reconcile"].return_value = {"lot": 42, "result": "merged"}
            patched["_invalidate"].return_value = {"lot": 42, "result": "invalidated"}
            patched["_disarm"].return_value = {"lot": 42, "result": "disarmed"}
            patched["_open_pr"].return_value = (_status(), "opened")
            patched["_leave_queue"].return_value = False
            patched["_fresh"].return_value = (self.ROW, marks)
            patched["_renew"].return_value = True
            patched["_target_lock"].return_value = (None, False)
            out = fire._act(cfg, None, self.ROW, marks, gh=lambda *a, **k: None,
                            dry_run=dry_run, repo=repo, cache={})
        return out, patched

    def _project(self, cfg):
        return fire._project(cfg, None, "manaty/nexlink")

    def _queue_row(self, state="landed"):
        return {"work_item_id": 2234, "target": "develop", "state": state,
                "source": "github.com/manaty/nexlink", "branch": "integration/paquet-52-nexlink",
                "item_state": "merged"}

    def test_host_declaring_the_project_enters_the_queue(self):
        cfg = types.SimpleNamespace(radar={"projects": {"nexlink": {
            "repo": "/srv/nexlink", "github": "manaty/nexlink", "target": "develop",
            "land": "local", "gates": ["true"]}}})
        out, calls = self._act(cfg, self._project(cfg))
        self.assertEqual(out["result"], "queued")
        calls["_queue"].assert_called_once()
        calls["_merge_pr"].assert_not_called()

    def test_host_without_the_project_never_merges_directly(self):
        """Le témoin rouge du paquet 54 : la config locale de l'hôte ne doit
        pas choisir la fusion directe pour un projet dont la file est utilisée
        ailleurs (état partagé)."""
        cfg = types.SimpleNamespace(radar={})
        out, calls = self._act(cfg, None, shared_rows=[self._queue_row()])
        self.assertNotEqual(out["result"], "merged",
                            "un hôte sans radar.projects.nexlink a fusionné en direct")
        calls["_merge_pr"].assert_not_called()
        calls["_queue"].assert_called_once()

    def test_two_hosts_choose_the_same_path(self):
        declaring = types.SimpleNamespace(radar={"projects": {"nexlink": {
            "repo": "/srv/nexlink", "github": "manaty/nexlink", "target": "develop",
            "land": "local", "gates": ["true"]}}})
        silent = types.SimpleNamespace(radar={})
        out_declaring, _ = self._act(declaring, self._project(declaring))
        out_silent, calls_silent = self._act(silent, None,
                                             shared_rows=[self._queue_row()])
        self.assertEqual(out_declaring["result"], "queued")
        self.assertEqual(out_silent["result"], "queued",
                         "la voie dépend encore de l'hôte qui prend le bail")
        calls_silent["_merge_pr"].assert_not_called()

    def test_unknown_landing_never_opens_a_pr_or_merges(self):
        """Sans déclaration du projet ni trace de sa file, le feu s'abstient :
        aucune PR (donc aucune CI), aucune fusion directe."""
        cfg = types.SimpleNamespace(radar={})
        out, calls = self._act(cfg, None)
        self.assertEqual(out["result"], "no-land")
        self.assertIn("landing mode", out["detail"])
        calls["_open_pr"].assert_not_called()
        calls["_merge_pr"].assert_not_called()
        calls["_queue"].assert_not_called()

    def test_other_declared_project_does_not_authorize_the_queue(self):
        """Un hôte qui déclare un autre projet refuse : ses portes de
        atterrissage ne sont pas celles du lot (#1467, garde de profil)."""
        cfg = types.SimpleNamespace(radar={"projects": {"ameesh": {
            "repo": "/srv/ameesh", "github": "manaty/ameesh", "land": "local",
            "gates": ["true"]}}})
        out, calls = self._act(cfg, None, shared_rows=[self._queue_row()])
        self.assertEqual(out["result"], "no-land")
        calls["_queue"].assert_not_called()
        calls["_merge_pr"].assert_not_called()

    def test_land_off_is_still_a_direct_merge(self):
        """Une déclaration positive `land: off` garde la fusion directe."""
        cfg = types.SimpleNamespace(radar={"projects": {"nexlink": {
            "repo": "/srv/nexlink", "github": "manaty/nexlink", "target": "develop",
            "land": "off"}}})
        out, calls = self._act(cfg, self._project(cfg))
        self.assertEqual(out["result"], "merged")
        calls["_merge_pr"].assert_called_once()
        calls["_queue"].assert_not_called()

    def test_shared_queue_wins_over_missing_or_off_local_land(self):
        for mode in ({}, {"land": "off"}):
            with self.subTest(mode=mode):
                cfg = types.SimpleNamespace(radar={"projects": {"nexlink": {
                    "repo": "/srv/nexlink", "github": "manaty/nexlink", **mode}}})
                out, calls = self._act(cfg, self._project(cfg), shared_rows=[self._queue_row()])
                self.assertEqual(out["result"], "queued")
                calls["_merge_pr"].assert_not_called()

    def test_missing_land_without_queue_abstains(self):
        cfg = types.SimpleNamespace(radar={"projects": {"nexlink": {
            "repo": "/srv/nexlink", "github": "manaty/nexlink"}}})
        out, calls = self._act(cfg, self._project(cfg))
        self.assertEqual(out["result"], "no-land")
        calls["_merge_pr"].assert_not_called()

    def test_non_direct_paths_disarm_before_any_other_action(self):
        for rows in ([], [self._queue_row()]):
            for dry_run in (False, True):
                with self.subTest(rows=bool(rows), dry_run=dry_run):
                    out, calls = self._act(types.SimpleNamespace(radar={}), None,
                                           shared_rows=rows, status=_status(armed=True),
                                           dry_run=dry_run)
                    self.assertEqual(out["result"], "would-disarm" if dry_run else "disarmed")
                    self.assertEqual(calls["_disarm"].call_count, 0 if dry_run else 1)
                    for name in ("_open_pr", "_merge_pr", "_queue"):
                        calls[name].assert_not_called()


class LandingPathTest(unittest.TestCase):
    """La décision seule : déclaration du projet, état partagé, ou abstention."""

    def _rows(self, *rows):
        ops = mock.Mock()
        ops.queue.return_value = list(rows)
        return mock.patch.object(fire.storage, "of", return_value=mock.Mock(work=ops))

    def test_local_project_wins_without_shared_state(self):
        cfg = types.SimpleNamespace(radar={"projects": {"p": {
            "repo": "/srv/p", "github": "acme/p", "land": "pr", "gates": ["true"]}}})
        project = fire._project(cfg, None, "acme/p")
        with self._rows():
            path, why = fire.landing_path(cfg, object(), project, None, "acme/p")
        self.assertEqual(path, "queue", why)

    def test_shared_queue_declares_the_path_without_local_config(self):
        cfg = types.SimpleNamespace(radar={})
        with self._rows({"source": "github.com/manaty/nexlink", "state": "queued"}):
            path, why = fire.landing_path(cfg, object(), None, None, "manaty/nexlink")
        self.assertEqual(path, "queue", why)

    def test_queue_source_matches_a_local_checkout_origin(self):
        cfg = types.SimpleNamespace(radar={})
        repo = mock.Mock()
        with mock.patch.object(fire.merge_queue, "source_of",
                               return_value="git.example:2222/team/repo"), \
                self._rows({"source": "git.example:2222/team/repo", "state": "landed"}):
            path, why = fire.landing_path(cfg, object(), None, repo, None)
        self.assertEqual(path, "queue", why)

    def test_no_declaration_and_no_queue_abstains(self):
        cfg = types.SimpleNamespace(radar={})
        with self._rows():
            path, why = fire.landing_path(cfg, object(), None, None, "manaty/nexlink")
        self.assertEqual(path, "unknown", why)
        self.assertIn("#2256", why)

    def test_unreadable_landing_conditions_abstain(self):
        cfg = types.SimpleNamespace(radar={"projects": {"p": {"repo": "/srv/p",
                                                              "land": "bogus"}}})
        with self._rows({"source": "github.com/manaty/nexlink", "state": "queued"}):
            path, why = fire.landing_path(cfg, object(), None, None, "manaty/nexlink")
        self.assertEqual(path, "unknown")
        self.assertIn("cannot read", why)

    def test_unreadable_shared_queue_cannot_authorize_direct_merge(self):
        cfg = types.SimpleNamespace(radar={"projects": {"p": {
            "repo": "/srv/p", "github": "acme/p", "land": "off"}}})
        with mock.patch.object(fire.storage, "of", side_effect=RuntimeError("unavailable")):
            path, _ = fire.landing_path(cfg, object(), fire._project(cfg, None, "acme/p"),
                                        None, "acme/p")
        self.assertEqual(path, "unknown")


if __name__ == "__main__":
    unittest.main()
