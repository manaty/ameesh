# SPDX-License-Identifier: AGPL-3.0-only
"""#1372 CANON-PROJECTION : une carte Process trop longue peut porter une
PROJECTION COURTE, délimitée par `canon.PROCESS_PROJECTION_START` et
`canon.PROCESS_PROJECTION_END`. Le brief de reprise (et le résumé de rotation,
et la première session) injecte la projection À LA PLACE du texte entier : la
fin du canon (§12 paquets, §14 staging) n'est plus mangée par le début.

Sans projection utilisable, la perte n'est plus silencieuse : le brief dit que
seul le début est injecté ; `canon check` signale la carte (projection absente,
illisible ou au-delà de son budget) sans bloquer aucun agent. Le budget du brief
(`BRIEF_MAX_BYTES`) ne change pas.

Diagnostic docs-01 #42432 (15/10/2026 00:00) : carte home de 33 472 octets,
`BRIEF_MAX_BYTES` = 16 000 moins la part réservée au texte (min 4 000) → la
coupe tombait dans §7 ; §12 et §14 n'étaient jamais injectés.
"""
from __future__ import annotations

import dataclasses
import os
import unittest

from ameesh import canon, config as config_mod, reprise

from .support import PgTestCase
from .test_canon import _TmpMixin, codes, commit_all, write
from .test_l538_process_inject import RACINE, TITRE, _canon_approuve, carte

DEBUT = canon.PROCESS_PROJECTION_START
FIN = canon.PROCESS_PROJECTION_END

#: une règle bavarde : le corps dépasse la part de règles d'un brief (≈ 11 900
#: octets), comme la carte home — sans projection, §12 et §14 tombent.
BAVARD = "- une règle très bavarde et répétée\n"


def corps_long(fin: str = "") -> str:
    """Un corps du genre de la carte home : long, §12 et §14 à la FIN."""
    return ("# Règles\n\n" + BAVARD * 500
            + "\n## 12. Paquets\n\n- PAQUETS : intégration groupée des lots\n"
            + "\n## 14. Staging et prod\n\n- STAGING : pas de go owner\n" + fin)


def projection(texte: str) -> str:
    """Le bloc de projection, marqueurs sur leurs propres lignes."""
    return "\n%s\n\n%s\n\n%s\n" % (DEBUT, texte.strip(), FIN)


PROJECTION_COURTE = ("**§1 Classes** : léger, sensible, release.\n\n"
                     "**§12 Paquets** : intégration groupée, manifeste gelé.\n\n"
                     "**§14 Staging** : pas de go owner hors prod.\n")


def carte_longue(proj: str = PROJECTION_COURTE, **fields) -> str:
    return carte(body=corps_long(projection(proj) if proj else ""), **fields)


