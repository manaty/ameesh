# SPDX-License-Identifier: AGPL-3.0-only
"""CLI `agent-mesh` : mesh list, show, clés, approbations, work_items."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unittest

from ameesh import authority, mail, migrations, registry, signing

from .support import PgTestCase

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
        self.assertIn("NOM", proc.stdout)
        self.assertIn(OWNER, proc.stdout)
        self.assertIn(AGENT, proc.stdout)
        self.assertIn("NON LUS", proc.stdout)
        lignes = [l for l in proc.stdout.splitlines() if l.startswith(AGENT)]
        self.assertEqual(len(lignes), 1)
        # La fraîcheur est un âge, pas une valeur figée : sous charge, « 0s »
        # peut être « 2s » (suite complète, 279 s). On borne, on ne fige pas.
        # Le compte de non-lus est vérifié plus haut sur la sortie JSON.
        age = re.search(r"il y a\s+(\d+)s", lignes[0])
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
        self.assertIn("harnais  : deepseek", proc.stdout)
        self.assertIn("aucune clé publique", proc.stdout)
        proc = self.mesh("show", "inconnu")
        self.assertEqual(proc.returncode, 1)

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
        self.assertIn("rôle agent", proc.stdout)  # moindre privilège par défaut

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
        self.assertIn("autorité du propriétaire", proc.stdout)
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
        self.assertIn("erreur", proc.stderr)

    # -- approbations ------------------------------------------------------
    def test_approve_verify_consume(self):
        authority.register_key(self.db, OWNER, self.public, role="owner")
        artifact = authority.hash_artifact("diff --git a/x b/x\n+1")
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, "--kind", "diff", "--as", OWNER,
                         "--expires", "48h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("approbation #1", proc.stdout)
        self.assertIn(self.fingerprint[:16], proc.stdout)

        proc = self.mesh("verify", "--action", "merge", "--hash", artifact, "--json")
        resultat = json.loads(proc.stdout)
        self.assertTrue(resultat["ok"], resultat["reason"])
        self.assertEqual(resultat["approval"]["approver"], OWNER)

        proc = self.mesh("verify", "--action", "merge", "--hash", artifact,
                         "--consume", "--by", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("consommée", proc.stdout)
        proc = self.mesh("verify", "--action", "merge", "--hash", artifact)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("consommée", proc.stderr)

    def test_approve_sans_identite_refuse(self):
        """Signer une approbation ne se déduit pas du dossier (incident mesh-design)."""
        authority.register_key(self.db, OWNER, self.public, role="owner")
        artifact = authority.hash_artifact("diff sans identité")
        env = self.env()
        env.pop("AGENT_MAIL_NAME", None)
        proc = self.mesh("approve", "--key", self.key_path, "--action", "merge",
                         "--hash", artifact, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("non lié", proc.stderr)
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
        self.assertIn("rôle", proc.stderr)
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
        self.assertIn("clé publique", proc.stderr)
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
        self.assertIn("VALIDE", proc.stdout)

    # -- work_items --------------------------------------------------------
    def test_work_cycle_cli(self):
        proc = self.mesh("work", "add", "--title", "Banc v1 points 3-5",
                         "--type", "evolution", "--app", "nexlink",
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
        self.assertIn("épuisée", proc.stderr)

        proc = self.mesh("work", "move", "1", "blocked", "--note", "attente humaine")
        self.assertEqual(proc.returncode, 0)
        proc = self.mesh("work", "note", "1", "relance demain", "--actor", "codex3")
        self.assertEqual(proc.returncode, 0)

        proc = self.mesh("work", "show", "1", "--json")
        item = json.loads(proc.stdout)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["events"][0]["note"], "relance demain")
        self.assertEqual(item["events"][-1]["note"], "création")

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
        self.assertIn("2 message(s) à importer", proc.stdout)
        self.assertEqual(len(os.listdir(inbox)), 2)  # rien n'a bougé

        proc = self.mesh("import-v0", "--state", state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) importé(s)", proc.stdout)
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
        self.assertIn("2 message(s) exporté(s)", proc.stdout)
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
        self.assertIn("à jour", proc.stdout)
        proc = self.mesh("doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict    : OK", proc.stdout)
        total = len(migrations.discover())
        self.assertIn("%d/%d appliquées" % (total, total), proc.stdout)


if __name__ == "__main__":
    unittest.main()
