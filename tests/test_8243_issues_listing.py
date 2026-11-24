# SPDX-License-Identifier: AGPL-3.0-only
"""#8243 : la projection GitHub lit les issues page par page, et une panne
répétée de la projection alerte le pilote du projet.

La liste complète de manaty/ameesh (6 443 issues et PR, 65 pages) dépassait
le délai unique de 60 s de `gh` : une seule invocation `--paginate` échouait
en permanence, et la panne restait dans le journal. Aucune requête réseau
ici : un faux `gh` rend les pages, et `mail.send` est remplacé.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import plan_github

from .support import PgTestCase

REPO = "acme/web"
SETTINGS = {"projects": {"web": REPO}, "host": "atelier", "interval": 300}


class _PageGh:
    """Un faux `gh` qui pagine comme l'API : `page` et `per_page` compris."""

    def __init__(self, count=252, pulls=(251, 252)):
        self.calls: list = []
        self.rows = []
        for number in range(1, count + 1):
            row = {"number": number, "id": 1000 + number,
                   "title": "titre %d" % number, "body": "corps %d" % number,
                   "state": "open", "state_reason": None,
                   "labels": [{"name": "ameesh"}]}
            if number in pulls:
                row["pull_request"] = {"url": "https://example.invalid/pr/%d" % number}
            self.rows.append(row)
        #: numéro de page qui expire (124), pour prouver la borne par page
        self.fail_page: int | None = None
        #: rend toujours une page pleine, pour la garde de fin de pagination
        self.full_forever = False

    def run(self, argv, stdin=None, timeout=None):
        self.calls.append((list(argv), timeout))
        path = next((a for a in argv if a.startswith("repos/")), "")
        query = path.partition("?")[2]
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        page = int(params.get("page") or 1)
        size = int(params.get("per_page") or 30)
        if self.fail_page == page:
            return 124, "", "timed out (60.0s)"
        if self.full_forever:
            chunk = self.rows[:size]
        else:
            chunk = self.rows[(page - 1) * size:page * size]
        return 0, "".join(json.dumps(row) + "\n" for row in chunk), ""


class IssuesListingTest(unittest.TestCase):
    def test_une_page_par_appel_sans_paginate(self):
        gh = _PageGh()
        rows = plan_github._Repo(gh, REPO, False).issues()
        # 252 lignes, dont les PR 251 et 252 : 250 issues, jamais les PR
        self.assertEqual(len(rows), 250)
        self.assertNotIn(251, [row["number"] for row in rows])
        self.assertEqual([row["number"] for row in rows], list(range(1, 251)))
        pages = []
        for call, timeout in gh.calls:
            self.assertNotIn("--paginate", call)
            path = next(a for a in call if a.startswith("repos/"))
            self.assertIn("state=all", path)
            self.assertIn("per_page=100", path)
            # le délai est propre à cette lecture et s'applique à CHAQUE page
            self.assertEqual(timeout, plan_github.ISSUES_PAGE_TIMEOUT)
            pages.append(int(path.rsplit("page=", 1)[1]))
        self.assertEqual(pages, [1, 2, 3])

    def test_une_page_qui_expire_est_dite_et_arrete_la_liste(self):
        gh = _PageGh()
        gh.fail_page = 2
        with self.assertRaises(plan_github.GithubError) as caught:
            plan_github._Repo(gh, REPO, False).issues()
        # l'erreur nomme le code du délai, pas une liste tronquée prise pour
        # une liste complète ; la première page a été lue, la troisième jamais
        self.assertIn("124", str(caught.exception))
        self.assertEqual(len(gh.calls), 2)

    def test_garde_de_fin_de_pagination(self):
        gh = _PageGh()
        gh.full_forever = True
        with mock.patch.object(plan_github, "ISSUES_MAX_PAGES", 3):
            with self.assertRaises(plan_github.GithubError) as caught:
                plan_github._Repo(gh, REPO, False).issues()
        self.assertIn("more than 3 pages", str(caught.exception))
        self.assertEqual(len(gh.calls), 3)


