# SPDX-License-Identifier: AGPL-3.0-only
"""#538 PROCESS-INJECT : les fiches type Process du canon (`processus/*.md`)
sont lues, validées par `canon check`, injectées en tête du brief de chaque
agent (nouvelle session, rotation, reprise) et, après `canon sync`, une
modification est poussée aux agents de l'hôte pour leur prochain tour.

Attendu (bureau → mesh-design, 2026-10-12 23:39, courrier 14173) :
(1) `PROFILE_TYPES` reconnaît `Process`, avec `title`, `updated` et
    `responsible` obligatoires ; `canon check` signale une fiche Process
    invalide, ou une règle dont `expire:` est dépassé ;
(2) le texte des fiches est court (moins de deux pages), sinon un dépassement
    est signalé ; il ouvre le brief déterministe et le résumé de rotation ;
(3) une modification d'une fiche Process, après `canon sync`, est poussée aux
    agents de l'hôte (message `event`) pour leur prochain tour.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import shutil
import unittest
from unittest import mock

from ameesh import canon, canon_sync, mail, registry, reprise
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
from .test_canon import EXAMPLE, _TmpMixin, codes, commit_all, fiche, publish, write

RACINE = "processus/lots.md"
TITRE = "Processus de lot"


def carte(body: str = "# Processus de lot\n\n- une règle de travail\n", **fields) -> str:
    """Une fiche Process minimale : frontmatter + corps (les règles)."""
    champs = {"type": "Process", "title": TITRE,
              "description": "Règles de travail des agents",
              "responsible": "human:alice", "updated": "2026-10-12"}
    champs.update(fields)
    return fiche(**champs) + "\n" + body + "\n"


def carte_moins(contenu: str, cle: str) -> str:
    """La fiche sans la ligne de frontmatter `cle:` (pour tester l'obligation)."""
    return "\n".join(l for l in contenu.split("\n") if not l.startswith(cle + ":"))


def _hier() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _demain() -> str:
    return (dt.date.today() + dt.timedelta(days=1)).isoformat()


class ProcessLectureTest(_TmpMixin, unittest.TestCase):
    """(1) lecture et validation des fiches Process."""

    def charge(self, contenu: str, **kwargs):
        root = self.example_copy()
        write(root, RACINE, contenu)
        return canon.load(root, untrusted=True, **kwargs), root

    def test_une_fiche_process_est_lue_avec_son_texte(self):
        loaded, _root = self.charge(carte(
            body="# Processus de lot\n\n- une règle de travail\n", expire=_demain()))
        self.assertEqual(len(loaded.processes), 1)
        regle = loaded.processes[0]
        self.assertEqual(regle.title, TITRE)
        self.assertEqual(regle.updated, "2026-10-12")
        self.assertEqual(regle.responsible, "human:alice")
        self.assertEqual(regle.expire, _demain())
        self.assertIn("une règle de travail", regle.body)
        self.assertTrue(regle.valid)
        self.assertEqual(codes(canon.validate(loaded)), {"canon-untrusted"})

    def test_le_type_process_est_dans_les_types_lus(self):
        self.assertIn("Process", canon.PROFILE_TYPES)

    def test_title_updated_et_responsable_obligatoires(self):
        for cle, code in (("updated", "process-updated-missing"),
                          ("responsible", "process-responsible-missing")):
            with self.subTest(code=code):
                loaded, _root = self.charge(carte_moins(carte(), cle))
                findings = canon.validate(loaded)
                trouves = [f for f in findings if f.code == code]
                self.assertTrue(trouves, sorted(codes(findings)))
                self.assertTrue(all(f.severity == canon.ERROR for f in trouves))
                self.assertTrue(all(f.process == TITRE for f in trouves))
                self.assertTrue(loaded.processes)
                self.assertTrue(all(not p.valid for p in loaded.processes))

    def test_fiche_sans_titre_est_une_erreur_et_aucune_regle(self):
        loaded, _root = self.charge(carte_moins(carte(), "title"))
        findings = canon.validate(loaded)
        self.assertIn("fiche-title-missing", codes(findings, canon.ERROR))
        self.assertEqual(loaded.processes, [])

    def test_dates_illisibles_signalees(self):
        loaded, _root = self.charge(carte(updated="12/10/2026", expire="bientôt"))
        findings = canon.validate(loaded)
        self.assertIn("process-updated-invalid", codes(findings, canon.ERROR))
        self.assertIn("process-expire-invalid", codes(findings, canon.ERROR))

    def test_expire_depassee_signalee_sans_bloquer_les_agents(self):
        loaded, _root = self.charge(carte(expire=_hier()))
        findings = canon.validate(loaded)
        trouvees = [f for f in findings if f.code == "process-expired"]
        self.assertTrue(trouvees, sorted(codes(findings)))
        self.assertTrue(all(f.severity == canon.WARNING for f in trouvees))
        self.assertTrue(all(f.process == TITRE for f in trouvees))
        blocages = canon.blocking(findings)
        self.assertEqual(blocages.global_errors, [])
        self.assertEqual(dict(blocages.by_agent), {})
        self.assertEqual(dict(blocages.by_host), {})
        self.assertEqual(canon_sync.assess(loaded, "atelier", findings)[0],
                         canon_sync.CANON_OK)

    def test_une_fiche_invalide_ne_bloque_aucun_agent(self):
        # updated manquant : erreur de la fiche, pas de l'hôte (même règle que
        # les fiches WorkPackage, L29)
        loaded, _root = self.charge(carte_moins(carte(), "updated"))
        findings = canon.validate(loaded)
        self.assertIn("process-updated-missing", codes(findings, canon.ERROR))
        self.assertEqual(canon.blocking(findings).global_errors, [])
        self.assertEqual(canon_sync.assess(loaded, "atelier", findings)[0],
                         canon_sync.CANON_OK)

    def test_un_texte_de_plus_de_deux_pages_est_signale(self):
        long = "# Règles\n\n" + ("- une règle très bavarde\n" * 400)
        self.assertGreater(len(long.encode()), canon.PROCESS_BODY_MAX)
        loaded, _root = self.charge(carte(body=long))
        findings = canon.validate(loaded)
        trouvees = [f for f in findings if f.code == "process-too-long"]
        self.assertTrue(trouvees, sorted(codes(findings)))
        self.assertTrue(all(f.severity == canon.WARNING for f in trouvees))
        self.assertIn(str(canon.PROCESS_BODY_MAX), trouvees[0].message)

    def test_le_texte_est_garde_dans_show_json(self):
        loaded, _root = self.charge(carte())
        data = loaded.to_dict()
        self.assertEqual([p["title"] for p in data["processes"]], [TITRE])
        self.assertEqual(data["processes"][0]["updated"], "2026-10-12")
        self.assertEqual(data["processes"][0]["responsible"], "human:alice")


class BriefProcessTest(PgTestCase):
    """(2) injection en tête du brief déterministe."""

    def setUp(self) -> None:
        super().setUp()
        self.root = os.path.join(self.tmp, "canon-acme")
        shutil.copytree(EXAMPLE, self.root)
        write(self.root, RACINE, carte())
        self.cfg = dataclasses.replace(self.cfg, canon=self.root, canon_ref="",
                                       canon_untrusted=True)

    def _row(self, name: str = "ouvrier") -> dict:
        return {"name": name, "harness": "deepseek", "host": self.cfg.host,
                "cwd": os.path.join(self.tmp, "travail"), "team": "acme-web",
                "responsible": "human:alice"}

    def _brief(self, cfg=None, row=None) -> str:
        return reprise.deterministic_brief(cfg or self.cfg, self.db,
                                           row or self._row())

    def test_le_brief_ouvre_sur_les_regles_du_canon(self):
        texte = self._brief()
        self.assertIn(reprise.PROCESS_RULES_HEADER, texte)
        self.assertLess(texte.index(reprise.PROCESS_RULES_HEADER),
                        texte.index("## Identity and role"))
        self.assertIn(TITRE, texte)
        self.assertIn("human:alice", texte)
        self.assertIn("2026-10-12", texte)
        self.assertIn("une règle de travail", texte)
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)

    def test_sans_fiche_process_le_brief_est_inchange(self):
        self.cfg = dataclasses.replace(self.cfg, canon="", canon_untrusted=False)
        texte = self._brief()
        self.assertNotIn(reprise.PROCESS_RULES_HEADER, texte)

    def test_les_regles_sont_bornees_et_ne_coupent_pas_la_consigne(self):
        write(self.root, RACINE, carte(body="# Règles\n\n" + ("- règle\n" * 3000)))
        texte = self._brief()
        self.assertIn(reprise.PROCESS_RULES_HEADER, texte)
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)
        self.assertIn("## Instructions", texte)
        self.assertIn("[…]", texte)   # le texte de la fiche est coupé, pas le brief

    def test_with_process_rules_prefixe_sans_doubler(self):
        une_fois = reprise.with_process_rules(self.cfg, "Résumé de l'agent.")
        self.assertTrue(une_fois.startswith(reprise.PROCESS_RULES_HEADER))
        self.assertIn("Résumé de l'agent.", une_fois)
        deux = reprise.with_process_rules(self.cfg, une_fois)
        self.assertEqual(deux, une_fois)
        self.assertEqual(reprise.with_process_rules(
            dataclasses.replace(self.cfg, canon="", canon_untrusted=False),
            "Résumé de l'agent."), "Résumé de l'agent.")


