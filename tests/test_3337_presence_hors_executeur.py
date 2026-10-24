# SPDX-License-Identifier: AGPL-3.0-only
"""#3337 — une identité qui écrit hors exécuteur n'est plus affichée absente.

Constat (24/10) : `agent-mail list` montrait « bureau seen 834398s ago,
unread:4394 » alors que le bureau venait d'écrire (#132899) depuis sa VM.
Une session `externe` n'a pas d'exécuteur : elle écrit sans que `last_seen`
avance, donc la colonne « seen » la présentait comme absente. Le correctif
lit le dernier message déposé (`last_mail_ts`, `registry.presence`) et
l'affiche comme signe de vie, en nommant la session hors exécuteur.

Le nom d'expéditeur reste déclaré, pas prouvé : `wrote` est un signe de vie,
jamais une autorité.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import mail, registry

from .support import PgTestCase


class PresenceRegistreTest(unittest.TestCase):
    def test_le_message_le_plus_recent_gagne(self):
        self.assertEqual(registry.presence(
            {"last_seen_ts": 10.0, "last_mail_ts": 20.0}), (20.0, "wrote"))
        self.assertEqual(registry.presence(
            {"last_seen_ts": 20.0, "last_mail_ts": 10.0}), (20.0, "seen"))
        self.assertEqual(registry.presence({"last_seen_ts": 10.0}), (10.0, "seen"))
        self.assertEqual(registry.presence(None), (0.0, "seen"))

    def test_les_deux_noms_de_colonne_sont_lus(self):
        # `registry.overview` rend `last_seen_ts`/`last_mail_ts` ; le repli
        # fichier de `mail list` ne rend que `last_seen` (le helper accepte
        # les deux noms).
        self.assertEqual(registry.presence(
            {"last_seen": 30.0, "last_mail": 40.0}), (40.0, "wrote"))


class PresenceHorsExecuteurTest(PgTestCase):
    """L'incident rejoué : un agent `externe` écrit, puis on lit `mail list`."""

    def _externe(self, name: str = "bureau-3337") -> str:
        registry.upsert(self.db, name, harness="codex", host="pc-smichea",
                        cwd="/home/smichea/Work", mode="externe")
        # comme le bureau : aucune inscription de hook/exécuteur depuis 9 jours
        self.db.execute(
            "UPDATE agent_registry SET last_seen = now() - interval '9 days 16 hours' "
            "WHERE name = %s", (name,))
        registry.upsert(self.db, "destinataire-3337", harness="deepseek",
                        host=self.cfg.host)
        return name

    def _ligne(self, sortie: str, name: str) -> str:
        lignes = [ligne for ligne in sortie.splitlines() if ligne.startswith(name)]
        self.assertEqual(len(lignes), 1, sortie)
        return lignes[0]

    def test_mail_list_montre_wrote_et_hors_executeur(self):
        name = self._externe()
        mail.send(self.db, name, "destinataire-3337", "je suis là", host="ameesh-agents-2")

        self.db.execute(
            "UPDATE agent_registry SET last_seen = now() - interval '2 days' "
            "WHERE name = 'destinataire-3337'")
        proc = self.cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ligne = self._ligne(proc.stdout, name)
        self.assertIn("wrote", ligne)
        self.assertIn("(outside runner)", ligne)
        self.assertNotIn("seen", ligne)
        # L'ordre suit le signe de vie : celle qui vient d'écrire passe avant
        # une inscription plus ancienne.
        noms = [ligne.split()[0] for ligne in proc.stdout.splitlines()]
        self.assertLess(noms.index(name), noms.index("destinataire-3337"))

        # Le message n'a pas bougé `last_seen` : c'est bien la nouvelle source
        # qui le montre vivant, pas une écriture dans le registre.
        row = registry.get(self.db, name)
        self.assertLess(row["last_seen_ts"], time.time() - 8 * 86400)

    def test_sans_message_l_absence_est_nommee_hors_executeur(self):
        name = self._externe("bureau-3337-silencieux")
        proc = self.cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ligne = self._ligne(proc.stdout, name)
        self.assertIn("seen", ligne)
        self.assertIn("(outside runner)", ligne)

    def test_mesh_list_et_json_exposent_le_signe_de_vie(self):
        name = self._externe()
        avant = time.time()
        mail.send(self.db, name, "destinataire-3337", "je suis là", host="ameesh-agents-2")

        proc = self.mesh("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        par_nom = {row["name"]: row for row in json.loads(proc.stdout)}
        self.assertGreaterEqual(float(par_nom[name]["last_mail_ts"]), avant - 1)
        # `last_seen` reste celui de l'inscription : les deux signaux sont
        # distincts, l'affichage choisit le plus récent.
        self.assertLess(float(par_nom[name]["last_seen_ts"]), avant - 8 * 86400)

        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        header = proc.stdout.splitlines()[0]
        colonne = header.index("ACTIVITY")
        ligne = self._ligne(proc.stdout, name)
        self.assertEqual(ligne[colonne:colonne + 5].strip(), "wrote",
                         "la colonne ACTIVITY doit nommer l'écriture :\n" + ligne)

    def test_un_agent_sous_executeur_garde_seen(self):
        registry.upsert(self.db, "ouvrier-3337", harness="deepseek", host=self.cfg.host)
        proc = self.cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ligne = self._ligne(proc.stdout, "ouvrier-3337")
        self.assertIn("seen", ligne)
        self.assertNotIn("(outside runner)", ligne)
