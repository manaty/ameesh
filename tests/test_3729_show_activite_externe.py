# SPDX-License-Identifier: AGPL-3.0-only
"""#3729 — `ameesh show` dit la dernière écriture d'une session externe.

Constat (auditeur, 26/10) : `ameesh show bureau` affichait « seen 11d ago »
et l'hôte déclaré (pc-smichea), alors que le bureau venait d'écrire depuis un
autre hôte. `ameesh list` lisait déjà `registry.presence` (#3337) ; `show`
restait sur `last_seen_ts` seul, d'où une escalade à tort pour un agent jugé
mort.

Ici, `show` nomme le signe de vie le plus récent (`wrote`/`seen`) et l'hôte du
dernier dépôt quand il diffère de l'hôte déclaré. Le nom d'un expéditeur reste
déclaré, pas prouvé : c'est un signe de vie, jamais une autorité.
"""
from __future__ import annotations

import json
import time

from ameesh import mail, registry

from .support import PgTestCase


class ShowActiviteExterneTest(PgTestCase):
    def _externe(self, name: str = "bureau-3729",
                 host: str = "pc-smichea") -> str:
        registry.upsert(self.db, name, harness="codex", host=host,
                        cwd="/home/smichea/Work", mode="externe")
        # comme le bureau : aucune inscription de hook/exécuteur depuis 12 jours
        self.db.execute(
            "UPDATE agent_registry SET last_seen = now() - interval '12 days' "
            "WHERE name = %s", (name,))
        registry.upsert(self.db, "destinataire-3729", harness="deepseek",
                        host=self.cfg.host)
        return name

    def _ligne(self, sortie: str, name: str) -> str:
        lignes = [ligne for ligne in sortie.splitlines() if ligne.startswith(name)]
        self.assertEqual(len(lignes), 1, sortie)
        return lignes[0]

    def test_show_nomme_l_ecriture_et_l_hote_du_dernier_depot(self):
        name = self._externe()
        mail.send(self.db, name, "destinataire-3729", "je suis là",
                  host="ameesh-agents-2")

        proc = self.mesh("show", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("activity : wrote ", proc.stdout)
        self.assertIn("from ameesh-agents-2", proc.stdout)
        # L'hôte déclaré reste dit : c'est la preuve du dernier dépôt qui
        # qualifie l'activité, pas l'inscription au registre.
        self.assertIn("host: pc-smichea", proc.stdout)

        data = json.loads(self.mesh("show", name, "--json").stdout)
        self.assertGreaterEqual(float(data["last_mail_ts"]), time.time() - 120)
        self.assertEqual(data["last_mail_host"], "ameesh-agents-2")
        # Les deux signaux restent distincts : `last_seen` n'a pas bougé.
        self.assertLess(float(data["last_seen_ts"]), time.time() - 11 * 86400)

    def test_show_sans_message_nomme_seen(self):
        name = self._externe("bureau-3729-silencieux")
        proc = self.mesh("show", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("activity : seen ", proc.stdout)
        self.assertNotIn("wrote", proc.stdout)

    def test_show_d_un_executeur_garde_seen(self):
        registry.upsert(self.db, "ouvrier-3729", harness="deepseek",
                        host=self.cfg.host)
        proc = self.mesh("show", "ouvrier-3729")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("activity : seen ", proc.stdout)

    def test_le_dernier_depot_sur_l_hote_declare_ne_repete_pas_l_hote(self):
        name = self._externe("bureau-3729-local")
        mail.send(self.db, name, "destinataire-3729", "je suis là",
                  host="pc-smichea")
        proc = self.mesh("show", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("activity : wrote ", proc.stdout)
        self.assertNotIn(" from ", proc.stdout)

    def test_list_nomme_aussi_l_hote_du_dernier_depot(self):
        name = self._externe()
        mail.send(self.db, name, "destinataire-3729", "je suis là",
                  host="ameesh-agents-2")
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        header = proc.stdout.splitlines()[0]
        self.assertIn("ACTIVITY", header)
        ligne = self._ligne(proc.stdout, name)
        colonne = header.index("ACTIVITY")
        self.assertEqual(ligne[colonne:colonne + 5].strip(), "wrote", ligne)
        self.assertIn("from ameesh-agents-2", ligne)
