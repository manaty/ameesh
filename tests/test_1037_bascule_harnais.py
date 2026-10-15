# SPDX-License-Identifier: AGPL-3.0-only
"""#1037 : apres une bascule de harnais, la ligne de l'agent ne garde pas les
traces du harnais quitte.

Constat (auditeur, 13/10) : la session vivante d'un agent pouvait etre Codex
alors que sa ligne exposait un modele (et une session) Claude perimes — la
porte §2 « autre famille de modele » pouvait donc compter un avis Codex comme
Claude. Trois temoins de contrat ici :

* un hook — sous bail ou non — ne reecrit jamais la DECLARATION d'un agent
  `execute` (le harnais du tour, L242b, faisait clignoter `harness` ; modele et
  session restaient ceux de la session precedente) ; sous bail, le dossier et
  l'hote du tour continuent de suivre (L61, deja tenu par ses bornes) ;
* `adopt` pose la session vivante de 0384 (`session_harness`, `session_cwd`,
  `session_origin`) et efface modele, effort et tier quand le harnais declare
  change — en base comme dans l'etat local, que l'exécuteur relit avant les
  colonnes du registre ;
* la porte de relecture relit desormais les tours du GEL
  (`test_1053_famille_au_gel`, #1053/#1777) ; le lecteur de session vivante
  (`registry.live_harness`), qui servait a cette porte, garde son temoin
  direct ci-dessous.
"""
from __future__ import annotations

import dataclasses
import os
import unittest

from ameesh import registry, reprise
from ameesh.backend import PgBackend

from .support import PgTestCase
from .test_l39_adoption_reprise import SID, _dossier, _rollout


class HookNeReecritPasLaDeclarationTest(PgTestCase):
    """Le hook d'un tour emprunte (L242b) ne touche pas la declaration."""

    def test_hook_sous_bail_laisse_harnais_modele_et_session(self):
        registry.upsert(self.db, "ouvrier", harness="claude", host=self.cfg.host,
                        cwd=self.tmp, session_id="session-declaree",
                        model="claude-opus-5-5")
        avant = registry.get(self.db, "ouvrier")
        ailleurs = os.path.join(self.tmp, "ailleurs")
        os.makedirs(ailleurs, exist_ok=True)
        PgBackend(self.cfg, self.db).register("ouvrier", "codex", ailleurs, None,
                                              leased=True)
        apres = registry.get(self.db, "ouvrier")
        self.assertEqual(apres["harness"], "claude")
        self.assertEqual(apres["model"], "claude-opus-5-5")
        self.assertEqual(apres["session_id"], "session-declaree")
        # L61 : sous bail, le dossier du tour suit le hook (sa session, non :
        # L166 l'a deja filtree) — la declaration ne porte que harnais/modele.
        self.assertEqual(apres["cwd"], ailleurs)
        self.assertEqual(apres["host"], avant["host"])
        self.assertEqual(apres["mode"], "execute")
        self.assertGreaterEqual(apres["last_seen_ts"], avant["last_seen_ts"])


class AdoptBasculeTest(PgTestCase):
    """`adopt` d'un autre harnais : session vivante posee, reglages purges."""

    def setUp(self):
        super().setUp()
        self.travail = os.path.join(self.tmp, "travail")
        os.makedirs(self.travail, exist_ok=True)
        self.compte = _dossier(os.path.join(self.tmp, "comptes", "codex-1"),
                               creds="auth.json")
        _rollout(self.compte, SID, self.travail)

    def _cfg(self):
        return dataclasses.replace(
            self.cfg, accounts={"codex": [{"name": "primaire", "path": self.compte}]})

    def test_bascule_efface_les_reglages_du_harnais_quitte(self):
        registry.upsert(self.db, "coord", harness="claude", host=self.cfg.host,
                        cwd=self.travail, model="claude-opus-5-5")
        self.db.execute("UPDATE agent_registry SET effort = 'high', tier = 'max' "
                        "WHERE name = 'coord'")
        dossier = self.cfg.agent_dir("coord")
        os.makedirs(dossier, exist_ok=True)
        for cle in ("model", "effort", "tier"):
            with open(os.path.join(dossier, cle), "w", encoding="utf-8") as fh:
                fh.write("reglage-du-harnais-quitte")
        out = reprise.adopt(self._cfg(), self.db, "coord", session_id=SID,
                            harness="codex")
        row = registry.get(self.db, "coord")
        self.assertEqual(row["harness"], "codex")
        self.assertIsNone(row["model"])
        self.assertIsNone(row["effort"])
        self.assertIsNone(row["tier"])
        self.assertEqual(row["session_id"], SID)
        self.assertEqual(row["session_harness"], "codex")
        self.assertEqual(row["session_origin"], SID)
        self.assertEqual(row["session_cwd"], self.travail)
        for cle in ("model", "effort", "tier"):
            self.assertFalse(os.path.exists(os.path.join(dossier, cle)), cle)
        self.assertTrue(any("dropped" in note for note in out["notes"]),
                        out["notes"])


class SessionVivanteTest(PgTestCase):
    """`registry.live_harness` suit la session vivante, pas l'etiquette declaree.

    #1777 : la porte §2 a change de source (les tours du gel), ce lecteur reste
    l'API du registre pour ses autres appelants."""

    def test_le_harnais_lu_est_celui_de_la_session_vivante(self):
        registry.upsert(self.db, "auteur", harness="claude", host=self.cfg.host,
                        cwd=self.tmp, model="claude-opus-5-5")
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_origin = %s, "
            "session_harness = 'codex' WHERE name = 'auteur'", (SID, SID))
        row = registry.get(self.db, "auteur")
        self.assertEqual(row["harness"], "claude")             # etiquette declaree
        self.assertEqual(registry.live_harness(row), "codex")  # session vivante


if __name__ == "__main__":
    unittest.main()
