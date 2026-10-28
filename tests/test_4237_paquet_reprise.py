# SPDX-License-Identifier: AGPL-3.0-only
"""#4237 : une composition perdue à la relève du bail est marquée et reprise.

Constat h-06 (#4189, 28/10) : une recomposition du pilote lancée hors du tic
(`paquet-auto.py --force --exclude`) a eu ses tests verts, puis son `packet
record` refusé à la relève du bail (GestureRefused) ; rien n'était poussé ni
marqué, le nouveau détenteur n'avait rien à reprendre. Témoins :

- paquet-auto : un refus de geste après des tests verts garde la tête testée
  (réf réservée poussée, marque au journal du porteur) ; `--publish` vérifie
  la chaîne (base = tête de la cible, une fusion par unité, second parent =
  source enregistrée, manifeste seul au-dessus) et publie sans retester, ou
  sort en 3 ;
- paquet-horaire : la relève entre les tests et l'enregistrement fait publier
  la tête testée par le tic du nouvel hôte, sans recomposer ; une chaîne
  refusée recompose dans le même tic ; `recompose` pose la marque du pilote
  (hôte, pid, époque, motifs), reprise par le tic.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_3807_paquet_auto import paquet_auto as auto
from tests.test_3845_paquet_horaire import (
    HEAD, HEAD2, NOW, Base, FakeShell, carrier, ph, red_run)

REFUSAL = ("gesture packet refused for mesh-design: this session is fenced by the "
           "lease epoch (epoch 289 in the registry, 288 in the environment)")
NAME = "h-20261027-04"


def git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class Chain(unittest.TestCase):
    """Un dépôt jetable : deux lots composés sur `main`, puis le manifeste."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4237-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        # les worktrees jetables de paquet-auto restent sous ce banc
        scratch = root / "tmp"
        scratch.mkdir()
        env = mock.patch.dict(os.environ, {"TMPDIR": str(scratch)})
        env.start()
        self.addCleanup(env.stop)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = root / "repo"
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        for key, value in (("user.name", "banc"), ("user.email", "banc@example.invalid"),
                           ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self.base = self.commit("base.txt", "base\n", "base")
        git(self.repo, "push", "-q", "origin", "main")
        self.sources = {}
        for lot in (1, 2):
            git(self.repo, "switch", "-q", "-c", "lot/%d" % lot, self.base)
            self.sources[lot] = self.commit("lot%d.txt" % lot, "%d\n" % lot, "#%d" % lot)
        git(self.repo, "switch", "-q", "-c", "integration/paquet-" + NAME, self.base)
        for lot in (1, 2):
            git(self.repo, "merge", "-q", "--no-ff", "-m", "composition : #%d" % lot,
                self.sources[lot])
        self.rel = auto.manifest_rel(NAME)
        table = "\n".join("| %d | #%d | `%s` | `x` | composition |"
                          % (lot, lot, self.sources[lot][:12]) for lot in (1, 2))
        self.head = self.commit(self.rel, (
            "# Paquet\n\n## Composition (2 unités)\n\n"
            "| unité | lot(s) | source | commit | note |\n|---|---|---|---|---|\n"
            "%s\n\n## Contrôles locaux\n\n- Tests locaux : 3 run(s) verts.\n" % table),
            "paquet : manifeste")
        git(self.repo, "switch", "-q", "--detach", self.base)
        git(self.repo, "branch", "-D", "integration/paquet-" + NAME)

    def commit(self, name, content, message):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def candidates(self, **override):
        sources = dict(self.sources, **override)
        return [auto.Candidate(lot, sha) for lot, sha in sources.items()]


class CheckChainTest(Chain):
    def test_chaine_publiable(self):
        specs = auto.check_chain(self.repo, self.head, self.base, self.rel, self.candidates())
        self.assertEqual([lots for lots, _ in specs], [[1], [2]])

    def test_cible_bougee_refusee(self):
        moved = self.commit("autre.txt", "x\n", "cible bougée")
        with self.assertRaisesRegex(auto.ChainRefused, "autre base"):
            auto.check_chain(self.repo, self.head, moved, self.rel, self.candidates())

    def test_tete_gelee_changee_refusee(self):
        regel = [auto.Candidate(1, self.sources[1]), auto.Candidate(2, self.base)]
        with self.assertRaisesRegex(auto.ChainRefused, "#2 : source enregistrée"):
            auto.check_chain(self.repo, self.head, self.base, self.rel, regel)

    def test_lot_plus_pret_refuse(self):
        with self.assertRaisesRegex(auto.ChainRefused, "#2 n'est plus prêt"):
            auto.check_chain(self.repo, self.head, self.base, self.rel,
                             [auto.Candidate(1, self.sources[1])])

    def test_fusion_aux_parents_conformes_mais_arbre_altere_refusee(self):
        """Témoin de la relecture ameesh-cx1 (B1) : amend de la dernière fusion
        avec injected.txt, deux parents conservés, manifeste recommité."""
        last = git(self.repo, "rev-parse", self.head + "^")
        git(self.repo, "switch", "-q", "--detach", last)
        (self.repo / "injected.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--amend", "--no-edit")
        self.assertEqual(len(git(self.repo, "rev-list", "--parents", "-n", "1",
                                 "HEAD").split()), 3)
        git(self.repo, "checkout", "-q", self.head, "--", self.rel)
        altered = self.commit(self.rel, git(self.repo, "show", "%s:%s" % (self.head, self.rel))
                              + "\n", "paquet : manifeste")
        with self.assertRaisesRegex(auto.ChainRefused, "arbre différent"):
            auto.check_chain(self.repo, altered, self.base, self.rel, self.candidates())

    def test_commit_en_trop_sur_le_manifeste_refuse(self):
        git(self.repo, "switch", "-q", "--detach", self.head)
        extra = self.commit("glisse.txt", "x\n", "glissé")
        with self.assertRaisesRegex(auto.ChainRefused, "seul commit du manifeste"):
            auto.check_chain(self.repo, extra, self.base, self.rel, self.candidates())


class KeepTestedTest(Chain):
    def test_tete_poussee_sous_la_ref_reservee_et_marquee(self):
        notes = []

        def run(cmd, **_kw):
            notes.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"ok", b"")

        with mock.patch.object(auto, "run_command", side_effect=run):
            ref = auto.keep_tested(self.repo, 4000, NAME, self.base, self.head,
                                   sender="mesh-design")
        self.assertEqual(ref, auto.TESTED_REF_PREFIX + NAME)
        self.assertEqual(git(self.origin, "rev-parse", ref), self.head)
        cmd = notes[0]
        self.assertEqual(cmd[:4], ["ameesh", "work", "note", "4000"])
        self.assertEqual(cmd[-2:], ["--actor", "mesh-design"])
        match = auto.TESTED_RE.match(cmd[4])
        self.assertEqual(match.groups(), (self.base, self.head, ref))


