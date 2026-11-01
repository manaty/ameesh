# SPDX-License-Identifier: AGPL-3.0-only
"""CLI `agent-mesh` : mesh list, show, clés, approbations, work_items."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import authority, mail, migrations, registry, signing, storage

from .support import EMPTY_CONFIG, PgTestCase

OWNER = "proprietaire"
AGENT = "deepseek7"


class MeshCliTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register(OWNER, "claude", cwd=self.tmp)
        self.register(AGENT, "deepseek", cwd=self.tmp)
        self.seed = signing.backend("pure").generate_seed()
        self.public = signing.backend("pure").public_from_seed(self.seed)
        self.fingerprint = signing.fingerprint(self.public)
        # la clé de test sur disque (0600) pour la CLI
        self.key_path = os.path.join(self.tmp, "owner.key")
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(self.seed))
        os.chmod(self.key_path, 0o600)

    # -- list / show -------------------------------------------------------
    def test_list_et_json(self):
        authority.register_key(self.db, OWNER, self.public, role="owner")
        mail.send(self.db, OWNER, AGENT, "un message")
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NAME", proc.stdout)
        self.assertIn(OWNER, proc.stdout)
        self.assertIn(AGENT, proc.stdout)
        self.assertIn("UNREAD", proc.stdout)
        lignes = [l for l in proc.stdout.splitlines() if l.startswith(AGENT)]
        self.assertEqual(len(lignes), 1)
        # La fraîcheur est un âge, pas une valeur figée : sous charge, « 0s »
        # peut être « 2s » (suite complète, 279 s). On borne, on ne fige pas.
        # Le compte de non-lus est vérifié plus haut sur la sortie JSON.
        age = re.search(r"(\d+)s ago", lignes[0])
        self.assertIsNotNone(age, lignes[0])
        self.assertLessEqual(int(age.group(1)), 60)
        ligne_owner = [l for l in proc.stdout.splitlines() if l.startswith(OWNER)][0]
        self.assertIn("owner", ligne_owner)  # clé propriétaire enregistrée

        proc = self.mesh("list", "--json")
        rows = json.loads(proc.stdout)
        par_nom = {row["name"]: row for row in rows}
        self.assertEqual(int(par_nom[AGENT]["unread"]), 1)
        self.assertTrue(par_nom[OWNER]["key_ready"])
        self.assertFalse(par_nom[AGENT]["key_ready"])

    def test_show(self):
        proc = self.mesh("show", AGENT)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("harness  : deepseek", proc.stdout)
        self.assertIn("no public key", proc.stdout)
        proc = self.mesh("show", "inconnu")
        self.assertEqual(proc.returncode, 1)

    def test_list_json_does_not_read_work_delays(self):
        """The staging registry read must work even if lot timelines fail."""
        from ameesh import mesh_cli
        with contextlib.closing(self.connect()) as connection:
            expected = mesh_cli.agent_rows(self.cfg, connection)
        output = io.StringIO()
        with mock.patch.object(mesh_cli, "_open", side_effect=lambda cfg: self.connect()), \
                mock.patch.object(mesh_cli.work, "delays", side_effect=AssertionError(
                    "JSON registry reads must not load lot timelines")) as delays, \
                contextlib.redirect_stdout(output):
            self.assertEqual(mesh_cli.cmd_list(self.cfg, argparse.Namespace(json=True)), 0)
        delays.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), expected)

    # -- clés --------------------------------------------------------------
    def test_key_generate_refuse_sans_drapeau_proprietaire(self):
        proc = self.mesh("key", "generate", "--out", os.path.join(self.tmp, "cles"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--i-am-the-owner", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cles")))

    def test_key_cycle_cli(self):
        dossier = os.path.join(self.tmp, "cles")
        proc = self.mesh("key", "generate", "--out", dossier, "--name", "test",
                         "--i-am-the-owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        public_path = os.path.join(dossier, "test.pub")
        self.assertTrue(os.path.exists(public_path))
        empreinte = signing.fingerprint(signing.read_public(public_path))

        proc = self.mesh("key", "register", OWNER, "--public-key", public_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(empreinte, proc.stdout)
        self.assertIn("role agent", proc.stdout)  # moindre privilège par défaut

        proc = self.mesh("key", "show", OWNER, "--json")
        self.assertEqual(json.loads(proc.stdout)["public_key_fingerprint"], empreinte)
        self.assertEqual(json.loads(proc.stdout)["key_role"], "agent")

        proc = self.mesh("key", "list")
        self.assertIn(OWNER, proc.stdout)
        self.assertIn("agent", proc.stdout)

        # acte explicite : la même clé devient clé propriétaire
        proc = self.mesh("key", "register", OWNER, "--public-key", public_path,
                         "--role", "owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("owner's authority", proc.stdout)
        self.assertEqual(
            json.loads(self.mesh("key", "show", OWNER, "--json").stdout)["key_role"], "owner")

        proc = self.mesh("key", "revoke", OWNER)
        self.assertEqual(proc.returncode, 0)
        proc = self.mesh("key", "show", OWNER, "--json")
        self.assertTrue(json.loads(proc.stdout)["key_revoked_ts"])
        # révoquer deux fois → erreur
        self.assertEqual(self.mesh("key", "revoke", OWNER).returncode, 1)

    def test_key_register_cle_privee_refusee(self):
        proc = self.mesh("key", "register", OWNER, "--public-key", self.key_path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error", proc.stderr)

    # -- approbations ------------------------------------------------------
    def test_approve_verify_consume(self):
        authority.register_key(self.db, OWNER, self.public, role="owner")
        artifact = authority.hash_artifact("diff --git a/x b/x\n+1")
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, "--kind", "diff", "--as", OWNER,
                         "--expires", "48h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("approval #1", proc.stdout)
        self.assertIn(self.fingerprint[:16], proc.stdout)

        proc = self.mesh("verify", "--action", "merge", "--hash", artifact, "--json")
        resultat = json.loads(proc.stdout)
        self.assertTrue(resultat["ok"], resultat["reason"])
        self.assertEqual(resultat["approval"]["approver"], OWNER)

        proc = self.mesh("verify", "--action", "merge", "--hash", artifact,
                         "--consume", "--by", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("consumed", proc.stdout)
        proc = self.mesh("verify", "--action", "merge", "--hash", artifact)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("consumed", proc.stderr)

    def test_approve_sans_identite_refuse(self):
        """Signer une approbation ne se déduit pas du dossier (incident mesh-design)."""
        authority.register_key(self.db, OWNER, self.public, role="owner")
        artifact = authority.hash_artifact("diff sans identité")
        env = self.env()
        env.pop("AGENT_MAIL_NAME", None)
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not bound", proc.stderr)
        self.assertEqual(authority.list_approvals(self.db), [])

        # identité explicite : l'approbation passe, sans --as
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, env=self.env(AGENT_MAIL_NAME=OWNER))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(authority.list_approvals(self.db)), 1)

    def test_approve_refuse_avec_cle_d_agent(self):
        """Une clé d'agent ne peut pas approuver, même enregistrée."""
        proc = self.mesh("key", "register", OWNER, "--public-key",
                         self._public_path(), "--role", "agent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        artifact = authority.hash_artifact("diff d'agent")
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, "--as", OWNER)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("role", proc.stderr)
        self.assertEqual(authority.list_approvals(self.db), [])

    def _public_path(self) -> str:
        path = os.path.join(self.tmp, "owner.pub")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_public(self.public))
        return path

    def test_approve_refuse_sans_cle_enregistree(self):
        artifact = authority.hash_artifact("peu importe")
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, "--as", OWNER)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("public key", proc.stderr)
        self.assertEqual(authority.list_approvals(self.db), [])

    def test_approvals_liste(self):
        authority.register_key(self.db, OWNER, self.public, role="owner")
        artifact = authority.hash_artifact("release")
        authority.create_approval(
            self.db, self.seed, approver=OWNER, action="production",
            artifact_kind="release", artifact_hash=artifact, ttl=3600)
        proc = self.mesh("approvals", "--json")
        rows = json.loads(proc.stdout)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["verdict"])
        proc = self.mesh("approvals")
        self.assertIn("VALID", proc.stdout)

    # -- work_items --------------------------------------------------------
    def test_work_cycle_cli(self):
        proc = self.mesh("work", "add", "--title", "Banc v1 points 3-5",
                         "--type", "evolution", "--app", "nexlink", "--new-project",
                         "--assignee", AGENT, "--actor", "orchestrateur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #1", proc.stdout)

        proc = self.mesh("work", "list", "--json")
        rows = json.loads(proc.stdout)
        self.assertEqual(rows[0]["state"], "intake")
        self.assertEqual(rows[0]["assignee"], AGENT)

        for state in ("build", "qa", "build", "qa", "build"):
            proc = self.mesh("work", "move", "1", state, "--actor", AGENT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", "1", "qa")
        self.assertEqual(proc.returncode, 0)
        proc = self.mesh("work", "move", "1", "build")  # 3e retour : refusé
        self.assertEqual(proc.returncode, 1)
        self.assertIn("exhausted", proc.stderr)

        proc = self.mesh("work", "move", "1", "blocked", "--note", "attente humaine")
        self.assertEqual(proc.returncode, 0)
        proc = self.mesh("work", "note", "1", "relance demain", "--actor", "codex3")
        self.assertEqual(proc.returncode, 0)

        proc = self.mesh("work", "show", "1", "--json")
        item = json.loads(proc.stdout)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["events"][0]["note"], "relance demain")
        self.assertEqual(item["events"][-1]["note"],   # #506: an undeclared project
                         "created; project 'nexlink' introduced by orchestrateur (--new-project)")

    def test_work_etat_inconnu(self):
        proc = self.mesh("work", "add", "--title", "x")
        self.assertEqual(proc.returncode, 0)
        proc = self.mesh("work", "move", "1", "promu")
        self.assertEqual(proc.returncode, 2)  # choix argparse

    def test_import_export_v0(self):
        """Bascule : la boîte fichier v0 entre en base, et sait en ressortir."""
        state = os.path.join(self.tmp, "v0state")
        inbox = os.path.join(state, "inbox", AGENT)
        os.makedirs(inbox)
        ts = 1770000000.0
        for index, (sender, text) in enumerate([("orchestrateur", "premier"), ("codex3", "deuxième")]):
            with open(os.path.join(inbox, "%d-%s-%d.json" % (int(ts * 1000) + index, sender, index)),
                      "w", encoding="utf-8") as fh:
                json.dump({"from": sender, "to": AGENT, "ts": ts + index, "text": text,
                           "host": "laptop"}, fh)
        os.makedirs(os.path.join(state, "agents"))
        with open(os.path.join(state, "agents", AGENT + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"name": AGENT, "tool": "deepseek", "cwd": self.tmp,
                       "session_id": "sess-v0", "last_seen": ts}, fh)

        proc = self.mesh("import-v0", "--state", state, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) to import", proc.stdout)
        self.assertEqual(len(os.listdir(inbox)), 2)  # rien n'a bougé
        self.assertIsNone(registry.get(self.db, AGENT)["session_id"])  # registre compris

        proc = self.mesh("import-v0", "--state", state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) imported", proc.stdout)
        messages = mail.unread(self.db, AGENT)
        self.assertEqual([m["body"] for m in messages], ["premier", "deuxième"])
        self.assertEqual([m["sender"] for m in messages], ["orchestrateur", "codex3"])
        self.assertAlmostEqual(float(messages[0]["created_ts"]), ts, places=3)
        self.assertEqual(len(os.listdir(os.path.join(inbox, "imported"))), 2)
        row = registry.get(self.db, AGENT)
        self.assertEqual(row["harness"], "deepseek")
        self.assertEqual(row["session_id"], "sess-v0")
        self.assertEqual(row["cwd"], self.tmp)

        cible = os.path.join(self.tmp, "v0back")
        proc = self.mesh("export-v0", "--state", cible, "--agents", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) exported", proc.stdout)
        fichiers = sorted(os.listdir(os.path.join(cible, "inbox", AGENT)))
        self.assertEqual(len(fichiers), 2)
        with open(os.path.join(cible, "inbox", AGENT, fichiers[0]), encoding="utf-8") as fh:
            exporte = json.load(fh)
        self.assertEqual(exporte["from"], "orchestrateur")
        self.assertEqual(exporte["text"], "premier")
        self.assertEqual(mail.unread(self.db, AGENT), [])  # déplacés, pas copiés

    def test_migrate_noop_et_doctor(self):
        proc = self.mesh("migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("up to date", proc.stdout)
        proc = self.doctor()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict    : OK", proc.stdout)
        total = len(migrations.discover())
        self.assertIn("%d/%d applied" % (total, total), proc.stdout)

    def doctor(self, problemes: tuple = ()) -> subprocess.CompletedProcess:
        """`doctor` sans la garde d'installation de l'hôte (#5287).

        Le banc du composeur joue les tests avec le Python du venv installé et
        `PYTHONPATH` vers son arbre jetable : la garde #2763 y dit, à raison,
        « loaded outside the venv ». Ce test porte sur le schéma de test, pas
        sur l'installation de l'hôte : on fixe donc la réponse de la garde,
        ici seulement, et `doctor` reste rouge sur un écart.
        """
        code = ("import sys\n"
                "from ameesh import main, venv_guard\n"
                "venv_guard.installation_problems = lambda prefix=None: %r\n"
                "sys.exit(main.main(['doctor']))\n" % (list(problemes),))
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60.0,
            env=self.env(), cwd=self.tmp,
        )

    def test_doctor_reste_rouge_sur_un_ecart_d_installation(self):
        # #5287 : le banc fixe la réponse de la garde, il ne la retire pas
        proc = self.doctor(("editable install: /tmp/x.pth",))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("install    : KO — editable install: /tmp/x.pth", proc.stdout)
        self.assertIn("verdict    : KO", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class DelaysCliTest(MeshCliTest):
    """R19 : `ameesh work` et `ameesh list` exposent les jalons et les délais."""

    def test_jalons_et_delais_dans_work_et_list(self):
        proc = self.mesh("work", "add", "--title", "Délais", "--assignee", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", "1", "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "milestone", "1", "frozen", "--sha", "a" * 40,
                         "--actor", AGENT,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("milestone frozen", proc.stdout)

        # un lot gelé : le pied de `list` dit ce qui attend une revue
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(": 1 (freeze 1)", proc.stdout)
        self.assertIn("oldest freeze", proc.stdout)

        # #2962 : un SHA complet doit se résoudre ou nommer la tête gelée
        # courante ; ce témoin-ci suit la frise, pas la porte d'écriture.
        proc = self.mesh("work", "milestone", "1", "verdict", "ok", "--sha", "a" * 40,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DELAY", proc.stdout)
        self.assertIn("review", proc.stdout)

        proc = self.mesh("work", "list", "--json")
        lot = json.loads(proc.stdout)[0]
        self.assertIsNotNone(lot["delays"]["frozen_ts"])
        self.assertIsNotNone(lot["delays"]["reviewed_ts"])
        self.assertEqual(lot["delays"]["reviewed_verdict"], "ok")
        self.assertEqual(lot["delays"]["verdicts"], 1)
        self.assertIsNone(lot["delays"]["merged_ts"])

        proc = self.mesh("work", "show", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("milestones", proc.stdout)
        self.assertIn("delays", proc.stdout)
        self.assertIn("request → freeze", proc.stdout)

        proc = self.mesh("work", "show", "1", "--json")
        item = json.loads(proc.stdout)
        self.assertEqual(item["delays"]["reviewed_verdict"], "ok")
        self.assertEqual([m["kind"] for m in item["milestones"]].count("verdict"), 1)

        proc = self.mesh("list")
        self.assertIn(": 1 (review 1)", proc.stdout)
        self.assertNotIn("oldest freeze", proc.stdout)

        # un jalon automatique ne se déclare pas à la main (choix argparse)
        proc = self.mesh("work", "milestone", "1", "requested")
        self.assertEqual(proc.returncode, 2)

        # la forme de `list --json` ne change pas : un tableau d'agents
        proc = self.mesh("list", "--json")
        self.assertIsInstance(json.loads(proc.stdout), list)

    def test_un_jalon_d_agent_ne_se_signe_pas_au_nom_d_un_autre(self):
        """#2928 : `--actor` d'un autre agent est refusé au chemin d'écriture."""
        self.mesh("work", "add", "--title", "Jalon étranger", "--assignee", AGENT)
        self.mesh("work", "move", "1", "build")
        proc = self.mesh("work", "milestone", "1", "verdict", "ok",
                         "--sha", "a" * 40, "--actor", OWNER,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("milestone refused", proc.stderr)
        self.assertIn(OWNER, proc.stderr)
        item = json.loads(self.mesh("work", "show", "1", "--json").stdout)
        self.assertEqual([m for m in item["milestones"]
                          if m["kind"] == "verdict"], [])

    def test_gel_effectif_derive_dans_work_show_et_list(self):
        """#1756 : `show`/`list --freeze` exposent le gel effectif et sa provenance."""
        self.mesh("work", "add", "--title", "Gel dérivé", "--assignee", AGENT)
        self.mesh("work", "move", "1", "build")
        # avis favorable sans gel déclaré : le candidat vient de l'avis.
        # #2962 : ce témoin lit la frise, pas la porte d'écriture du jalon —
        # un SHA complet synthétique y serait refusé ; le jalon est posé au
        # stockage, comme les autres fixtures de lecture.
        storage.of(self.db).work.add_milestone(
            1, "verdict", sha="a" * 40, actor=AGENT, verdict="ok", note="")

        item = json.loads(self.mesh("work", "show", "1", "--json").stdout)
        self.assertEqual(item["freeze"]["sha"], "a" * 40)
        self.assertEqual(item["freeze"]["origin"], "verdict")
        self.assertEqual(item["freeze"]["causes"], [])

        rows = json.loads(self.mesh("work", "list", "--json", "--freeze").stdout)
        self.assertEqual(rows[0]["freeze"]["sha"], "a" * 40)
        self.assertEqual(rows[0]["freeze"]["origin"], "verdict")
        self.assertIsNone(rows[0]["freeze"]["declared"])

        # sans --freeze, la forme de `list --json` ne change pas
        rows = json.loads(self.mesh("work", "list", "--json").stdout)
        self.assertNotIn("freeze", rows[0])

        # un gel déclaré sur le même commit l'emporte, en forme complète
        proc = self.mesh("work", "milestone", "1", "frozen", "--sha", "a" * 40,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = json.loads(self.mesh("work", "show", "1", "--json").stdout)
        self.assertEqual(item["freeze"]["origin"], "frozen")
        self.assertIsNotNone(item["freeze"]["declared"])

    def test_lot_filtre_hors_fenetre_garde_ses_delais(self):
        """Un lot ancien filtré garde sa frise, derrière 200 lots récents (B3).

        Les délais ne doivent pas dépendre de la fenêtre globale des lots les
        plus récemment modifiés : `work list --assignee` rend la ligne demandée
        avec ses jalons, même si elle est hors de cette fenêtre.
        """
        proc = self.mesh("work", "add", "--title", "ancien", "--assignee", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.mesh("work", "move", "1", "build")
        proc = self.mesh("work", "milestone", "1", "frozen", "--sha", "a" * 40,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute(
            "INSERT INTO work_items(title, state, assignee) "
            "SELECT 'récent-'||i, 'intake', 'autre' FROM generate_series(1, 201) i")

        proc = self.mesh("work", "list", "--assignee", AGENT, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual([row["id"] for row in rows], [1])
        self.assertIsNotNone(rows[0]["delays"], "le lot filtré a perdu sa frise")
        self.assertEqual(rows[0]["delays"]["work_item_id"], 1)
        self.assertIsNotNone(rows[0]["delays"]["frozen_ts"])

        # la sortie lisible porte aussi la phase courante
        proc = self.mesh("work", "list", "--assignee", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("freeze", proc.stdout)


class AideSansEffetTest(unittest.TestCase):
    """`--help` n'exécute jamais une sous-commande (relecture L28 : `mail migrate
    --help` avait appliqué des migrations à une base vivante)."""

    def run_cli(self, *args):
        env = dict(os.environ, AMEESH_CONFIG=EMPTY_CONFIG,
                   AMEESH_DSN="postgresql://personne@127.0.0.1:1/aucune",
                   PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
        return subprocess.run([sys.executable, "-m", "ameesh", *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_aide_des_sous_commandes_mail(self):
        for sub in ("migrate", "list", "doctor", "inbox", "send"):
            proc = self.run_cli("mail", sub, "--help")
            self.assertEqual(proc.returncode, 0, (sub, proc.stderr))
            self.assertIn("agent-mail", proc.stdout)

    def test_aide_de_register_et_stop(self):
        for sub in ("register", "stop"):
            proc = subprocess.run(
                [sys.executable, "-c", "import sys; from ameesh import runner; sys.exit(runner.main(sys.argv[1:]))",
                 sub, "--help"],
                env=dict(os.environ, AMEESH_CONFIG=EMPTY_CONFIG,
                         AMEESH_DSN="postgresql://personne@127.0.0.1:1/aucune",
                         PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src")),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, (sub, proc.stderr))
            self.assertIn("usage", proc.stdout)
