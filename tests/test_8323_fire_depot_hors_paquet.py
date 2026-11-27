# SPDX-License-Identifier: AGPL-3.0-only
"""#8323 : le feu d'un lot dont la PR vise une cible déclarée sans paquet.

#8095 : lot nexlink (issue manaty/nexlink) dont la PR vit dans manaty-its et
vise son `main`. La garde du paquet (#2333) lisait le dépôt de l'issue et
refusait au nom du §11 bis, alors qu'aucun paquet ne porte manaty-its/main.
Le dépôt réel de la PR est celui que la marque du gel nomme
(`ameesh-freeze-repo`). Une cible n'échappe au paquet que sur une
déclaration **positive** (`radar.projects.<p>.packets = false`, même `github`,
même `target`) ; le feu lui applique alors les gardes du §2 et atterrit en
direct (`land = off`). Toute absence ou illisibilité refuse (relecture
ameesh-dev3 / ameesh-cx2 de 99864bef).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import fire, packet_guard, radar

A = "a" * 40
ITS = {"repo": "/srv/manaty-its", "github": "manaty/manaty-its", "target": "main",
       "land": "off", "packets": False}
NEXLINK = {"repo": "/srv/nexlink", "github": "manaty/nexlink", "target": "develop",
           "land": "local", "gates": ["true"]}
ITEM = {"id": 8095, "title": "t", "state": "validated", "branch": "codex/home-server-console",
        "branch_target": "main", "issue_ref": "manaty/nexlink#4841", "app": "nexlink",
        "assignee": ""}


def _cfg(**projects):
    return types.SimpleNamespace(radar={"projects": projects},
                                 github={"projects": {"ameesh": "manaty/ameesh",
                                                      "nexlink": "manaty/nexlink"}})


def _freeze(repo: str = "manaty/manaty-its") -> dict:
    note = "PR manaty/manaty-its#8" + ("\nameesh-freeze-repo: %s" % repo if repo else "")
    return {"kind": "frozen", "sha": A, "actor": "codex1", "at_ts": 1.0, "id": 1,
            "note": note}


def _check(repo, target="main", cfg=None, source="codex/home-server-console"):
    return packet_guard.check_merge(None, 8095, source, target, A, repo=repo,
                                    cfg=_cfg(its=ITS, nexlink=NEXLINK) if cfg is None else cfg)


class RadarDeclarationTest(unittest.TestCase):
    def test_packets_defaults_to_true_and_false_needs_github_and_target(self):
        self.assertTrue(radar.parse_settings({"projects": {"n": NEXLINK}}).projects["n"].packets)
        self.assertFalse(radar.parse_settings({"projects": {"i": ITS}}).projects["i"].packets)
        for bad in ({"packets": "no"}, {"packets": False, "github": ""},
                    {"packets": False, "target": ""}):
            with self.subTest(bad=bad), self.assertRaises(radar.SettingsError):
                radar.parse_settings({"projects": {"i": dict(ITS, **bad)}})


class PacketGuardDeclarationTest(unittest.TestCase):
    def test_a_target_declared_without_packet_gets_the_section_2_gates(self):
        out = _check("manaty/manaty-its")
        self.assertTrue(out["allowed"])
        self.assertIn("§2", out["why"])
        self.assertFalse(out["journaled"])

    def test_absence_of_declaration_refuses(self):
        """Relecture ameesh-dev3 B/P1 : un dépôt absent d'une liste locale
        partielle n'est jamais une preuve."""
        cases = {
            "nexlink, github.projects partial, no radar": (
                "manaty/nexlink", "develop", types.SimpleNamespace(
                    github={"projects": {"ameesh": "manaty/ameesh"}})),
            "nexlink, radar declares only its": ("manaty/nexlink", "develop", _cfg(its=ITS)),
            "canon repository, nothing declared": ("manaty/home", "main", _cfg()),
            "its without radar": ("manaty/manaty-its", "main", types.SimpleNamespace()),
            "its declared with packets": ("manaty/manaty-its", "main",
                                          _cfg(its=dict(ITS, packets=True))),
            "its, another target": ("manaty/manaty-its", "develop", _cfg(its=ITS)),
            "its, a second declaration keeps packets": (
                "manaty/manaty-its", "main",
                _cfg(its=ITS, its2=dict(ITS, packets=True, repo="/srv/its2"))),
            "unreadable repository": ("", "main", _cfg(its=ITS)),
            "unreadable radar": ("manaty/manaty-its", "main",
                                 types.SimpleNamespace(radar={"projects": {"its": 3}})),
            "no cfg": ("manaty/manaty-its", "main", None),
        }
        for label, (repo, target, cfg) in cases.items():
            with self.subTest(label), self.assertRaises(packet_guard.PacketGuardError) \
                    as caught:
                packet_guard.check_merge(None, 8095, "lot/8095-x", target, A,
                                         repo=repo, cfg=cfg)
            self.assertIn("§11 bis", str(caught.exception))