class EpochLostTest(unittest.TestCase):
    def test_record_refuse_par_la_garde_est_une_perte_de_bail(self):
        refused = subprocess.CompletedProcess([], 1, b"", REFUSAL.encode())
        with mock.patch.object(auto, "run_command", return_value=refused):
            with self.assertRaises(auto.EpochLost):
                auto.record_packet(pathlib.Path("."), 4000, "a", "b", "br", "main")
        other = subprocess.CompletedProcess([], 1, b"", b"head not on branch")
        with mock.patch.object(auto, "run_command", return_value=other):
            with self.assertRaises(auto.PaquetAutoError) as caught:
                auto.record_packet(pathlib.Path("."), 4000, "a", "b", "br", "main")
        self.assertNotIsInstance(caught.exception, auto.EpochLost)

    def compose(self, refuse_after_tests):
        """#4304 : poussée, gel et ci-full avant les tests par unité ; la garde
        refuse (`EpochLost`) avant les tests ou après des tests verts."""
        tmp = tempfile.TemporaryDirectory(prefix="paquet-4237-")
        self.addCleanup(tmp.cleanup)
        worktree = pathlib.Path(tmp.name) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text("", encoding="utf-8")
        composition = auto.Composition(units=[auto.Unit(1, "a" * 40, "b" * 40)],
                                       discarded=[], head="c" * 40)
        green = auto.TestRun("unité #1", ["tests.test_x"], 0, "Ran 1 test\nOK", 0.1)
        args = argparse.Namespace(
            repo=tmp.name, name=NAME, target="main", carrier=4000, base=None,
            local=False, no_packet_record=False, keep_worktree=False,
            keep_failed_hours=24.0, force=True, tests_timeout=10, tests_parallel=1,
            pilot="mesh-design", sender="mesh-design", exclude=[3801],
            exclude_reason=[])
        tested = []

        def guard(*_args):
            if tested or not refuse_after_tests:
                raise auto.EpochLost(REFUSAL)

        def tests(*_args, **_kwargs):
            tested.append(True)
            return [green], [], []

        patches = dict(
            validate_target=None, validate_branch=None,
            fresh_base="e" * 40, remote_branch=None, ensure_carrier_branch=None,
            clear_local_branch=None, prune_stale_worktrees=[], make_worktree=worktree,
            compute_candidates=([auto.Candidate(1, "a" * 40)], {"ready": [1]}),
            compose_candidates=composition, run_freeze_checks=(0, "ok", 0.1),
            composition_table="## Composition\n", write_manifest="d" * 40,
            record_packet="", run_inclusion=(0, "ok"), pull_request_state=None,
            ensure_pull_request=(7, "http://pr"), freeze_carrier="",
            request_full_ci="", note_carrier="", send_mail=None,
            remove_worktree=None)
        with mock.patch.multiple(auto, **{k: mock.DEFAULT for k in patches}) as mocks, \
                mock.patch.object(auto, "check_epoch", side_effect=guard), \
                mock.patch.object(auto, "check_local_tests", side_effect=tests), \
                mock.patch.object(auto, "push_branch") as push, \
                mock.patch.object(auto, "keep_tested") as keep:
            for key, value in patches.items():
                mocks[key].return_value = value
            with self.assertRaises(auto.EpochLost):
                auto.compose_packet(args)
        return tmp, keep, push, mocks

    def test_refus_apres_tests_verts_garde_la_tete_testee(self):
        """La passe 4189d : tests verts, geste suivant refusé → tête gardée."""
        tmp, keep, push, mocks = self.compose(refuse_after_tests=True)
        keep.assert_called_once()
        self.assertEqual(keep.call_args.args, (pathlib.Path(tmp.name).resolve(), 4000,
                                               NAME, "e" * 40, "d" * 40))
        self.assertIn("verts", keep.call_args.kwargs["tests"])
        self.assertEqual(keep.call_args.kwargs["sender"], "mesh-design")
        # #4304 : la tête était poussée et gelée avant ses tests ; la fin est due
        push.assert_called_once()
        mocks["freeze_carrier"].assert_called_once()
        mocks["send_mail"].assert_not_called()

    def test_refus_avant_les_tests_ne_garde_rien(self):
        _tmp, keep, push, _mocks = self.compose(refuse_after_tests=False)
        keep.assert_not_called()
        push.assert_not_called()