class ProjectionLectureTest(_TmpMixin, unittest.TestCase):
    """Lecture et validation de la projection (`canon check`)."""

    def charge(self, contenu: str, chemin: str = RACINE):
        root = self.example_copy()
        write(root, chemin, contenu)
        return canon.load(root, untrusted=True)

    def test_la_projection_est_lue_entre_les_marqueurs(self):
        loaded = self.charge(carte_longue())
        self.assertEqual(len(loaded.processes), 1)
        regle = loaded.processes[0]
        self.assertEqual(regle.projection.strip(), PROJECTION_COURTE.strip())
        self.assertEqual(regle.projection_error, "")
        self.assertIn(DEBUT, regle.body)          # la carte reste entière
        self.assertIn("§14 Staging", regle.projection)

    def test_une_carte_sans_marqueur_n_a_pas_de_projection(self):
        # carte ancienne : rien n'est inventé, le texte entier reste la règle
        loaded = self.charge(carte(body=corps_long()))
        self.assertEqual(loaded.processes[0].projection, "")
        self.assertEqual(loaded.processes[0].projection_error, "")

    def test_un_marqueur_en_ligne_ne_declenche_rien(self):
        # les marqueurs sont des LIGNES : une mention au fil du texte n'ouvre
        # pas de projection (sinon un texte forgé détournerait le brief)
        body = "# Règles\n\nVoir %s pour le format.\n" % DEBUT
        loaded = self.charge(carte(body=body))
        self.assertEqual(loaded.processes[0].projection, "")
        self.assertEqual(loaded.processes[0].projection_error, "")

    def test_les_defauts_de_forme_sont_signales(self):
        cas = {
            "unterminated": carte(body="# Règles\n\n%s\n\n- sans fin\n" % DEBUT),
            "stray-end": carte(body="# Règles\n\n%s\n" % FIN),
            "empty": carte(body="# Règles\n\n%s\n\n%s\n" % (DEBUT, FIN)),
            "multiple": carte(body="# Règles\n\n%s\n\n- a\n\n%s\n\n%s\n\n- b\n\n%s\n"
                                   % (DEBUT, FIN, DEBUT, FIN)),
        }
        for code, contenu in cas.items():
            with self.subTest(code=code):
                loaded = self.charge(contenu)
                regle = loaded.processes[0]
                self.assertEqual(regle.projection, "")
                self.assertEqual(regle.projection_error, code)
                findings = canon.validate(loaded)
                self.assertIn("process-projection-%s" % code, codes(findings))
                self.assertEqual([f.severity for f in findings
                                  if f.code == "process-projection-%s" % code],
                                 [canon.WARNING])
                self.assertEqual(canon.blocking(findings).global_errors, [])

    def test_une_projection_valide_retire_le_constat_de_longueur(self):
        loaded = self.charge(carte_longue())
        self.assertGreater(len(loaded.processes[0].body.encode()), canon.PROCESS_BODY_MAX)
        findings = canon.validate(loaded)
        self.assertNotIn("process-too-long", codes(findings))
        self.assertNotIn("process-projection-too-long", codes(findings))

    def test_une_carte_longue_sans_projection_est_signalee(self):
        loaded = self.charge(carte(body=corps_long()))
        findings = canon.validate(loaded)
        trouvees = [f for f in findings if f.code == "process-too-long"]
        self.assertTrue(trouvees, sorted(codes(findings)))
        self.assertEqual(trouvees[0].severity, canon.WARNING)
        self.assertIn(DEBUT, trouvees[0].message)
        self.assertIn(FIN, trouvees[0].message)
        self.assertEqual(canon.blocking(findings).global_errors, [])

    def test_une_projection_trop_longue_est_signalee(self):
        longue = "- une règle de projection\n" * 600   # ≈ 15 600 octets
        self.assertGreater(len(longue.encode()), canon.PROCESS_PROJECTION_MAX)
        loaded = self.charge(carte_longue(longue))
        self.assertGreater(len(loaded.processes[0].projection.encode()),
                           canon.PROCESS_PROJECTION_MAX)
        findings = canon.validate(loaded)
        self.assertIn("process-projection-too-long", codes(findings))
        self.assertNotIn("process-too-long", codes(findings))
        self.assertEqual(canon.blocking(findings).global_errors, [])

    def test_une_projection_au_budget_est_valide(self):
        # la borne haute est INCLUSIVE : au budget exact, aucun constat
        exacte = "a" * canon.PROCESS_PROJECTION_MAX
        loaded = self.charge(carte_longue(exacte))
        self.assertEqual(len(loaded.processes[0].projection.encode()),
                         canon.PROCESS_PROJECTION_MAX)
        findings = canon.validate(loaded)
        self.assertNotIn("process-too-long", codes(findings))
        self.assertNotIn("process-projection-too-long", codes(findings))
        self.assertEqual(loaded.processes[0].projection_error, "")

    def test_la_projection_est_exposee_dans_show_json(self):
        loaded = self.charge(carte_longue())
        data = loaded.to_dict()
        self.assertEqual(data["processes"][0]["projection_bytes"],
                         len(loaded.processes[0].projection.encode()))
        self.assertNotIn("projection_error", data["processes"][0])
        casse = self.charge(carte(body="# Règles\n\n%s\n" % DEBUT))
        self.assertEqual(casse.to_dict()["processes"][0]["projection_error"],
                         "unterminated")


