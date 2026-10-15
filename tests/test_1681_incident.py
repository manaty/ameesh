# SPDX-License-Identifier: AGPL-3.0-only
"""#1681 — l'incident de CI ouvert après un atterrissage (développement rouge).

Le canon `processus/lots.md` §2 et §11 bis : quand la suite de la branche
d'intégration échoue après un atterrissage, l'outil ouvre le lot de correction
prioritaire et désigne l'auteur du lot fautif d'après la carte d'impact — sans
accuser sans preuve.

Témoins : la règle pure (`attribute` : un seul lot, plusieurs candidats, aucun,
non rattachable), l'identité d'une exécution (`ref_of`), et l'écriture
(`open_incident`) : une seule fois par exécution, jamais d'exception propagée,
l'auteur désigné assigné quand le registre l'accepte et le lot rendu à la file
sinon.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import incident


def commit(sha: str, lot, files, author: str = "deepseek4", summary: str = "travail"):
    return {"sha": sha, "lot": lot, "author": author, "summary": summary, "files": files}


class TestCarteImpact(unittest.TestCase):
    """`attribute` : la règle seule, sans réseau ni magasin."""

    def test_lot_unique_designe(self):
        out = incident.attribute(
            ["mobile/src/features/pages/screens/PageEditScreen.test.tsx"],
            [commit("a" * 40, 1598, ["mobile/src/features/pages/screens/PageEditScreen.tsx"]),
             commit("b" * 40, 1111, ["backend/src/other/thing.ts"])])
        self.assertEqual(out["status"], "unique")
        self.assertEqual(out["lot"], 1598)
        self.assertEqual(out["author"], "deepseek4")
        self.assertEqual([h["file"] for h in out["candidates"][0]["hits"]],
                         ["mobile/src/features/pages/screens/PageEditScreen.tsx"])

    def test_source_du_meme_nom_dans_un_autre_repertoire(self):
        self.assertTrue(incident.related("backend/src/a/foo.spec.ts", "backend/src/b/foo.ts"))
        self.assertFalse(incident.related("backend/src/a/foo.spec.ts", "backend/src/b/bar.ts"))

    def test_infrastructure_du_meme_paquet_seulement(self):
        self.assertTrue(incident.related("mobile/src/a/b.test.ts", "mobile/package.json"))
        self.assertFalse(incident.related("mobile/src/a/b.test.ts", "backend/package.json"))
        self.assertTrue(incident.related("mobile/src/a/b.test.ts", "scripts/ci/related-tests.mjs"))

    def test_descente_dans_un_sous_repertoire_du_module(self):
        self.assertTrue(incident.related("mobile/src/a/b.test.ts", "mobile/src/a/deep/c.ts"))
        self.assertFalse(incident.related("mobile/src/a/deep/b.test.ts", "mobile/src/a/c.ts"))

    def test_plusieurs_candidats_aucune_accusation(self):
        out = incident.attribute(
            ["mobile/src/a/b.test.ts"],
            [commit("a" * 40, 1598, ["mobile/src/a/b.ts"]),
             commit("b" * 40, 1600, ["mobile/src/a/c.ts"])])
        self.assertEqual(out["status"], "multiple")
        self.assertIsNone(out["lot"])
        self.assertEqual(len(out["candidates"]), 2)

    def test_commit_sans_lot_interdit_l_accusation(self):
        out = incident.attribute(
            ["mobile/src/a/b.test.ts"],
            [commit("a" * 40, 1598, ["mobile/src/a/b.ts"]),
             commit("b" * 40, None, ["mobile/src/a/b.ts"])])
        self.assertEqual(out["status"], "multiple")
        self.assertIsNone(out["lot"])

    def test_aucun_commit_relie(self):
        out = incident.attribute(["mobile/src/a/b.test.ts"], [commit("a" * 40, 1598, ["backend/x.ts"])])
        self.assertEqual(out["status"], "none")
        self.assertIsNone(out["lot"])

    def test_commits_relies_sans_lot(self):
        out = incident.attribute(["mobile/src/a/b.test.ts"], [commit("a" * 40, None, ["mobile/src/a/b.ts"])])
        self.assertEqual(out["status"], "unattributed")

    def test_echec_agrege_non_rattachable(self):
        out = incident.attribute(["<suite>", ""], [commit("a" * 40, 1598, ["mobile/src/a/b.ts"])])
        self.assertEqual(out["status"], "unattributable")
        self.assertEqual(out["modules"], [])

    def test_modules_dedoublonnes(self):
        out = incident.attribute(
            ["mobile/src/a/b.test.ts", "./mobile/src/a/b.test.ts"],
            [commit("a" * 40, 1598, ["mobile/src/a/b.ts"])])
        self.assertEqual(out["modules"], ["mobile/src/a/b.test.ts"])
        self.assertEqual(len(out["candidates"][0]["hits"]), 1)

    def test_ref_et_titres(self):
        self.assertEqual(incident.ref_of("manaty/nexlink", 12345),
                         "ci-incident:manaty/nexlink:12345")
        unique = {"status": "unique", "lot": 1598}
        self.assertIn("lot #1598", incident.title_of(branch="develop", run_id=1, attribution=unique))
        body = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=1,
                                modules=["mobile/src/a/b.test.ts"], tests=["b > échoue"],
                                attribution=unique, author="deepseek4")
        self.assertIn("ci-incident:manaty/nexlink:1", body)
        self.assertIn("prioritaire", body)
        multiple = incident.body_of(repo="manaty/nexlink", branch="develop", run_id=2,
                                    modules=["mobile/src/a/b.test.ts"], attribution={"status": "multiple"})
        self.assertIn("aucun lot n'est accusé".lower(), multiple.lower())


class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.by_ref = []

    def by_issue_ref(self, ref):
        self.by_ref.append(ref)
        return [r for r in self.rows if r.get("issue_ref") == ref]


class _Ops:
    def __init__(self, work_store):
        self.work = work_store


class TestOuverture(unittest.TestCase):
    """`open_incident` : une écriture par exécution, jamais d'exception levée."""

    def _db(self, ops):
        return mock.patch.object(incident.storage, "of", return_value=ops)

    def test_creation_puis_idempotence(self):
        store = _Store()
        created = {"id": 9001}
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value=created) as add, \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=77, app="nexlink",
                modules=["mobile/src/a/b.test.ts"],
                commits=[commit("a" * 40, 1598, ["mobile/src/a/b.ts"])])
            self.assertTrue(out["created"])
            self.assertEqual(out["id"], 9001)
            self.assertTrue(out["assigned"])
            self.assertEqual(add.call_args.kwargs["issue_ref"], "ci-incident:manaty/nexlink:77")
            self.assertEqual(add.call_args.kwargs["priority"], 1)
            self.assertIsNone(add.call_args.kwargs.get("assignee"))
            assign.assert_called_once()

            store.rows.append({"id": 9001, "issue_ref": "ci-incident:manaty/nexlink:77",
                               "state": "intake"})
            again = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=77,
                modules=["mobile/src/a/b.test.ts"],
                commits=[commit("a" * 40, 1598, ["mobile/src/a/b.ts"])])
            self.assertFalse(again["created"])
            self.assertEqual(again["id"], 9001)
            self.assertIn("déjà ouvert", again["why"])
            self.assertEqual(add.call_count, 1)

    def test_echec_d_ecriture_rendu_jamais_leve(self):
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", side_effect=RuntimeError("magasin indisponible")), \
                mock.patch.object(incident.work, "assign") as assign:
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=78,
                modules=["mobile/src/a/b.test.ts"],
                commits=[commit("a" * 40, None, ["mobile/src/a/b.ts"])])
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

    def test_auteur_non_assignable_le_lot_reste_dans_la_file(self):
        store = _Store()
        with self._db(_Ops(store)), \
                mock.patch.object(incident.work, "add", return_value={"id": 9003}), \
                mock.patch.object(incident.work, "assign", side_effect=RuntimeError("non réveillable")):
            out = incident.open_incident(
                object(), repo="manaty/nexlink", branch="develop", run_id=80,
                modules=["mobile/src/a/b.test.ts"],
                commits=[commit("a" * 40, 1598, ["mobile/src/a/b.ts"], author="agent-arrêté")])
        self.assertTrue(out["created"])
        self.assertFalse(out["assigned"])
        self.assertIn("non assigné", out["why"])


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

    def _run(self, **over):
        row = {"repo": "manaty/nexlink", "branch": "develop", "run_id": 501,
               "head_sha": "h" * 40, "workflow": "tests.yml", "status": "failure",
               "url": "https://exemple/501", "concluded_ts": "2026-10-15T14:00:00Z"}
        row.update(over)
        return row

    def _db(self, ci_store, work_store):
        return mock.patch.object(incident.storage, "of",
                                 return_value=_Full(ci_store, work_store))

    def test_rouge_designation_et_ouverture(self):
        store = _Store()
        lands = lambda repo, sha: [commit("c" * 40, 1598, ["mobile/src/a.ts"])]
        with self._db(_CiStore([self._run()],
                               {("manaty/nexlink", 501): [{"module": "mobile/src/a.test.ts",
                                                           "test": "a > casse"}]}), store), \
                mock.patch.object(incident.work, "add", return_value={"id": 9100}), \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lands)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "unique")
        self.assertEqual(out[0]["lot"], 1598)
        self.assertTrue(out[0]["created"])
        self.assertEqual(out[0]["id"], 9100)
        self.assertEqual(out[0]["app"], "nexlink")

    def test_verte_aucun_incident(self):
        with self._db(_CiStore([self._run(status="success")]), _Store()), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lambda *a: [])
        self.assertEqual(out, [])
        add.assert_not_called()

    def test_tete_remplacee_verdict_perime(self):
        with self._db(_CiStore([self._run()], replaced=True), _Store()), \
                mock.patch.object(incident.work, "add") as add:
            out = incident.consume(object(), lands=lambda *a: [])
        self.assertEqual(len(out), 1)
        self.assertIn("périmé", out[0]["why"])
        add.assert_not_called()

    def test_releve_illisible_ne_leve_pas(self):
        with self._db(_CiStore([], broken=True), _Store()):
            out = incident.consume(object(), integration={"manaty/nexlink": ("develop", "nexlink")},
                                   lands=lambda *a: [])
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
                                   lands=lambda *a: [])
        self.assertEqual(len(out), 2)
        self.assertTrue(all("illisible" in line["why"] for line in out))


if __name__ == "__main__":
    unittest.main()
