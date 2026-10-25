# SPDX-License-Identifier: AGPL-3.0-only
"""#2585 — pas de `REVUE #N` quand une autre famille a déjà relu la tête.

Un lot léger peut porter avant sa fusion l'avis d'une autre famille sur la
tête qui entre (gel relu, revue désignée). Ouvrir en plus la mission de
relecture après fusion demanderait une relecture déjà rendue. La décision est
lue sur les jalons du lot source : un avis `ok` ferme l'ouverture s'il porte
sur le sha fusionné, ou sur le dernier gel **prouvé entré** par Git dans la
fusion (`plan_git.probe`), d'une famille **tracée à l'instant du verdict**
(`turn_costs`, jamais le registre courant), connue et autre que celle de
l'auteur. Un avis bloquant, une famille inconnue, un sha différent ou une
preuve Git absente ne dispensent de rien.

Deux niveaux : la règle seule (magasin simulé), puis les jalons réels sur
Postgres — ouverture par `open_after_merge` / `open_for_merge`, preuve Git sur
un dépôt réel (gel A relu, tête B fusionnée, puis A réellement fusionné), et
rattrapage borné (`catch_up`) qui marque le candidat terminal au lieu de le
retenter.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from ameesh import registry, review_lot, storage, work

from .support import PgTestCase, patch_families


class FakeOps:
    """Le magasin `work` en mémoire, pour la règle seule."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.notes: list = []
        self.added: list = []
        self.events_rows: dict = {}
        self.milestones_rows: dict = {}
        self.next_id = 900

    def open_by_ref(self, ref):
        return [i for i in self.items.values()
                if i.get("issue_ref") == ref
                and i.get("state") not in ("merged", "promoted", "closed")]

    def by_issue_ref(self, ref):
        return [i for i in self.items.values() if i.get("issue_ref") == ref]

    def events(self, item_id, limit):
        return self.events_rows.get(int(item_id), [])

    def milestones(self, item_id, limit=100):
        return self.milestones_rows.get(int(item_id), [])

    def note(self, item_id, state, text, actor):
        self.notes.append((int(item_id), text, actor))

    def add(self, **kw):
        self.next_id += 1
        item = dict(kw, id=self.next_id, state="intake", assignee=None)
        self.items[self.next_id] = item
        self.added.append(item)
        return item


LOT = 2585
SHA = "a" * 40
MERGE_COMMIT = "d" * 40


