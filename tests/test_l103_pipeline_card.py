# SPDX-License-Identifier: AGPL-3.0-only
"""L103 T1 (#4395) : la carte `Pipeline` du canon (`pipelines/*.md`).

Une carte `type: Pipeline` porte un bloc `pipeline:` (alias transitoire
`workflow:`, jamais les deux) de la forme canon d'un bloc `workflow:` de carte
Process ; `canon check` le compile avec l'encodeur de `process_card`. Une carte
fautive fait échouer `canon check` (constat `pipeline-*`, code 1) sans bloquer
aucun agent ; `canon show --json` liste les cartes.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

from ameesh import canon, canon_cli, canon_sync, process_card
from ameesh.process_workflow import WorkflowRefusal

from .support import REPO
from .test_canon import EXAMPLE, _TmpMixin, codes, commit_all, publish, write
from .test_l43_perimetre_hote import HOST, _Canons

ACME = os.path.join(REPO, "examples", "processus", "acme-work-delivery.md")
CHEMIN = "pipelines/acme-work-delivery.md"


def carte(*, cle: str = "pipeline", name: str | None = None,
          responsible: str = "human:alice", extra: str = "") -> str:
    """La carte acme de l'exemple Process, retypée en Pipeline."""
    with open(ACME, encoding="utf-8") as handle:
        texte = handle.read()
    texte = texte.replace("type: Process\n", "type: Pipeline\n", 1)
    texte = texte.replace("responsible: human:alice\n",
                          "responsible: %s\n" % responsible, 1)
    texte = texte.replace("\nworkflow:\n", "\n%s:\n" % cle, 1)
    if name is not None:
        texte = texte.replace("title: ", "name: %s\ntitle: " % name, 1)
    if extra:
        texte = texte.replace("\n---\n", "\n%s\n---\n" % extra.rstrip("\n"), 1)
    return texte


def sans(texte: str, cle: str) -> str:
    return "\n".join(l for l in texte.split("\n") if not l.startswith(cle + ":"))


