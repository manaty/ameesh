# SPDX-License-Identifier: AGPL-3.0-only
"""L39 (décision 0030, point 3) : `ameesh adopt`, `ameesh resume`, compte
d'origine de la session en registre (`agent_registry.session_account`).

Aucun vrai harnais, aucun vrai compte : les dossiers de comptes sont des
dossiers du bac à sable (fichier d'identifiants VIDE, seule sa présence
compte), les sessions des fichiers écrits ici, les tours ceux des faux
harnais de `tests/fakebin`.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from ameesh import (account_turn, accounts, fil, mail, platform, registry, reprise,
                    storage, work)
from ameesh import main as mesh_main
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

#: #1196 : un SID unique par exécution. Un SID fixe se retrouve dans l'argv du
#: CLI d'un job parallèle (psql/psycopg du même run) ; `holders()` le voit et
#: refuse `adopt` à tort — test dépendant de l'environnement (canon §3).
#: Identifiant FICTIF, tiré à chaque exécution : un id fixe publié (fil,
#: documentation) peut être cité par un processus réel du même hôte et faire
#: croire à une session ouverte (#1283). Voir `reprise.holders`.
SID = str(uuid.uuid4())


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
    # L82: this fixture is a closed, quiet session, ready for adoption.
    quiet = time.time() - 120
    os.utime(chemin, (quiet, quiet))
    return chemin


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Session holders are an explicit observation in these adoption tests;
        # unrelated non-dumpable host processes must not decide their result.
        # Platform discovery and fail-closed refusals have their own L63 tests.
        holders = mock.patch.object(reprise, "holders", return_value=[])
        holders.start()
        self.addCleanup(holders.stop)
        for table in ("account_active", "account_holds", "account_switches", "turn_costs",
                      "spend_pending", "quota_gauge_readings",
                      "account_rate_events", "account_turn_slots", "account_turn_starts"):
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
        self.assertIn("Adoption of coord", fil_txt)
        self.assertIn("of account secondaire", fil_txt)
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

    def test_un_brief_qui_cite_la_session_ne_bloque_pas_l_adoption(self):
        """#1283 : un processus réel dont l'argv CITE l'id (brief JSON) n'est
        pas un détenteur ; l'adoption n'a pas à être forcée pour lui."""
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        pret = os.path.join(self.tmp, "citant-pret")
        citant = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(60)",
             pret, "--json", json.dumps({"brief": "reprends la session %s" % SID})])
        self.addCleanup(lambda: (citant.kill(), citant.wait()))
        fin = time.monotonic() + 10
        while not os.path.exists(pret):
            self.assertLess(time.monotonic(), fin, "délai dépassé")
            time.sleep(0.05)
        out = reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                            harness="codex")
        self.assertFalse(out["forced"])
        self.assertEqual(registry.get(self.db, "coord")["session_id"], SID)

    def test_introuvable_refus_clair(self):
        comptes = self._codex()
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                          harness="codex")
        texte = str(ctx.exception)
        self.assertIn("not found", texte)
        self.assertIn(os.path.join(comptes["codex"][0]["path"], "sessions"), texte)
        self.assertIn("declared accounts: primaire, secondaire", texte)
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
        tenant = [{"pid": os.getpid(), "why": "open file", "cmd": "test harness"}]
        with open(chemin, "a", encoding="utf-8"), \
                mock.patch.object(reprise, "holders", return_value=tenant):
            with self.assertRaises(reprise.RepriseError) as ctx:
                reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                              harness="codex")
            self.assertIn("close the interactive session first", str(ctx.exception))
            self.assertIn("pid %d" % os.getpid(), str(ctx.exception))
            self.assertEqual(registry.get(self.db, "coord")["mode"], "externe")
            avertissements: list[str] = []
            out = reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                                harness="codex", force=True, warn=avertissements.append)
        self.assertTrue(out["forced"])
        self.assertTrue(any("--force" in a for a in avertissements))
        self.assertIn("--force", self._fil())
        self.assertEqual(registry.get(self.db, "coord")["mode"], "execute")

    def test_hote_muet_refuse_avec_ou_sans_force(self):
        """L63b : sans moyen de vérifier que la session est fermée, `adopt`
        refuse, avec ou sans `--force`. Table injectée (NotAvailable) : la
        lecture réelle de la table des processus a ses témoins dans
        `tests/test_l63_platform.py.AdoptFailClosedTest`."""
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        self._externe("coord")
        muet = platform.NotAvailable(
            "file holders", "open files of process(es) of this user unreadable: "
            "4242 gpg-agent")
        for force in (False, True):
            with self.subTest(force=force), \
                    mock.patch.object(reprise, "holders", side_effect=muet):
                with self.assertRaises(reprise.RepriseError) as ctx:
                    reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                                  harness="codex", force=force)
            self.assertIn("cannot check", str(ctx.exception))
            self.assertIn("with or without --force", str(ctx.exception))
        # rien n'a été adopté : l'agent reste externe, sans session
        row = registry.get(self.db, "coord")
        self.assertEqual(row["mode"], "externe")
        self.assertNotEqual(row["session_id"], SID)

    def test_bail_vivant_refuse(self):
        comptes = self._codex()
        _rollout(comptes["codex"][0]["path"], SID, self.travail)
        self.register("vivant", "codex", cwd=self.travail)
        registry.claim(self.db, "vivant", "r1", 3600)
        with self.assertRaises(reprise.RepriseError) as ctx:
            reprise.adopt(self._cfg(comptes), self.db, "vivant", session_id=SID,
                          harness="codex")
        self.assertIn("live lease", str(ctx.exception))
        self.assertNotEqual(registry.get(self.db, "vivant")["session_id"], SID)

    def test_claude_et_deepseek_sans_compte_declare(self):
        claude = _dossier(os.path.join(self.tmp, "claude-home"))
        projet = os.path.join(claude, "projects", reprise.claude_project_key(self.travail))
        os.makedirs(projet)
        with open(os.path.join(projet, SID + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "cwd": self.travail}) + "\n")
        dsh = _dossier(os.path.join(self.tmp, "dsh-home"))
        os.makedirs(os.path.join(dsh, "sessions", "--tmp--", "session-" + SID))
        quiet = time.time() - 120
        for closed in (os.path.join(projet, SID + ".jsonl"),
                       os.path.join(dsh, "sessions", "--tmp--", "session-" + SID)):
            os.utime(closed, (quiet, quiet))
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
            self.assertTrue(any("will not find it" in n for n in out["notes"]))
        self.assertIsNone(registry.get(self.db, "cl")["session_account"])

    def test_cli_adopt_json(self):
        comptes = self._codex()
        _rollout(comptes["codex"][1]["path"], SID, self.travail)
        config = os.path.join(self.tmp, "config-hote.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"accounts": comptes}, fh)
        self._externe("coord")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env(AMEESH_CONFIG=config)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = isolated_entrypoint(mesh_main.main, ["adopt", "coord", "--session", SID,
                                   "--harness", "codex", "--json"])
        self.assertEqual(code, 0, stderr.getvalue())
        out = json.loads(stdout.getvalue())
        self.assertEqual((out["agent"], out["account"]), ("coord", "secondaire"))
        proc = self.mesh("show", "coord")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session  : %s (account secondaire)" % SID, proc.stdout)
        proc = self.mesh("adopt", "coord", "--session", "absente", "--harness", "codex",
                         env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not found", proc.stderr)


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
        self.assertIn("Resume of ouv", self._fil())
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
        # Public permissions make the account unusable on every platform;
        # a missing auth.json alone is valid with the macOS keychain.
        os.chmod(secondaire, 0o755)
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
        self.assertIn("unusable", out["why"])
        row = registry.get(self.db, "coord")
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["session_account"])
        self.assertEqual(row["status"], "queued")
        brief = row["pending_prompt"]
        for attendu in ("# Resume brief for coord", "## Identity and role",
                        "human owner: human:proprio",
                        "## Work items assigned to you",
                        "#%s [intake] Câbler la reprise — waiting for: start by coord"
                        % lot["id"],
                        "## Latest exchanges in thread atelier",
                        "Le lot de câblage attend ta revue.",
                        "## Mail", "2 unread message(s)",
                        "## Previous session (read-only)", "sess-b", "account secondaire",
                        transcript, "## Instructions", "Termine la revue."):
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
        self.assertIn("will summarize", out["why"])
        self.assertEqual(registry.get(self.db, "rot")["session_id"], "sess-p")
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM account_switches")[0]
                         ["n"], 0)
        # saturé (100 %) : plus de tour de résumé possible → brief déterministe
        _rollout(primaire, "sess-p", self.travail, used_percent=100)
        self.runner("stop", "rot")
        out = reprise.resume(self._cfg(comptes), self.db, "rot")
        self.assertFalse(out["kept"])
        self.assertIn("saturated", out["why"])

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
        self.assertIn("# Resume brief for fr", row["pending_prompt"])
        self.assertIn("--fresh", row["pending_prompt"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.agent_dir("fr"), "session")))

    def test_marge_de_derive_sur_le_fichier_local(self):
        """N4 (#1898) : la reprise par fichier local lit le mtime avec la même
        marge de dérive d'horloges que l'exécuteur (#1481)."""
        self.register("ml", "codex", cwd=self.travail)
        os.makedirs(self.cfg.agent_dir("ml"), exist_ok=True)
        chemin = os.path.join(self.cfg.agent_dir("ml"), "session")
        reset = time.time()
        row = {"name": "ml", "host": self.cfg.host, "session_id": None,
               "session_reset_ts": reset}
        with open(chemin, "w") as fh:
            fh.write("sess\n")
        os.utime(chemin, (reset + 2, reset + 2))    # dans la marge : oubliée
        self.assertIsNone(reprise._local_session(self.cfg, row))
        with open(chemin, "w") as fh:
            fh.write("sess\n")
        os.utime(chemin, (reset + 30, reset + 30))  # au-delà : reprise
        self.assertEqual(reprise._local_session(self.cfg, row), "sess")

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
        self.assertIn("turn in progress", str(ctx.exception))

    def test_resume_without_live_runner_warns(self):
        # #293: an agent that is not stopped (here paused by host pressure,
        # "blocked") but whose runner is gone holds no live lease: the
        # instruction waits for a runner, and resume must say so.
        self.register("orphelin", "codex", cwd=self.travail)
        registry.set_session(self.db, "orphelin", "sess-o")
        self.db.execute("UPDATE agent_registry SET status = 'blocked', lease_owner = 'r-mort', "
                        "lease_expires_at = now() - interval '1 hour' "
                        "WHERE name = 'orphelin'")
        avertissements: list[str] = []
        out = reprise.resume(self.cfg, self.db, "orphelin", warn=avertissements.append)
        self.assertFalse(out["runner_live"])
        self.assertTrue(any("no runner holds orphelin's lease" in a
                            for a in avertissements), avertissements)

    def test_resume_warns_only_without_runner(self):
        # A live lease, or a recent runner pass for a stopped agent, avoids
        # the warning. The latter still has no live lease.
        self.register("vivant", "codex", cwd=self.travail)
        registry.set_session(self.db, "vivant", "sess-v")
        registry.claim(self.db, "vivant", "r1", 3600)
        avertissements: list[str] = []
        out = reprise.resume(self.cfg, self.db, "vivant", warn=avertissements.append)
        self.assertTrue(out["runner_live"])
        self._arrete("arrete", "sess-a", None)
        registry.runner_seen(self.db, self.cfg.host, "r1", ["arrete"])
        out = reprise.resume(self.cfg, self.db, "arrete", warn=avertissements.append)
        self.assertFalse(out["runner_live"])
        self.assertFalse(any("no runner" in a for a in avertissements), avertissements)

    def test_resume_stopped_without_runner_proof_warns_and_queues(self):
        for name, age, host, owner in [
            ("absent", None, None, None),
            ("perime", 121, self.cfg.host, "r1"),
            ("deplace", 0, "elsewhere", "r1"),
            ("sans-identite", 0, self.cfg.host, None),
            ("futur", -60, self.cfg.host, "r1"),
        ]:
            with self.subTest(name=name):
                self._arrete(name, "sess-" + name, None)
                if age is not None:
                    self.db.execute(
                        "UPDATE agent_registry SET runner_seen_at = clock_timestamp()"
                        " - %s * interval '1 second', runner_seen_host = %s,"
                        " runner_seen_owner = %s WHERE name = %s", (age, host, owner, name))
                warnings = []
                out = reprise.resume(self.cfg, self.db, name, brief="REPRENDRE",
                                     warn=warnings.append)
                self.assertFalse(out["runner_live"])
                self.assertIn(
                    "no runner has reported serving %s on %s recently: the instruction "
                    "waits until a runner serves it again" % (name, self.cfg.host), warnings)
                self.assertIn("REPRENDRE", registry.get(self.db, name)["pending_prompt"])

    def test_resume_stopped_respects_runner_interval(self):
        self._arrete("lent", "sess-lent", None)
        registry.runner_seen(self.db, self.cfg.host, "r1", ["lent"])
        self.db.execute("UPDATE agent_registry SET runner_seen_at = clock_timestamp()"
                        " - interval '3 minutes' WHERE name = 'lent'")
        for cfg in (dataclasses.replace(self.cfg, runner_liveness_interval=120),
                    dataclasses.replace(self.cfg, poll=120)):
            with self.subTest(poll=cfg.poll, interval=cfg.runner_liveness_interval):
                self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = 'lent'")
                warnings = []
                reprise.resume(cfg, self.db, "lent", warn=warnings.append)
                self.assertFalse(any("no runner" in w for w in warnings), warnings)


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
        os.chmod(secondaire, 0o755)  # unusable even with the macOS keychain
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
        self.assertIn("# Resume brief for rb", dernier["argv"][-1])
        # encadré comme un brief d'ameesh, jamais comme le résumé de l'agent
        self.assertIn('author="ameesh, deterministic brief" authority="none"',
                      dernier["argv"][-1])
        self.assertNotIn("you wrote yourself", dernier["argv"][-1])
        self.assertIn("deterministic brief (L39)", self._fil())


if __name__ == "__main__":
    unittest.main()