class RuleTest(unittest.TestCase):
    """La règle : un avis d'une autre famille couvre la tête, une seule fois."""

    def setUp(self):
        # #2664 : les familles viennent du canon compilé, jamais d'une liste
        # locale ; ce banc sans canon Git épingle les trois familles connues.
        patch_families(self)
        self.item = {"id": LOT, "title": "un chemin léger déjà relu", "app": "ameesh",
                     "assignee": "a1", "state": "merged", "branch_target": "main",
                     "type": "evolution"}
        self.ops = FakeOps({LOT: self.item})
        # #1053 : la famille de l'auteur vient du gel (tours tracés), pas du
        # registre courant ; ici `_turns_harness` rend codex.
        self.ops.milestones_rows[LOT] = [
            {"id": 1, "kind": "frozen", "sha": SHA, "actor": "a1",
             "at_ts": 1_700_000_000.0}]
        # #2585/B1 : la famille du relecteur est celle du tour au verdict,
        # jamais le registre courant (qui peut avoir tourné depuis).
        self.harness = {
            "a1": "codex",
            "r-claude": "claude",
            "r-claude2": "claude",
            "r-deepseek": "deepseek",
            "r-codex": "codex",
        }
        # le registre courant, distinct de la trace des tours : une rotation
        # peut l'avoir fait changer depuis le verdict.
        self.agents = {name: {"harness": value}
                       for name, value in self.harness.items()}
        store = mock.Mock(work=self.ops)
        for patch in (
            mock.patch.object(storage, "of", return_value=store),
            mock.patch.object(work, "get",
                              side_effect=lambda db, n: self.ops.items.get(int(n))),
            mock.patch.object(work, "add", side_effect=lambda db, **kw: self.ops.add(**kw)),
            mock.patch.object(review_lot, "_turns_harness",
                              side_effect=lambda db, agent, at_ts, **kw:
                              self.harness.get(agent, "")),
            mock.patch.object(registry, "get",
                              side_effect=lambda db, name: self.agents.get(name)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, klass=None, **kw):
        return review_lot.open_after_merge(
            None, lot=LOT,
            klass=klass if klass is not None else {"class": "light", "why": "policy of ameesh"},
            **kw)

    def gate_klass(self):
        return {"class": "light", "why": "policy of ameesh",
                "files": [{"path": "src/ameesh/review_lot.py", "class": "light"}]}

    def verdict(self, actor, sha, kind="ok"):
        self.ops.milestones_rows[LOT].append(
            {"id": 100 + len(self.ops.milestones_rows[LOT]), "kind": "verdict",
             "sha": sha, "actor": actor, "verdict": kind, "at_ts": 1_700_000_100.0})

    def test_un_avis_ok_d_une_autre_famille_n_ouvre_pas(self):
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha=SHA)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)
        self.assertEqual([entry["actor"] for entry in out["reviewed_by"]], ["r-claude"])
        self.assertIn("déjà rendu", out["why"])
        self.assertEqual(self.ops.added, [])

    def test_un_avis_de_la_famille_de_l_auteur_n_ouvre_pas_de_dispense(self):
        self.verdict("r-codex", SHA)
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)
        self.assertEqual(len(self.ops.added), 1)

    def test_un_avis_sur_une_autre_tete_n_ouvre_pas_de_dispense(self):
        self.verdict("r-claude", "b" * 40)
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)

    def test_un_avis_bloquant_n_ouvre_pas_de_dispense(self):
        self.verdict("r-claude", SHA, "blocked")
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)

    def test_un_avis_de_famille_inconnue_ne_prouve_rien(self):
        self.verdict("mystere", SHA)
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)

    def test_sans_preuve_git_un_avis_sur_le_gel_ne_couvre_pas_la_fusion(self):
        """B1 cx1 : le gel A relu et une tête B différente fusionnée. Sans
        preuve Git de l'entrée de A dans B, l'avis sur A ne dispense pas de la
        REVUE sur B (le test avec dépôt réel est plus bas)."""
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha=MERGE_COMMIT)
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_un_jalon_merged_ancien_n_elargit_pas_la_tete_examinee(self):
        """Le sha de fusion constaté fait foi : un jalon `merged` égal au gel
        ne dispense pas une tête C différente passée par l'appelant."""
        self.ops.milestones_rows[LOT].append(
            {"id": 50, "kind": "merged", "sha": SHA, "actor": "a1",
             "at_ts": 1_700_000_050.0})
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha=MERGE_COMMIT)
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_sans_sha_de_fusion_aucune_preuve_garde_la_relecture(self):
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha="")
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_famille_du_relecteur_lue_au_verdict_pas_au_registre(self):
        """B1/P2 cx1 : le registre dit claude, mais l'avis a été rendu sous
        codex — la famille de l'auteur (codex) ne compte donc pas."""
        self.verdict("r-claude", SHA)
        self.harness["r-claude"] = "codex"
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)

    def test_avis_rendu_sous_une_autre_famille_compte_apres_rotation(self):
        """Inverse : le registre courant dit codex (la famille de l'auteur),
        mais l'avis a été rendu sous claude — il reste indépendant."""
        self.verdict("r-claude", SHA)
        self.agents["r-claude"] = {"harness": "codex"}
        out = self.open(merged_sha=SHA)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)

    def test_un_avis_abrege_couvre_la_tete(self):
        self.verdict("r-claude", SHA[:12])
        out = self.open(merged_sha=SHA)
        self.assertFalse(out["created"], out)

    def test_un_lot_critique_garde_la_relecture_tant_qu_un_avis_manque(self):
        self.verdict("r-claude", "b" * 40)
        out = self.open(self.gate_klass(), merged_sha="b" * 40)
        self.assertTrue(out["created"], out)
        self.assertEqual(out["reviews"], 2, out)

    def test_deux_avis_de_familles_distinctes_dispensent_un_lot_critique(self):
        self.verdict("r-claude", "b" * 40)
        self.verdict("r-deepseek", "b" * 40)
        out = self.open(self.gate_klass(), merged_sha="b" * 40)
        self.assertFalse(out["created"], out)
        self.assertEqual(sorted(entry["family"] for entry in out["reviewed_by"]),
                         ["claude", "deepseek"])

    def test_deux_avis_de_la_meme_famille_ne_suffisent_pas(self):
        self.verdict("r-claude", "b" * 40)
        self.verdict("r-claude2", "b" * 40)
        out = self.open(self.gate_klass(), merged_sha="b" * 40)
        self.assertTrue(out["created"], out)

    def test_un_auteur_sans_famille_ne_dispense_pas(self):
        """Sans gel ni tour, la famille de l'auteur n'est pas prouvée :
        l'avis existe, mais « une autre famille » ne l'est pas."""
        self.ops.milestones_rows[LOT] = []
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha=SHA)
        self.assertTrue(out["created"], out)

    def test_un_auteur_humain_accepte_un_avis_d_une_famille_connue(self):
        """Un auteur humain n'a pas de famille de modèle : toute famille
        connue est indépendante de lui."""
        self.item["assignee"] = "human:smichea"
        self.ops.milestones_rows[LOT] = []
        self.verdict("r-claude", SHA)
        out = self.open(merged_sha=SHA)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)