class PipelineCardTest(_TmpMixin, unittest.TestCase):
    def charge(self, cartes: dict):
        root = os.path.join(self.make_tmp(), "acme")
        shutil.copytree(EXAMPLE, root)
        if canon.git_toplevel(root):
            self.skipTest("le dossier temporaire est dans un dépôt git")
        for chemin, contenu in cartes.items():
            write(root, chemin, contenu)
        loaded = canon.load(root, untrusted=True)
        return loaded, canon.validate(loaded)

    def pipeline_errors(self, found) -> set:
        return {f.code for f in canon.errors(found)}

    def test_type_lu(self):
        self.assertIn("Pipeline", canon.PROFILE_TYPES)
        self.assertIn("Process", canon.PROFILE_TYPES)

    def test_carte_valide_compilee(self):
        loaded, found = self.charge({CHEMIN: carte()})
        self.assertEqual(canon.errors(found), [], [f.to_dict() for f in found])
        [pipeline] = loaded.pipelines
        self.assertTrue(pipeline.valid)
        self.assertEqual(pipeline.name, "acme-work-delivery")
        self.assertIsNotNone(pipeline.artifact)
        self.assertEqual((len(pipeline.artifact.stages), len(pipeline.artifact.transitions),
                          len(pipeline.artifact.types)), (8, 20, 7))
        self.assertEqual(pipeline.artifact.workflow_ref, pipeline.fiche.ref)
        self.assertIs(loaded.pipeline("acme-work-delivery"), pipeline)
        # aucune règle de travail : une carte Pipeline n'est pas une carte Process
        self.assertEqual(loaded.processes, [])

    def test_show_json_liste_la_carte(self):
        loaded, _found = self.charge({CHEMIN: carte(name="livraison")})
        data = json.loads(json.dumps(loaded.to_dict()))
        [item] = data["pipelines"]
        self.assertEqual((item["name"], item["path"], item["valid"], item["initial"],
                          item["transitions"]), ("livraison", CHEMIN, True, "draft", 20))
        self.assertEqual(item["responsible"], "human:alice")

    def test_alias_workflow_accepte(self):
        loaded, found = self.charge({CHEMIN: carte(cle="workflow")})
        self.assertEqual(canon.errors(found), [])
        self.assertTrue(loaded.pipelines[0].valid)

    def test_les_deux_cles_refusees(self):
        texte = carte()
        bloc = texte.split("\npipeline:\n", 1)[1].split("\n---\n", 1)[0]
        texte = texte.replace("\n---\n", "\nworkflow:\n%s\n---\n" % bloc, 1)
        loaded, found = self.charge({CHEMIN: texte})
        erreurs = [f for f in found if f.code == "pipeline-invalid"]
        self.assertTrue(erreurs, sorted(codes(found)))
        self.assertIn("key-conflict", erreurs[0].message)
        self.assertFalse(loaded.pipelines[0].valid)

    def test_bloc_absent_ou_fautif(self):
        cas = {
            "absent": "---\ntype: Pipeline\ntitle: vide\nresponsible: human:alice\n"
                      "updated: 2026-10-28\n---\n",
            "prose": "---\ntype: Pipeline\ntitle: prose\nresponsible: human:alice\n"
                     "updated: 2026-10-28\npipeline: \"une phrase\"\n---\n",
            "garde": carte().replace("guard: stop_work", "guard: inconnue"),
        }
        for nom, texte in cas.items():
            with self.subTest(nom=nom):
                loaded, found = self.charge({"pipelines/%s.md" % nom: texte})
                erreurs = [f for f in found if f.code == "pipeline-invalid"]
                self.assertTrue(erreurs, sorted(codes(found)))
                self.assertEqual(erreurs[0].pipeline, nom)
                self.assertEqual(erreurs[0].severity, canon.ERROR)
                [pipeline] = loaded.pipelines
                self.assertFalse(pipeline.valid)
                self.assertIsNone(pipeline.artifact)
                self.assertTrue(pipeline.error)

    def test_responsable_et_date(self):
        for texte, code in ((sans(carte(), "responsible"), "pipeline-responsible-missing"),
                            (carte(responsible="human:inconnu"),
                             "pipeline-responsible-unresolved"),
                            (carte(responsible="agent:a1"), "pipeline-responsible-unresolved"),
                            (sans(carte(), "updated"), "pipeline-updated-missing"),
                            (carte().replace("updated: 2026-10-21", "updated: hier"),
                             "pipeline-updated-invalid")):
            with self.subTest(code=code):
                loaded, found = self.charge({CHEMIN: texte})
                self.assertIn(code, self.pipeline_errors(found))
                self.assertFalse(loaded.pipelines[0].valid)

    def test_nom_hors_grammaire(self):
        for nom in ("'a b'", "-tiret", "'%s'" % ("x" * 65), "[a, b]"):
            with self.subTest(nom=nom):
                loaded, found = self.charge({CHEMIN: carte(name=nom)})
                self.assertIn("pipeline-name-invalid", self.pipeline_errors(found))
                self.assertFalse(loaded.pipelines[0].valid)

    def test_nom_reserve_par_le_wrapper(self):
        # `ameesh module list` prints the installation evidence before any
        # dispatch: a card of that name would be unreachable (#4968/N4).
        loaded, found = self.charge({CHEMIN: carte(name="list")})
        self.assertIn("pipeline-name-reserved", self.pipeline_errors(found))
        self.assertFalse(loaded.pipelines[0].valid)

    def test_nom_par_defaut_et_doublon(self):
        loaded, found = self.charge({CHEMIN: carte(),
                                     "pipelines/autre.md": carte(name="acme-work-delivery")})
        doublons = [f for f in found if f.code == "pipeline-duplicate"]
        self.assertEqual(len(doublons), 2)
        self.assertTrue(all(not p.valid for p in loaded.pipelines))

    def test_carte_hors_pipelines_refusee(self):
        for n, chemin in enumerate(("agents/acme.md", "processus/acme.md", "pipelines.md")):
            with self.subTest(chemin=chemin):
                loaded, found = self.charge({chemin: carte(name="p%d" % n)})
                self.assertIn("pipeline-path", self.pipeline_errors(found))
                self.assertFalse(loaded.pipelines[0].valid)
                self.assertEqual(loaded.processes, [])

    def test_carte_fautive_ne_bloque_aucun_agent(self):
        texte = sans(carte().replace("guard: stop_work", "guard: inconnue"), "updated")
        loaded, found = self.charge({CHEMIN: texte, "pipelines/sans-titre.md":
                                     sans(carte(name="sans-titre"), "title")})
        erreurs = canon.errors(found)
        self.assertTrue({"pipeline-invalid", "pipeline-updated-missing",
                         "fiche-title-missing"} <= {f.code for f in erreurs})
        self.assertTrue(all(f.pipeline for f in erreurs), [f.to_dict() for f in erreurs])
        blocages = canon.blocking(found)
        self.assertEqual(blocages.global_errors, [])
        self.assertEqual(dict(blocages.by_agent), {})
        self.assertEqual(dict(blocages.by_host), {})
        self.assertEqual(canon_sync.assess(loaded, "atelier", found)[0], canon_sync.CANON_OK)

    def test_frontmatter_illisible_ne_bloque_aucun_agent(self):
        _loaded, found = self.charge({CHEMIN: "---\ntype: Pipeline\ntitle: [\n---\n"})
        erreurs = [f for f in found if f.code == "frontmatter-invalid"]
        self.assertTrue(erreurs, sorted(codes(found)))
        self.assertEqual(erreurs[0].pipeline, CHEMIN)
        self.assertEqual(canon.blocking(found).global_errors, [])

    def test_canon_check_sort_1_sur_carte_fautive(self):
        for texte, attendu in ((carte(), 0),
                               (carte().replace("guard: stop_work", "guard: inconnue"), 1)):
            with self.subTest(attendu=attendu):
                loaded, _found = self.charge({CHEMIN: texte})
                args = argparse.Namespace(host=HOST, json=True, canon=None)
                out = io.StringIO()
                with mock.patch.object(canon_cli, "_load_all", return_value=[loaded]), \
                        mock.patch.object(canon_cli, "_recorded", return_value=None), \
                        contextlib.redirect_stdout(out):
                    code = canon_cli.cmd_check(mock.Mock(host=HOST), args)
                self.assertEqual(code, attendu, out.getvalue())
                data = json.loads(out.getvalue())
                if attendu:
                    self.assertIn("pipeline-invalid",
                                  {f["code"] for f in data["findings"]})
                    self.assertTrue(all(f.get("pipeline") for f in data["findings"]
                                        if f["code"].startswith("pipeline-")))