class PublishTest(Chain):
    def args(self, **extra):
        values = dict(repo=str(self.repo), name=NAME, target="main", carrier=4000,
                      publish=auto.TESTED_REF_PREFIX + NAME, publish_sha=self.head,
                      local=False,
                      no_packet_record=False, keep_worktree=False, pilot="mesh-design",
                      sender="mesh-design")
        values.update(extra)
        return argparse.Namespace(**values)

    def finishing(self):
        """Les gestes de fin (#4304 : poussée, enregistrement et gel déjà faits)."""
        return mock.patch.multiple(
            auto, check_epoch=mock.DEFAULT, compute_candidates=mock.DEFAULT,
            pull_request_state=mock.DEFAULT, ensure_pull_request=mock.DEFAULT,
            note_carrier=mock.DEFAULT, request_full_ci=mock.DEFAULT,
            send_mail=mock.DEFAULT, record_packet=mock.DEFAULT,
            push_branch=mock.DEFAULT, freeze_carrier=mock.DEFAULT)

    def push(self, args, branch_head=None):
        git(self.repo, "push", "-q", "origin", "%s:%s" % (self.head, args.publish))
        git(self.repo, "push", "-q", "-f", "origin", "%s:refs/heads/%s"
            % (branch_head or self.head, auto.package_branch(NAME)))

    def publish(self, args, candidates=None, branch_head=None):
        self.push(args, branch_head)
        with self.finishing() as mocks:
            mocks["compute_candidates"].return_value = (candidates or self.candidates(), {})
            mocks["pull_request_state"].return_value = "OPEN"
            mocks["ensure_pull_request"].return_value = (7, "http://pr")
            mocks["note_carrier"].return_value = ""
            mocks["request_full_ci"].return_value = ""
            rc = auto.publish_tested(args)
        return rc, mocks

    def test_publie_sans_retester(self):
        with mock.patch.object(auto, "run_tests") as tests, \
                mock.patch.object(auto, "run_freeze_checks") as checks:
            rc, mocks = self.publish(self.args())
        self.assertEqual(rc, 0)
        tests.assert_not_called()
        checks.assert_not_called()
        # #4304 : déjà poussée, enregistrée et gelée par le détenteur précédent
        for done in ("record_packet", "push_branch", "freeze_carrier"):
            mocks[done].assert_not_called()
        body = mocks["ensure_pull_request"].call_args.args[4]
        self.assertIn("non rejoués", body)
        self.assertIn("#1 `%s`" % self.sources[1][:12], body)
        self.assertIn("non rejoués", mocks["note_carrier"].call_args.args[2])
        self.assertEqual(mocks["request_full_ci"].call_args.args[1:], (4000, self.head))
        self.assertIn("PR #7 http://pr", mocks["send_mail"].call_args.args[2])
        # la réf réservée est retirée une fois publiée
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.origin), "rev-parse", "--verify", "--quiet",
             auto.TESTED_REF_PREFIX + NAME], capture_output=True).returncode, 1)

    def test_cible_bougee_sort_en_3(self):
        self.commit("autre.txt", "x\n", "cible bougée")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.push(self.args())
        with self.finishing() as mocks:
            mocks["compute_candidates"].return_value = (self.candidates(), {})
            rc = auto.main(["--repo", str(self.repo), "--name", NAME, "--carrier", "4000",
                            "--publish", self.args().publish, "--publish-sha", self.head])
        self.assertEqual(rc, auto.PUBLISH_REFUSED)
        mocks["ensure_pull_request"].assert_not_called()
        mocks["send_mail"].assert_not_called()

    def test_ref_deplacee_hors_de_la_tete_attestee_refusee(self):
        # la réf réservée est mobile : elle désigne une autre tête que la marque
        with self.assertRaisesRegex(auto.ChainRefused, "la marque de tests atteste"):
            self.publish(self.args(publish_sha="f" * 40))

    def test_branche_du_paquet_deplacee_refusee(self):
        # la branche poussée ne désigne plus la tête testée : rien n'est fini
        with self.assertRaisesRegex(auto.ChainRefused, "pas la tête testée"):
            self.publish(self.args(), branch_head=self.base)

    def test_publish_refuse_exclude_et_local(self):
        for extra in (["--local"], ["--exclude", "3"], []):
            self.assertEqual(auto.main(["--repo", str(self.repo), "--name", NAME,
                                        "--carrier", "4000", "--publish", "x",
                                        *(extra and ["--publish-sha", "a" * 40]),
                                        *extra]), 2)