class ReviewAlreadyDoneDbTest(PgTestCase):
    """Les jalons réels : avis au sha fusionné, ouverture, propagation."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host)
        registry.upsert(self.db, "codex1", harness="codex", host=self.cfg.host)
        registry.upsert(self.db, "deepseek2", harness="deepseek", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger déjà relu", app="ameesh",
                            assignee="a1")["id"]
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 minute')",
                        ("a1", "deepseek"))
        work.milestone(self.db, self.lot, "frozen", sha=SHA, actor="a1")

    def verdict(self, actor, sha=SHA, kind="ok", harness="codex"):
        # #2585/B1 : la famille du relecteur est lue sur ses tours autour du
        # verdict, jamais sur le registre courant.
        if harness:
            self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                            " VALUES (%s, %s, now() - interval '1 second')",
                            (actor, harness))
        work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor, verdict=kind)

    def reviews(self):
        return storage.of(self.db).work.by_issue_ref(review_lot.ref_of(self.lot))

    def open(self, merged_sha=SHA, **kw):
        return review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
            merged_sha=merged_sha, **kw)

    def test_un_avis_ok_d_une_autre_famille_n_ouvre_pas(self):
        self.verdict("codex1")
        out = self.open()
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)
        self.assertEqual([entry["actor"] for entry in out["reviewed_by"]], ["codex1"])
        self.assertEqual(self.reviews(), [])
        events = storage.of(self.db).work.events(self.lot, 50)
        self.assertTrue(any(str(e.get("note") or "").startswith(
            review_lot.TERMINAL_PREFIX + str(self.lot)) for e in events), events)

    def test_un_avis_de_la_famille_de_l_auteur_ouvre_la_relecture(self):
        self.verdict("deepseek2", harness="deepseek")
        out = self.open()
        self.assertTrue(out["created"], out)
        self.assertEqual(len(self.reviews()), 1)

    def test_un_avis_bloquant_ouvre_la_relecture(self):
        self.verdict("codex1", kind="blocked")
        out = self.open()
        self.assertTrue(out["created"], out)

    def test_famille_du_relecteur_lue_au_verdict_pas_au_registre(self):
        """B1/P2 cx1 : le registre dit claude, mais l'avis a été rendu sous
        codex, la famille de l'auteur — il ne compte pas."""
        registry.upsert(self.db, "codex1", harness="claude", host=self.cfg.host)
        self.verdict("codex1", harness="codex")
        with mock.patch.object(review_lot, "_author_at_freeze",
                               return_value={"name": "a1", "harness": "codex"}):
            out = self.open()
        self.assertTrue(out["created"], out)

    def test_avis_rendu_sous_une_autre_famille_compte_apres_rotation(self):
        """Inverse : le registre courant dit codex (la famille de l'auteur),
        mais l'avis a été rendu sous claude — il reste indépendant."""
        registry.upsert(self.db, "codex1", harness="codex", host=self.cfg.host)
        self.verdict("codex1", harness="claude")
        with mock.patch.object(review_lot, "_author_at_freeze",
                               return_value={"name": "a1", "harness": "codex"}):
            out = self.open()
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)

    def test_sans_preuve_git_un_avis_sur_le_gel_ne_couvre_pas_un_commit(self):
        """B1 cx1, témoin négatif sur les jalons réels : sans dépôt où prouver
        l'entrée du gel A dans le commit B, la REVUE reste due."""
        self.verdict("codex1", sha=SHA)
        out = self.open(merged_sha=MERGE_COMMIT)
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)
        self.assertEqual(len(self.reviews()), 1)

    def test_open_for_merge_propage_l_avis_deja_rendu(self):
        self.verdict("codex1")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "policy"}):
            out = review_lot.open_for_merge(self.db, self.lot, merged_sha=SHA)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)
        self.assertEqual([entry["actor"] for entry in out["reviewed_by"]], ["codex1"])
        self.assertEqual(self.reviews(), [])

    def test_la_fusion_ne_dit_pas_une_relecture_non_ouverte(self):
        """#2585 : le constat de fusion ne présente pas une relecture déjà
        rendue comme manquante."""
        self.verdict("codex1")
        declared = mock.patch.object(work, "_declared_merge_commit",
                                     side_effect=lambda item, sha, **kw: sha)
        declared.start()
        self.addCleanup(declared.stop)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "policy"}):
            out = work.merged(self.db, self.lot, sha=SHA, actor="a1")
        self.assertEqual(out["result"], "merged", out)
        self.assertNotIn("relecture non ouverte", out["detail"])
        self.assertTrue(out["review_lot"]["already_reviewed"], out["review_lot"])
        self.assertEqual(self.reviews(), [])


