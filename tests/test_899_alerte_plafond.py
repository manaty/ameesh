# SPDX-License-Identifier: AGPL-3.0-only
"""#899 — l'alerte à 80 % du plafond d'un job de CI.

La part psql d'une PR grandissait avec la suite (22-31 min mesurées, 30,4-35
sous saturation) et atteignait le plafond du job sans prévenir. Le plafond
reste borné (canon §3) ; ce lot ajoute l'alerte côté CI :

* le workflow déclare le plafond du job (`AMEESH_PLAFOND_MIN`, tenu à
  `timeout-minutes` par le contrat ci-dessous) et son début en epoch
  (`AMEESH_JOB_DEBUT`, première étape) ;
* `scripts/ci-flow.py publish --plafond-min N --debut T` publie **une fois**
  l'alerte à `ALERTE_SEUIL` (80 %) du plafond dans le check run de la part
  (titre, résumé, ligne JSON `type: alerte`) — sans jamais changer sa
  conclusion, et sans rien publier si l'un des deux manque ;
* `summary` affiche la part du plafond consommée, mesure de la croissance
  d'une exécution à l'autre.

#1044 (suite) — les nombres non finis (NaN, ±infini, débordement de
conversion) sont refusés comme illisibles pour les deux champs, avant tout
calcul et toute écriture ; et `summary` étiquette la durée de l'alerte « au
moment de l'alerte » quand aucune durée finale n'est attestée (part
interrompue sans ligne `fin`).

Aucun réveil ni annotation d'échec : `ameesh.ci.events_of` ne lit que les
échecs de test, l'alerte ne crée donc pas de courrier (vérifié ici).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import unittest
import unittest.mock

from ameesh import ci

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ciflux = _charger("ci_flux_899", "ci-flow.py")

DEPOT = "manaty/ameesh"
DEBUT = 1_700_000_000.0
PART = "psql 1/3"


def ev_debut(part: str = PART, **extra) -> dict:
    return dict({"v": 1, "schema": ci.SCHEMA, "type": "debut", "depot": DEPOT,
                 "tete": "a" * 40, "execution": 42, "part": part,
                 "modules": ["test_a"], "classes": {"test_a": "P1"},
                 "ts": "2026-10-13T19:00:00.000Z"}, **extra)


def ev_fin(statut: str = "succes", duree_s: float = 100.0) -> dict:
    return {"v": 1, "schema": ci.SCHEMA, "type": "fin", "part": PART,
            "statut": statut, "duree_s": duree_s, "comptes": {"succes": 3},
            "ts": "2026-10-13T19:10:00.000Z"}


def alerte_a(ecoule_s: float, plafond_min: float = 30.0) -> dict:
    return {"v": 1, "schema": ci.SCHEMA, "type": "alerte", "seuil": ciflux.ALERTE_SEUIL,
            "ecoule_s": ecoule_s, "plafond_min": plafond_min,
            "ts": "2026-10-13T19:24:00.000Z"}


class FauxGitHub:
    def __init__(self, refus: bool = False):
        self.appels, self.refus = [], refus

    def ready(self):
        return True

    def create(self, nom, output):
        if self.refus:
            raise ciflux.urllib.error.HTTPError("u", 403, "Resource not accessible", {}, None)
        self.appels.append(("create", nom, output, None))
        return 7

    def update(self, ident, output, conclusion=None):
        self.appels.append(("update", ident, output, conclusion))

    def sorties(self) -> list[dict]:
        return [appel[2] for appel in self.appels]

    def alertes(self) -> list[dict]:
        return [o for o in self.sorties() if "⚠" in json.dumps(o, ensure_ascii=False)]


class SeuilTest(unittest.TestCase):
    """Le seuil : 80 % du plafond, jamais avant, jamais sans plafond ni début."""

    def test_seuil_a_80_pour_cent(self):
        seuil = ciflux.ALERTE_SEUIL * 30 * 60  # 1440 s
        self.assertEqual(seuil, 1440)
        self.assertIsNone(ciflux.alerte_plafond(DEBUT, 30, DEBUT + seuil - 1))
        alerte = ciflux.alerte_plafond(DEBUT, 30, DEBUT + seuil)
        self.assertIsNotNone(alerte)
        self.assertEqual(alerte["type"], "alerte")
        self.assertEqual(alerte["seuil"], ciflux.ALERTE_SEUIL)
        self.assertEqual(alerte["ecoule_s"], seuil)
        self.assertEqual(alerte["plafond_min"], 30)
        self.assertTrue(alerte["ts"].endswith("Z"), alerte["ts"])

    def test_plafond_45_de_880(self):
        """#880 portera le plafond psql à 45 min : le seuil suit le plafond."""
        seuil = ciflux.ALERTE_SEUIL * 45 * 60
        self.assertIsNone(ciflux.alerte_plafond(DEBUT, 45, DEBUT + seuil - 1))
        self.assertIsNotNone(ciflux.alerte_plafond(DEBUT, 45, DEBUT + seuil))

    def test_sans_plafond_ni_debut(self):
        for debut, plafond in ((None, 30), (DEBUT, None), (None, None),
                               ("", ""), ("bientôt", "quarante"), (DEBUT, 0)):
            with self.subTest(debut=debut, plafond=plafond):
                self.assertIsNone(ciflux.alerte_plafond(debut, plafond, DEBUT + 100000))

    def test_nombres_non_finis_refuses(self):
        """#1044 N1 : NaN, ±infini et débordement ne sont jamais des nombres."""
        for valeur in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999", 10 ** 400):
            with self.subTest(valeur=valeur):
                self.assertIsNone(ciflux._nombre(valeur))
        self.assertIsNone(ciflux._nombre(None))
        self.assertEqual(ciflux._nombre("45"), 45.0)
        self.assertEqual(ciflux._nombre(30), 30.0)

    def test_alerte_refuse_les_non_finis_des_deux_champs(self):
        """#1044 N1 : plafond nan n'alerte plus après 10 s, début nan ne lève plus."""
        for valeur in ("nan", "inf", "-inf", 10 ** 400):
            with self.subTest(champ="plafond", valeur=valeur):
                self.assertIsNone(ciflux.alerte_plafond(DEBUT, valeur, DEBUT + 10))
            with self.subTest(champ="debut", valeur=valeur):
                self.assertIsNone(ciflux.alerte_plafond(valeur, 30, DEBUT + 10))

    def test_contexte_de_la_part(self):
        alerte = ciflux.alerte_plafond(DEBUT, 30, DEBUT + 1440,
                                       ciflux.contexte_de([ev_debut()]))
        self.assertEqual(alerte["part"], PART)
        self.assertEqual(alerte["depot"], DEPOT)
        self.assertEqual(alerte["tete"], "a" * 40)
        self.assertEqual(alerte["execution"], 42)