class _ProjectionGh:
    """Un faux `gh` dont seule la liste des issues échoue (502)."""

    def __init__(self):
        self.fail = True
        self.calls: list = []

    def run(self, argv, stdin=None, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if joined == "api repos/%s" % REPO:
            return 0, json.dumps({"full_name": REPO, "visibility": "private"}), ""
        if "/issues?" in joined:
            if self.fail:
                return 1, "", "gh: Server Error (HTTP 502)"
            return 0, "", ""
        return 0, "", ""


class ProjectionAlertTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # état de panne par test : les fichiers ne débordent pas d'un test à
        # l'autre, comme le ferait un hôte neuf
        self.state_dir = tempfile.mkdtemp(prefix="8243-state-", dir=self.tmp)
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.cfg = dataclasses.replace(
            self.cfg, github=dict(SETTINGS), host="atelier", state_dir=self.state_dir)
        self.gh = _ProjectionGh()
        self.logs: list = []
        # pas de bail dans le processus de test : les cas qui l'exercent le
        # posent explicitement
        self.role_env = mock.patch.dict(os.environ, {"AMEESH_ROLE": ""})
        self.role_env.start()
        self.addCleanup(self.role_env.stop)

    def projector(self, now=1000.0):
        return plan_github.Projector(self.cfg, log=self.logs.append, gh=self.gh,
                                     clock=lambda: now)

    def tick(self, now, pilot="pilote"):
        """Une passe d'un processus neuf, comme la minuterie `--once`."""
        with mock.patch.object(plan_github, "project_pilot", return_value=pilot), \
                mock.patch("ameesh.mail.send") as send:
            self.projector(now).tick(self.db, now=now)
        return send

    def test_trois_pannes_consecutives_alertent_une_fois(self):
        for now in (1000.0, 1300.0):
            self.assertEqual(self.tick(now).call_count, 0)
        send = self.tick(1600.0)
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        self.assertEqual(args[2], "pilote")
        self.assertIn("3 passes", args[3])
        self.assertIn(REPO, args[3])
        self.assertTrue(kwargs["payload"]["urgent"])
        self.assertEqual(kwargs["payload"]["github_projection_failure"]["repo"], REPO)
        # la quatrième panne ne redemande pas l'alerte de la même panne
        self.assertEqual(self.tick(1900.0).call_count, 0)

    def test_un_succes_remet_le_compteur_a_zero(self):
        self.tick(1000.0)
        self.tick(1300.0)
        self.gh.fail = False
        self.tick(1600.0)                       # passe réussie : compteur à zéro
        self.gh.fail = True
        self.assertEqual(self.tick(1900.0).call_count, 0)
        self.assertEqual(self.tick(2200.0).call_count, 0)
        send = self.tick(2500.0)
        self.assertEqual(send.call_count, 1)
        self.assertIn("3 passes", send.call_args.args[3])
        # une nouvelle panne a sa propre clé d'idempotence
        self.assertIn("github-projection-failure:", send.call_args.kwargs["idempotency_key"])

    def test_sans_destinataire_le_journal_le_dit_et_rien_n_est_envoye(self):
        for now in (1000.0, 1300.0, 1600.0):
            self.assertEqual(self.tick(now, pilot=None).call_count, 0)
        self.assertTrue(any("no lease holder or project pilot" in line
                            for line in self.logs))

    def test_l_alerte_va_au_detenteur_du_bail(self):
        # `role run` pose AMEESH_ROLE : c'est le rôle qui détient le bail, donc
        # le destinataire, sans lire un nom de rôle en dur.
        with mock.patch.dict(os.environ, {"AMEESH_ROLE": "mesh-design"}), \
                mock.patch.object(plan_github, "project_pilot") as pilot:
            send = self.tick(1000.0)
            self.assertEqual(send.call_count, 0)
            send = self.tick(1300.0)
            self.assertEqual(send.call_count, 0)
            send = self.tick(1600.0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], "mesh-design")
        self.assertEqual(send.call_args.kwargs["payload"]
                         ["github_projection_failure"]["role"], "mesh-design")
        pilot.assert_not_called()
