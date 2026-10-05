# SPDX-License-Identifier: AGPL-3.0-only
"""Politique de revue par classe de risque : portées, classes, refus (R19)."""
from __future__ import annotations

import unittest

from ameesh import review


def policy(**over):
    """Une politique valide, surchargeable."""
    raw = {
        "default": "normal",
        "rules": [
            {"paths": ["supabase/migrations/**", "**/*.sql"], "class": "sensitive"},
            {"paths": ["src/ameesh/**"], "class": "normal"},
            {"paths": ["docs/**", "**/*.md"], "class": "light"},
        ],
    }
    raw.update(over)
    parsed, problems = review.parse(raw)
    assert not problems, problems
    return parsed


class GlobTest(unittest.TestCase):
    def test_etoile_ne_traverse_pas_les_dossiers(self):
        self.assertTrue(review.compile_glob("*.sql").match("a.sql"))
        self.assertFalse(review.compile_glob("*.sql").match("db/a.sql"))
        self.assertTrue(review.compile_glob("db/*.sql").match("db/a.sql"))
        self.assertFalse(review.compile_glob("db/*.sql").match("db/sub/a.sql"))

    def test_double_etoile_traverse_et_peut_etre_vide(self):
        self.assertTrue(review.compile_glob("**/*.sql").match("a.sql"))
        self.assertTrue(review.compile_glob("**/*.sql").match("db/sub/a.sql"))
        self.assertTrue(review.compile_glob("docs/**").match("docs/a.md"))
        self.assertTrue(review.compile_glob("docs/**").match("docs/sub/a.md"))
        self.assertFalse(review.compile_glob("docs/**").match("doc/a.md"))

    def test_double_etoile_traverse_le_saut_de_ligne(self):
        """`**` vaut « n'importe quoi », sauts de ligne compris (revue codex2).

        Un dossier dont le nom contient un LF (`db/line<LF>break/`) ne doit pas
        échapper à `**/*.sql` : sinon un fichier SQL serait classé par le défaut,
        c'est-à-dire léger, alors que la portée le dit sensible.
        """
        chemin = "db/line\nbreak/new.sql"
        self.assertTrue(review.compile_glob("**/*.sql").match(chemin))
        self.assertTrue(review.compile_glob("db/**").match(chemin))
        result = review.classify(policy(default="light"), [chemin])
        self.assertEqual(result["class"], "sensitive")
        self.assertEqual(result["files"][0]["rule"],
                         ["supabase/migrations/**", "**/*.sql"])
        self.assertFalse(result["default_used"])

    def test_motif_entier_et_classes(self):
        # `docs` seul ne couvre pas `docs/x.md` : il faut `docs/**`.
        self.assertFalse(review.compile_glob("docs").match("docs/x.md"))
        self.assertTrue(review.compile_glob("src/[ab]/*.ts").match("src/a/x.ts"))
        self.assertFalse(review.compile_glob("src/[ab]/*.ts").match("src/c/x.ts"))
        self.assertTrue(review.compile_glob("src/[!a]/*.ts").match("src/c/x.ts"))
        self.assertTrue(review.compile_glob("src/?/x.ts").match("src/a/x.ts"))

    def test_chemin_normalise(self):
        self.assertEqual(review.normalize_path("./a\\b/./c"), "a/b/c")
        self.assertEqual(review.normalize_path("a/../b/"), "b")
        self.assertEqual(review.normalize_path("../a"), "../a")
        self.assertTrue(review.compile_glob("./docs/**").match("docs/a.md"))
        self.assertTrue(review.compile_glob("/docs/**").match("docs/a.md"))


class ClassifyTest(unittest.TestCase):
    def test_classe_d_un_fichier_est_la_plus_haute(self):
        # `docs/schema.sql` matche `**/*.sql` (sensible) et `docs/**` (léger).
        result = review.classify(policy(), ["docs/schema.sql"])
        self.assertEqual(result["class"], "sensitive")
        self.assertEqual(result["files"], [
            {"path": "docs/schema.sql", "class": "sensitive",
             "rule": ["supabase/migrations/**", "**/*.sql"]}])

    def test_classe_du_changement_est_le_maximum(self):
        result = review.classify(policy(), ["docs/readme.md", "src/ameesh/work.py"])
        self.assertEqual(result["class"], "normal")
        result = review.classify(policy(), ["src/ameesh/work.py", "supabase/migrations/0013_x.sql"])
        self.assertEqual(result["class"], "sensitive")

    def test_defaut_quand_aucune_portee_ne_matche(self):
        result = review.classify(policy(), ["Makefile"])
        self.assertEqual(result["class"], "normal")
        self.assertTrue(result["default_used"])
        self.assertIsNone(result["files"][0]["rule"])
        light = review.classify(policy(default="light"), ["Makefile"])
        self.assertEqual(light["class"], "light")

    def test_chemins_hors_depot_et_liste_vide_refuses(self):
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), ["../secret.txt"])
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), ["/etc/passwd"])
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), ["C:/secret.txt"])
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), ["a/../../secret.txt"])
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), [])
        with self.assertRaises(review.ReviewError):
            review.classify(policy(), [""])