# ---------------------------------------------------------------------------
# paquet-horaire
# ---------------------------------------------------------------------------

TESTED_REF = auto.TESTED_REF_PREFIX + NAME


class Shell(FakeShell):
    def __init__(self):
        super().__init__()
        self.publish_rc = 0
        self.epoch_out = "289\n"

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        if len(cmd) > 1 and cmd[1] == "-c":
            self.calls.append(list(cmd))
            return ph.Result(0, self.epoch_out)
        if len(cmd) > 1 and str(cmd[1]).endswith("paquet-auto.py") and "--publish" in cmd:
            self.calls.append(list(cmd))
            if self.publish_rc:
                return ph.Result(self.publish_rc, "", "tête testée non publiable : base")
            self.items[4000]["freeze"] = {"declared": {"sha": HEAD2}}
            return ph.Result(0, "chaîne vérifiée : 2 unité(s)\n")
        return super().run(cmd, cwd=cwd, input_text=input_text, timeout=timeout)


class HoraireBase(Base):
    def setUp(self):
        super().setUp()
        self.shell = Shell()
        self.ctx.shell = self.shell
        self.ctx.epoch = 289

    def autos(self):
        return [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)]

    def lost(self, *after, step="composition", base="-", extra=""):
        """Tentative d'agents-1 (époque 288), tests verts, record refusé."""
        item = carrier(sha="" if base == "-" else base, created=NOW - 3600)
        notes = [ph.MARK_ATTEMPT + "%s n=1 base=%s par=agents-1:42%s epoque=288"
                 % (step, base, extra),
                 "%sbase=%s tête=%s réf=%s" % (auto.TESTED_MARK, "e" * 40, HEAD2, TESTED_REF)]
        notes += list(after)
        item["events"] = [{"note": n, "created_ts": NOW - 3000 + i}
                          for i, n in enumerate(notes)]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item