class ProjectionBriefTest(_TmpMixin, PgTestCase):
    """La projection ouvre le brief déterministe ; la perte est dite sinon."""

    def setUp(self) -> None:
        super().setUp()
        self.clone = _canon_approuve(self.tmp, "acme", carte_longue())
        self.cfg = dataclasses.replace(self.cfg, canon=self.clone, canon_ref="",
                                       canon_untrusted=False)

    def _row(self, name: str = "ouvrier", **extra) -> dict:
        row = {"name": name, "harness": "deepseek", "host": self.cfg.host,
               "cwd": os.path.join(self.tmp, "travail"), "team": "acme-web",
               "responsible": "human:alice"}
        row.update(extra)
        return row

    def _brief(self, cfg=None, row=None) -> str:
        return reprise.deterministic_brief(cfg or self.cfg, self.db,
                                           row or self._row())

    def test_le_brief_injecte_la_projection_et_sa_fin(self):
        texte = self._brief()
        self.assertIn(reprise.PROCESS_RULES_HEADER, texte)
        # le début ET la fin de la projection : §12 et §14 sont là
        self.assertIn("§1 Classes", texte)
        self.assertIn("§12 Paquets", texte)
        self.assertIn("§14 Staging", texte)
        self.assertIn("pas de go owner hors prod", texte)
        # le texte entier ne double pas la projection
        self.assertNotIn("PAQUETS : intégration groupée des lots", texte)
        self.assertNotIn(BAVARD.strip(), texte)
        # ni les marqueurs, ni de coupe : la projection tient entière
        self.assertNotIn(DEBUT, texte)
        self.assertNotIn("[…]", texte)
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)
        # identité de la carte : titre, responsable, date
        self.assertIn(TITRE, texte)
        self.assertIn("human:alice", texte)
        self.assertIn("2026-10-12", texte)

    def test_une_carte_longue_sans_projection_est_dite(self):
        # carte ancienne : le brief garde le début (comportement d'avant) mais
        # DIT que la fin n'est pas injectée — plus de coupe silencieuse
        write(self.clone, RACINE, carte(body=corps_long()))
        commit_all(self.clone, "carte longue sans projection")
        texte = self._brief()
        self.assertIn(reprise.PROCESS_RULES_HEADER, texte)
        self.assertIn(reprise.RULES_DIAG_PREFIX, texte)
        self.assertIn("has no short projection", texte)
        self.assertIn("the brief keeps what fits", texte)
        self.assertIn("[…]", texte)
        self.assertNotIn("§14 Staging", texte)      # la fin manque, et c'est dit
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)

    def test_une_projection_illisible_est_dite(self):
        write(self.clone, RACINE, carte(body="# Règles\n\n%s\n\n- sans fin\n" % DEBUT))
        commit_all(self.clone, "projection sans fin")
        texte = self._brief()
        self.assertIn(reprise.RULES_DIAG_PREFIX, texte)
        self.assertIn("unusable short projection (unterminated)", texte)
        self.assertIn("whole text is used instead", texte)
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)

    def test_une_projection_trop_longue_est_dite_et_bornee(self):
        write(self.clone, RACINE, carte_longue("- une règle de projection\n" * 600))
        commit_all(self.clone, "projection trop longue")
        texte = self._brief()
        self.assertIn(reprise.RULES_DIAG_PREFIX, texte)
        self.assertIn("short projection", texte)
        self.assertIn("too long", texte)
        self.assertIn("cut to its budget", texte)
        self.assertIn("[…]", texte)                 # la projection est coupée…
        self.assertIn("## Instructions", texte)     # …pas la consigne
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)

    def test_le_bloc_coupe_est_dit(self):
        # deux cartes longues sans projection : le bloc est coupé, et le brief
        # le dit — même quand aucune carte prise seule ne porte le diagnostic
        write(self.clone, RACINE, carte(body=corps_long()))
        write(self.clone, "processus/autre.md", carte(
            title="Autre processus", body=corps_long()))
        commit_all(self.clone, "deux cartes longues")
        texte = self._brief()
        self.assertIn("the rules block is cut", texte)
        self.assertIn("the end is not injected", texte)
        self.assertLessEqual(len(texte.encode()), reprise.BRIEF_MAX_BYTES)

    def test_un_resume_plein_ne_chasse_pas_la_projection(self):
        # la place des règles reste réservée d'abord (#538) : la projection
        # entière survit, le texte de reprise est tronqué
        plein = "a" * reprise.BRIEF_MAX_BYTES
        relu = reprise.with_process_rules(self.cfg, plein, agent="ouvrier",
                                          host=self.cfg.host)
        self.assertIn("§14 Staging", relu)
        self.assertIn("[…]", relu)
        self.assertLessEqual(len(relu.encode()), reprise.BRIEF_MAX_BYTES)

    def test_la_projection_utf8_est_bornee_sans_casser_un_caractere(self):
        # une projection pleine d'accents : la borne est en OCTETS, jamais au
        # milieu d'un caractère (encodable, sans U+FFFD)
        write(self.clone, RACINE, carte_longue("é" * 12_000))
        commit_all(self.clone, "projection utf-8")
        bloc = reprise.process_rules_block(self.cfg, agent="ouvrier",
                                           host=self.cfg.host)
        self.assertLessEqual(len(bloc.encode()), reprise.BRIEF_MAX_BYTES)
        self.assertNotIn("\ufffd", bloc)
        bloc.encode("utf-8").decode("utf-8")

    def test_le_budget_du_brief_ne_change_pas(self):
        # #1372 : la correction passe par la projection, pas par une hausse du
        # budget ; toute hausse exige une décision owner distincte
        self.assertEqual(reprise.BRIEF_MAX_BYTES, 16000)
        self.assertEqual(reprise.BRIEF_TEXT_MIN_BYTES, 4000)

    def test_une_projection_a_son_budget_est_injectee_entiere(self):
        # borne haute : une projection de exactement `PROCESS_PROJECTION_MAX`
        # octets tient dans le bloc, même avec la part texte réservée pleine
        # (4 000 octets) — le budget annoncé est bien celui qui est injectable
        exacte = "a" * canon.PROCESS_PROJECTION_MAX
        write(self.clone, RACINE, carte_longue(exacte))
        commit_all(self.clone, "projection au budget exact")
        relu = reprise.with_process_rules(self.cfg, "b" * reprise.BRIEF_TEXT_MIN_BYTES,
                                          agent="ouvrier", host=self.cfg.host)
        self.assertIn(exacte, relu)             # la projection entière est là
        self.assertNotIn("[…]", relu)           # ni les règles ni la consigne coupées
        self.assertIn("b" * 100, relu)
        self.assertLessEqual(len(relu.encode()), reprise.BRIEF_MAX_BYTES)