class PacketCarrierVetoTest(unittest.TestCase):
    """Relecture ameesh-dev3 de 90e65bd2 : `packets = false` ne lève jamais le
    paquet d'un dépôt porteur, et un porteur non identifiable fait douter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8323-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _clone(self, name, url):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        for argv in (["init", "-q"], ["remote", "add", "origin", url]):
            subprocess.run(["git", "-C", path, *argv], check=True, capture_output=True)
        return path

    def test_a_packet_repository_cannot_be_declared_without_packet(self):
        for repo, target, github in (("manaty/ameesh", "main", {"ameesh": "manaty/ameesh"}),
                                     ("manaty/nexlink", "develop",
                                      {"nexlink": "manaty/nexlink"})):
            cfg = types.SimpleNamespace(
                github={"projects": github},
                radar={"projects": {"p": {"repo": "/srv/p", "github": repo,
                                          "target": target, "land": "off",
                                          "packets": False}}})
            with self.subTest(repo=repo), self.assertRaises(packet_guard.PacketGuardError):
                packet_guard.check_merge(None, 8323, "lot/8323-x", target, A,
                                         repo=repo, cfg=cfg)

    def test_a_carrier_declared_by_its_clone_only_vetoes(self):
        alias = {"repo": self._clone("its", "git@github.com:manaty/manaty-its.git"),
                 "target": "main"}
        with self.assertRaises(packet_guard.PacketGuardError):
            _check("manaty/manaty-its", cfg=_cfg(its=ITS, alias=alias))

    def test_an_unidentified_carrier_refuses(self):
        for label, alias in (("no origin", {"repo": os.path.join(self.tmp, "missing"),
                                            "target": "main"}),):
            with self.subTest(label), self.assertRaises(packet_guard.PacketGuardError):
                _check("manaty/manaty-its", cfg=_cfg(its=ITS, alias=alias))

    def test_a_carrier_of_another_repository_does_not_veto(self):
        alias = {"repo": self._clone("am", "git@github.com:manaty/ameesh.git"),
                 "target": "main"}
        self.assertTrue(_check("manaty/manaty-its", cfg=_cfg(its=ITS, alias=alias))["allowed"])


class FireRepositoryTest(unittest.TestCase):
    def test_the_freeze_mark_names_the_pr_repository(self):
        self.assertEqual(fire._slug(_cfg(), ITEM, None, [_freeze()]), "manaty/manaty-its")

    def test_without_a_mark_the_issue_still_names_it(self):
        for marks in (None, [], [_freeze("")]):
            with self.subTest(marks=marks):
                self.assertEqual(fire._slug(_cfg(), ITEM, None, marks), "manaty/nexlink")

    def _give(self, marks, cfg):
        with mock.patch.object(fire.work, "get", return_value=dict(ITEM)), \
                mock.patch.object(fire.work, "milestones", return_value=marks), \
                mock.patch.object(fire, "review_checkout", return_value=None), \
                mock.patch.object(fire.work, "milestone",
                                  side_effect=lambda db, lot, kind, **kw: dict(kw, kind=kind)):
            return fire._give_locked(None, 8095, sha=A, actor="orchestrateur", cfg=cfg)

    def test_witness_a_nexlink_lot_whose_pr_targets_another_repository(self):
        row = self._give([_freeze()], _cfg(its=ITS, nexlink=NEXLINK))
        self.assertEqual((row["kind"], row["sha"]), ("fire", A))

    def test_the_same_lot_is_refused_without_the_declaration_or_the_mark(self):
        cases = {"no declaration": ([_freeze()], _cfg(nexlink=NEXLINK)),
                 "frozen in nexlink": ([_freeze("manaty/nexlink")], _cfg(its=ITS)),
                 "no mark": ([_freeze("")], _cfg(its=ITS))}
        for label, (marks, cfg) in cases.items():
            with self.subTest(label), self.assertRaises(fire.FireError) as caught:
                self._give(marks, cfg)
            self.assertIn("§11 bis", str(caught.exception))


def _status(**extra):
    row = {"number": 8, "state": "OPEN", "head": A, "base": "main", "draft": False,
           "ci": "green", "merged": False, "merge_commit": "", "armed": False,
           "url": "https://github.com/manaty/manaty-its/pull/8"}
    row.update(extra)
    return row


class FirePassageTest(unittest.TestCase):
    """Relecture ameesh-cx2 / ameesh-dev3 B/P2 : le parcours complet du passage,
    garde, voie d'atterrissage et portes du §2, jusqu'à la fusion directe."""

    def _act(self, cfg, *, marks=None, status=None, verdicts=(True, ""), pr=(True, ""),
             assignee_checkout=None):
        marks = marks or [_freeze(), {"kind": "fire", "sha": A, "actor": "orchestrateur",
                                      "at_ts": 2.0, "id": 2}]
        ops = mock.Mock()
        ops.queue.return_value = []
        names = ("_repo_of", "_pr_status", "_repo_info", "ready_verdicts", "ready_pr",
                 "_merge_pr", "_reconcile", "_queue", "_open_pr", "_leave_queue",
                 "_fresh", "_renew", "_target_lock", "_unlock", "_passive", "_wake",
                 "_disarm", "_invalidate", "review_checkout")
        row = dict(ITEM, state="validated")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched, \
                mock.patch.object(fire.storage, "of", return_value=mock.Mock(work=ops)):
            patched["_repo_of"].return_value = assignee_checkout
            patched["review_checkout"].return_value = None
            patched["_pr_status"].return_value = status or _status()
            patched["_repo_info"].return_value = {"slug": "manaty/manaty-its",
                                                  "default_branch": "main", "merge": True}
            patched["ready_verdicts"].return_value = (*verdicts, {"class": "sensitive"})
            patched["ready_pr"].return_value = pr
            patched["_merge_pr"].return_value = (True, "PR merged")
            patched["_reconcile"].return_value = {"lot": 8095, "result": "merged"}
            patched["_invalidate"].return_value = {"lot": 8095, "result": "invalidated"}
            patched["_disarm"].return_value = {"lot": 8095, "result": "disarmed"}
            patched["_leave_queue"].return_value = False
            patched["_fresh"].return_value = (row, marks)
            patched["_renew"].return_value = True
            patched["_target_lock"].return_value = (None, False)
            out = fire._act(cfg, None, row, marks, gh=lambda *a, **k: None,
                            dry_run=False, repo=None, cache={})
        return out, patched

    def test_the_declared_target_lands_directly_under_the_section_2_gates(self):
        out, calls = self._act(_cfg(its=ITS, nexlink=NEXLINK))
        self.assertEqual(out["slug"], "manaty/manaty-its")
        self.assertEqual(out["landing"], "direct")
        calls["_pr_status"].assert_called()
        self.assertEqual(calls["_pr_status"].call_args.args[0], "manaty/manaty-its")
        calls["ready_verdicts"].assert_called()
        calls["ready_pr"].assert_called()
        calls["_queue"].assert_not_called()
        calls["_merge_pr"].assert_called_once()
        self.assertIn(A, repr(calls["_merge_pr"].call_args))

    def test_a_foreign_assignee_checkout_is_set_aside(self):
        """Relecture ameesh-dev3 de 9976aaae : le clone nexlink de l'assigné
        ne choisit ni le projet ni la voie quand la PR vit dans manaty-its."""
        tmp = tempfile.mkdtemp(prefix="ameesh-8323-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for argv in (["init", "-q"],
                     ["remote", "add", "origin", "git@github.com:manaty/nexlink.git"]):
            subprocess.run(["git", "-C", tmp, *argv], check=True, capture_output=True)
        out, calls = self._act(_cfg(its=ITS, nexlink=NEXLINK), assignee_checkout=tmp)
        self.assertEqual((out["slug"], out["project"], out["landing"]),
                         ("manaty/manaty-its", "its", "direct"))
        self.assertNotEqual(out["repo"], tmp)
        calls["_queue"].assert_not_called()
        calls["_merge_pr"].assert_called_once()

    def test_the_section_2_gates_still_hold(self):
        for label, kw in (("reviews", {"verdicts": (False, "no review verdict ok")}),
                          ("green CI", {"pr": (False, "CI not green on the frozen head")})):
            with self.subTest(label):
                out, calls = self._act(_cfg(its=ITS, nexlink=NEXLINK), **kw)
                calls["_merge_pr"].assert_not_called()

    def test_without_the_declaration_nothing_merges(self):
        for label, cfg in (("no radar entry", _cfg(nexlink=NEXLINK)),
                           ("declared with packets", _cfg(its=dict(ITS, packets=True)))):
            with self.subTest(label):
                out, calls = self._act(cfg)
                calls["_merge_pr"].assert_not_called()

    def test_a_pr_retargeted_on_a_packet_target_is_refused_by_the_guard(self):
        """La garde lit la base réelle de la PR : retargetée sur develop (non
        déclarée sans paquet), elle est refusée au nom du §11 bis."""
        out, calls = self._act(_cfg(its=ITS, nexlink=NEXLINK), status=_status(base="develop"))
        calls["_merge_pr"].assert_not_called()
        self.assertIn("§11 bis", out.get("detail", "") + out.get("why", ""))


class FireCommandTest(unittest.TestCase):
    """`ameesh work fire 8095 --sha …` sans `--repo` (relecture ameesh-dev3 de
    9976aaae) : le checkout choisi d'office est vérifié contre le dépôt de la
    PR ; le clone nexlink de l'assigné n'atteint ni le feu ni le passage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8323-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _clone(self, name, url):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        for argv in (["init", "-q"], ["remote", "add", "origin", url]):
            subprocess.run(["git", "-C", path, *argv], check=True, capture_output=True)
        return path

    def _run(self, checkout, repo_arg=None):
        args = types.SimpleNamespace(id=8095, sha=A, withdraw=False, repo=repo_arg,
                                     actor="orchestrateur", note="", exception="",
                                     reason="", json=False, dry_run=False)
        row = dict(ITEM, assignee="codex1")
        with mock.patch.object(fire.work, "get", return_value=row), \
                mock.patch.object(fire.work, "milestones", return_value=[_freeze()]), \
                mock.patch.object(fire.work, "corrector", return_value="orchestrateur"), \
                mock.patch("ameesh.registry.get", return_value={"cwd": checkout}), \
                mock.patch.object(os, "getcwd", return_value=checkout), \
                mock.patch.object(fire, "give", return_value={"sha": A}) as give, \
                mock.patch.object(fire, "run_pass", return_value=[]) as passage:
            fire.run(_cfg(its=dict(ITS, repo=os.path.join(self.tmp, "absent")),
                          nexlink=dict(NEXLINK, repo=os.path.join(self.tmp, "absent"))),
                     None, args)
        return give.call_args.kwargs["repo"], passage.call_args.kwargs["repo"]

    def test_the_nexlink_checkout_of_the_assignee_is_set_aside(self):
        nexlink = self._clone("nexlink", "git@github.com:manaty/nexlink.git")
        self.assertEqual(self._run(nexlink), (None, None))

    def test_a_checkout_of_the_pr_repository_serves_both_steps(self):
        its = self._clone("its", "git@github.com:manaty/manaty-its.git")
        self.assertEqual(self._run(its), (its, its))

if __name__ == "__main__":
    unittest.main()