class ResumeTestedTest(HoraireBase):
    def test_releve_entre_tests_et_record_publie_sans_recomposer(self):
        """Témoin du lot : le tic du nouvel hôte reprend la tête testée."""
        item = self.lost(ph.MARK_FAILED + "composition n=1 base=- rc=1")
        self.assertIn("ci-full demandée sur %s" % HEAD2[:12], ph.tick(self.ctx))
        autos = self.autos()
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0][autos[0].index("--publish") + 1], TESTED_REF)
        self.assertEqual(autos[0][autos[0].index("--publish-sha") + 1], HEAD2)
        self.assertNotIn("--force", autos[0])
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertTrue(tried[-1].startswith(ph.MARK_ATTEMPT + "composition n=2 base=- "))
        self.assertIn(" epoque=289 publie=%s@%s" % (TESTED_REF, HEAD2), tried[-1])
        self.assertEqual(len(self.shell.ran("ci", "run")), 1)

    def test_refus_de_garde_apres_tests_verts_publie_sans_recomposer(self):
        """#4245 + #4304 : le refus de garde est non concluant ; la reprise du
        tic suivant finit la tête testée gardée au lieu de recomposer."""
        item = self.lost(ph.MARK_FENCED + "composition n=1 base=- époque=288")
        ph.tick(self.ctx)
        autos = self.autos()
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0][autos[0].index("--publish") + 1], TESTED_REF)
        self.assertEqual(autos[0][autos[0].index("--publish-sha") + 1], HEAD2)
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        # même numéro : le refus de garde n'use pas de tentative
        self.assertTrue(tried[-1].startswith(ph.MARK_ATTEMPT + "composition n=1 base=- "))
        self.assertIn(" publie=%s@%s" % (TESTED_REF, HEAD2), tried[-1])

    def test_ancien_processus_mort_sans_marque_d_echec(self):
        self.lost()
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        self.assertIn("--publish", self.autos()[0])

    def test_chaine_refusee_recompose_dans_le_meme_tic(self):
        item = self.lost()
        self.shell.publish_rc = auto.PUBLISH_REFUSED
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        autos = self.autos()
        self.assertEqual(len(autos), 2)
        self.assertNotIn("--publish", autos[1])
        self.assertIn("--force", autos[1])
        self.assertIn(ph.MARK_FAILED + "composition n=2 base=- rc=3", ph.marks(item))
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertTrue(tried[-1].startswith(ph.MARK_ATTEMPT + "composition n=3 "))

    def test_publication_morte_se_reprend_en_recomposant(self):
        item = self.lost(ph.MARK_ATTEMPT + "composition n=2 base=- par=agents-2:7 "
                         "epoque=289 publie=%s@%s" % (TESTED_REF, HEAD2),
                         ph.MARK_FAILED + "composition n=2 base=- rc=1")
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        self.assertNotIn("--publish", self.autos()[0])
        self.assertIn("--force", self.autos()[0])
        found = ph.attempts(item)
        self.assertEqual((found[1].publish, found[2].publish),
                         ("%s@%s" % (TESTED_REF, HEAD2), ""))

    def test_tete_testee_non_poussee_non_publiee(self):
        item = self.lost()
        item["events"][1]["note"] = item["events"][1]["note"].replace(TESTED_REF, "-")
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        self.assertNotIn("--publish", self.autos()[0])

    def test_tete_testee_d_une_tentative_anterieure_ignoree(self):
        item = self.lost()
        item["events"][1]["created_ts"] = NOW - 4000
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        self.assertNotIn("--publish", self.autos()[0])