class ProjectionMultiCanonTest(_TmpMixin, PgTestCase):
    """La projection lue est celle du canon qui possède l'agent (isolation)."""

    def test_la_projection_du_canon_de_l_agent_seulement(self):
        acme = _canon_approuve(self.tmp, "acme", carte_longue(
            "**§12 Paquets acme** : la règle d'acme\n"))
        beta = _canon_approuve(self.tmp, "beta", carte_longue(
            "**§12 Paquets beta** : la règle de beta\n"))
        cfg = dataclasses.replace(
            self.cfg, canon=acme, canon_ref="", canon_untrusted=False,
            extra_canons=(config_mod.CanonEntry(beta, "", False),))
        beta_charge = next(c for c in canon.load_configured(cfg)
                           if os.path.realpath(c.root) == os.path.realpath(beta))
        self.assertNotEqual(beta_charge.id, "")
        row = {"name": "ouvrier", "harness": "deepseek", "host": cfg.host,
               "cwd": os.path.join(self.tmp, "travail"), "team": "acme-web",
               "responsible": "human:alice", "canon": beta_charge.id}
        texte = reprise.deterministic_brief(cfg, self.db, row)
        self.assertIn("la règle de beta", texte)
        self.assertNotIn("la règle d'acme", texte)
        # ligne historique (canon NULL) : les règles d'acme
        row.pop("canon")
        texte = reprise.deterministic_brief(cfg, self.db, row)
        self.assertIn("la règle d'acme", texte)
        self.assertNotIn("la règle de beta", texte)