class ParseTest(unittest.TestCase):
    def test_politique_absente_ou_vide(self):
        parsed, problems = review.parse(None)
        self.assertEqual(problems, [])
        self.assertEqual(parsed.default, "normal")
        self.assertEqual(parsed.rules, ())
        parsed, problems = review.parse({})
        self.assertEqual(problems, [])
        self.assertEqual(parsed.default, "normal")

    def test_mapping_attendu(self):
        parsed, problems = review.parse(["nope"])
        self.assertEqual(parsed.default, "normal")
        self.assertEqual([code for code, _s, _m in problems], [review.INVALID])
        self.assertEqual(problems[0][1], "error")

    def test_defaut_inconnu(self):
        _parsed, problems = review.parse({"default": "urgent"})
        self.assertEqual([code for code, _s, _m in problems], [review.UNKNOWN_CLASS])
        self.assertEqual(problems[0][1], "error")

    def test_regles_invalides_ignorees_avec_constat(self):
        _parsed, problems = review.parse({"default": "normal", "rules": [
            {"paths": ["a/**"]},                      # classe manquante
            {"class": "light"},                       # portées manquantes
            {"paths": [], "class": "light"},          # portées vides
            {"paths": ["b/**"], "class": "urgent"},   # classe inconnue
            "pas un mapping",
        ]})
        codes = [code for code, _s, _m in problems]
        self.assertEqual(codes, [review.BAD_RULE, review.BAD_RULE, review.BAD_RULE,
                                 review.UNKNOWN_CLASS, review.BAD_RULE])
        self.assertTrue(all(severity == "error" for _c, severity, _m in problems))

    def test_une_regle_valide_survit_aux_invalides(self):
        parsed, problems = review.parse({"default": "normal", "rules": [
            {"paths": ["x/**"], "class": "urgent"},
            {"paths": ["y/**"], "class": "sensitive"},
        ]})
        self.assertEqual(len(problems), 1)
        self.assertEqual([r.cls for r in parsed.rules], ["sensitive"])
        self.assertEqual(review.classify(parsed, ["y/a"])["class"], "sensitive")

    def test_regles_non_liste(self):
        _parsed, problems = review.parse(
            {"default": "normal", "rules": {"paths": ["a/**"], "class": "light"}})
        self.assertEqual([code for code, _s, _m in problems], [review.BAD_RULE])


class FederationPolicyTest(unittest.TestCase):
    """La politique vit sous `review_policies.classes` ; le reste d'OKF est ignoré."""

    def test_classes_lues(self):
        parsed, problems = review.policy_from_federation({
            "review_policies": {"risk_classes": {
                "default": "light",
                "rules": [{"paths": ["a/**"], "class": "sensitive"}]}},
        })
        self.assertEqual(problems, [])
        self.assertEqual(parsed.default, "light")
        self.assertEqual(review.classify(parsed, ["a/x"])["class"], "sensitive")

    def test_self_approval_seul_reste_compatible(self):
        parsed, problems = review.policy_from_federation(
            {"review_policies": {"self_approval": "forbidden"}})
        self.assertEqual(problems, [])
        self.assertEqual(parsed.default, "normal")
        self.assertEqual(parsed.rules, ())

    def test_absente_ou_non_mapping(self):
        for federation in (None, {}, {"review_policies": None}):
            _parsed, problems = review.policy_from_federation(federation)
            self.assertEqual(problems, [])
        _parsed, problems = review.policy_from_federation({"review_policies": ["x"]})
        self.assertEqual([code for code, _s, _m in problems], [review.INVALID])

    def test_classes_invalides_rapportees(self):
        _parsed, problems = review.policy_from_federation(
            {"review_policies": {"risk_classes": {"default": "urgent"}}})
        self.assertEqual([code for code, _s, _m in problems], [review.UNKNOWN_CLASS])

    def test_defaut_manquant_avertit_sans_bloquer(self):
        parsed, problems = review.policy_from_federation({
            "review_policies": {"risk_classes": {
                "rules": [{"paths": ["a/**"], "class": "light"}]}},
        })
        self.assertEqual([code for code, _s, _m in problems], [review.NO_DEFAULT])
        self.assertEqual(problems[0][1], "warning")
        self.assertEqual(parsed.default, "normal")
        self.assertEqual(review.classify(parsed, ["b/x"])["class"], "normal")

    def test_mapping_vide_ne_produit_rien(self):
        _parsed, problems = review.policy_from_federation(
            {"review_policies": {"risk_classes": {}}})
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