class GitProofDbTest(PgTestCase):
    """B1 cx1 : le gel A relu ne couvre la fusion que si Git prouve son entrée.

    Un dépôt réel : gel A sur une branche, tête B différente sur la cible
    (témoin négatif), puis A réellement fusionnée (témoin positif)."""

    def _git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        proc = subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, name):
        self.write(name, name + "\n")
        self._git("add", name)
        self._git("commit", "-q", "-m", "ajoute " + name)
        return self._git("rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "t")
        self._git("config", "user.email", "t@example.invalid")
        self.commit("socle")
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host)
        registry.upsert(self.db, "codex1", harness="codex", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger déjà relu",
                            app="ameesh", assignee="a1")["id"]
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 minute')",
                        ("a1", "deepseek"))
        self._git("checkout", "-q", "-b", "lot/2585-A")
        self.frozen = self.commit("A")
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="a1")
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 second')",
                        ("codex1", "codex"))
        work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                       actor="codex1", verdict="ok")

    def open(self, merged_sha, target=""):
        return review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
            merged_sha=merged_sha, repo=self.repo, target=target)

    def test_un_gel_A_ne_couvre_pas_une_tete_B_differente(self):
        self._git("checkout", "-q", "main")
        merged = self.commit("B")
        out = self.open(merged)
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_un_gel_A_prouve_entre_dans_la_fusion_ne_cree_rien(self):
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", "merge A", "lot/2585-A")
        merged = self._git("rev-parse", "HEAD")
        out = self.open(merged)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)
        self.assertEqual([entry["actor"] for entry in out["reviewed_by"]], ["codex1"])

    def test_une_tete_X_avancee_apres_le_gel_ne_dispense_pas(self):
        """B1 cx1 : le gel A relu, puis X ajouté sur la branche et fusionné.
        A est un ancêtre du commit de fusion, jamais la tête mergée : la REVUE
        reste due."""
        self._git("checkout", "-q", "lot/2585-A")
        self.commit("X")
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", "merge X", "lot/2585-A")
        merged = self._git("rev-parse", "HEAD")
        out = self.open(merged, target="main")
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_la_voie_pr_avec_une_tete_avancee_ne_dispense_pas(self):
        """B1 dev1 : voie `pr`, `merged_sha` = tête du lot C (A+C), A ancêtre
        de C. L'ancêtre ne prouve pas le delta : la REVUE reste due."""
        self._git("checkout", "-q", "lot/2585-A")
        head = self.commit("X")
        out = self.open(head, target="main")
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_une_resolution_qui_ajoute_du_contenu_ne_dispense_pas(self):
        """B1 cx1/dev1 : une fusion propre du gel A, puis une résolution qui
        ajoute un fichier change le delta du commit de fusion — REVUE due."""
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "--no-commit", "lot/2585-A")
        self.write("resolution", "contenu ajouté par la résolution\n")
        self._git("add", "resolution")
        self._git("commit", "-q", "-m", "resolution")
        merged = self._git("rev-parse", "HEAD")
        out = self.open(merged, target="main")
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"], out)

    def test_un_cherry_pick_exact_du_gel_dispense(self):
        """Équivalence validée : le gel A est rejoué tel quel sur une cible
        avancée (même delta, autre commit) — la relecture le couvre."""
        self._git("checkout", "-q", "main")
        self.commit("B")
        self._git("cherry-pick", self.frozen)
        merged = self._git("rev-parse", "HEAD")
        out = self.open(merged, target="main")
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)

    def test_open_for_merge_transmet_le_depot_a_la_preuve(self):
        """Le dépôt du relevé arrive jusqu'à la preuve Git, pas seulement à la
        classe : sans transmission, un gel A entré dans un commit de fusion
        serait rouvert par le rattrapage."""
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", "merge A", "lot/2585-A")
        merged = self._git("rev-parse", "HEAD")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "policy"}):
            out = review_lot.open_for_merge(self.db, self.lot,
                                            merged_sha=merged, repo=self.repo)
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)