class CheckRunTest(unittest.TestCase):
    """L'alerte se voit dans le check run et ne devient jamais un échec."""

    def test_visible_et_ignoree_par_ameesh(self):
        sortie = ciflux.check_output([ev_debut()], alerte_a(1440.0))
        self.assertIn("80 %", sortie["title"])
        self.assertIn("80 % du plafond du job atteint", sortie["summary"])
        self.assertIn("24 min 00 s écoulées sur 30 min", sortie["summary"])
        self.assertIn('"type": "alerte"', sortie["text"])
        # ameesh.ci ne relève que les échecs de test : pas de courrier d'alerte
        self.assertEqual(ci.events_of([{"name": "tests (flux) psql 1/3", "output": sortie}], 42), [])
        # sans alerte, la sortie est exactement celle d'avant #899
        sans = ciflux.check_output([ev_debut()])
        self.assertNotIn("plafond", sans["title"].lower())
        self.assertNotIn('"alerte"', sans["text"])
        self.assertNotIn("⚠", sans["summary"])

    def test_conclusion_inchangee(self):
        """L'alerte ne porte aucune conclusion : elle ne fait pas échouer la part."""
        sortie = ciflux.check_output([ev_debut(), ev_fin()], alerte_a(1440.0))
        self.assertNotIn("conclusion", sortie)

    def test_texte(self):
        self.assertEqual(ciflux.seuil_pourcent(alerte_a(1440.0)), 80)
        self.assertIn("45.0 s écoulées sur 45 min", ciflux.texte_alerte(alerte_a(45.0, 45.0)))


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dossier)
        self.flux = self.dossier / "flux.jsonl"

    def ecrire(self, *events):
        with self.flux.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")

    def args(self, **kw):
        # plafond_min=0 et debut=0 coupent le repli sur l'environnement : les
        # tests ne dépendent pas des variables du job qui les exécute
        return argparse.Namespace(**dict(
            dict(file=str(self.flux), name="tests (flux) psql 1/3", interval=1.0,
                 max_duration=100.0, plafond_min=0, debut=0), **kw))

    def etat(self) -> dict:
        return json.loads(pathlib.Path(str(self.flux) + ".check").read_text(encoding="utf-8"))

    def test_publie_l_alerte_une_fois(self):
        gh = FauxGitHub()
        self.ecrire(ev_debut())
        heures = iter([DEBUT + 100, DEBUT + 1439, DEBUT + 1440, DEBUT + 1500])
        tours = {"n": 0}

        def dormir(_):
            tours["n"] += 1
            if tours["n"] == 3:
                self.ecrire(ev_fin())  # la part finit juste après l'alerte

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ciflux.publish(self.args(plafond_min=30, debut=DEBUT), gh=gh,
                           dormir=dormir, horloge=lambda: 0.0,
                           maintenant=lambda: next(heures))
        # une seule alerte publiée, et une seule annotation de job (le log que
        # l'étape `fermer` relit)
        self.assertEqual(len(gh.alertes()), 2, "alerte publiée puis gardée à la conclusion")
        self.assertEqual(err.getvalue().count("::warning"), 1, err.getvalue())
        self.assertIn("80 %", gh.alertes()[0]["title"])
        # l'alerte est dans l'état, la durée du job y est mesurée à la fin
        self.assertEqual(self.etat()["plafond_min"], 30)
        self.assertEqual(self.etat()["debut"], DEBUT)
        self.assertEqual(self.etat()["fin_ecoule_s"], 1500.0)
        self.assertEqual(self.etat()["conclusion"], "success")
        # la part reste verte : l'alerte n'a pas changé la conclusion
        self.assertEqual(gh.appels[-1][3], "success")

    def test_sans_plafond_rien_ne_change(self):
        gh = FauxGitHub()
        self.ecrire(ev_debut())
        ciflux.publish(self.args(), gh=gh, dormir=lambda _: self.ecrire(ev_fin()),
                       horloge=lambda: 0.0, maintenant=lambda: DEBUT + 100000)
        self.assertEqual(gh.alertes(), [])
        self.assertNotIn("plafond_min", self.etat())

    def test_close_reprend_l_alerte_quand_la_part_n_a_pas_fini(self):
        """Part interrompue au plafond : `close` (if: always()) garde l'alerte."""
        gh = FauxGitHub()
        self.ecrire(ev_debut())
        stop = self.dossier / "flux.jsonl.stop"
        ciflux.publish(self.args(plafond_min=30, debut=DEBUT), gh=gh,
                       dormir=lambda _: stop.touch(), horloge=lambda: 0.0,
                       maintenant=lambda: DEBUT + 1440)
        self.assertNotIn("conclusion", self.etat())
        ciflux.close(self.args(), gh=gh)
        self.assertIn("⚠", gh.sorties()[-1]["title"])
        self.assertEqual(gh.appels[-1][3], "cancelled")

    def test_repli_sur_l_environnement(self):
        """Le workflow passe les options ; l'environnement sert de repli."""
        gh = FauxGitHub()
        self.ecrire(ev_debut())
        env = {"AMEESH_PLAFOND_MIN": "30", "AMEESH_JOB_DEBUT": str(int(DEBUT))}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            ciflux.publish(self.args(plafond_min=None, debut=None), gh=gh,
                           dormir=lambda _: self.ecrire(ev_fin()), horloge=lambda: 0.0,
                           maintenant=lambda: DEBUT + 1440)
        self.assertEqual(len(gh.alertes()), 2)

    def test_refus_de_github_n_alerte_pas_l_infini(self):
        """Un jeton en lecture seule (fourche) : aucune publication, aucun échec."""
        gh = FauxGitHub(refus=True)
        self.ecrire(ev_debut())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ciflux.publish(self.args(plafond_min=30, debut=DEBUT), gh=gh,
                                            dormir=lambda _: None, horloge=lambda: 0.0,
                                            maintenant=lambda: DEBUT + 1440), 0)
        self.assertEqual(gh.appels, [])

    def test_non_finis_ne_sont_pas_publies_ni_persistes(self):
        """#1044 N1 : NaN/infini (options ou environnement) : rien n'est calculé
        ni écrit dans l'état, et le publieur ne lève pas."""
        gh = FauxGitHub()
        self.ecrire(ev_debut())
        env = {ciflux.ENV_PLAFOND: "nan", ciflux.ENV_DEBUT: "inf"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(ciflux.publish(self.args(plafond_min=None, debut=None), gh=gh,
                                            dormir=lambda _: self.ecrire(ev_fin()),
                                            horloge=lambda: 0.0,
                                            maintenant=lambda: DEBUT + 10), 0)
        self.assertEqual(gh.alertes(), [])
        self.assertNotIn("plafond_min", self.etat())
        self.assertNotIn("debut", self.etat())
        brut = (self.dossier / "flux.jsonl.check").read_text(encoding="utf-8")
        self.assertNotIn("NaN", brut)
        self.assertNotIn("Infinity", brut)


class ResumeTest(unittest.TestCase):
    """Le résumé du job donne la part du plafond consommée (la croissance)."""

    def setUp(self):
        self.dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dossier)
        self.flux = self.dossier / "flux.jsonl"
        with self.flux.open("w", encoding="utf-8") as fh:
            for ev in (ev_debut(), ev_fin()):
                fh.write(json.dumps(ev) + "\n")

    def etat(self, **champs):
        (self.dossier / "flux.jsonl.check").write_text(json.dumps(champs), encoding="utf-8")

    def test_ratio_et_alerte(self):
        self.etat(plafond_min=30, fin_ecoule_s=1440.0, alerte=alerte_a(1440.0),
                  conclusion="success")
        texte = ciflux.summary([str(self.flux)])
        self.assertIn("⚠ Plafond du job : 24 min 00 s à la fin de la part sur 30 min (80 %)",
                      texte)
        self.assertIn("alerte à 80 % publiée dans le check run", texte)

    def test_sous_le_seuil_sans_marque(self):
        self.etat(plafond_min=30, fin_ecoule_s=600.0, conclusion="success")
        texte = ciflux.summary([str(self.flux)])
        self.assertIn("Plafond du job : 10 min 00 s à la fin de la part sur 30 min (33 %)", texte)
        self.assertNotIn("⚠", texte)

    def test_seuil_atteint_sans_alerte_publiee(self):
        """Le ratio suffit à voir le seuil franchi, même sans alerte publiée."""
        self.etat(plafond_min=30, fin_ecoule_s=1500.0, conclusion="success")
        texte = ciflux.summary([str(self.flux)])
        self.assertIn("⚠", texte)
        self.assertIn("seuil de 80 % atteint", texte)

    def test_resume_sans_etat_inchange(self):
        self.assertNotIn("Plafond", ciflux.summary([str(self.flux)]))

    def test_alerte_sans_fin_n_est_pas_une_duree_finale(self):
        """#1044 N2 : part interrompue — la durée de l'alerte est étiquetée."""
        self.etat(plafond_min=30, alerte=alerte_a(1440.0), conclusion="cancelled")
        texte = ciflux.summary([str(self.flux)])
        self.assertIn("24 min 00 s écoulées au moment de l'alerte sur 30 min (80 %)", texte)
        self.assertIn("durée finale non attestée", texte)
        self.assertNotIn("à la fin de la part", texte)

    def test_duree_finale_attestee_prime_sur_l_alerte(self):
        """#1044 N2 : avec `fin_ecoule_s`, la mesure finale reste la référence."""
        self.etat(plafond_min=30, fin_ecoule_s=1500.0, alerte=alerte_a(1440.0),
                  conclusion="success")
        texte = ciflux.summary([str(self.flux)])
        self.assertIn("25 min 00 s à la fin de la part sur 30 min (83 %)", texte)
        self.assertNotIn("au moment de l'alerte", texte)

    def test_etat_avec_nombres_non_finis_ignore(self):
        """#1044 N1 : un état hérité avec NaN/infini n'affiche aucun plafond."""
        (self.dossier / "flux.jsonl.check").write_text(
            '{"plafond_min": NaN, "fin_ecoule_s": Infinity,'
            ' "alerte": {"ecoule_s": NaN, "plafond_min": Infinity, "seuil": NaN}}',
            encoding="utf-8")
        texte = ciflux.summary([str(self.flux)])
        self.assertNotIn("Plafond", texte)
        self.assertNotIn("NaN", texte)
        self.assertNotIn("Infinity", texte)