class PilotRecomposeTest(HoraireBase):
    def frozen(self, *notes):
        item = carrier(sha=HEAD, created=NOW - 3600)
        item["events"] = [{"note": n, "created_ts": NOW - 3000 + i}
                          for i, n in enumerate(notes)]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def test_marque_du_pilote_puis_paquet_auto(self):
        self.ctx.epoch = 0
        item = self.frozen(ph.MARK_CI + HEAD)
        ph.pilot_recompose(self.ctx, 4000, [3801], ["B de relecture (#3801)"])
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertEqual(len(tried), 1)
        mark = tried[0]
        self.assertTrue(mark.startswith(ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=%s:%d "
                                        "run=pilote sans=3801 epoque=289 motifs="
                                        % (HEAD, self.ctx.host, self.ctx.pid)), mark)
        found = ph.attempts(item)[0]
        self.assertEqual((found.epoch, found.exclude, found.reasons),
                         (289, [3801], ["B de relecture (#3801)"]))
        cmd = self.autos()[0]
        self.assertIn("--force", cmd)
        self.assertEqual(cmd[cmd.index("--exclude") + 1], "3801")
        self.assertEqual(cmd[cmd.index("--exclude-reason") + 1], "B de relecture (#3801)")

    def test_recomposition_du_pilote_perdue_reprise_avec_ses_motifs(self):
        item = self.frozen(
            ph.MARK_CI + HEAD,
            ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=agents-1:42 run=pilote "
            "sans=3801 epoque=288 motifs=%s"
            % (HEAD, json.dumps(["B de relecture"], ensure_ascii=False)))
        self.shell.auto_out = "aucune unité composée\n"
        ph.tick(self.ctx)
        cmd = self.autos()[0]
        self.assertEqual(cmd[cmd.index("--exclude-reason") + 1], "B de relecture")
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertIn("run=pilote sans=3801 epoque=289", tried[-1])

    def test_recomposition_du_pilote_n_use_pas_le_retrait_automatique(self):
        item = self.frozen(
            ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=agents-1:42 run=pilote "
            "sans= epoque=288" % ("c" * 40),
            ph.MARK_CI + HEAD)
        self.assertEqual(ph.attempts(item)[0].exclude, [])
        self.shell.items[3801] = {"id": 3801, "assignee": "mty-amh-impl-02"}
        self.shell.ci[4000] = {"runs": [red_run()]}
        ph.tick(self.ctx)
        cmd = self.autos()[0]
        self.assertEqual(cmd[cmd.index("--exclude") + 1], "3801")
        self.assertEqual(cmd[cmd.index("--exclude-reason") + 1], ph.EXCLUDE_CI_FULL_REASON)

    def test_refus_hors_porteur_horaire_ou_tentative_vivante(self):
        self.frozen()
        self.shell.items[4000]["title"] = "Paquet manuel"
        with self.assertRaises(ph.InputError):
            ph.pilot_recompose(self.ctx, 4000, [3801], [])
        self.frozen(ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=agents-2:7 run=77 "
                    "sans=3801 epoque=289" % HEAD)
        with self.assertRaisesRegex(ph.InputError, "en cours"):
            ph.pilot_recompose(self.ctx, 4000, [3807], [])
        with self.assertRaisesRegex(ph.InputError, "motif"):
            ph.pilot_recompose(self.ctx, 4000, [3807], ["a", "b"])
        self.assertEqual(self.autos(), [])

    def test_session_hors_bail_refusee_avant_toute_marque(self):
        self.ctx.epoch = 0
        item = self.frozen()
        self.shell.run = lambda cmd, **kw: (ph.Result(1, "", REFUSAL) if cmd[1] == "-c"
                                            else Shell.run(self.shell, cmd, **kw))
        with self.assertRaisesRegex(ph.HoraireError, "bail non vérifié"):
            ph.pilot_recompose(self.ctx, 4000, [3801], [])
        self.assertEqual(ph.marks(item), [])

    def test_sous_commande_recompose_au_parseur(self):
        # ni clone réel ni canon compilé : un banc (#4044) n'a ni `.git` ni config
        clone = tempfile.TemporaryDirectory(prefix="paquet-4237-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        zone = mock.Mock(timezone="Europe/Paris")
        with mock.patch.object(ph, "pose_git_identity"), \
                mock.patch.object(ph, "check_fresh"), mock.patch.object(ph, "align_schema"), \
                mock.patch("ameesh.config.load", return_value=None), \
                mock.patch("ameesh.mechanics.configured", return_value=zone), \
                mock.patch.object(ph, "pilot_recompose", return_value="ok") as called, \
                mock.patch.object(ph, "Shell", return_value=self.shell):
            rc = ph.main(["recompose", "--repo", clone.name, "--carrier",
                          "4000", "--exclude", "3801", "--exclude-reason", "B"])
        self.assertEqual(rc, 0)
        self.assertEqual(called.call_args.args[1:], (4000, [3801], ["B"]))

if __name__ == "__main__":
    unittest.main()
