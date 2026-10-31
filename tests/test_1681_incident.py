# SPDX-License-Identifier: AGPL-3.0-only
"""#1681 — l'incident de CI ouvert après un atterrissage (développement rouge).

Le canon `processus/lots.md` §2 et §11 bis : quand la suite de la branche
d'intégration échoue après un atterrissage, l'outil ouvre le lot de correction
prioritaire et désigne l'auteur du lot fautif d'après la carte d'impact — sans
accuser sans preuve.

Témoins : la règle (`attribute` : un seul candidat, plusieurs, aucun, inconnu
faute de carte), le port des liens mesurés (`links_of` sur une carte
`ameesh-carte/1`), l'identité d'une exécution (`ref_of`), l'atterrissage lu à
l'API (`ci.landed_commits`, dont le paquet composé du train), et l'écriture
(`open_incident`, `consume`) : une seule fois par exécution, jamais d'exception
propagée, l'auteur du lot candidat assigné quand le registre l'accepte et le
lot rendu à la file sinon.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

import ameesh

from ameesh import incident, radar, runner, train, train_shadow


def commit(sha: str, lot, files, author: str = "deepseek4", summary: str = "travail"):
    return {"sha": sha, "lot": lot, "author": author, "summary": summary, "files": files}


def diagnosis_report(head: str, **over):
    """Le rapport `ameesh-train-diagnosis/1` tel que le train le produit (#1467).

    Même forme que `train_diagnosis.unresolved` : construit ici pour que ce
    consommateur soit témoin du contrat sans dépendre du module du train.
    """
    out = {
        "schema": "ameesh-train-diagnosis/1",
        "packet": {"source": "manaty/nexlink", "target": "develop",
                   "base": "b" * 40, "head": head, "tree": "t" * 40},
        "ci": [{"workflow": "tests.yml", "run": 501, "attempt": 2,
                "failed_jobs": [{"id": 42, "name": "pytest"}]}],
        "environment": {"status": "unknown", "receipts": []},
        "attribution": {"status": "unknown", "candidates": [], "proven_lots": []},
        "missing": ["liste complète des tests en échec liée à la tentative"],
    }
    out.update(over)
    return out


class TestCarteImpact(unittest.TestCase):
    """`attribute` : des liens qualifiés, jamais une preuve de culpabilité."""

    MODULE = "mobile/src/features/pages/screens/PageEditScreen.test.tsx"
    FILE = "mobile/src/features/pages/screens/PageEditScreen.tsx"

    def test_sans_liens_qualifies_inconnu(self):
        out = incident.attribute([self.MODULE], [commit("a" * 40, 1598, [self.FILE])])
        self.assertEqual(out["status"], "unknown")
        self.assertIsNone(out["lot"])
        self.assertEqual(out["candidates"], [])

    def test_lot_unique_candidat(self):
        out = incident.attribute(
            [self.MODULE],
            [commit("a" * 40, 1598, [self.FILE]),
             commit("b" * 40, 1111, ["backend/src/other/thing.ts"])],
            links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "unique")
        self.assertEqual(out["lot"], 1598)
        self.assertEqual(out["author"], "deepseek4")
        self.assertEqual([h["file"] for h in out["candidates"][0]["hits"]], [self.FILE])

    def test_port_appelable(self):
        seen = []

        def links(module):
            seen.append(module)
            return [self.FILE]

        out = incident.attribute([self.MODULE], [commit("a" * 40, 1598, [self.FILE])],
                                 links=links)
        self.assertEqual(out["status"], "unique")
        self.assertEqual(seen, [self.MODULE])

    def test_port_muet_ne_relie_rien(self):
        def links(module):
            raise RuntimeError("carte illisible")

        out = incident.attribute([self.MODULE], [commit("a" * 40, 1598, [self.FILE])],
                                 links=links)
        self.assertEqual(out["status"], "none")

    def test_plusieurs_candidats_aucune_designation(self):
        out = incident.attribute(
            [self.MODULE],
            [commit("a" * 40, 1598, [self.FILE]),
             commit("b" * 40, 1600, [self.FILE])],
            links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "multiple")
        self.assertIsNone(out["lot"])
        self.assertEqual(len(out["candidates"]), 2)

    def test_commit_sans_lot_interdit_la_designation(self):
        out = incident.attribute(
            [self.MODULE],
            [commit("a" * 40, 1598, [self.FILE]),
             commit("b" * 40, None, [self.FILE])],
            links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "multiple")
        self.assertIsNone(out["lot"])

    def test_lien_sans_commit_relie(self):
        out = incident.attribute([self.MODULE], [commit("a" * 40, 1598, ["backend/x.ts"])],
                                 links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "none")
        self.assertIsNone(out["lot"])

    def test_commits_relies_sans_lot(self):
        out = incident.attribute([self.MODULE], [commit("a" * 40, None, [self.FILE])],
                                 links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "unattributed")

    def test_module_hors_carte_ne_relie_rien(self):
        out = incident.attribute([self.MODULE], [commit("a" * 40, 1598, [self.FILE])],
                                 links={"tests/test_autre.py": [self.FILE]})
        self.assertEqual(out["status"], "none")

    def test_echec_agrege_non_rattachable(self):
        out = incident.attribute(["<suite>", ""], [commit("a" * 40, 1598, [self.FILE])],
                                 links={self.MODULE: [self.FILE]})
        self.assertEqual(out["status"], "unattributable")
        self.assertEqual(out["modules"], [])

    def test_modules_dedoublonnes(self):
        out = incident.attribute([self.MODULE, "./" + self.MODULE],
                                 [commit("a" * 40, 1598, [self.FILE])],
                                 links={self.MODULE: [self.FILE]})
        self.assertEqual(out["modules"], [self.MODULE])
        self.assertEqual(len(out["candidates"][0]["hits"]), 1)

    def test_ref_titres_et_corps(self):
        self.assertEqual(incident.ref_of("manaty/nexlink", 12345),
                         "ci-incident:manaty/nexlink:12345")
        unique = {"status": "unique", "lot": 1598}
        title = incident.title_of(branch="develop", run_id=1, attribution=unique)
        self.assertIn("lot #1598 candidat", title)
        body = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=1,
                                modules=[self.MODULE], tests=["b > échoue"],
                                attribution=unique, author="deepseek4")
        self.assertIn("ci-incident:manaty/nexlink:1", body)
        self.assertIn("prioritaire", body)
        self.assertIn("candidat, pas une preuve", body)
        unknown = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=2,
                                   modules=[self.MODULE], attribution={"status": "unknown"})
        self.assertIn("carte d'impact absente", unknown)


class TestLiens(unittest.TestCase):
    """`links_of` : le port test→fichiers, lu dans la carte mesurée."""

    def test_carte_mesuree(self):
        carte = {
            "blocs": {"src/ameesh/ci.py": {"<module>": {"modules": ["test_ci"]},
                                           "main": {"modules": ["test_ci", "test_cli"]}}},
            "donnees": {"tests/parts/durees.json": ["test_parts"]},
            "tables": {"ci_runs": ["test_ci"]},
            "migrations": {"0442_train.sql": ["ci_runs"], "0999_vide.sql": []},
        }
        liens = incident.links_of(carte)
        self.assertEqual(liens["test_ci"],
                         {"src/ameesh/ci.py", "0442_train.sql", "0999_vide.sql"})
        self.assertEqual(liens["test_cli"], {"src/ameesh/ci.py", "0999_vide.sql"})
        self.assertEqual(liens["test_parts"],
                         {"tests/parts/durees.json", "0999_vide.sql"})
        # une migration sans table connue est reliée à tous les tests (la carte)
        self.assertIn("0999_vide.sql", liens["test_parts"])

    def test_cle_de_module(self):
        carte = {"blocs": {"src/ameesh/ci.py": {"<module>": {"modules": ["test_ci"]}}}}
        liens = incident.links_of(carte)
        self.assertEqual(incident._link_files(liens, "tests/test_ci.py"),
                         {"src/ameesh/ci.py"})


class TestDiagnosticFactuel(unittest.TestCase):
    """`diagnosis_of` : le rapport commun lu comme donnée, jamais une autorité."""

    HEAD = "h" * 40

    def test_schema_ou_rapport_etranger_ignore(self):
        self.assertIsNone(incident.diagnosis_of(None))
        self.assertIsNone(incident.diagnosis_of({}))
        self.assertIsNone(incident.diagnosis_of(
            diagnosis_report(self.HEAD, schema="ameesh-train-diagnosis/2")))

    def test_tete_exacte_exigee(self):
        autre = diagnosis_report("x" * 40)
        self.assertIsNone(incident.diagnosis_of(autre, head_sha=self.HEAD))
        self.assertIsNone(incident.diagnosis_of(autre))
        self.assertIsNone(incident.diagnosis_of(diagnosis_report(""), head_sha=""))

    def test_champs_de_liste_mal_formes_ignores(self):
        for field in ("ci", "missing"):
            for value in (1, "illisible", {}):
                with self.subTest(field=field, value=value):
                    report = diagnosis_report(self.HEAD, **{field: value})
                    self.assertIsNone(incident.diagnosis_of(report, head_sha=self.HEAD))
        for section, field in (("environment", "receipts"),
                               ("attribution", "candidates"),
                               ("attribution", "proven_lots")):
            for value in (1, "illisible", {}, ["reçu libre"]):
                with self.subTest(section=section, field=field, value=value):
                    report = diagnosis_report(self.HEAD)
                    report[section][field] = value
                    self.assertIsNone(incident.diagnosis_of(report, head_sha=self.HEAD))
        report = diagnosis_report(self.HEAD)
        report["ci"][0]["failed_jobs"] = 1
        self.assertIsNone(incident.diagnosis_of(report, head_sha=self.HEAD))

    def test_rapport_unknown_rend_les_faits_et_les_manques(self):
        out = incident.diagnosis_of(diagnosis_report(self.HEAD), head_sha=self.HEAD)
        self.assertEqual(out["schema"], incident.DIAGNOSIS_SCHEMA)
        self.assertEqual(out["packet"]["head"], self.HEAD)
        self.assertEqual(out["packet"]["target"], "develop")
        self.assertEqual(out["environment"], {"status": "unknown", "receipts": 0})
        self.assertEqual(out["attribution"]["status"], "unknown")
        self.assertEqual(out["attribution"]["candidates"], [])
        self.assertEqual(out["attribution"]["proven_lots"], [])
        self.assertEqual(out["ci"][0]["attempt"], 2)
        self.assertEqual(out["ci"][0]["failed_jobs"], [{"id": 42, "name": "pytest"}])
        self.assertEqual(out["missing"],
                         ["liste complète des tests en échec liée à la tentative"])

    def test_prouve_sans_recu_retombe_en_candidat(self):
        out = incident.diagnosis_of(
            diagnosis_report(self.HEAD, attribution={
                "status": "proven", "candidates": [],
                "proven_lots": [{"work_item_id": 1598}]}), head_sha=self.HEAD)
        self.assertEqual(out["attribution"]["proven_lots"], [])
        self.assertEqual(out["attribution"]["candidates"], [{"work_item_id": 1598}])
        self.assertEqual(out["attribution"]["status"], "unknown")
        self.assertTrue(any("reçu" in phrase for phrase in out["missing"]))
        body = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=501,
                                diagnosis=out)
        self.assertNotIn("**proven**", body)

    def test_prouve_avec_recu_conserve(self):
        out = incident.diagnosis_of(
            diagnosis_report(self.HEAD,
                             environment={"status": "known",
                                          "receipts": [{"result": "green"}]},
                             attribution={"status": "known", "candidates": [],
                                          "proven_lots": [{"work_item_id": 1598}]}),
            head_sha=self.HEAD)
        self.assertEqual([lot["work_item_id"] for lot in out["attribution"]["proven_lots"]],
                         [1598])
        self.assertEqual(out["environment"]["receipts"], 1)

    def test_corps_porte_le_diagnostic_sans_accuser(self):
        diagnosis = incident.diagnosis_of(diagnosis_report(self.HEAD), head_sha=self.HEAD)
        body = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=501,
                                head_sha=self.HEAD, modules=["mobile/src/a.test.ts"],
                                attribution={"status": "unknown"}, diagnosis=diagnosis)
        self.assertIn("ameesh-train-diagnosis/1", body)
        self.assertIn("tentative `2`", body)
        self.assertIn("liste complète des tests en échec", body)
        self.assertIn("ne désigne aucun lot", body)
        self.assertNotIn("fautif", body)


class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.by_ref = []
        self.by_source_refs = []

    def by_issue_ref(self, ref):
        self.by_ref.append(ref)
        return [r for r in self.rows if r.get("issue_ref") == ref]

    def by_source(self, source):
        # #3607 : l'identité de l'exécution peut vivre dans `source`, l'`issue_ref`
        # étant devenu le ticket miroir.
        self.by_source_refs.append(source)
        return [r for r in self.rows if r.get("source") == source]


class _Ops:
    def __init__(self, work_store):
        self.work = work_store


class _Api:
    """Un client `gh api` figé : `{chemin: JSON}`, et les chemins appelés."""

    def __init__(self, data):
        self.data, self.calls = dict(data), []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.data:
            raise RuntimeError("404 %s" % path)
        return self.data[path]


class TestAtterrissage(unittest.TestCase):
    """`ci.landed_commits` : les commits de l'atterrissage, lus à l'API."""

    def _head(self, base):
        return {"parents": [{"sha": base}]}

    def test_un_seul_lot_les_fichiers_de_la_comparaison(self):
        from ameesh import ci
        api = _Api({
            "repos/manaty/nexlink/commits/" + "h" * 40: self._head("b" * 40),
            "repos/manaty/nexlink/compare/%s...%s" % ("b" * 40, "h" * 40): {
                "commits": [{"sha": "c" * 40, "author": {"login": "deepseek4"},
                             "commit": {"message": "feat: adresse\n\nameesh-work: 1598"}}],
                "files": [{"filename": "mobile/src/a.ts"}]},
        })
        out = ci.landed_commits("manaty/nexlink", "h" * 40, api=api)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["lot"], 1598)
        self.assertEqual(out[0]["author"], "deepseek4")
        self.assertEqual(out[0]["files"], ["mobile/src/a.ts"])

    def test_paquet_un_candidat_par_lot_et_ses_fichiers(self):
        from ameesh import ci
        api = _Api({
            "repos/manaty/nexlink/commits/" + "h" * 40: self._head("b" * 40),
            "repos/manaty/nexlink/compare/%s...%s" % ("b" * 40, "h" * 40): {
                "commits": [{"sha": "c1", "author": {"login": "a1"},
                             "commit": {"message": "lot 11\n\nameesh-work: 11"}},
                            {"sha": "c2", "author": {"login": "a2"},
                             "commit": {"message": "lot 12\n\nameesh-work: 12"}}],
                "files": [{"filename": "mobile/src/a.ts"}, {"filename": "backend/src/b.ts"}]},
            "repos/manaty/nexlink/commits/c1": {"files": [{"filename": "mobile/src/a.ts"}]},
            "repos/manaty/nexlink/commits/c2": {"files": [{"filename": "backend/src/b.ts"}]},
        })
        out = ci.landed_commits("manaty/nexlink", "h" * 40, api=api)
        self.assertEqual([c["lot"] for c in out], [11, 12])
        self.assertEqual(out[0]["files"], ["mobile/src/a.ts"])
        self.assertEqual(out[1]["files"], ["backend/src/b.ts"])

    def test_sans_marqueur_un_candidat_sans_lot(self):
        from ameesh import ci
        api = _Api({
            "repos/manaty/nexlink/commits/" + "h" * 40: self._head("b" * 40),
            "repos/manaty/nexlink/compare/%s...%s" % ("b" * 40, "h" * 40): {
                "commits": [{"sha": "c" * 40, "author": {"login": "x"},
                             "commit": {"message": "chore: divers"}}],
                "files": [{"filename": "mobile/src/a.ts"}]},
        })
        out = ci.landed_commits("manaty/nexlink", "h" * 40, api=api)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["lot"])
        self.assertEqual(out[0]["files"], ["mobile/src/a.ts"])

    def test_paquet_compose_n_attribue_aucun_fichier(self):
        from ameesh import ci
        api = _Api({
            "repos/manaty/nexlink/commits/" + "h" * 40: {"parents": [{"sha": "b" * 40}]},
            "repos/manaty/nexlink/compare/%s...%s" % ("b" * 40, "h" * 40): {
                "commits": [{"sha": "calc", "author": {"login": "train"},
                             "commit": {"message": "paquet\n\nameesh-work: 11\n\nameesh-work: 12"}}],
                "files": [{"filename": "mobile/src/a.ts"}, {"filename": "backend/src/b.ts"}]},
        })
        out = ci.landed_commits("manaty/nexlink", "h" * 40, api=api)
        self.assertEqual([c["lot"] for c in out], [11, 12])
        self.assertEqual([c["files"] for c in out], [[], []])
        self.assertEqual(incident.attribute(["mobile/src/a.test.ts"], out)["status"], "unknown")
        self.assertEqual(incident.attribute(["mobile/src/a.test.ts"], out,
                                            links={"mobile/src/a.test.ts": ["mobile/src/a.ts"]}
                                            )["status"], "none")


class TestOuverture(unittest.TestCase):
    """`open_incident` : une écriture par exécution, jamais d'exception levée."""

    MODULE = "mobile/src/a/b.test.ts"
    FILE = "mobile/src/a/b.ts"

    def _db(self, ops):
        return mock.patch.object(incident.storage, "of", return_value=ops)

    def test_creation_puis_idempotence(self):
        store = _Store()
        created = {"id": 9001}
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value=created) as add, \
                mock.patch.object(incident.work, "get", return_value={"assignee": "deepseek4"}), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=77, app="nexlink",
                modules=[self.MODULE], commits=[commit("a" * 40, 1598, [self.FILE])],
                links={self.MODULE: [self.FILE]})
            self.assertTrue(out["created"])
            self.assertEqual(out["id"], 9001)
            self.assertTrue(out["assigned"])
            self.assertEqual(add.call_args.kwargs["issue_ref"], "ci-incident:manaty/nexlink:77")
            self.assertTrue(add.call_args.kwargs["unique_ref"],
                            "#2954 : la création de l'incident est unique par exécution")
            self.assertEqual(add.call_args.kwargs["priority"], 1)
            self.assertIsNone(add.call_args.kwargs.get("assignee"))
            assign.assert_called_once()

            store.rows.append({"id": 9001, "issue_ref": "ci-incident:manaty/nexlink:77",
                               "state": "intake"})
            again = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=77,
                modules=[self.MODULE], commits=[commit("a" * 40, 1598, [self.FILE])],
                links={self.MODULE: [self.FILE]})
            self.assertFalse(again["created"])
            self.assertEqual(again["id"], 9001)
            self.assertIn("déjà ouvert", again["why"])
            self.assertEqual(add.call_count, 1)

    def test_sans_carte_aucune_designation(self):
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value={"id": 9004}), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=81,
                modules=[self.MODULE], commits=[commit("a" * 40, 1598, [self.FILE])])
        self.assertEqual(out["status"], "unknown")
        self.assertIsNone(out["lot"])
        assign.assert_not_called()

    def test_echec_d_ecriture_rendu_jamais_leve(self):
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", side_effect=RuntimeError("magasin indisponible")), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=78,
                modules=[self.MODULE], commits=[commit("a" * 40, None, [self.FILE])],
                links={self.MODULE: [self.FILE]})
        self.assertIsNone(out["id"])
        self.assertFalse(out["created"])
        self.assertIn("non ouvert", out["why"])
        self.assertEqual(out["status"], "unattributed")
        assign.assert_not_called()

    def test_course_perdue_rend_le_gagnant(self):
        store = _Store()
        winner = {"id": 9002, "issue_ref": "ci-incident:manaty/nexlink:79", "state": "intake"}

        def losing_add(*_a, **_k):
            store.rows.append(winner)
            raise RuntimeError("verrou")

        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", side_effect=losing_add), \
                mock.patch.object(incident.work, "assign"):
            out = incident.open_incident(object(), repo="manaty/nexlink", branch="develop", run_id=79)
        self.assertEqual(out["id"], 9002)
        self.assertFalse(out["created"])
        self.assertIn("course perdue", out["why"])

    def test_incident_ouvert_a_la_main_par_la_source(self):
        """#3607 : l'exécution portée par `source`, l'`issue_ref` étant devenu
        le ticket miroir, est déjà ouverte : l'outil ne crée pas de doublon.

        Le cas réel : #3600 ouvert à la main à 19:23Z pour le run 40223148747
        (`source = ci-incident:…`), la synchro posant ensuite
        `issue_ref = manaty/ameesh#2787` ; à 19:56Z `open_incident` a créé le
        P1 #3603 pour la même exécution.
        """
        ref = "ci-incident:manaty/ameesh:40223148747"
        store = _Store([{"id": 8999, "source": ref, "issue_ref": "manaty/ameesh#2787",
                         "state": "qa"}])
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add") as add, \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(object(), repo="manaty/ameesh", branch="main",
                                         run_id=40223148747, app="ameesh")
        self.assertFalse(out["created"])
        self.assertEqual(out["id"], 8999)
        self.assertIn("incident #8999 déjà ouvert", out["why"])
        add.assert_not_called()
        assign.assert_not_called()
        self.assertEqual(store.by_source_refs, [ref])

    def test_course_perdue_rend_le_gagnant_au_ticket_miroir(self):
        """#3607 : sous le verrou unique_ref, le gagnant a déjà reçu son ticket
        miroir quand le perdant relit ; `source` garde l'identité de
        l'exécution et le perdant rend le gagnant, sans second lot."""
        ref = "ci-incident:manaty/nexlink:79"
        store = _Store()
        winner = {"id": 9007, "source": ref, "issue_ref": "manaty/nexlink#12",
                  "state": "intake"}

        def losing_add(*_a, **_k):
            store.rows.append(winner)
            raise RuntimeError("verrou")

        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", side_effect=losing_add), \
                mock.patch.object(incident.work, "assign"):
            out = incident.open_incident(object(), repo="manaty/nexlink",
                                         branch="develop", run_id=79)
        self.assertEqual(out["id"], 9007)
        self.assertFalse(out["created"])
        self.assertIn("course perdue", out["why"])

    def test_auteur_non_assignable_le_lot_reste_dans_la_file(self):
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value={"id": 9003}), \
                mock.patch.object(incident.work, "get", return_value={"assignee": "agent-arrêté"}), \
                mock.patch.object(incident.work, "assign", side_effect=RuntimeError("non réveillable")):
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=80,
                modules=[self.MODULE], commits=[commit("a" * 40, 1598, [self.FILE],
                                                       author="agent-arrêté")],
                links={self.MODULE: [self.FILE]})
        self.assertTrue(out["created"])
        self.assertFalse(out["assigned"])
        self.assertIn("non assigné", out["why"])

    def test_auteur_est_l_agent_du_lot_pas_l_auteur_git(self):
        # un commit de fusion porte souvent le compte de l'owner ou du
        # composeur : l'incident va à l'agent assigné au lot candidat
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value={"id": 9005}), \
                mock.patch.object(incident.work, "get", return_value={"assignee": "deepseek4"}) as get, \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/ameesh", branch="main", run_id=82,
                modules=[self.MODULE], commits=[commit("a" * 40, 1242, [self.FILE],
                                                       author="smichea")],
                links={self.MODULE: [self.FILE]})
        get.assert_called_once()
        self.assertEqual(get.call_args.args[1], 1242)
        self.assertEqual(assign.call_args.args[2], "deepseek4")
        self.assertEqual(out["author"], "deepseek4")
        self.assertTrue(out["assigned"])

    def test_lot_sans_assigne_personne_n_est_designe(self):
        store = _Store()
        for found in ({"assignee": None}, None, RuntimeError("magasin")):
            kwargs = ({"side_effect": found} if isinstance(found, Exception)
                      else {"return_value": found})
            with self._db(_Ops(store)), \
                    mock.patch.object(incident.work, "add", return_value={"id": 9006}), \
                    mock.patch.object(incident.work, "get", **kwargs), \
                    mock.patch.object(incident.work, "assign") as assign:
                out = incident.open_incident(
                    object(), repo="manaty/ameesh", branch="main", run_id=83,
                    modules=[self.MODULE], commits=[commit("a" * 40, 1242, [self.FILE],
                                                           author="smichea")],
                    links={self.MODULE: [self.FILE]})
            assign.assert_not_called()
            self.assertTrue(out["created"])
            self.assertEqual(out["status"], "unique")
            self.assertEqual(out["author"], "")


