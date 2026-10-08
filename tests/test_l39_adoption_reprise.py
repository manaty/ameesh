# SPDX-License-Identifier: AGPL-3.0-only
"""L39 (décision 0030, point 3) : `ameesh adopt`, `ameesh resume`, compte
d'origine de la session en registre (`agent_registry.session_account`).

Aucun vrai harnais, aucun vrai compte : les dossiers de comptes sont des
dossiers du bac à sable (fichier d'identifiants VIDE, seule sa présence
compte), les sessions des fichiers écrits ici, les tours ceux des faux
harnais de `tests/fakebin`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
from unittest import mock

from ameesh import account_turn, accounts, fil, mail, registry, reprise, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

SID = "00000000-0000-7000-8000-000000000001"


def _dossier(chemin: str, *, creds: str | None = None) -> str:
    os.makedirs(chemin, exist_ok=True)
    os.chmod(chemin, 0o700)
    if creds:
        with open(os.path.join(chemin, creds), "w", encoding="utf-8"):
            pass
        os.chmod(os.path.join(chemin, creds), 0o600)
    return chemin


def _rollout(home: str, sid: str, cwd: str, used_percent: float | None = None) -> str:
    """Un journal de session Codex (forme réelle : session_meta en tête)."""
    dossier = os.path.join(home, "sessions", "2026", "10", "07")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, "rollout-2026-10-07T09-13-13-%s.jsonl" % sid)
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta",
                             "payload": {"id": sid, "cwd": cwd}}) + "\n")
        if used_percent is not None:
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count", "rate_limits": {
                    "plan_type": "pro",
                    "primary": {"used_percent": used_percent, "window_minutes": 300,
                                "resets_at": time.time() + 3600},
                    "secondary": None}}}) + "\n")
    return chemin


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("account_active", "account_holds", "account_switches", "turn_costs",
                      "spend_pending", "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.travail = os.path.join(self.tmp, "travail")
        os.makedirs(self.travail, exist_ok=True)

    def _codex(self) -> dict:
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        return {"codex": [{"name": "primaire", "path": a}, {"name": "secondaire", "path": b}]}

    def _cfg(self, comptes: dict | None = None):
        return dataclasses.replace(self.cfg, accounts=comptes or {})

    def _externe(self, name: str, harness: str = "codex") -> None:
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host, mode="externe")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio' "
                        "WHERE name = %s", (name,))

    def _fil(self) -> str:
        texte = []
        for racine, _d, fichiers in os.walk(self.cfg.threads_root):
            for nom in fichiers:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as fh:
                    texte.append(fh.read())
        return "\n".join(texte)

    def _worker(self, name: str, cfg):
        runner = Runner(cfg, self.db, once=True)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def _tour(self, worker) -> dict | None:
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        with mock.patch.dict(os.environ, env, clear=False):
            spec = worker.pick()
            if spec is None:
                return None
            worker.run_turn(spec)
            return spec


# --------------------------------------------------------------------------
# adopt
# --------------------------------------------------------------------------

class AdoptTest(_Base):
    def test_session_codex_trouvee_sous_son_compte(self):
        comptes = self._codex()
        chemin = _rollout(comptes["codex"][1]["path"], SID, self.travail)
        self._externe("coord")
        out = reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                            harness="codex", actor="human:proprio")
        self.assertEqual((out["account"], out["path"], out["cwd"]),
                         ("secondaire", chemin, self.travail))
        self.assertFalse(out["forced"])
        self.assertEqual(out["previous_mode"], "externe")
        row = registry.get(self.db, "coord")
        self.assertEqual((row["mode"], row["status"], row["session_id"], row["session_account"],
                          row["cwd"], row["host"]),
                         ("execute", "queued", SID, "secondaire", self.travail, self.cfg.host))
        self.assertEqual(row["pending_prompt"], reprise.DEFAULT_ADOPT)
        self.assertTrue(registry.wakeable(self.db, self.cfg, "coord")[0])
        fil_txt = self._fil()
        self.assertIn("Adoption de coord", fil_txt)
        self.assertIn("du compte secondaire", fil_txt)
        self.assertIn('"audit": "adopt"', fil_txt)

    def test_brief_donne_et_compte_vise(self):
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        autre = os.path.join(self.tmp, "ailleurs")
        os.makedirs(autre)
        out = reprise.adopt(self._cfg(comptes), self.db, "neuf", session_id=SID,
                            harness="codex", account="primaire", cwd=autre,
                            brief="Termine le lot 12.", chantier="atelier")
        self.assertEqual((out["account"], out["cwd"]), ("primaire", autre))
        row = registry.get(self.db, "neuf")  # agent créé par l'adoption
        self.assertEqual((row["chantier"], row["harness"], row["pending_prompt"],
                          row["session_account"]),
                         ("atelier", "codex", "Termine le lot 12.", "primaire"))

    def test_introuvable_refus_clair(self):
        comptes = self._codex()
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                          harness="codex")
        texte = str(ctx.exception)
        self.assertIn("introuvable", texte)
        self.assertIn(os.path.join(comptes["codex"][0]["path"], "sessions"), texte)
        self.assertIn("comptes déclarés : primaire, secondaire", texte)
        # présente sous un AUTRE compte que celui visé : on dit lequel
        _rollout(comptes["codex"][1]["path"], SID, self.travail)
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                          harness="codex", account="primaire")
        self.assertIn("--account secondaire", str(ctx.exception))
        with self.assertRaises(reprise.RepriseError):  # compte inconnu
            reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                          harness="codex", account="tertiaire")
        with self.assertRaises(reprise.RepriseError):  # jamais un chemin
            reprise.adopt(self._cfg(comptes), self.db, "coord", session_id="../x",
                          harness="codex")
        self.assertIsNone(registry.get(self.db, "coord"))

    def test_session_ouverte_refusee_puis_force(self):
        comptes = self._codex()
        chemin = _rollout(comptes["codex"][0]["path"], SID, self.travail)
        self._externe("coord")
        with open(chemin, "a", encoding="utf-8"):  # un « harnais » la tient ouverte
            with self.assertRaises(reprise.RepriseError) as ctx:
                reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                              harness="codex")
            self.assertIn("fermez d'abord la session interactive", str(ctx.exception))
            self.assertIn("pid %d" % os.getpid(), str(ctx.exception))
            self.assertEqual(registry.get(self.db, "coord")["mode"], "externe")
            avertissements: list[str] = []
            out = reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                                harness="codex", force=True, warn=avertissements.append)
        self.assertTrue(out["forced"])
        self.assertTrue(any("--force" in a for a in avertissements))
        self.assertIn("--force", self._fil())
        self.assertEqual(registry.get(self.db, "coord")["mode"], "execute")

    def test_bail_vivant_refuse(self):
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        self.register("vivant", "codex", cwd=self.travail)
        registry.claim(self.db, "vivant", "r1", 3600)
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "vivant", session_id=SID,
                          harness="codex")
        self.assertIn("bail vivant", str(ctx.exception))
        self.assertNotEqual(registry.get(self.db, "vivant")["session_id"], SID)

    def test_claude_et_deepseek_sans_compte_declare(self):
        claude = _dossier(os.path.join(self.tmp, "claude-home"))
        projet = os.path.join(claude, "projects", reprise.claude_project_key(self.travail))
        os.makedirs(projet)
        with open(os.path.join(projet, SID + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "cwd": self.travail}) + "\n")
        dsh = _dossier(os.path.join(self.tmp, "dsh-home"))
        os.makedirs(os.path.join(dsh, "sessions", "--tmp--", "session-" + SID))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude, "DSH_HOME": dsh}):
            out = reprise.adopt(self._cfg(), self.db, "cl", session_id=SID, harness="claude")
            self.assertEqual((out["account"], out["cwd"]), (None, self.travail))
            self.assertEqual(out["notes"], [])
            out = reprise.adopt(self._cfg(), self.db, "ds", session_id=SID,
                                harness="deepseek", cwd=self.travail)
            self.assertTrue(out["path"].endswith("session-" + SID))
            # Claude lancé ailleurs que le dossier de la session : constat
            autre = os.path.join(self.tmp, "autre")
            os.makedirs(autre)
            self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = 'cl'")
            out = reprise.adopt(self._cfg(), self.db, "cl", session_id=SID, harness="claude",
                                cwd=autre)
            self.assertTrue(any("ne la retrouvera pas" in n for n in out["notes"]))
        self.assertIsNone(registry.get(self.db, "cl")["session_account"])

    def test_cli_adopt_json(self):
        comptes = self._codex()
        _rollout(comptes["codex"][1]["path"], SID, self.travail)
        config = os.path.join(self.tmp, "config-hote.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"accounts": comptes}, fh)
        self._externe("coord")
        proc = self.mesh("adopt", "coord", "--session", SID, "--harness", "codex", "--json",
                         env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["agent"], out["account"]), ("coord", "secondaire"))
        proc = self.mesh("show", "coord")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session  : %s (compte secondaire)" % SID, proc.stdout)
        proc = self.mesh("adopt", "coord", "--session", "absente", "--harness", "codex",
                         env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("introuvable", proc.stderr)


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------

class ResumeTest(_Base):
    def _arrete(self, name: str, session: str, compte: str | None) -> None:
        self.register(name, "codex", cwd=self.travail)
        registry.set_session(self.db, name, session, compte)
        proc = self.runner("stop", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_arrete_repris_sur_sa_session(self):
        comptes = self._codex()
        self._arrete("ouv", "sess-a", "primaire")
        cfg = self._cfg(comptes)
        out = reprise.resume(cfg, self.db, "ouv")
        self.assertTrue(out["kept"])
        self.assertFalse(out["deterministic"])
        self.assertEqual((out["previous_status"], out["account"], out["next_account"]),
                         ("stopped", "primaire", "primaire"))
        row = registry.get(self.db, "ouv")
        self.assertEqual((row["status"], row["stop_reason"], row["session_id"],
                          row["session_account"], row["pending_prompt"]),
                         ("queued", None, "sess-a", "primaire", reprise.DEFAULT_RESUME))
        self.assertIn("Reprise de ouv", self._fil())
        # la simulation du choix de compte n'a RIEN écrit
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM account_active")[0]["n"],
                         0)
        # l'exécuteur reprend bien la session enregistrée, sous son compte
        runner, worker = self._worker("ouv", dataclasses.replace(
            cfg, budget_usd_per_hour=10.0, budget_check_interval=0.0))
        try:
            self.assertIsNotNone(self._tour(worker))
        finally:
            worker.watchdog_stop.set()
        dernier = self.turns()[-1]
        self.assertIn("sess-a", dernier["argv"])
        self.assertEqual(dernier["compte"]["CODEX_HOME"], comptes["codex"][0]["path"])

    def test_compte_d_origine_inutilisable_codex_brief_deterministe(self):
        comptes = self._codex()
        secondaire = comptes["codex"][1]["path"]
        transcript = _rollout(secondaire, "sess-b", self.travail)
        os.unlink(os.path.join(secondaire, "auth.json"))  # compte inutilisable
        self._arrete("coord", "sess-b", "secondaire")
        self.db.execute("UPDATE agent_registry SET chantier = 'atelier', "
                        "responsible = 'human:proprio' WHERE name = 'coord'")
        lot = work.add(self.db, title="Câbler la reprise", assignee="coord")
        fil.record(self.cfg, self.db, sender="relais", recipients=["coord"],
                   text="Le lot de câblage attend ta revue.", project="atelier")
        mail.send(self.db, "relais", "coord", "Où en est la revue ?", host=self.cfg.host)
        mail.send(self.db, "relais", "coord", "Relance.", host=self.cfg.host)
        out = reprise.resume(self._cfg(comptes), self.db, "coord", brief="Termine la revue.")
        self.assertFalse(out["kept"])
        self.assertTrue(out["deterministic"])
        self.assertEqual(out["forgotten_session"], "sess-b")
        self.assertIn("inutilisable", out["why"])
        row = registry.get(self.db, "coord")
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["session_account"])
        self.assertEqual(row["status"], "queued")
        brief = row["pending_prompt"]
        for attendu in ("# Brief de reprise de coord", "## Identité et rôle",
                        "responsable humain : human:proprio",
                        "## Lots qui te sont assignés",
                        "#%s [intake] Câbler la reprise — attend : démarrage par coord"
                        % lot["id"],
                        "## Derniers échanges du fil atelier",
                        "Le lot de câblage attend ta revue.",
                        "## Courrier", "2 message(s) non lu(s)",
                        "## Ancienne session (lecture seule)", "sess-b", "compte secondaire",
                        transcript, "## Consigne", "Termine la revue."):
            self.assertIn(attendu, brief)
        self.assertLessEqual(len(brief.encode("utf-8")), reprise.BRIEF_MAX_BYTES)
        # déterministe (aucun appel de modèle) : même état, même brief
        un, deux = (reprise.deterministic_brief(self._cfg(comptes), self.db, row,
                                                session="sess-b", account="secondaire",
                                                reason="x", consigne="y") for _ in range(2))
        self.assertEqual(un, deux)

    def test_ancien_compte_au_seuil_mais_utilisable_rotation_de_l_executeur(self):
        comptes = self._codex()
        primaire = comptes["codex"][0]["path"]
        _rollout(primaire, "sess-p", self.travail, used_percent=95)  # au seuil
        self._arrete("rot", "sess-p", "primaire")
        out = reprise.resume(self._cfg(comptes), self.db, "rot")
        self.assertTrue(out["kept"])
        self.assertEqual(out["next_account"], "secondaire")
        self.assertIn("résumera", out["why"])
        self.assertEqual(registry.get(self.db, "rot")["session_id"], "sess-p")
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM account_switches")[0]
                         ["n"], 0)
        # saturé (100 %) : plus de tour de résumé possible → brief déterministe
        _rollout(primaire, "sess-p", self.travail, used_percent=100)
        self.runner("stop", "rot")
        out = reprise.resume(self._cfg(comptes), self.db, "rot")
        self.assertFalse(out["kept"])
        self.assertIn("saturé", out["why"])

    def test_fresh_oublie_la_session_et_le_fichier_local(self):
        self._arrete("fr", "sess-f", None)
        os.makedirs(self.cfg.agent_dir("fr"), exist_ok=True)
        with open(os.path.join(self.cfg.agent_dir("fr"), "session"), "w") as fh:
            fh.write("sess-f\n")
        proc = self.mesh("resume", "fr", "--fresh", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["forgotten_session"], out["deterministic"]), ("sess-f", True))
        row = registry.get(self.db, "fr")
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["status"], "queued")
        self.assertIn("# Brief de reprise de fr", row["pending_prompt"])
        self.assertIn("--fresh", row["pending_prompt"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("fr"), "session")))

    def test_agent_externe_refuse(self):
        self._externe("ext")
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.resume(self.cfg, self.db, "ext")
        self.assertIn("ameesh adopt", str(ctx.exception))
        proc = self.mesh("resume", "ext")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ameesh adopt", proc.stderr)
        with self.assertRaises(reprise.RepriseError):
            reprise.resume(self.cfg, self.db, "inconnu")

    def test_au_repos_sous_un_executeur_et_tour_en_cours(self):
        self.register("repos", "codex", cwd=self.travail)
        registry.set_session(self.db, "repos", "sess-r")
        registry.set_pending_prompt(self.db, "repos", "AVANT")
        registry.claim(self.db, "repos", "r1", 3600)
        out = reprise.resume(self.cfg, self.db, "repos", brief="APRES")
        self.assertTrue(out["kept"])
        row = registry.get(self.db, "repos")
        self.assertEqual(row["pending_prompt"].split("\n\n"), ["AVANT", "APRES"])
        self.assertEqual(row["lease_owner"], "r1")  # le bail n'est pas touché
        # --fresh sous un bail vivant : passe par l'exécuteur (demande de redémarrage)
        out = reprise.resume(self.cfg, self.db, "repos", fresh=True)
        self.assertFalse(out["applied"])
        self.assertIsNotNone(registry.get(self.db, "repos")["restart_requested_ts"])
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'repos'")
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.resume(self.cfg, self.db, "repos")
        self.assertIn("tour en cours", str(ctx.exception))


# --------------------------------------------------------------------------
# session_account : écrit et effacé par l'exécuteur (faux harnais)
# --------------------------------------------------------------------------

class SessionAccountTest(_Base):
    def test_ecrit_au_tour_et_efface_a_l_oubli(self):
        comptes = self._codex()
        self.register("sa", "codex", cwd=self.travail)
        cfg = dataclasses.replace(self._cfg(comptes), budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0)
        runner, worker = self._worker("sa", cfg)
        try:
            registry.set_pending_prompt(self.db, "sa", "travail")
            self.assertIsNotNone(self._tour(worker))
            row = registry.get(self.db, "sa")
            self.assertEqual((row["session_id"], row["session_account"]),
                             ("codex-thread-1", "primaire"))
            # oubli fencé (rotation) : le compte part avec la session
            self.assertTrue(registry.clear_session(self.db, "sa", runner.runner_id,
                                                   worker.epoch))
            self.assertIsNone(registry.get(self.db, "sa")["session_account"])
        finally:
            worker.watchdog_stop.set()
        # une AUTRE session inscrite à la main : compte inconnu
        registry.set_session(self.db, "sa", "s1", "primaire")
        registry.upsert(self.db, "sa", session_id="s2")
        self.assertIsNone(registry.get(self.db, "sa")["session_account"])
        registry.set_session(self.db, "sa", "s2", "primaire")
        registry.upsert(self.db, "sa", session_id="s2")  # même session : compte gardé
        self.assertEqual(registry.get(self.db, "sa")["session_account"], "primaire")

    def test_le_registre_prime_sur_le_marqueur(self):
        comptes = self._codex()
        self.register("pr", "codex", cwd=self.travail)
        cfg = self._cfg(comptes)
        os.makedirs(cfg.agent_dir("pr"), exist_ok=True)
        with open(os.path.join(cfg.agent_dir("pr"), "events.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "ameesh.account", "harness": "codex",
                                 "account": "primaire"}) + "\n")
        row = registry.get(self.db, "pr")
        self.assertEqual(account_turn.recorded_session_account(cfg, row),
                         account_turn.marker_account(cfg, "pr", "codex"))
        registry.set_session(self.db, "pr", "s", "secondaire")
        self.assertEqual(account_turn.recorded_session_account(
            cfg, registry.get(self.db, "pr")), "secondaire")

    def test_adoption_puis_tour_sur_la_session_adoptee(self):
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        cfg = dataclasses.replace(self._cfg(comptes), budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0)
        self._externe("coord")
        reprise.adopt(cfg, self.db, "coord", session_id=SID, harness="codex")
        runner, worker = self._worker("coord", cfg)
        try:
            self.assertIsNotNone(self._tour(worker))
        finally:
            worker.watchdog_stop.set()
        dernier = self.turns()[-1]
        self.assertEqual(dernier["argv"][dernier["argv"].index("resume") + 1], SID)
        self.assertEqual(dernier["compte"]["CODEX_HOME"], comptes["codex"][0]["path"])
        self.assertIn(reprise.DEFAULT_ADOPT, dernier["argv"][-1])
        row = registry.get(self.db, "coord")
        self.assertEqual((row["session_id"], row["session_account"]), (SID, "primaire"))

    def test_rotation_sans_resume_possible_ouvre_sur_le_brief_deterministe(self):
        comptes = self._codex()
        secondaire = comptes["codex"][1]["path"]
        self.register("rb", "codex", cwd=self.travail)
        registry.set_session(self.db, "rb", "sess-x", "secondaire")
        os.unlink(os.path.join(secondaire, "auth.json"))  # ancien compte inutilisable
        cfg = dataclasses.replace(self._cfg(comptes), budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0)
        runner, worker = self._worker("rb", cfg)
        try:
            registry.set_pending_prompt(self.db, "rb", "suite")
            self.assertIsNotNone(self._tour(worker))
        finally:
            worker.watchdog_stop.set()
        dernier = self.turns()[-1]
        self.assertNotIn("resume", dernier["argv"])
        self.assertIn("# Brief de reprise de rb", dernier["argv"][-1])
        self.assertIn("brief déterministe (L39)", self._fil())


if __name__ == "__main__":
    unittest.main()