class WorkflowTest(unittest.TestCase):
    """Le contrat de la workflow : plafond = timeout-minutes, début mesuré."""

    TEXTE = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def bloc(self, job: str) -> str:
        return re.search(r"\n  %s:\n(.*?)(?=\n  [a-z-]+:\n)" % job, self.TEXTE, re.S).group(1)

    def test_plafond_declare_egal_au_timeout(self):
        for job in ("psycopg", "psql"):
            bloc = self.bloc(job)
            timeout = re.search(r"timeout-minutes:\s*(\d+)", bloc).group(1)
            self.assertIn('AMEESH_PLAFOND_MIN: "%s"' % timeout, bloc, job)
            self.assertRegex(bloc, r"- [&*]debut\b", job)

    def test_debut_mesure_et_passe_a_publish(self):
        self.assertIn('AMEESH_JOB_DEBUT=$(date -u +%s)', self.TEXTE)
        self.assertIn('--plafond-min "$AMEESH_PLAFOND_MIN"', self.TEXTE)
        self.assertIn('--debut "$AMEESH_JOB_DEBUT"', self.TEXTE)

    def test_l_alerte_ne_change_pas_le_check_requis(self):
        """Le job `tests` (le check requis, dernier du fichier) ignore le plafond."""
        debut_tests = self.TEXTE.index("\n  tests:\n")
        self.assertNotIn("AMEESH_PLAFOND_MIN", self.TEXTE[debut_tests:])


if __name__ == "__main__":
    unittest.main()