class RotationProcessTest(PgTestCase):
    """(2) le résumé de rotation — écrit par l'agent — reçoit les règles."""

    def setUp(self) -> None:
        super().setUp()
        self.root = os.path.join(self.tmp, "canon-acme")
        shutil.copytree(EXAMPLE, self.root)
        write(self.root, RACINE, carte(
            body="# Processus de lot\n\n- la règle que la rotation doit porter\n"))

    def _worker(self, name: str = "rot538"):
        cfg = dataclasses.replace(self.cfg, canon=self.root, canon_ref="",
                                  canon_untrusted=True,
                                  session_min_turns=1, session_max_turn_seconds=0.0,
                                  session_max_tokens=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def test_le_resume_de_rotation_porte_les_regles_en_tete(self):
        worker = self._worker()
        resume = "## État\n\nLe lot avance."
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "premier tour", "ids": []}))
                with mock.patch.dict(os.environ, {"AMEESH_TEST_RESULT": resume}):
                    self.assertTrue(worker.maybe_rotate())
            self.assertIn("la règle que la rotation doit porter", worker.resume_summary)
            self.assertLess(worker.resume_summary.index(reprise.PROCESS_RULES_HEADER),
                            worker.resume_summary.index(resume))
            # l'historique de session garde le résumé de l'agent, sans les règles
            with open(os.path.join(worker.state_dir, "session-history.jsonl"),
                      encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.readline())["resume"], resume)
        finally:
            worker.watchdog_stop.set()