class CatchUpAlreadyDoneDbTest(PgTestCase):
    """#2585 au rattrapage : un candidat déjà relu est terminal, pas retenté."""

    def setUp(self):
        super().setUp()
        # #3426 : le rattrapage ne tente un lot que sur un hôte qui peut
        # nommer un dépôt (ici le dossier de l'assigné) ; la classe est
        # doublée par le témoin, le contenu du dossier importe peu.
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        registry.upsert(self.db, "codex1", harness="codex", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger déjà relu", app="ameesh",
                            assignee="a1")["id"]
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 minute')",
                        ("a1", "deepseek"))
        work.milestone(self.db, self.lot, "frozen", sha=SHA, actor="a1")
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 second')",
                        ("codex1", "codex"))
        work.milestone(self.db, self.lot, "verdict", sha=SHA, actor="codex1",
                       verdict="ok")
        # la voie déclarée est neutralisée : seule la fusion et le rattrapage
        # sont l'objet du témoin.
        declared = mock.patch.object(work, "_declared_merge_commit",
                                     side_effect=lambda item, sha, **kw: sha)
        declared.start()
        self.addCleanup(declared.stop)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "sensitive", "why": "témoin"}):
            self.assertEqual(work.merged(self.db, self.lot, sha=SHA,
                                         actor="a1")["result"], "merged")
        self.assertEqual(storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot)), [])

    def test_catch_up_marque_terminal_et_ne_rouvre_pas(self):
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "policy"}):
            out = review_lot.catch_up(self.db)
        self.assertEqual((out["examined"], out["missing"], out["reviewed"],
                          out["terminal"], out["opened"]),
                         (1, 1, 1, 1, 0), out)
        self.assertEqual(storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot)), [])
        events = storage.of(self.db).work.events(self.lot, 50)
        self.assertTrue(any(str(e.get("note") or "").startswith(
            review_lot.TERMINAL_PREFIX + str(self.lot)) for e in events), events)
        # la marque terminale écarte le candidat du balayage suivant
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "policy"}):
            again = review_lot.catch_up(self.db)
        self.assertEqual(again["examined"], 0, again)


if __name__ == "__main__":
    unittest.main()
