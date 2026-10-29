# SPDX-License-Identifier: AGPL-3.0-only
"""#1280 — terminer un lot sans branche par `done` : livré sans fusion.

Un lot de relecture après fusion (`source`/`issue_ref`
`review-after-merge:#<parent>`) qui a livré son verdict n'a ni fusion à
enregistrer, ni abandon à assumer, ni remplaçant : `work close --done`. Un lot
de code (avec branche) reste à fusionner : `done` le refuse.

Le titre `REVUE #N` rattache le parent relu, il ne prouve pas : `done` exige le
jalon `verdict` sur ce parent, rendu par le **lecteur de la mission** — son
assigné, ou un relecteur désigné (arbitrage mesh-design 34598, 14/10 ; R
codex3 B1) — et **après l'ouverture de la mission** (R dev4 B1 : la R1 du même
lecteur ne prouve pas une mission R2 de delta) ; sans lui, une relecture
« livrée » ne serait qu'une affirmation, et le verdict d'un autre lecteur ne
prouverait pas cette mission. Les missions
ouvertes à la main hors convention (`R1`/`R2 <harnais> #<parent>`, cas #1249)
suivent les mêmes preuves : pas de branche ni de PR, parent réel, verdict livré
sur le parent par ce lecteur — un verdict `blocked` est une relecture livrée,
pas un GO produit.

La régularisation d'un avis déjà fermé « superseded by » son propre parent
(#1043, #1218, #1249, #1269) garde la fermeture, sa date et son jalon, et ne
journalise que la raison : aucune réouverture, aucune fausse fusion.
"""
from __future__ import annotations

import unittest

from ameesh import registry, storage, work

from .support import PgTestCase


class CloseHowTest(unittest.TestCase):
    """Le libellé d'une fermeture, jamais « superseded by #None »."""

    def test_libelles(self):
        self.assertEqual(work.close_how("done"), "done (delivered without merge)")
        self.assertEqual(work.close_how("superseded", 12), "superseded by #12")
        self.assertEqual(work.close_how("superseded"), "superseded")
        self.assertEqual(work.close_how("superseded", None), "superseded")
        self.assertEqual(work.close_how("abandoned"), "abandoned")
        self.assertEqual(work.close_how(None), "abandoned")


class ManualReviewTitleTest(unittest.TestCase):
    """#1280 : le rattachement des missions manuelles `R1`/`R2 <harnais>
    #<parent>` (cas #1249) est ancré en tête et le parent est le premier
    `#<id>` de l'en-tête ; un titre `R1`/`R2` sans `#<id>` reste une mission
    (parent inconnu, donc refusée), jamais un lot ordinaire."""

    def parent_of(self, title):
        return work._review_parent({"title": title})

    def test_formes_ancrees(self):
        self.assertEqual(self.parent_of("R1 Codex #1239 : reprise du suivi CI"),
                         (True, 1239))
        self.assertEqual(self.parent_of("R1 delta Codex #1037 r2 : registration"),
                         (True, 1037))
        self.assertEqual(self.parent_of("R1 Codex de #1066 sur gel après #964"),
                         (True, 1066))
        self.assertEqual(self.parent_of("R2 Claude #1133 : avis native"),
                         (True, 1133))

    def test_convention_revue_inchangee(self):
        self.assertEqual(self.parent_of("REVUE #55 : relecture après fusion"),
                         (True, 55))

    def test_mission_sans_parent(self):
        self.assertEqual(self.parent_of("R1 Codex : reprise du suivi CI"),
                         (True, None))

    def test_titre_ordinaire(self):
        self.assertEqual(self.parent_of("R2D2 #12 : un lot ordinaire"), (False, None))
        self.assertEqual(self.parent_of("reprise du suivi"), (False, None))


class CloseDoneTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("relecteur", "reader-due", "another-reader"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")
        self.parent = work.add(self.db, title="lot relu", app="ameesh",
                               actor="auteur")

    def review(self, parent=None, **extra):
        parent = parent or self.parent
        ref = "review-after-merge:#%d" % int(parent["id"])
        kwargs = {"assignee": "relecteur"}
        kwargs.update(extra)
        return work.add(self.db, title="relecture après fusion", type="evolution",
                        source=ref, issue_ref=ref, app="ameesh", **kwargs)

    def manual_review(self, parent=None, title=None, **extra):
        """Une mission ouverte à la main, hors convention `REVUE #N`."""
        parent = parent or self.parent
        kwargs = {"assignee": "relecteur"}
        kwargs.update(extra)
        return work.add(self.db, title=title or ("R1 Codex #%d : reprise du suivi CI"
                                                 % int(parent["id"])),
                        source="mail", issue_ref="manaty/ameesh#941",
                        app="ameesh", **kwargs)

    def verdict(self, parent=None, verdict="ok", sha="e" * 40, actor="relecteur"):
        """Le verdict rendu sur le parent relu, preuve exigée par `done`.

        Posé au stockage : ces témoins lisent la garde de clôture, pas la
        porte d'écriture du verdict (#2962)."""
        parent = parent or self.parent
        return storage.of(self.db).work.add_milestone(
            parent["id"], "verdict", sha=sha, actor=actor, verdict=verdict, note="")

    def closed_milestones(self, item_id):
        return [m for m in work.milestones(self.db, item_id) if m["kind"] == "closed"]

    # -- la voie nominale -------------------------------------------------
    def test_done_ferme_un_lot_sans_branche(self):
        lot = work.add(self.db, title="une analyse", app="ameesh")
        out = work.close(self.db, lot["id"], done=True, actor="t")
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["close_reason"], "done")
        self.assertIsNone(out["superseded_by"])
        self.assertEqual([m["kind"] for m in work.milestones(self.db, lot["id"])],
                         ["closed", "requested"])

    def test_done_ferme_un_avis_avec_son_parent(self):
        review = self.review()
        self.verdict()
        work.move(self.db, review["id"], "build")
        work.move(self.db, review["id"], "qa")
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))
        self.assertTrue(any("done" in (e["note"] or "")
                            for e in work.events(self.db, review["id"])))

    def test_done_ferme_un_avis_faussement_merged(self):
        """#938/#1076/#1231 : une fusion déclarée à tort sur un avis (aucun
        code produit) se régularise en `done`, sans réécrire la fusion."""
        review = self.review()
        self.verdict()
        work.close_merged(self.db, review["id"], sha="a" * 40, actor="t",
                          source="sync-merges")
        self.assertEqual(work.get(self.db, review["id"])["state"], "delivered")
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_ferme_un_avis_historique_merged(self):
        """#949/#1041/#1042/#1047/#1068 : les avis ouverts à la main ne portent
        pas la source `review-after-merge:` ; leur titre dit le parent relu, et
        le verdict doit être enregistré sur ce parent (inventaire 14/10)."""
        review = work.add(self.db, title="REVUE #%d : R1 autre famille"
                                          % self.parent["id"],
                          source="mail", issue_ref="manaty/ameesh#1", app="ameesh",
                          assignee="relecteur")
        self.verdict()
        work.close_merged(self.db, review["id"], sha="c" * 40, actor="t",
                          source="sync-merges")
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    # -- les missions manuelles, cas #1249 --------------------------------
    def test_done_ferme_une_mission_manuelle_avec_verdict_blocked(self):
        """#1249 : `R1 Codex #<parent>`, sans branche ni PR, se clôt `done`
        sur le verdict livré du parent — `blocked` compris, qui est une
        relecture livrée et pas un GO produit."""
        review = self.manual_review()
        self.verdict(verdict="blocked", sha="f" * 40)
        out = work.close(self.db, review["id"], done=True, actor="regul")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_rectifie_une_mission_manuelle_superseded_par_son_parent(self):
        """Régularisation du cas #1249 : fermé `superseded by` son parent relu,
        rectifié `done` sans rouvrir ni effacer la fermeture et son jalon."""
        review = self.manual_review()
        self.verdict(verdict="blocked", sha="f" * 40)
        work.close(self.db, review["id"], superseded_by=self.parent["id"],
                   note="clôture administrative du suivi de revue", actor="qa")
        before = work.get(self.db, review["id"])
        before_ms = self.closed_milestones(review["id"])
        out = work.close(self.db, review["id"], done=True, actor="regul")
        after = work.get(self.db, review["id"])
        self.assertEqual((after["state"], after["close_reason"]), ("closed", "done"))
        self.assertIsNone(after["superseded_by"])
        self.assertEqual(after["closed_ts"], before["closed_ts"])
        self.assertEqual([m["id"] for m in self.closed_milestones(review["id"])],
                         [m["id"] for m in before_ms])
        self.assertEqual([m["at"] for m in self.closed_milestones(review["id"])],
                         [m["at"] for m in before_ms])
        self.assertIn("rectification", work.events(self.db, review["id"])[0]["note"])

    def test_done_refuse_une_mission_manuelle_sans_parent(self):
        """Un titre `R1`/`R2` ancré sans `#<id>` reste une mission : parent
        inconnu, donc refus — jamais un lot ordinaire qui se clôt tout seul."""
        review = work.add(self.db, title="R1 Codex : reprise du suivi CI",
                          source="mail", app="ameesh")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("not attached to its parent", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")

    def test_done_refuse_une_mission_manuelle_sans_verdict(self):
        review = self.manual_review()
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")

    def test_done_refuse_une_mission_manuelle_avec_branche(self):
        review = self.manual_review(branch="lot/1249-x", branch_target="main")
        self.verdict()
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("carries branch", str(caught.exception))

    def test_done_refuse_un_lot_avec_pr(self):
        """Une PR rattache le lot au code : il se fusionne, il ne se clôt pas."""
        review = self.manual_review()
        self.verdict()
        work.close_merged(self.db, review["id"], sha="d" * 40, actor="t",
                          source="sync-github", pr_ref="manaty/ameesh#941")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("carries PR", str(caught.exception))

    # -- la preuve est celle du lecteur de la mission (B1) ---------------
    def test_done_refuse_le_verdict_d_un_autre_lecteur(self):
        """R codex3 B1 : un verdict existe sur le parent, mais il est d'un
        autre agent ; la mission assignée à `reader-due` n'est pas prouvée."""
        review = self.review(assignee="reader-due")
        self.verdict(actor="another-reader")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True, actor="reader-due")
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        self.assertIn("reader-due", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")

    def test_done_accepte_le_verdict_du_lecteur_de_la_mission(self):
        """Le verdict rendu par le lecteur assigné à la mission la prouve."""
        review = self.review(assignee="relecteur")
        self.verdict()  # acteur « relecteur »
        out = work.close(self.db, review["id"], done=True, actor="relecteur")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_refuse_un_verdict_anterieur_a_la_mission(self):
        """R dev4 B1 : le verdict que le lecteur a rendu *avant* l'ouverture
        de la mission (sa R1, pour une mission R2 de delta) ne prouve pas
        cette mission ; le verdict rendu ensuite la prouve."""
        self.verdict()                       # la R1, avant la mission
        review = self.review(assignee="relecteur")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True, actor="t")
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")
        self.verdict()                       # la R2, après l'ouverture
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_rectifie_apres_un_verdict_posterieur_a_la_mission(self):
        """La rectification `done` porte la même garde temporelle : un verdict
        antérieur à la mission laisse la fermeture intacte ; un verdict
        postérieur la régularise, sans réouverture."""
        self.verdict()                       # avant la mission
        review = self.review()
        work.close(self.db, review["id"], superseded_by=self.parent["id"], actor="t")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review["id"], done=True, actor="t")
        after = work.get(self.db, review["id"])
        self.assertEqual(after["close_reason"], "superseded")
        self.verdict()                       # après l'ouverture
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_lit_un_verdict_au_dela_des_cent_derniers_jalons(self):
        """La preuve ne se perd pas dans l'historique du parent : un verdict
        postérieur à la mission reste valide même quand plus de cent jalons
        ont été écrits depuis (lecture de tout l'historique)."""
        review = self.review()
        self.verdict()
        for _ in range(101):
            work.milestone(self.db, self.parent["id"], "frozen",
                           sha="a" * 40, actor="t")
        out = work.close(self.db, review["id"], done=True, actor="t")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))

    def test_done_refuse_un_avis_sans_lecteur(self):
        """Sans assigné ni relecteur désigné, aucun verdict ne peut prouver
        *cette* mission : la clôture `done` est refusée."""
        review = self.review(assignee=None)
        self.verdict()
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("aucun lecteur assigné", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")

    def test_done_refuse_la_rectification_sur_le_verdict_d_un_autre(self):
        """La rectification `done` porte la même garde : le verdict du parent
        doit être celui du lecteur de la mission, pas d'un autre."""
        review = self.review(assignee="reader-due")
        self.verdict(actor="another-reader")
        work.close(self.db, review["id"], superseded_by=self.parent["id"], actor="qa")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True, actor="reader-due")
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        after = work.get(self.db, review["id"])
        self.assertEqual(after["close_reason"], "superseded")
        self.assertEqual(int(after["superseded_by"]), int(self.parent["id"]))

    # -- la régularisation d'un avis fermé par son propre parent ----------
    def test_done_rectifie_un_avis_superseded_par_son_parent(self):
        review = self.review()
        self.verdict()
        work.close(self.db, review["id"], superseded_by=self.parent["id"],
                   note="nudge stale", actor="t")
        before = work.get(self.db, review["id"])
        before_ms = self.closed_milestones(review["id"])
        out = work.close(self.db, review["id"], done=True, actor="regul")
        after = work.get(self.db, review["id"])
        self.assertEqual(after["state"], "closed")
        self.assertEqual(after["close_reason"], "done")
        self.assertIsNone(after["superseded_by"])
        # la fermeture, sa date et son jalon sont conservés : pas de réouverture
        self.assertEqual(after["closed_ts"], before["closed_ts"])
        after_ms = self.closed_milestones(review["id"])
        self.assertEqual([m["id"] for m in after_ms], [m["id"] for m in before_ms])
        self.assertEqual([m["at"] for m in after_ms], [m["at"] for m in before_ms])
        note = work.events(self.db, review["id"])[0]["note"]
        self.assertIn("rectification", note)
        self.assertIn("#%d" % self.parent["id"], note)

    def test_done_rectifie_un_avis_historique_par_son_titre(self):
        """#1043/#1218 : un avis ouvert à la main (source `mail`) se régularise
        par le parent nommé dans son titre `REVUE #N`, verdict enregistré."""
        review = work.add(self.db, title="REVUE #%d : R1 delta Codex"
                                          % self.parent["id"],
                          source="mail", issue_ref="manaty/ameesh#800",
                          app="ameesh", assignee="relecteur")
        self.verdict()
        work.close(self.db, review["id"], superseded_by=self.parent["id"], actor="t")
        out = work.close(self.db, review["id"], done=True, actor="regul")
        self.assertEqual((out["state"], out["close_reason"]), ("closed", "done"))
        self.assertIsNone(out["superseded_by"])

    def test_done_ne_rectifie_pas_un_titre_qui_ne_nomme_pas_le_parent(self):
        review = work.add(self.db, title="REVUE #424242 : R1", source="mail",
                          app="ameesh")
        work.close(self.db, review["id"], superseded_by=self.parent["id"], actor="t")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review["id"], done=True, actor="t")

    def test_done_ne_rectifie_pas_un_vrai_remplacant(self):
        """#1203 : un avis remplacé par un autre avis n'est pas régularisé."""
        other_parent = work.add(self.db, title="autre lot relu", app="ameesh")
        other = self.review(other_parent)
        review = self.review()
        work.close(self.db, review["id"], superseded_by=other["id"], actor="t")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review["id"], done=True, actor="t")
        after = work.get(self.db, review["id"])
        self.assertEqual(after["close_reason"], "superseded")
        self.assertEqual(int(after["superseded_by"]), int(other["id"]))

    def test_done_ne_rectifie_pas_un_lot_ordinaire_ferme(self):
        lot = work.add(self.db, title="ordinaire", app="ameesh")
        work.close(self.db, lot["id"], abandoned=True, actor="t")
        with self.assertRaises(work.WorkError):
            work.close(self.db, lot["id"], done=True, actor="t")
        self.assertEqual(work.get(self.db, lot["id"])["close_reason"], "abandoned")

    # -- les refus --------------------------------------------------------
    def test_done_refuse_un_lot_avec_branche(self):
        lot = work.add(self.db, title="du code", app="ameesh",
                       branch="lot/1280-x", branch_target="main")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, lot["id"], done=True)
        self.assertIn("carries branch", str(caught.exception))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_done_refuse_un_avis_sans_parent(self):
        ref = "review-after-merge:#424242"
        lot = work.add(self.db, title="avis orphelin", source=ref, issue_ref=ref,
                       app="ameesh")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, lot["id"], done=True)
        self.assertIn("not attached to its parent", str(caught.exception))

    def test_done_refuse_un_avis_sans_verdict_sur_le_parent(self):
        """Le titre rattache, il ne prouve pas : pas de verdict, pas de `done`."""
        review = self.review()
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        self.assertEqual(work.get(self.db, review["id"])["state"], "intake")

    def test_done_refuse_une_rectification_sans_verdict(self):
        """#1043/#1218/#1269 : la rectification `done` exige le verdict du
        parent ; sans lui, la fermeture reste intacte (aucun geste)."""
        review = self.review()
        work.close(self.db, review["id"], superseded_by=self.parent["id"], actor="t")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, review["id"], done=True)
        self.assertIn("no verdict milestone on its parent", str(caught.exception))
        after = work.get(self.db, review["id"])
        self.assertEqual(after["close_reason"], "superseded")
        self.assertEqual(int(after["superseded_by"]), int(self.parent["id"]))

    def test_done_refuse_un_merged_ordinaire_sans_branche(self):
        lot = work.add(self.db, title="sans branche", app="ameesh")
        work.close_merged(self.db, lot["id"], sha="b" * 40, actor="t")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, lot["id"], done=True)
        self.assertIn("without branch or review parent", str(caught.exception))

    def test_close_exige_une_seule_raison(self):
        lot = work.add(self.db, title="une seule raison", app="ameesh")
        with self.assertRaises(work.WorkError):
            work.close(self.db, lot["id"], abandoned=True, done=True)

    # -- la CLI -----------------------------------------------------------
    def test_cli_close_done_affiche_et_refuse(self):
        review = self.review()
        self.verdict()
        out = self.mesh("work", "close", str(review["id"]), "--done")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("done (delivered without merge)", out.stdout)
        out = self.mesh("work", "show", str(review["id"]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("closed   : done (delivered without merge)", out.stdout)
        lot = work.add(self.db, title="du code", app="ameesh",
                       branch="lot/1280-y", branch_target="main")
        out = self.mesh("work", "close", str(lot["id"]), "--done")
        self.assertEqual(out.returncode, 1)
        self.assertIn("carries branch", out.stderr)
        out = self.mesh("work", "move", str(review["id"]), "closed")
        self.assertEqual(out.returncode, 1)
        self.assertIn("--done", out.stderr)
