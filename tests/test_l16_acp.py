# SPDX-License-Identifier: AGPL-3.0-only
"""L16 : adaptateur ACP générique, testé contre un FAUX agent ACP.

Aucun agent réel, aucun réseau, aucune dépense : `tests/fake_acp_agent.py` parle
JSON-RPC sur stdio comme un agent ACP, et ses modes pilotent les cas (reprise,
permission, annulation, panne de protocole, fichiers, délai).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from ameesh import acp, adapters

from .support import SRC, child_env

FAKE_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_acp_agent.py")


def descriptor_doc(**ameesh) -> dict:
    base = {
        "id": "faux-acp",
        "name": "Faux agent ACP",
        "version": "1.0.0",
        "schema_version": "1",
        "description": "Agent ACP factice des tests L16.",
        "capabilities": {"session/new": True, "session/prompt": True},
        "authentication": {},
        "distribution": {},
        "ameesh": {
            "protocol": "acp",
            "binary": sys.executable,
            "command": [FAKE_AGENT],
            "stream": "acp-json",
            **ameesh,
        },
    }
    return base


class AcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l16-acp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "agent.jsonl")

    def descriptor(self, doc: dict | None = None, name: str = "descripteur.json") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc if doc is not None else descriptor_doc(), fh)
        return path

    def run_bridge(self, mode: str = "normal", *, descriptor: str | None = None,
                   args: list[str] | None = None, env: dict | None = None,
                   cwd: str | None = None, timeout: float = 30.0):
        variables = {
            "FAKE_ACP_MODE": mode,
            "FAKE_ACP_LOG": self.log,
            "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        variables.update(env or {})
        command = [sys.executable, "-m", "ameesh.acp", "run",
                   "--descriptor", descriptor or self.descriptor(),
                   "--agent", sys.executable,
                   "--prompt", "fais X"]
        command += args or []
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd or self.tmp, env=child_env(**variables))

    @staticmethod
    def events(proc) -> list[dict]:
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def called(self, method: str) -> list[dict]:
        return [c for c in self.calls() if c.get("method") == method]

    # -- tour nominal ------------------------------------------------------
    def test_tour_normal(self):
        proc = self.run_bridge("normal")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.events(proc)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "session")
        self.assertIn("text", kinds)
        self.assertIn("tool", kinds)
        final = events[-1]
        self.assertEqual(final["type"], "final")
        self.assertEqual(final["stopReason"], "end_turn")
        # l'usage passe par un événement `status`/`step_end` : c'est lui que lit
        # le grand livre d'un harnais hors Claude/Codex (0019 §3)
        etapes = [e for e in events if e["type"] == "status"]
        self.assertEqual(len(etapes), 1)
        self.assertEqual(etapes[0]["phase"], "step_end")
        self.assertEqual(etapes[0]["usage"]["inputTokens"], 1234)
        self.assertEqual(etapes[0]["usage"]["input_tokens"], 1234)
        self.assertEqual(final["usage"]["used"], 1234)
        self.assertEqual(final["costUsd"], 0.02)
        self.assertIn("bonjour", final["text"])
        self.assertEqual([c["method"] for c in self.calls()][:3],
                         ["initialize", "session/new", "session/prompt"])

    def test_reprise_par_resume_puis_load_puis_neuve(self):
        # resume : l'agent annonce sessionCapabilities.resume
        proc = self.run_bridge("resume", args=["--session", "s-old"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([c["method"] for c in self.calls()][:2],
                         ["initialize", "session/resume"])
        self.assertEqual(self.events(proc)[0],
                         {"type": "session", "sessionId": "s-old", "resumed": "resume"})
        # load : rejeu, non affiché
        os.unlink(self.log)
        proc = self.run_bridge("load", args=["--session", "s-old"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session/load", [c["method"] for c in self.calls()])
        self.assertNotIn("REJEU", proc.stdout)
        # ni resume ni load : session neuve (le résumé L11 est dans la consigne)
        os.unlink(self.log)
        proc = self.run_bridge("noresume", args=["--session", "s-old"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session/new", [c["method"] for c in self.calls()])
        self.assertIn("ne sait pas reprendre", proc.stderr)

    def test_permission_refusee_par_defaut(self):
        proc = self.run_bridge("permission")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.events(proc)
        permission = [e for e in events if e["type"] == "permission"]
        self.assertEqual(len(permission), 1)
        self.assertEqual(permission[0]["decision"], "deny")
        self.assertIn("refus par défaut", permission[0]["reason"])
        reponse = self.called("permission_answer")[0]["result"]
        self.assertEqual(reponse, {"outcome": {"outcome": "selected",
                                               "optionId": "reject-1"}})

    def test_permission_accordee_par_la_politique(self):
        doc = descriptor_doc(permissions={"default": "deny", "allow_kinds": ["read"]})
        proc = self.run_bridge("permission", descriptor=self.descriptor(doc))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        permission = [e for e in self.events(proc) if e["type"] == "permission"][0]
        self.assertEqual(permission["decision"], "allow")
        reponse = self.called("permission_answer")[0]["result"]
        self.assertEqual(reponse["outcome"]["optionId"], "allow-1")

    def test_annulation_propre(self):
        env = child_env(FAKE_ACP_MODE="hang", FAKE_ACP_IGNORE_SIGTERM="1",
                        FAKE_ACP_LOG=self.log,
                        PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.Popen(
            [sys.executable, "-m", "ameesh.acp", "run",
             "--descriptor", self.descriptor(), "--agent", sys.executable,
             "--prompt", "fais X"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=self.tmp, env=env)
        # attend que le tour soit en cours, puis interrompt comme l'exécuteur
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.called("session/prompt"):
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("session/cancel", [c["method"] for c in self.calls()])
        final = [json.loads(line) for line in out.splitlines() if line.strip()][-1]
        self.assertEqual(final["type"], "final")
        self.assertEqual(final["stopReason"], "cancelled")

    def test_agent_mort(self):
        proc = self.run_bridge("exit")
        self.assertEqual(proc.returncode, 1)
        events = self.events(proc)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("s'est arrêté", events[-1]["message"])

    def test_protocole_invalide_finit_en_erreur(self):
        for mode in ("garbage", "unknown", "badresponse"):
            with self.subTest(mode=mode):
                os.path.exists(self.log) and os.unlink(self.log)
                proc = self.run_bridge(mode, timeout=20)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                events = self.events(proc)
                self.assertEqual(events[-1]["type"], "error")
                self.assertIn("error", [e["type"] for e in events])

    def test_enveloppe_json_rpc_invalide_finit_en_erreur(self):
        """Un id non conforme ne doit pas tuer le lecteur en silence.

        Le tour doit se conclure en erreur **vite** (pas au délai
        d'inactivité), avec un événement `error` et un code de sortie non nul.
        """
        for cas in ("id", "method", "jsonrpc", "params", "errorcode"):
            with self.subTest(case=cas):
                os.path.exists(self.log) and os.unlink(self.log)
                debut = time.monotonic()
                proc = self.run_bridge("badenveloppe", env={"FAKE_ACP_CASE": cas},
                                       args=["--idle-timeout", "30"], timeout=20)
                duree = time.monotonic() - debut
                self.assertEqual(proc.returncode, 1, proc.stderr)
                events = self.events(proc)
                self.assertEqual(events[-1]["type"], "error")
                self.assertLess(duree, 10.0, "le tour a attendu le délai au lieu d'échouer")

    def test_delai_d_initialisation(self):
        proc = self.run_bridge("mute", args=["--init-timeout", "0.5"], timeout=20)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("délai dépassé", self.events(proc)[-1]["message"])

    def test_descripteur_modifie_depuis_la_commande_refuse(self):
        """Le pont épingle l'empreinte vue par l'exécuteur (chaîne de confiance)."""
        from ameesh import harnesses
        path = self.descriptor()
        sha = harnesses.load(path).sha256
        doc = descriptor_doc()
        doc["ameesh"]["command"] = [FAKE_AGENT, "--autre-chose"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        proc = self.run_bridge("normal", descriptor=path,
                               args=["--descriptor-sha256", sha], timeout=20)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("modifié", self.events(proc)[-1]["message"])
        self.assertEqual(self.calls(), [])  # l'agent n'a jamais été lancé

    def test_fichiers_borne_au_dossier_du_tour(self):
        dos = os.path.join(self.tmp, "travail")
        os.makedirs(dos)
        fichier = os.path.join(dos, "note.txt")
        dehors = os.path.join(self.tmp, "secret.txt")
        with open(dehors, "w", encoding="utf-8") as fh:
            fh.write("CONTENU_SECRET\n")
        proc = self.run_bridge("fs", cwd=dos,
                               env={"FAKE_ACP_FILE": fichier, "FAKE_ACP_OUTSIDE": dehors})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(fichier, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "écrit par l'agent\n")
        final = self.events(proc)[-1]
        self.assertIn("hors du dossier de travail", final["text"])
        self.assertNotIn("CONTENU_SECRET", final["text"])
        self.assertIn("fs refusé hors du dossier", proc.stderr)

    def test_modele_par_option_de_configuration(self):
        proc = self.run_bridge("normal", args=["--model", "modele-1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reglage = self.called("session/set_config_option")
        self.assertEqual(reglage[0]["configId"], "model")
        self.assertEqual(reglage[0]["value"], "modele-1")
        modeles = [e for e in self.events(proc) if e.get("model")]
        self.assertEqual(modeles[0]["model"], "modele-1")

    def test_authentification_nommee(self):
        doc = descriptor_doc(acp={"auth_method": "fake-auth"})
        proc = self.run_bridge("auth", descriptor=self.descriptor(doc))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.called("authenticate")[0]["methodId"], "fake-auth")

    def test_message_trop_grand_est_une_panne(self):
        env = {**os.environ, "FAKE_ACP_MODE": "garbage", "FAKE_ACP_GARBAGE_LEN": "5000"}
        peer = acp.JsonRpcPeer([sys.executable, FAKE_AGENT], cwd=self.tmp, env=env,
                               max_message=1024)
        try:
            with self.assertRaises(acp.AcpError) as ctx:
                peer.request("initialize", {}, timeout=5)
            self.assertIn("trop grand", str(ctx.exception))
        finally:
            peer.close()

    def test_usage_acp_alimente_le_grand_livre(self):
        """Le grand livre générique compte l'usage d'un harnais ACP (budget L13)."""
        from ameesh import cost as cost_mod
        state = os.path.join(self.tmp, "state")
        agent = os.path.join(state, "a")
        os.makedirs(agent)
        with open(os.path.join(agent, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "status", "phase": "step_end",
                "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                          "outputTokens": 0}}) + "\n")
        book = cost_mod.CostBook(state_dir=state, db=None, tools={"a": "faux-acp"})
        usage = book.turn_usage("a", 0, session="s1", model="")
        self.assertEqual(usage.input_tokens, 1_000_000)
        self.assertGreater(usage.usd, 0.0)


# ==========================================================================
# unités : politique de permission, confinement des chemins, lecteur de flux
# ==========================================================================

class PermissionTest(unittest.TestCase):
    def test_refus_par_defaut(self):
        decision, option, raison = acp.permission_decision(
            {}, {"kind": "execute"}, [{"optionId": "r", "kind": "reject_once"}])
        self.assertEqual((decision, option), ("deny", "r"))
        self.assertIn("refus par défaut", raison)

    def test_allow_et_deny_explicites(self):
        decision, option, _ = acp.permission_decision(
            {"default": "allow"}, {"kind": "read"},
            [{"optionId": "a", "kind": "allow_once"}])
        self.assertEqual((decision, option), ("allow", "a"))
        decision, option, raison = acp.permission_decision(
            {"default": "allow", "deny_kinds": ["execute"]}, {"kind": "execute"},
            [{"optionId": "r", "kind": "reject_once"}])
        self.assertEqual((decision, option), ("deny", "r"))
        self.assertIn("explicitement refusé", raison)

    def test_sans_option_utilisable_rend_cancelled(self):
        decision, option, _ = acp.permission_decision(
            {}, {"kind": "edit"}, [{"optionId": "x", "kind": "allow_once"}])
        self.assertEqual((decision, option), ("deny", None))


class ConfinementTest(unittest.TestCase):
    """`fs/*` : la frontière est la racine épinglée + `dir_fd` + `O_NOFOLLOW`,
    pas un contrôle de chemin suivi d'une ouverture par nom (course codex2)."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l16-fs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.racine = os.path.join(self.tmp, "travail")
        os.makedirs(self.racine)
        self.dehors = os.path.join(self.tmp, "secret.txt")
        with open(self.dehors, "w", encoding="utf-8") as fh:
            fh.write("secret")

    def racine_epinglee(self) -> int:
        """Un fd de racine épinglé pour le test, refermé au nettoyage."""
        fd = acp._open_root(self.racine)
        self.addCleanup(os.close, fd)
        return fd

    def lire(self, path: str) -> str:
        racine = self.racine_epinglee()
        fd = acp._open_beneath(racine, self.racine, path, os.O_RDONLY)
        with os.fdopen(fd, encoding="utf-8") as fh:
            return fh.read()

    def test_ouverture_dans_le_dossier(self):
        dedans = os.path.join(self.racine, "a.txt")
        with open(dedans, "w", encoding="utf-8") as fh:
            fh.write("a")
        self.assertEqual(self.lire(dedans), "a")
        self.assertEqual(self.lire("a.txt"), "a")  # relatif à la racine

    def test_dehors_et_traversee_refuses(self):
        racine = self.racine_epinglee()
        for chemin in (self.dehors, os.path.join(self.racine, "../../etc/passwd"),
                       "../secret.txt", self.racine):
            with self.subTest(chemin=chemin):
                with self.assertRaises(acp.AcpRpcError):
                    acp._open_beneath(racine, self.racine, chemin, os.O_RDONLY)

    def test_composant_double_point_refuse_meme_dans_la_racine(self):
        """`..` est refusé **syntaxiquement**, avant toute normalisation."""
        os.makedirs(os.path.join(self.racine, "sous"))
        with open(os.path.join(self.racine, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("a")
        with self.assertRaises(acp.AcpRpcError):
            self.lire("sous/../a.txt")

    def test_racine_atteinte_par_un_lien_refusee(self):
        """Un ancêtre du dossier de travail symbolique est refusé à l'acquisition."""
        lien = os.path.join(self.tmp, "racine-lien")
        os.symlink(self.racine, lien)
        with self.assertRaises(acp.AcpRpcError):
            acp._open_root(lien)

    def test_racine_epinglee_ne_suit_pas_le_remplacement_du_chemin(self):
        """La racine est un fd : remplacer son chemin ne déplace pas la frontière."""
        with open(os.path.join(self.racine, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("origine")
        racine = self.racine_epinglee()
        ailleurs = os.path.join(self.tmp, "piege")
        os.makedirs(ailleurs)
        with open(os.path.join(ailleurs, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("piege")
        os.rename(self.racine, self.racine + "-ancien")
        os.symlink(ailleurs, self.racine)
        fd = acp._open_beneath(racine, self.racine, "a.txt", os.O_RDONLY)
        with os.fdopen(fd, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "origine")

    def test_lien_refuse_meme_vers_un_dossier_interne(self):
        sous = os.path.join(self.racine, "sous")
        os.makedirs(sous)
        with open(os.path.join(sous, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("f")
        lien = os.path.join(self.racine, "raccourci")
        os.symlink(sous, lien)
        racine = self.racine_epinglee()
        with self.assertRaises(acp.AcpRpcError):
            acp._open_beneath(racine, self.racine, os.path.join(lien, "f.txt"),
                              os.O_RDONLY)

    def test_lien_final_refuse(self):
        lien = os.path.join(self.racine, "lien")
        os.symlink(self.dehors, lien)
        racine = self.racine_epinglee()
        with self.assertRaises(acp.AcpRpcError):
            acp._open_beneath(racine, self.racine, lien, os.O_RDONLY)

    def test_composant_remplace_par_un_lien_avant_l_ouverture(self):
        """Course : le composant est un dossier au contrôle, un lien à l'ouverture.

        Comme il n'y a plus de contrôle suivi d'une ouverture par nom, c'est
        l'ouverture elle-même qui refuse (aucun contenu extérieur n'est lu).
        """
        dossier = os.path.join(self.racine, "sous")
        os.makedirs(dossier)
        with open(os.path.join(dossier, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("interne")
        self.assertEqual(self.lire(os.path.join(dossier, "f.txt")), "interne")
        # remplacement du composant par un lien vers un dossier extérieur
        ext = os.path.join(self.tmp, "ext")
        os.makedirs(ext)
        with open(os.path.join(ext, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("CONTENU_SECRET")
        os.unlink(os.path.join(dossier, "f.txt"))
        os.rmdir(dossier)
        os.symlink(ext, dossier)
        with self.assertRaises(acp.AcpRpcError):
            self.lire(os.path.join(dossier, "f.txt"))

    def test_ecriture_a_travers_un_lien_refusee(self):
        dossier = os.path.join(self.racine, "sous")
        os.makedirs(dossier)
        lien = os.path.join(dossier, "f.txt")
        os.symlink(self.dehors, lien)
        racine = self.racine_epinglee()
        with self.assertRaises(acp.AcpRpcError):
            acp._open_beneath(racine, self.racine, lien,
                              os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with open(self.dehors, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "secret")  # jamais écrasé


class EnveloppeTest(unittest.TestCase):
    """Validation de l'enveloppe JSON-RPC (défaut 3 codex2)."""

    def test_enveloppes_refusees(self):
        mauvaises = [
            {},
            {"jsonrpc": "1.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": [1], "result": {}},
            {"jsonrpc": "2.0", "id": {"a": 1}, "result": {}},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "m"}},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": "x", "message": "m"}},
            {"jsonrpc": "2.0", "method": {"a": 1}, "params": {}},
            {"jsonrpc": "2.0", "method": "x", "params": "texte"},
            {"jsonrpc": "2.0", "method": "x", "id": True, "params": {}},
        ]
        for message in mauvaises:
            with self.subTest(message=message):
                with self.assertRaises(acp.AcpError):
                    acp._validate_message(message)

    def test_enveloppes_acceptees(self):
        bonnes = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": "abc", "result": None},
            {"jsonrpc": "2.0", "method": "session/update", "params": {}},
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "session/request_permission",
             "params": {}},
            {"jsonrpc": "2.0", "id": 4, "error": {"code": -32601, "message": "non"}},
        ]
        for message in bonnes:
            acp._validate_message(message)  # ne lève pas


class AcpStreamTest(unittest.TestCase):
    def parse(self, event: dict) -> dict:
        return adapters.AcpStream().parse(json.dumps(event))

    def test_correspondance_des_evenements(self):
        self.assertEqual(self.parse({"type": "session", "sessionId": "s"})["session"], "s")
        self.assertEqual(self.parse({"type": "text", "text": "salut"})["display"], ["salut"])
        self.assertIn("réflexion", self.parse({"type": "thought", "text": "hmm"})["display"][0])
        outil = self.parse({"type": "tool", "title": "lire", "status": "completed"})
        self.assertIn("lire", outil["display"][0])
        usage = self.parse({"type": "usage", "usage": {"input_tokens": 10},
                            "costUsd": 0.5})
        self.assertEqual(usage["usage"], {"input_tokens": 10})
        self.assertEqual(usage["cost"], 0.5)
        etape = self.parse({"type": "status", "phase": "step_end",
                            "usage": {"inputTokens": 42, "outputTokens": 1}})
        self.assertEqual(etape["usage"]["inputTokens"], 42)
        final = self.parse({"type": "final", "stopReason": "end_turn", "text": "fini",
                            "usage": {"input_tokens": 3}, "costUsd": 0.1})
        self.assertEqual(final["final"], "fini")
        self.assertEqual(final["cost"], 0.1)
        self.assertIn("end_turn", final["display"][0])
        refus = self.parse({"type": "final", "stopReason": "refusal"})
        self.assertIn("refusé", refus["error"])
        err = self.parse({"type": "error", "message": "boum"})
        self.assertEqual(err["error"], "boum")
        self.assertEqual(self.parse("pas du json"), {})


if __name__ == "__main__":
    unittest.main()