class PushProcessTest(_TmpMixin, PgTestCase):
    """(3) une modification après `canon sync` part vers les agents de l'hôte."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        write(self.clone, RACINE, carte())
        commit_all(self.clone, "règles de travail")

    def sync(self) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, canon.load(self.clone), "atelier")

    def _sujets(self, name: str) -> list[dict]:
        return mail.unread(self.db, name)

    def test_premier_sync_ne_pousse_rien(self):
        self.sync()
        self.assertEqual(self._sujets("orchestre"), [])

    def test_une_modification_est_poussee_au_prochain_tour(self):
        self.sync()
        self.assertEqual(self._sujets("orchestre"), [])
        write(self.clone, RACINE, carte(
            body="# Processus de lot\n\n- la règle nouvelle\n"))
        commit_all(self.clone, "nouvelle règle")
        self.sync()
        recus = self._sujets("orchestre")
        self.assertEqual(len(recus), 1, recus)
        self.assertEqual(recus[0]["kind"], "event")
        self.assertIn("la règle nouvelle", recus[0]["body"])
        self.assertIn(TITRE, recus[0]["body"])
        # l'autre hôte n'est pas touché
        self.assertEqual(self._sujets("ouvrier"), [])
        # un sync sans modification ne repousse rien
        self.sync()
        self.assertEqual(len(self._sujets("orchestre")), 1)

    def test_une_modification_hors_process_ne_pousse_rien(self):
        self.sync()
        write(self.clone, "agents/ouvrier.md", fiche(
            type="Agent", title="ouvrier", responsible="human:alice",
            harness="deepseek", provider="deepseek", credential_mode="api-key"))
        commit_all(self.clone, "fiche agent retouchée")
        self.sync()
        self.assertEqual(self._sujets("orchestre"), [])

