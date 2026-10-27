# SPDX-License-Identifier: AGPL-3.0-only
"""#3838 — la carte ne se mesure jamais sur une PR de paquet.

Constat : la PR #2923 (h-32) portait #3754 (`scripts/test-parts.py` et les
durées). Le déclencheur `pull_request` de carte.yml (`paths:` scripts/map*.py,
scripts/test-parts.py, tests/support.py, carte.yml) a lancé deux fois la carte
sur le paquet — runs 40417160764 (26/10 23:1xZ) et 40424552119 (27/10 00:09Z).
Ses 18 jobs de mesure tenaient 18 des 24 runners auto-hébergés, et la ci-full
du même paquet (40425070099) avait 18 jobs en file ; le pilote a annulé la
carte à la main les deux fois.

La règle du 24/10 (#3343) — pas de mesure contre la ci-full d'un paquet — était
contournée par le déclencheur PR dès qu'un paquet contenait un lot de mesure.
Le lot d'origine a déjà été mesuré sur sa propre PR et la carte quotidienne de
03:30Z couvre le reste : une PR dont la branche de tête est
`integration/paquet-*` ne lance plus aucun job de carte. GitHub ne filtre
`on.pull_request` que sur la branche de base, pas sur la tête : la garde est
une condition de job (`github.head_ref`), répétée sur les jobs `carte` et
`menage-mesures` qui portent `always()`.

Sans base : lecture du workflow, puis évaluation des conditions de job et de
leurs `needs` pour les événements réels (PR de paquet, PR de lot, planification,
dispatch), comme GitHub les évaluerait.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")

#: les jobs sont à deux espaces, sous la clé `jobs:` ; s'arrêter à `on:` évite
#: de prendre `schedule:`/`pull_request:` pour des jobs.
JOBS = WORKFLOW.split("\njobs:\n", 1)[1]
JOB = re.compile(r"(?ms)^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:\s*$|\Z)")
IF = re.compile(r"(?m)^    if:\s*(.+?)\s*$")
NEEDS = re.compile(r"(?m)^    needs:\s*(.+?)\s*$")
NEED_RESULT = re.compile(r"needs\.([a-z0-9-]+)\.result")


def jobs() -> dict[str, str]:
    """Le corps de chaque job, dans l'ordre du fichier (dépendances d'abord)."""
    return {m.group(1): m.group(2) for m in JOB.finditer(JOBS)}


def condition(nom: str) -> str:
    m = IF.search(jobs()[nom])
    return m.group(1) if m else "success()"


def needs(nom: str) -> list[str]:
    m = NEEDS.search(jobs()[nom])
    if m is None:
        return []
    brut = m.group(1).strip()
    if brut.startswith("["):
        return [x.strip().strip("'\"") for x in brut[1:-1].split(",") if x.strip()]
    return [brut.strip("'\"")]


def holds(nom: str, *, event_name: str, head_ref: str = "",
          repo_full_name: str = "manaty/ameesh",
          need_results: dict[str, str] | None = None) -> bool:
    """Évalue la condition du job comme GitHub, sans base ni code tiers.

    Les contextes utilisés par carte.yml sont remplacés par des variables
    locales ; `startsWith`, `always`, `cancelled`, `success` et `failure` sont
    les fonctions GitHub correspondantes. Un job sans `if:` a la condition
    implicite `success()`.
    """
    need_results = dict(need_results or {})
    expr = condition(nom).strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    expr = NEED_RESULT.sub(
        lambda m: repr(need_results.get(m.group(1), "success")), expr)
    expr = expr.replace("github.event.pull_request.head.repo.full_name",
                        "repo_full_name")
    expr = expr.replace("github.repository", "repo_full_name")
    expr = expr.replace("github.event.pull_request.head.ref", "head_ref")
    expr = expr.replace("github.head_ref", "head_ref")
    expr = expr.replace("github.event_name", "event_name")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    context = {
        "event_name": event_name,
        "head_ref": head_ref,
        "repo_full_name": repo_full_name,
        "always": lambda: True,
        "cancelled": lambda: False,
        "success": lambda: all(r == "success" for r in need_results.values()),
        "failure": lambda: any(r == "failure" for r in need_results.values()),
        "startsWith": lambda texte, prefixe: str(texte).startswith(prefixe),
    }
    return bool(eval(expr, {"__builtins__": {}}, context))  # noqa: S307 — workflow du dépôt


def results(*, event_name: str, head_ref: str = "",
            repo_full_name: str = "manaty/ameesh") -> dict[str, str]:
    """Le résultat de chaque job : `success` s'il tourne, `skipped` sinon.

    Les jobs sont parcourus dans l'ordre du fichier : une dépendance est
    toujours évaluée avant ses dépendants (`jobs()` suit le YAML).
    """
    resultats: dict[str, str] = {}
    for nom in jobs():
        dependances = {dep: resultats.get(dep, "success") for dep in needs(nom)}
        if holds(nom, event_name=event_name, head_ref=head_ref,
                 repo_full_name=repo_full_name, need_results=dependances):
            resultats[nom] = "success"
        else:
            resultats[nom] = "skipped"
    return resultats


class CarteHorsPaquetTest(unittest.TestCase):

    def setUp(self):
        self.jobs = jobs()
        for nom in ("plan-mesure", "mesure", "carte", "menage-mesures"):
            self.assertIn(nom, self.jobs, f"job {nom} introuvable dans carte.yml")

    def test_une_pr_de_paquet_ne_lance_aucun_job_de_carte(self):
        # les deux têtes réelles de #2923 (h-32) et la forme générale.
        for tete in ("integration/paquet-h-20261027-0003",
                     "integration/paquet-84-nexlink"):
            with self.subTest(tete=tete):
                resultats = results(event_name="pull_request", head_ref=tete)
                self.assertEqual(
                    resultats, {nom: "skipped" for nom in self.jobs},
                    "aucun job de carte ne doit tourner sur une PR de paquet")

    def test_la_pr_d_un_lot_de_mesure_est_toujours_mesuree(self):
        # le lot d'origine (#3754) et un lot ordinaire : la mesure de contrôle
        # de sa propre PR reste due.
        for tete in ("lot/3754-mesure-durees", "lot/3838-carte-hors-paquet"):
            with self.subTest(tete=tete):
                resultats = results(event_name="pull_request", head_ref=tete)
                self.assertEqual(
                    resultats, {nom: "success" for nom in self.jobs},
                    "la PR d'un lot de mesure doit garder sa carte de contrôle")

    def test_les_evenements_hors_pr_ne_sont_pas_touches(self):
        for evenement in ("schedule", "workflow_dispatch"):
            with self.subTest(evenement=evenement):
                resultats = results(event_name=evenement)
                self.assertEqual(
                    resultats, {nom: "success" for nom in self.jobs},
                    f"{evenement} garde la mesure")

    def test_le_prefixe_ne_deborde_pas(self):
        # `integration/paquet` (sans tiret) n'est pas une branche de paquet :
        # la mesure de contrôle y reste due.
        resultats = results(event_name="pull_request",
                            head_ref="integration/paquet")
        self.assertEqual(resultats, {nom: "success" for nom in self.jobs})

    def test_un_fork_garde_ses_gardes_de_fork(self):
        # la garde #3838 ne remplace pas celle des fourches : une PR de paquet
        # reste sautée, une PR de fork hors paquet garde sa mesure hébergée.
        paquet = results(event_name="pull_request",
                         head_ref="integration/paquet-h-20261027-0003",
                         repo_full_name="autre/fork")
        self.assertEqual(paquet, {nom: "skipped" for nom in self.jobs})
        fork = results(event_name="pull_request", head_ref="lot/1-x",
                       repo_full_name="autre/fork")
        self.assertEqual(fork, {nom: "success" for nom in self.jobs})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
