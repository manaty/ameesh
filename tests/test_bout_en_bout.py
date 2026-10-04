# SPDX-License-Identifier: AGPL-3.0-only
"""Essai de bout en bout d'ameesh v1 sur le banc (spec §13, « v1 terminée »).

Un seul scénario, dans l'ordre de la spec, presque entièrement par la CLI :

1. canon d'exemple (organisation FICTIVE acme) dans un dépôt git temporaire
   (un dépôt nu tient lieu de branche canonique distante) ; `ameesh canon
   check` sans erreur ;
2. `ameesh canon sync` pour l'hôte `atelier` (agents orchestre et relecteur) ;
3. `agent-runner --once` réclame les deux agents factices et leur fait faire
   un tour (faux harnais de tests/fakebin — jamais claude ni codex) ;
4. message orchestre → relecteur : l'entrée apparaît dans le fil
   (`ameesh fil show acme-web`) et l'exécuteur réveille relecteur ;
5. action `git-merge` (faux gh — jamais le vrai) proposée, classe
   irréversible : refusée sans reçu ;
6. ameesh-approve lancé en local (127.0.0.1, port éphémère) ; la passkey
   LOGICIELLE d'alice (tests/webauthn_soft.py) est enrôlée par la voie canon :
   proposition du service → fiche Member → commit poussé (PR fusionnée) →
   `ameesh canon check` / `canon sync` ;
7. `ameesh action request` dépose la demande (jeton de service) et donne le
   lien ; signature au « téléphone » ; `ameesh action fetch-receipt`
   récupère le reçu, le vérifie et l'attache ; `ameesh receipt verify` ;
8. exécution : le faux gh fusionne puis perd la réponse (la relecture échoue
   aussi) → issue inconnue ; aucune nouvelle tentative automatique ;
9. `ameesh action reconcile` → confirmée ; une seule fusion ; le reçu ne se
   rejoue pas.

Compatibilité L3 : si la colonne `placement_ok` existe (lot L3 fusionné), le
verdict de placement écrit par `canon sync` est vérifié ; sinon ce contrôle est
sauté (rien d'autre ne dépend de L3 : la réclamation passe dans les deux cas).

Aucun contournement : la passkey fusionnée au canon devient active par
`ameesh canon sync` seul (§8.2).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from ameesh import canon, mail, receipts, registry

from . import banc_v1
from .support import FAKEBIN, PgTestCase
from .test_canon import EXAMPLE, commit_all, git, publish
from .webauthn_soft import ORIGIN, RP_ID

HOST = "atelier"
PROJECT = "acme-web"
ALICE = "human:alice"
ORCHESTRE = "agent:orchestre"
PR = "acme/acme-web#7"
FAKE_GH = os.path.join(FAKEBIN, "gh")
#: variables d'identité d'une session d'agent : jamais héritées du lanceur des tests
SESSION_VARS = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH")


class BoutEnBoutTest(PgTestCase):
    """L'essai de la spec §13, sur une base et un disque jetables."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.db.execute(
            "TRUNCATE actions, action_attempts, action_events, authenticators, "
            "standing_approvals, standing_reservations, mesh_consumed_nonces "
            "RESTART IDENTITY CASCADE")
        self.ws = os.path.join(self.tmp, "essai")
        self.travail = {name: os.path.join(self.ws, "travail", name)
                        for name in ("orchestre", "relecteur")}
        for path in self.travail.values():
            os.makedirs(path)
        self.gh_state = os.path.join(self.ws, "gh-etat.json")
        self.gh_log = os.path.join(self.ws, "gh-journal.jsonl")
        with open(self.gh_state, "w", encoding="utf-8") as fh:
            json.dump({"prs": {PR: {"state": "OPEN", "mergeCommit": None}}}, fh)
        self.absent = os.path.join(self.ws, "absent.json")
        self.clone = ""

    # -- environnement et commandes ------------------------------------------
    def e(self, **extra: str) -> dict:
        """Environnement des commandes : le canon de l'essai, l'hôte atelier, les
        faux harnais et le faux gh ; aucune configuration du développeur."""
        base = dict(
            # pas d'AMEESH_PROJECT : le fil d'un agent du canon suit son équipe (team)
            AMEESH_CANON=self.clone, AMEESH_HOST=HOST,
            AMEESH_HUMANS="alice,bruno",
            AMEESH_CONFIG=self.absent, AGENT_MESH_CONFIG=self.absent,
            AMEESH_APPROVE_CONFIG=self.absent,
            AMEESH_APPROVE_RP_ID=RP_ID, AMEESH_APPROVE_ORIGINS=ORIGIN,
            # ameesh, client d'ameesh-approve (posés une fois le service lancé)
            AMEESH_APPROVE_URL=getattr(self, "approve_url", None),
            AMEESH_APPROVE_TOKEN_FILE=getattr(self, "token_file", None),
            AMEESH_CLAUDE_BIN=os.path.join(FAKEBIN, "claude"),
            AMEESH_CODEX_BIN=os.path.join(FAKEBIN, "codex"),
            AMEESH_DSH_BIN=os.path.join(FAKEBIN, "dsh"),
            AMEESH_GH_BIN=FAKE_GH, AMEESH_FAKE_GH_STATE=self.gh_state,
            AMEESH_FAKE_GH_LOG=self.gh_log,
        )
        base.update(extra)
        env = self.env(**base)
        for name in SESSION_VARS:
            if name not in extra:
                env.pop(name, None)
        return env

    def run_mesh(self, *args: str, rc: int = 0, **extra: str) -> subprocess.CompletedProcess:
        proc = self.mesh(*args, env=self.e(**extra))
        self.assertEqual(proc.returncode, rc, "ameesh %s\n--- stdout\n%s--- stderr\n%s"
                         % (" ".join(args), proc.stdout, proc.stderr))
        return proc

    def run_approve(self, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, "-m", "ameesh.approve", *args],
                              capture_output=True, text=True, timeout=60, env=self.e(),
                              cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, "ameesh-approve %s\n%s%s"
                         % (" ".join(args), proc.stdout, proc.stderr))
        return proc

    def run_runner(self) -> str:
        proc = self.runner("--once", "--host", HOST, env=self.e())
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        return out

    def gh_calls(self, command: str) -> int:
        if not os.path.exists(self.gh_log):
            return 0
        with open(self.gh_log, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip()
                       and json.loads(line)["argv"][:2] == ["pr", command])

    def pr(self) -> dict:
        with open(self.gh_state, encoding="utf-8") as fh:
            return json.load(fh)["prs"][PR]

    def has_placement_column(self) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = 'agent_registry' AND column_name = 'placement_ok'",
            (self.schema,)))

    # -- ameesh-approve, sous-processus réel ---------------------------------
    def start_approve(self) -> int:
        directory = os.path.join(self.ws, "approve")
        os.makedirs(directory, mode=0o700)
        self.token_file = os.path.join(directory, "service-token")
        self.approve_state = os.path.join(directory, "etat")
        self.run_approve("gen-token", "--token-file", self.token_file)
        with open(self.token_file, encoding="utf-8") as fh:
            self.service_token = fh.read().strip()
        log_path = os.path.join(directory, "serve.log")
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "ameesh.approve", "serve", "--bind", "127.0.0.1",
             "--port", "0", "--token-file", self.token_file, "--state-dir", self.approve_state],
            stdout=log, stderr=subprocess.STDOUT, env=self.e(), cwd=self.tmp)

        def stop() -> None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
            log.close()
        self.addCleanup(stop)

        def port() -> int | None:
            with open(log_path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
            match = re.search(r"http://127\.0\.0\.1:(\d+)", text)
            if match:
                return int(match.group(1))
            if proc.poll() is not None:
                self.fail("ameesh-approve s'est arrêté (code %s) :\n%s" % (proc.returncode, text))
            return None
        found = self.wait_for(port, timeout=30)
        self.approve_url = "http://127.0.0.1:%d" % found
        return found

    # -- l'essai ---------------------------------------------------------------
    def test_essai_de_bout_en_bout(self):
        self.etape_1_canon_check()
        self.etape_2_canon_sync()
        self.etape_3_executeur()
        self.etape_4_message_et_fil()
        action_id = self.etape_5_action_refusee_sans_recu()
        port = self.start_approve()
        soft = self.etape_6_enrolement_par_le_canon(port)
        receipt_path = self.etape_7_recu(port, soft, action_id)
        self.etape_8_issue_inconnue(action_id, receipt_path)
        self.etape_9_reconciliation(action_id, receipt_path)

    # 1 ----------------------------------------------------------------------
    def etape_1_canon_check(self) -> None:
        """Canon d'exemple, cwd des placements de l'hôte atelier ramenés au bac à sable."""
        source = os.path.join(self.ws, "source-canon")
        shutil.copytree(EXAMPLE, source)
        for fiche, agent in (("orchestre-atelier.md", "orchestre"),
                             ("relecteur-atelier.md", "relecteur")):
            path = os.path.join(source, "placements", fiche)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            text, count = re.subn(r"(?m)^cwd: .*$", "cwd: %s" % self.travail[agent], text)
            self.assertEqual(count, 1, fiche)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        _bare, self.clone = publish(os.path.join(self.ws, "depots"), "acme", source)
        self.commit = git(self.clone, "rev-parse", "origin/main")

        proc = self.run_mesh("canon", "check")
        self.assertIn("canon valide — 0 erreur(s)", proc.stdout)
        self.assertIn("état du canon pour %s : ok" % HOST, proc.stdout)
        self.assertIn(self.commit[:12], proc.stdout)            # lu au commit canonique
        self.assertNotIn("NON APPROUVÉ", proc.stdout)

    # 2 ----------------------------------------------------------------------
    def etape_2_canon_sync(self) -> None:
        # premier amorçage du registre des authentificateurs : la branche
        # canonique de confiance est donnée explicitement (journalisé), jamais
        # tirée du commit lu
        report = json.loads(self.run_mesh("canon", "sync", "--json",
                                          "--bootstrap-ref", "main").stdout)
        self.assertEqual((report["host"], report["canon_status"], report["errors"],
                          report["untrusted"]), (HOST, "ok", 0, False))
        self.assertEqual((report["authenticators"]["status"], report["authenticators"]["trust"],
                          report["authenticators"]["branch"]),
                         ("synced", "bootstrap", "origin/main"))
        self.assertEqual({a["agent"]: a["action"] for a in report["actions"]},
                         {"orchestre": "créé", "relecteur": "créé"})
        for name, harness in (("orchestre", "claude"), ("relecteur", "codex")):
            row = registry.get(self.db, name)
            self.assertEqual((row["host"], row["harness"], row["responsible"], row["team"],
                              row["cwd"], row["status"]),
                             (HOST, harness, ALICE, PROJECT, self.travail[name], "idle"))
            self.assertEqual(row["canon_ref"],
                             "canon:agents/%s.md@%s" % (name, self.commit))
        self.assertIsNone(registry.get(self.db, "ouvrier"))     # placé sur le banc
        if self.has_placement_column():
            # L3 fusionné : le verdict de placement est écrit par canon sync (C4)
            rows = {r["name"]: r for r in self.db.query(
                "SELECT name, placement_ok FROM agent_registry ORDER BY name")}
            self.assertEqual({n: r["placement_ok"] for n, r in rows.items()},
                             {"orchestre": True, "relecteur": True})
        proc = self.run_mesh("canon", "check")
        self.assertIn("registre  : ok pour %s" % HOST, proc.stdout)

    # 3 ----------------------------------------------------------------------
    def etape_3_executeur(self) -> None:
        """Une consigne d'alice à chacun ; un passage de l'exécuteur les réclame tous deux."""
        for name in ("orchestre", "relecteur"):
            proc = self.run_mesh("mail", "send", name,
                                 "Bonjour %s, le lot 7 d'acme-web est à préparer." % name,
                                 "--from", "alice")
            self.assertIn("déposé pour : %s" % name, proc.stdout)
        out = self.run_runner()
        for name in ("orchestre", "relecteur"):
            self.assertIn("bail acquis : %s" % name, out)
        turns = self.turns()
        self.assertEqual(sorted((t["harness"], t["env"]["AGENT_MAIL_NAME"]) for t in turns),
                         [("claude", "orchestre"), ("codex", "relecteur")], out)
        for turn in turns:
            self.assertEqual((turn["env"]["AMEESH_HOST"], turn["env"]["AMEESH_SCHEMA"]),
                             (HOST, self.schema))
            self.assertTrue(turn["env"]["AMEESH_LEASE_EPOCH"])   # identité liée au bail
        for name, session in (("orchestre", "claude-sess-1"), ("relecteur", "codex-thread-1")):
            row = registry.get(self.db, name)
            self.assertEqual((row["status"], int(row["turns"]), row["session_id"],
                              row.get("lease_owner")), ("idle", 1, session, None))
            self.assertEqual(mail.unread(self.db, name), [])
        proc = self.run_mesh("list")
        self.assertIn("orchestre", proc.stdout)
        self.assertIn("relecteur", proc.stdout)

    # 4 ----------------------------------------------------------------------
    def etape_4_message_et_fil(self) -> None:
        text = ("Relecteur, le lot 7 est prêt : relis la PR %s avant la fusion." % PR)
        proc = self.run_mesh("mail", "send", "relecteur", text, AGENT_MAIL_NAME="orchestre")
        self.assertIn("déposé pour : relecteur", proc.stdout)

        proc = self.run_mesh("fil", "show", PROJECT)
        self.assertRegex(proc.stdout, r"(?m)^### \S+ — human:alice → agent:orchestre$")
        self.assertRegex(proc.stdout, r"(?m)^### \S+ — agent:orchestre → agent:relecteur$")
        self.assertIn(text, proc.stdout)
        self.assertIn(PROJECT, self.run_mesh("fil", "list").stdout)

        before = len(self.turns())
        out = self.run_runner()
        turns = self.turns()[before:]
        self.assertEqual([(t["harness"], t["env"]["AGENT_MAIL_NAME"]) for t in turns],
                         [("codex", "relecteur")], out)
        self.assertEqual(mail.unread(self.db, "relecteur"), [])

    # 5 ----------------------------------------------------------------------
    def etape_5_action_refusee_sans_recu(self) -> str:
        proc = self.run_mesh(
            "action", "propose", "--connector", "git-merge", "--operation", "merge",
            "--project", PROJECT, "--target", PR, "--args", '{"method": "squash"}',
            "--approver", ALICE, "--json", AGENT_MAIL_NAME="orchestre")
        action = json.loads(proc.stdout)
        self.assertEqual((action["state"], action["class"], action["requires_receipt"],
                          action["dedupe"], action["proposed_by"]),
                         ("proposed", "irreversible", True, "none", ORCHESTRE))
        self.action = action
        action_id = action["action_id"]
        proc = self.run_mesh("action", "execute", action_id, rc=1)
        self.assertIn("[receipt_required]", proc.stderr)
        self.assertEqual(self.gh_calls("merge"), 0)              # le monde n'a pas été touché
        self.assertEqual(self.pr()["state"], "OPEN")
        proc = self.run_mesh("decisions", "--for", ALICE)
        self.assertIn("actions à approuver", proc.stdout)
        self.assertIn(action_id, proc.stdout)
        return action_id

    # 6 ----------------------------------------------------------------------
    def etape_6_enrolement_par_le_canon(self, port: int):
        link = self.run_approve("enroll-link", "--approver", ALICE,
                                "--state-dir", self.approve_state).stdout.strip()
        soft = banc_v1.new_passkey()
        result = banc_v1.enroll(port, link, soft)
        self.assertEqual(result["credential_id"], soft.credential_id)
        proposal = os.path.join(self.approve_state, "proposals", result["proposal"])
        # rien d'actif avant la PR du canon : ni registre, ni demande possible
        self.assertEqual(receipts.list_authenticators(self.db, approver=ALICE), [])
        proc = self.run_mesh("action", "request", self.action["action_id"], "--approver", ALICE,
                             rc=1, AGENT_MAIL_NAME="orchestre")
        self.assertIn("[no_authenticator]", proc.stderr)

        # la « PR » : l'entrée proposée rejoint la fiche Member, commit poussé
        banc_v1.add_to_member(os.path.join(self.clone, "membres", "alice.md"),
                              banc_v1.proposal_entry(proposal))
        self.commit = commit_all(self.clone, "canon : passkey d'alice (PR revue, fusionnée)")
        loaded = canon.load(self.clone)
        alice = next(m for m in loaded.members if m.title == "alice")
        self.assertEqual([a["credential_id"] for a in alice.authenticators],
                         [soft.credential_id])
        self.assertIn("canon valide — 0 erreur(s)", self.run_mesh("canon", "check").stdout)
        report = json.loads(self.run_mesh("canon", "sync", "--json").stdout)
        self.assertEqual((report["canon_status"], report["errors"]), ("ok", 0))
        # canon sync recopie Member.authenticators dans le registre de confiance (§8.2)
        done = report["authenticators"]
        self.assertEqual((done["status"], done["errors"], done["added"]),
                         ("synced", [], ["%s/webauthn/%s" % (ALICE, soft.credential_id)]))
        # la branche de confiance vient du dernier commit appliqué (étape 2)
        self.assertEqual((done["trust"], done["branch"]), ("applied", "origin/main"))
        rows = receipts.list_authenticators(self.db, approver=ALICE)
        self.assertEqual([(r["facade"], r["credential_id"]) for r in rows],
                         [("webauthn", soft.credential_id)])
        proc = self.run_mesh("authenticator", "list", "--approver", ALICE)
        self.assertIn("membres/alice.md@%s" % self.commit, proc.stdout)
        return soft

    # 7 ----------------------------------------------------------------------
    def etape_7_recu(self, port: int, soft, action_id: str) -> str:
        # l'orchestre dépose la demande : ameesh-approve rend le lien du téléphone
        proc = self.run_mesh("action", "request", action_id, "--approver", ALICE, "--json",
                             AGENT_MAIL_NAME="orchestre")
        data = json.loads(proc.stdout)
        self.assertEqual((data["digest"], data["action_digest"], data["approver"]),
                         (self.action["digest"], self.action["digest"], ALICE))
        proc = self.run_mesh("action", "fetch-receipt", action_id, rc=5)
        self.assertIn("pas encore signée", proc.stdout)           # en attente du téléphone

        done = banc_v1.approve(port, data["link"], soft)
        self.assertEqual(done["result"], {"request_id": data["request_id"],
                                          "decision": "approve"})
        self.assertIn(PR, done["summary"])                       # résumé recalculé par le service
        receipt_path = os.path.join(self.ws, "recu.json")
        proc = self.run_mesh("action", "fetch-receipt", action_id, "--out", receipt_path,
                             "--by", ALICE)
        self.assertIn("reçu de la demande %s vérifié et attaché : action %s approved"
                      % (data["request_id"], action_id), proc.stdout)
        with open(receipt_path, encoding="utf-8") as fh:
            receipt = json.load(fh)
        self.assertEqual((receipt["v"], receipt["facade"], receipt["request"]["approver"],
                          receipt["request"]["action_id"], receipt["request"]["requested_by"]),
                         ("ameesh-receipt/1", "webauthn", ALICE, action_id, ORCHESTRE))
        proc = self.run_mesh("receipt", "verify", receipt_path, "--digest",
                             self.action["digest"], "--action-id", action_id)
        self.assertIn("reçu VALIDE : approve par %s (webauthn" % ALICE, proc.stdout)
        # le lien est dans le fil du projet ; le jeton de service nulle part
        thread = self.run_mesh("fil", "show", PROJECT).stdout
        self.assertIn("Demande d'approbation de l'action %s" % action_id, thread)
        self.assertIn(data["link"], thread)
        self.assertNotIn(self.service_token, thread)
        return receipt_path

    # 8 ----------------------------------------------------------------------
    def etape_8_issue_inconnue(self, action_id: str, receipt_path: str) -> None:
        # le faux gh fusionne puis perd la réponse ; la relecture échoue aussi
        proc = self.run_mesh("action", "execute", action_id, rc=4,
                             AMEESH_FAKE_GH_MODE="lose", AMEESH_FAKE_GH_VIEW="absent")
        self.assertIn("unknown", proc.stdout)
        self.assertEqual(self.pr()["state"], "MERGED")           # l'effet a eu lieu
        self.assertEqual(self.gh_calls("merge"), 1)
        proc = self.run_mesh("decisions", "--for", ALICE)
        self.assertIn("issues inconnues à trancher", proc.stdout)
        self.assertIn(action_id, proc.stdout)
        # aucune nouvelle tentative : ni exécution, ni nouvelle liaison du reçu
        self.assertIn("refus [state] : action %s : issue inconnue" % action_id,
                      self.run_mesh("action", "execute", action_id, rc=1).stderr)
        self.run_mesh("action", "approve", action_id, "--receipt", receipt_path, rc=1)
        self.assertEqual(self.gh_calls("merge"), 1)

    # 9 ----------------------------------------------------------------------
    def etape_9_reconciliation(self, action_id: str, receipt_path: str) -> None:
        proc = self.run_mesh("action", "reconcile", action_id)
        merged = self.pr()["mergeCommit"]["oid"]
        self.assertIn("réconciliée : confirmed (%s)" % merged, proc.stdout)
        shown = json.loads(self.run_mesh("action", "show", action_id, "--json").stdout)
        self.assertEqual((shown["state"], shown["attempts"]), ("confirmed", 1))
        events = [event["event"] for event in shown["events"]]
        self.assertIn("approval_requested", events)               # la demande, au journal
        events = [event for event in events if event != "approval_requested"]
        self.assertEqual(events[:3], ["proposed", "approved", "launched"], events)
        self.assertIn("unknown", events)
        self.assertEqual(events[-1], "confirmed", events)
        attempt, = shown["attempts_detail"]
        self.assertEqual((attempt["state"], attempt["approver"], attempt["external_ref"]),
                         ("confirmed", ALICE, merged))
        self.assertEqual((self.pr()["merges"], self.gh_calls("merge")), (1, 1))  # aucun doublon
        # le nonce a été consommé au lancement : le reçu ne se rejoue pas
        proc = self.run_mesh("receipt", "verify", receipt_path, "--digest",
                             self.action["digest"], "--action-id", action_id, rc=1)
        self.assertIn("[replay]", proc.stderr)
        self.assertIn("aucune décision en attente",
                      self.run_mesh("decisions", "--for", ALICE).stdout)
        # chaque transition est lisible dans le fil du projet (§7.1)
        thread = self.run_mesh("fil", "show", PROJECT).stdout
        for text in ("Action %s proposée par %s : git-merge merge sur %s" % (
                         action_id, ORCHESTRE, PR),
                     "Action %s approuvée" % action_id, "Action %s lancée" % action_id,
                     "Action %s — issue INCONNUE" % action_id,
                     "Action %s confirmée" % action_id):
            self.assertIn(text, thread)
        self.assertRegex(thread, r"(?m)^### \S+ — agent:orchestre → human:alice$")


if __name__ == "__main__":
    import unittest
    unittest.main()