class _CiStore:
    def __init__(self, rows, failures=None, replaced=False, broken=False):
        self.rows = list(rows)
        self.failures_map = dict(failures or {})
        self.replaced = replaced
        self.broken = broken

    def latest_concluded(self, branches):
        if self.broken:
            raise RuntimeError("base indisponible")
        return list(self.rows)

    def failures(self, repo, run_id):
        return list(self.failures_map.get((repo, int(run_id)), []))

    def head_replaced(self, repo, branch, head_sha, run_id):
        return self.replaced


class _Full:
    """Le magasin : `ci` pour le relevé, `work` pour l'écriture."""

    def __init__(self, ci_store, work_store):
        self.ci, self.work = ci_store, work_store


class TestConsommateur(unittest.TestCase):
    """`incident.consume` : la source courante, la même tête, une fois."""

    MODULE = "mobile/src/a.test.ts"
    FILE = "mobile/src/a.ts"
    LINKS = {MODULE: [FILE]}

    def _run(self, **over):
        row = {"repo": "manaty/nexlink", "branch": "develop", "run_id": 501,
               "head_sha": "h" * 40, "workflow": "tests.yml", "status": "failure",
               "url": "https://exemple/501", "concluded_ts": "2026-10-15T14:00:00Z"}
        row.update(over)
        return row

    def _db(self, ci_store, work_store):
        return mock.patch.object(incident.storage, "of",
                                 return_value=_Full(ci_store, work_store))

    def test_rouge_candidat_et_ouverture(self):
        store = _Store()
        lands = lambda repo, sha: [commit("c" * 40, 1598, [self.FILE])]
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9100}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lands, links=self.LINKS)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "unique")
        self.assertEqual(out[0]["lot"], 1598)
        self.assertTrue(out[0]["created"])
        self.assertEqual(out[0]["id"], 9100)
        self.assertEqual(out[0]["app"], "nexlink")

    def test_incident_deja_ouvert_sans_relevé_github(self):
        store = _Store([{"id": 9200, "issue_ref": "ci-incident:manaty/nexlink:501",
                         "state": "intake"}])
        lands = mock.Mock(return_value=[])
        diagnoses = mock.Mock(return_value=None)
        with self._db(_CiStore([self._run()]), store), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lands, links=self.LINKS,
                                   diagnoses=diagnoses)
        lands.assert_not_called()
        diagnoses.assert_not_called()
        add.assert_not_called()
        self.assertEqual(out[0]["id"], 9200)
        self.assertIn("déjà ouvert", out[0]["why"])

    def test_incident_ouvert_a_la_main_par_la_source_sans_releve_github(self):
        """#3607 : le veilleur voit le lot ouvert à la main par sa `source`,
        même quand la synchro a posé l'`issue_ref` au ticket miroir."""
        store = _Store([{"id": 9201,
                         "source": "ci-incident:manaty/nexlink:501",
                         "issue_ref": "manaty/nexlink#42", "state": "qa"}])
        lands = mock.Mock(return_value=[])
        with self._db(_CiStore([self._run()]), store), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lands, links=self.LINKS)
        lands.assert_not_called()
        add.assert_not_called()
        self.assertEqual(out[0]["id"], 9201)
        self.assertIn("déjà ouvert", out[0]["why"])

    def test_atterrissage_illisible_reste_dans_le_motif(self):
        store = _Store()

        def lands(*_a):
            raise RuntimeError("404")

        with self._db(_CiStore([self._run()]), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9201}):
            out = incident.consume(object(), lands=lands, links=self.LINKS)
        self.assertTrue(out[0]["created"])
        self.assertIn("atterrissage illisible", out[0]["why"])

    def test_sans_carte_inconnu(self):
        store = _Store()
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9102}), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.consume(object(), lands=lambda *a: [commit("c" * 40, 1598, [self.FILE])])
        self.assertEqual(out[0]["status"], "unknown")
        self.assertIsNone(out[0]["lot"])
        assign.assert_not_called()

    def test_port_contextuel_qualifie_la_ligne(self):
        store = _Store()
        seen = []

        def links_for(repo, sha):
            seen.append((repo, sha))
            return {self.MODULE: [self.FILE]}

        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9130}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(
                object(), lands=lambda *a: [commit("c" * 40, 1598, [self.FILE])],
                links_for=links_for)
        self.assertEqual(seen, [("manaty/nexlink", "h" * 40)])
        self.assertEqual(out[0]["status"], "unique")
        self.assertEqual(out[0]["lot"], 1598)
        self.assertEqual(out[0]["links"], "qualifiée (dépôt + tête)")

    def test_port_contextuel_none_ne_reprend_pas_la_carte_globale(self):
        store = _Store()
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9131}), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.consume(
                object(), lands=lambda *a: [commit("c" * 40, 1598, [self.FILE])],
                links=self.LINKS, links_for=lambda repo, sha: None)
        self.assertEqual(out[0]["status"], "unknown")
        self.assertIsNone(out[0]["lot"])
        self.assertEqual(out[0]["links"], "absente")
        assign.assert_not_called()

    def test_port_contextuel_illisible_garde_l_incident(self):
        store = _Store()

        def broken(repo, sha):
            raise RuntimeError("carte absente")

        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9132}):
            out = incident.consume(object(), lands=lambda *a: [], links_for=broken)
        self.assertEqual(out[0]["links"], "illisible : RuntimeError")
        self.assertEqual(out[0]["status"], "unknown")
        self.assertTrue(out[0]["created"])

    def test_port_contextuel_par_depot_et_tete(self):
        rows = [self._run(), self._run(repo="manaty/ameesh", branch="main", run_id=502,
                                       head_sha="g" * 40)]
        store = _Store()
        seen = []

        def links_for(repo, sha):
            seen.append((repo, sha))
            return {self.MODULE: [self.FILE]} if repo == "manaty/nexlink" else None

        failures = {("manaty/nexlink", 501): [{"module": self.MODULE, "test": "a > casse"}],
                    ("manaty/ameesh", 502): [{"module": self.MODULE, "test": "a > casse"}]}
        with self._db(_CiStore(rows, failures), store), \
                mock.patch.object(incident.work, "add",
                                  side_effect=[{"id": 9133}, {"id": 9134}]), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(
                object(), lands=lambda *a: [commit("c" * 40, 1598, [self.FILE])],
                links_for=links_for)
        self.assertEqual(sorted(seen), sorted([("manaty/nexlink", "h" * 40),
                                               ("manaty/ameesh", "g" * 40)]))
        self.assertEqual({line["repo"]: line["status"] for line in out},
                         {"manaty/nexlink": "unique", "manaty/ameesh": "unknown"})
        self.assertEqual({line["repo"]: line["links"] for line in out},
                         {"manaty/nexlink": "qualifiée (dépôt + tête)",
                          "manaty/ameesh": "absente"})

    def test_port_contextuel_sans_tete_observee_non_appele(self):
        store = _Store()
        called = []
        with self._db(_CiStore([self._run(head_sha="")],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9135}), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.consume(
                object(), lands=lambda *a: [], links=self.LINKS,
                links_for=lambda repo, sha: called.append((repo, sha)))
        self.assertEqual(called, [])
        self.assertEqual(out[0]["status"], "unknown")
        self.assertEqual(out[0]["links"], "absente (sans tête observée)")
        assign.assert_not_called()

    def test_verte_aucun_incident(self):
        with self._db(_CiStore([self._run(status="success")]), _Store()), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lambda *a: [], links=self.LINKS)
        self.assertEqual(out, [])
        add.assert_not_called()

    def test_tete_remplacee_verdict_perime(self):
        with self._db(_CiStore([self._run()], replaced=True), _Store()), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lambda *a: [], links=self.LINKS)
        self.assertEqual(len(out), 1)
        self.assertIn("périmé", out[0]["why"])
        add.assert_not_called()

    def test_releve_illisible_ne_leve_pas(self):
        with self._db(_CiStore([], broken=True), _Store()):
            out = incident.consume(object(), integration={"manaty/nexlink": ("develop", "nexlink")},
                                   lands=lambda *a: [], links=self.LINKS)
        self.assertEqual(len(out), 1)
        self.assertIn("illisible", out[0]["why"])

    def test_un_depot_illisible_n_empeche_pas_l_autre(self):
        class _Both:
            ci = _CiStore([], broken=True)

        with mock.patch.object(incident.storage, "of", return_value=_Both()), \
                mock.patch.object(incident.work, "add", return_value={"id": 9101}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), integration={"manaty/nexlink": ("develop", "nexlink"),
                                                          "manaty/ameesh": ("main", "ameesh")},
                                   lands=lambda *a: [], links=self.LINKS)
        self.assertEqual(len(out), 2)
        self.assertTrue(all("illisible" in line["why"] for line in out))

    def test_rapport_factuel_lu_et_verse_au_corps(self):
        store = _Store()
        seen = []

        def diagnoses(repo, sha):
            seen.append((repo, sha))
            return diagnosis_report(sha)

        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9110}) as add, \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lambda *a: [], diagnoses=diagnoses)
        self.assertEqual(seen, [("manaty/nexlink", "h" * 40)])
        self.assertEqual(out[0]["diagnosis"], "lu (tête exacte)")
        body = add.call_args.kwargs["body"]
        self.assertIn("ameesh-train-diagnosis/1", body)
        self.assertIn("liste complète des tests en échec", body)

    def test_rapport_etranger_ignore_et_incident_ouvert(self):
        store = _Store()
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9111}) as add, \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lambda *a: [],
                                   diagnoses=lambda repo, sha: diagnosis_report("x" * 40))
        self.assertEqual(out[0]["diagnosis"], "ignoré : schéma ou tête étrangers")
        self.assertNotIn("ameesh-train-diagnosis/1", add.call_args.kwargs["body"])

    def test_rapport_illisible_n_empeche_pas_l_incident(self):
        store = _Store()

        def broken(repo, sha):
            raise RuntimeError("magasin absent")

        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9112}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lambda *a: [], diagnoses=broken)
        self.assertEqual(out[0]["diagnosis"], "illisible : RuntimeError")
        self.assertTrue(out[0]["created"])

    def test_rapport_mal_forme_n_empeche_aucun_incident(self):
        rows = [self._run(), self._run(repo="manaty/ameesh", branch="main", run_id=502)]
        with self._db(_CiStore(rows), _Store()), \
                mock.patch.object(incident.work, "add", side_effect=[{"id": 9115}, {"id": 9116}]):
            out = incident.consume(object(), lands=lambda *a: [],
                                   diagnoses=lambda repo, sha: diagnosis_report(sha, ci=1))
        self.assertEqual(len(out), 2)
        self.assertTrue(all(line["created"] for line in out))
        self.assertTrue(all(line["diagnosis"] != "lu (tête exacte)" for line in out))

    def test_exception_de_decodage_n_empeche_pas_l_incident(self):
        with self._db(_CiStore([self._run()]), _Store()), \
                mock.patch.object(incident, "diagnosis_of", side_effect=ValueError("illisible")), \
                mock.patch.object(incident.work, "add", return_value={"id": 9117}):
            out = incident.consume(object(), lands=lambda *a: [],
                                   diagnoses=lambda repo, sha: diagnosis_report(sha))
        self.assertEqual(out[0]["diagnosis"], "illisible : ValueError")
        self.assertTrue(out[0]["created"])

    def test_sans_lecteur_le_releve_local_fait_foi(self):
        store = _Store()
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9113}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(),
                                   lands=lambda *a: [commit("c" * 40, 1598, [self.FILE])],
                                   links=self.LINKS)
        self.assertEqual(out[0]["diagnosis"], "absent")
        self.assertEqual(out[0]["status"], "unique")

    def test_sans_tete_observee_aucun_rapport_lu(self):
        store = _Store()
        called = []
        with self._db(_CiStore([self._run(head_sha="")],
                               {("manaty/nexlink", 501): [{"module": self.MODULE,
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9114}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lambda *a: [],
                                   diagnoses=lambda repo, sha: called.append((repo, sha)))
        self.assertEqual(called, [])
        self.assertEqual(out[0]["diagnosis"], "absent")


class TestRaccordTrain(unittest.TestCase):
    """`Runner.incident_once` : les faits du train #1467 quand ils sont là,
    le relevé direct de #1681 sinon — un incident n'est jamais perdu."""

    def _fake(self, lines=(), error=None):
        consume = mock.Mock(side_effect=error) if error is not None \
            else mock.Mock(return_value=list(lines))
        return types.SimpleNamespace(consume=consume)

    def _patch_train(self, fake):
        """Le module du train : `from . import` lit d'abord l'attribut du
        paquet dès qu'un import réel a eu lieu dans le processus."""
        return mock.patch.object(ameesh, "train_incidents", fake, create=True)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_les_passages_du_train_sont_remis_au_consommateur(self):
        db = object()
        fake = self._fake([{"repo": "manaty/nexlink", "branch": "develop", "run_id": 501,
                            "created": True, "id": 9100, "assigned": False,
                            "status": "unique"}])
        with mock.patch.dict(sys.modules, {"ameesh.train_incidents": fake}), \
                self._patch_train(fake), \
                mock.patch.object(incident, "consume") as direct, \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.incident_once(types.SimpleNamespace(train_passages=("p",), db=None), db)
        fake.consume.assert_called_once_with(db, ("p",), consumer=incident)
        direct.assert_not_called()
        self.assertTrue(any("lot #9100 ouvert" in call.args[0]
                            for call in log.call_args_list))

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_sans_passage_remis_vides(self):
        db = object()
        fake = self._fake()
        with mock.patch.dict(sys.modules, {"ameesh.train_incidents": fake}), \
                self._patch_train(fake), \
                mock.patch.object(incident, "consume") as direct, \
                mock.patch.object(runner, "log_async"):
            runner.Runner.incident_once(types.SimpleNamespace(db=None), db)
        fake.consume.assert_called_once_with(db, (), consumer=incident)
        direct.assert_not_called()

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_sans_module_ni_passage_le_releve_direct_fait_foi(self):
        db = object()
        with mock.patch.object(incident, "consume", return_value=[]) as direct, \
                mock.patch.object(runner, "log_async"):
            runner.Runner.incident_once(types.SimpleNamespace(db=None), db)
        direct.assert_called_once_with(db)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_faits_du_train_illisibles_releve_direct_sans_perte(self):
        db = object()
        fake = self._fake(error=RuntimeError("manifeste absent"))
        stub = types.SimpleNamespace(train_passages=("p",), db=None)
        opened = {"repo": "manaty/nexlink", "branch": "develop", "run_id": 501,
                  "created": True, "id": 9200, "assigned": True, "author": "deepseek4",
                  "status": "unique"}
        with mock.patch.dict(sys.modules, {"ameesh.train_incidents": fake}), \
                self._patch_train(fake), \
                mock.patch.object(incident, "consume", return_value=[opened]) as direct, \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.incident_once(stub, db)
            runner.Runner.incident_once(stub, db)
        self.assertEqual(fake.consume.call_count, 2)
        self.assertEqual(direct.call_count, 2)
        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(sum("train facts unreadable" in m for m in messages), 1)
        self.assertTrue(any("lot #9200 ouvert" in m for m in messages))

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_un_passage_d_ombre_ne_rend_pas_les_faits_du_train_illisibles(self):
        """#2682 : l'ombre n'a aucun fait de train ; le relevé direct fait foi."""
        db = object()
        project = radar.Project(name="ameesh", repo="/nonexistent", target="main",
                                github="manaty/ameesh")
        shadow = types.SimpleNamespace(
            policy=train.Policy(enabled=True),
            incident_facts=lambda db, fallback=None: train_shadow.NoTrainFacts(
                project, fallback=fallback))
        with mock.patch.object(incident, "consume", return_value=[]) as direct, \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.incident_once(
                types.SimpleNamespace(train_passages=(shadow,), db=None), db)
        direct.assert_called_once()
        self.assertIs(direct.call_args.args[0], db)
        self.assertFalse(any("train facts unreadable" in call.args[0]
                             for call in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