class PipelineBundleTest(_Canons, unittest.TestCase):
    """Même contrôle de chemin que `processus/` : bundle imbriqué, périmètre."""

    def test_perimetre_declare(self):
        clone = self.acme(scope="ameesh")
        write(clone, "ameesh/pipelines/acme.md", carte(name="acme", responsible="human:bob"))
        write(clone, "ameesh/plan/pipelines/hors.md", carte(name="hors", responsible="human:bob"))
        commit_all(clone, "pipelines")
        loaded = canon.load(clone, default=True)
        found = canon.validate(loaded)
        valides = {p.name: p.valid for p in loaded.pipelines}
        self.assertEqual(valides, {"acme": True, "hors": False})
        self.assertIn("pipeline-path", codes(found, canon.ERROR))
        acme = loaded.pipeline("acme")
        self.assertTrue(acme.fiche.ref.startswith("home:ameesh/pipelines/acme.md@"))
        self.assertEqual(canon_sync.assess(loaded, HOST, found)[0], canon_sync.CANON_OK)

    def test_bundle_imbrique(self):
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "mono")
        shutil.copytree(EXAMPLE, os.path.join(clone, "knowledge"))
        write(clone, "knowledge/pipelines/acme.md", carte(name="acme"))
        commit_all(clone)
        loaded = canon.load(os.path.join(clone, "knowledge"))
        self.assertEqual([p.name for p in loaded.pipelines], ["acme"])
        self.assertTrue(loaded.pipelines[0].valid)
        self.assertNotIn("pipeline-path", codes(canon.validate(loaded), canon.ERROR))

    def test_regle_de_chemin(self):
        ok = canon.process_path_ok
        self.assertTrue(ok("pipelines/a.md", None, canon.PIPELINE_DIR))
        self.assertFalse(ok("processus/a.md", None, canon.PIPELINE_DIR))
        self.assertTrue(ok("ameesh/pipelines/a.md", ["ameesh"], canon.PIPELINE_DIR))
        self.assertFalse(ok("ameesh/pipelines/a.md", None, canon.PIPELINE_DIR))
        # la règle des cartes Process ne change pas
        self.assertTrue(ok("processus/a.md"))
        self.assertFalse(ok("pipelines/a.md"))


class PipelineOfPageTest(unittest.TestCase):
    def test_lecture_et_compilation(self):
        block = process_card.pipeline_of_page(carte())
        artifact, encoding = process_card.compile_block(block, ref="file:x")
        self.assertEqual(len(artifact.stages), 8)
        self.assertEqual(encoding.source_schema, "process-workflow/1")
        self.assertEqual(process_card.pipeline_of_page(carte(cle="workflow")), block)

    def test_refus(self):
        cas = (("---\ntype: Process\ntitle: x\n---\n", "value-type"),
               ("pas de frontmatter", "frontmatter-missing"),
               ("---\ntype: Pipeline\ntitle: x\n---\n", "key-missing"),
               ("---\ntype: Pipeline\ntitle: x\npipeline: texte\n---\n", "value-type"))
        for texte, code in cas:
            with self.subTest(code=code):
                with self.assertRaises(WorkflowRefusal) as ctx:
                    process_card.pipeline_of_page(texte)
                self.assertEqual(ctx.exception.code, code)

    def test_process_check_inchange_sur_une_carte_pipeline(self):
        # `ameesh process check` reste réservé aux cartes Process
        with self.assertRaises(WorkflowRefusal) as ctx:
            process_card.workflow_of_page(carte())
        self.assertEqual(ctx.exception.code, "value-type")


if __name__ == "__main__":
    unittest.main()
