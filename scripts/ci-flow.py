#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lecture du flux des résultats de tests (L161b).

    scripts/ci-flow.py summary F.jsonl [F.jsonl…]   >> "$GITHUB_STEP_SUMMARY"
    scripts/ci-flow.py publish F.jsonl --name "tests (flux) psql 1/6" &   (L161c)
    scripts/ci-flow.py close F.jsonl --name "tests (flux) psql 1/6"      (L161c)
    scripts/ci-flow.py sweep [--sha TÊTE]                                (#856)

Le flux est écrit par `scripts/test-parts.py run --flow F.jsonl` : une
ligne JSON par événement, schéma `ameesh-test-event/1` (étude
docs/design/etudes/l161-tests-par-impact.md, section 3) :

* `debut` : une par part ; `pilote`, `modules` dans l'ordre d'exécution,
  `classes` (P0 module de test changé, P1 touché par un fichier changé, P3
  le reste), `estimee_s` ;
* `test` : une par test terminé ; `module`, `test` (nom pointé), `classe`,
  `raison` (le fichier changé qui l'a fait passer d'abord), `statut`
  (`succes`, `echec`, `erreur`, `saute`, `echec_attendu`,
  `succes_inattendu`), `duree_s`, et pour un échec `extrait` (40 lignes au
  plus) et `rejouer` (la commande qui le relance seul) ;
* `fin` : une par part ; `statut` (`succes`, `echec`, `interrompu`),
  `comptes` par statut, `duree_s` ;
* `impacted` (L161f, parts run from a plan only): once the P0 and P1
  modules of the part are done; `statut` (`vert`, `rouge`), `modules`,
  `rouges` (impacted modules that failed). When every part says `vert`, the
  impacted tests are green: the provisional green
  (`scripts/test-plan.py leaks`).

Chaque ligne porte aussi `v`, `schema`, `type`, `ts` (UTC) et le contexte
de la CI : `depot`, `tete` (commit poussé), `fusion` (commit testé),
`execution` (numéro d'exécution GitHub), `part`.

`summary` en fait le résumé Markdown du job. Une ligne illisible (fichier
coupé par l'arrêt du job) est comptée et ignorée ; un fichier absent donne
un résumé qui le dit, jamais une erreur : l'étape tourne `if: always()`.

L161c — `publish` suit le fichier pendant la part (en arrière-plan) et tient
le check run « tests (flux) <part> » de la tête poussée : chaque échec y
entre dès qu'il est écrit, en annotation (lisible dans la PR) et en ligne JSON
dans la sortie du check run, entre `MARKER` et la fin du bloc. Chaque ligne
publiée porte l'attempt de sa part (`GITHUB_RUN_ATTEMPT`, #901) : lors d'une
relance (`gh run rerun`), le check run de l'attempt précédent peut être encore
le dernier publié pendant que la nouvelle tentative est en file ; sans cette
marque, `ameesh notify` imputerait ses anciens échecs à la relance. C'est ce
que relève `ameesh notify` (`ameesh.ci`) pour l'envoyer à l'agent du lot. Il
s'arrête à la ligne `fin`. `close` (étape `if: always()`) le termine dans
tous les cas, et le crée s'il n'existe pas encore. Sans jeton, ou si GitHub
refuse (PR d'une fourche : jeton en lecture seule), rien n'est publié et
l'étape reste verte : la publication n'est jamais une cause d'échec.

#899 — le plafond du job reste borné (canon §3), mais la part qui grandit
avec la suite doit prévenir avant de l'atteindre : `publish --plafond-min N
--debut T` (le plafond du job, égal à son `timeout-minutes`, et son début en
epoch, écrit par l'étape « début du job ») publie **une fois** une alerte à
`ALERTE_SEUIL` (80 %) du plafond, en tête du titre et du résumé du check run
et en ligne JSON `type: alerte`. L'alerte ne change jamais la conclusion de
la part, et une valeur absente ou illisible ne publie rien. `summary` affiche
en plus, sur chaque part, la part du plafond consommée à la fin du job
(« Plafond du job : … sur N min (P %) »), avec un ⚠ au-delà du seuil : c'est
la mesure qui montre la croissance de la suite d'une exécution à l'autre.

#1044 — un nombre non fini (NaN, ±infini, ou un débordement de conversion)
n'est jamais un nombre : il est refusé comme illisible, avant tout calcul et
toute écriture. Et quand la part n'a pas de durée finale attestée (interrompue
sans ligne `fin`), `summary` donne la durée « au moment de l'alerte » et le
dit, au lieu de la présenter comme une durée de fin de part.

#856 — un runner arrêté (saturation #747) tue le publisher *et* l'étape
`close` : le check restait `in_progress` pour toujours, la PR ne pouvait plus
paraître verte. `sweep` est la réconciliation, indépendante du publisher : il
relit chaque check « tests (flux) » encore en cours et l'état terminal réel du
job et de l'exécution qui l'ont publié, puis conclut `cancelled` — jamais
`success`, faute de ligne `fin`. L'exécution se lit dans le `details_url`
quand il porte `/actions/runs/<id>` ; sinon dans le `check_suite` du check
(GitHub laisse l'URL par défaut `…/runs/<id_du_check>` sur les checks publiés
par un jeton d'exécution). Un check dont le job tourne encore, une autre
tentative ou une autre tête ne sont pas touchés. `publish` qui atteint son
arrêt de sûreté sans ligne `fin` laisse le check ouvert (le publisher vit
encore) : `close` ou `sweep` le conclura.

#1393 — `publish` pose à la création du check l'identité exacte de son job
(`external_id` = `ameesh-ci-flow/<exécution>/<tentative>`, lu dans les
variables GitHub) : le sweep la préfère au `details_url` et au `check_suite`,
dont le repli peut nommer *une autre* exécution de `tests.yml` sur la même
tête et faire conclure un check encore vivant. Une tentative nommée est relue
sur elle-même (`…/attempts/<n>`) : le check d'une tentative passée se conclut
sur l'état de *cette* tentative, pas sur celui de la tentative courante d'un
rerun. Le balayage est borné (`--max-duration`, défaut 100 s) et l'étape CI
l'entoure de `timeout 120` : sous le plafond de 5 min du job, un balayage lent
ne peut pas faire rougir le check requis « tests » (il ne change jamais son
verdict, et ne conclut jamais `success`).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

SCHEMA = "ameesh-test-event/1"
FAILURES = ("echec", "erreur")
#: tests listés au plus dans le résumé, par catégorie
LIMIT = 20
#: #899 : part du plafond du job où l'alerte est publiée (80 %)
ALERTE_SEUIL = 0.80
#: #899 : plafond du job (minutes, = timeout-minutes) et début du job (epoch)
ENV_PLAFOND = "AMEESH_PLAFOND_MIN"
ENV_DEBUT = "AMEESH_JOB_DEBUT"


def read(chemin: str | pathlib.Path) -> tuple[list[dict], int]:
    """Les événements d'un fichier de flux et le nombre de lignes illisibles."""
    events, illisibles = [], 0
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        if not ligne.strip():
            continue
        try:
            ev = json.loads(ligne)
        except json.JSONDecodeError:
            illisibles += 1
            continue
        if isinstance(ev, dict) and ev.get("v") == 1:
            events.append(ev)
        else:
            illisibles += 1
    return events, illisibles


def _instant(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(s: float) -> str:
    return f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60):02d} s"


def _nombre(valeur) -> float | None:
    """Un nombre fini d'option, d'environnement ou d'état, ou None s'il est
    absent, illisible ou non fini (#1044 : NaN, ±infini et débordement)."""
    try:
        nombre = float(valeur)
    except (TypeError, ValueError, OverflowError):
        return None
    return nombre if math.isfinite(nombre) else None


def _entier(valeur) -> int | None:
    """Un entier d'option ou d'environnement, ou None s'il est absent,
    illisible ou négatif (#1393 : numéro d'exécution, tentative)."""
    try:
        nombre = int(str(valeur).strip())
    except (TypeError, ValueError):
        return None
    return nombre if nombre > 0 else None


def _minutes(valeur) -> str:
    return "%g" % float(valeur)


def _horodatage(ts: float) -> str:
    """Un epoch en `ts` de flux (UTC, millisecondes), comme `test-parts.py`."""
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def alerte_plafond(debut, plafond_min, maintenant: float,
                   contexte: dict | None = None) -> dict | None:
    """#899 — l'alerte à `ALERTE_SEUIL` du plafond du job, ou None avant le seuil.

    `debut` (epoch, début du job) et `plafond_min` (le `timeout-minutes` du
    job) viennent du workflow ; absents ou illisibles, il n'y a pas d'alerte
    (jamais d'échec pour autant)."""
    debut, plafond = _nombre(debut), _nombre(plafond_min)
    if not debut or not plafond or plafond <= 0:
        return None
    ecoule = maintenant - debut
    if ecoule < ALERTE_SEUIL * plafond * 60.0:
        return None
    alerte = {"v": 1, "schema": SCHEMA, "type": "alerte", "seuil": ALERTE_SEUIL,
              "ecoule_s": round(ecoule, 1), "plafond_min": plafond,
              "ts": _horodatage(maintenant)}
    alerte.update(contexte or {})
    return alerte


def contexte_de(events: list[dict]) -> dict:
    """Le contexte de la part (depot, tete, execution, part), porté par l'alerte."""
    debut = next((e for e in events if e.get("type") == "debut"), {})
    return {cle: debut[cle] for cle in
            ("depot", "tete", "fusion", "execution", "part", "pilote") if debut.get(cle)}


def seuil_pourcent(alerte: dict) -> int:
    """Le seuil de l'alerte en pourcentage entier (80 par défaut)."""
    return round((_nombre(alerte.get("seuil")) or ALERTE_SEUIL) * 100)


def texte_alerte(alerte: dict) -> str:
    """Le texte lisible de l'alerte (résumé du check run, résumé du job)."""
    return ("⚠ %d %% du plafond du job atteint : %s écoulées sur %s min."
            % (seuil_pourcent(alerte),
               _duration(_nombre(alerte.get("ecoule_s")) or 0.0),
               _minutes(_nombre(alerte.get("plafond_min")) or 0)))


def etat_publication(chemin) -> dict:
    """L'état écrit par `publish` à côté du flux (`F.jsonl.check`), ou {}."""
    try:
        etat = json.loads(pathlib.Path(str(chemin) + ".check").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return etat if isinstance(etat, dict) else {}


def alerte_de(etat: dict) -> dict | None:
    """L'alerte de plafond portée par un état de publication, si c'en est une."""
    alerte = (etat or {}).get("alerte")
    return alerte if isinstance(alerte, dict) else None


def summary(chemins: list[str]) -> str:
    """Résumé Markdown d'un ou plusieurs fichiers de flux."""
    check_output: list[str] = []
    for chemin in chemins:
        if not pathlib.Path(chemin).is_file():
            check_output.append(f"### Flux des tests\n\nAucun flux : `{chemin}` absent "
                          "(la part n'a pas démarré).\n")
            continue
        events, illisibles = read(chemin)
        debut = next((e for e in events if e.get("type") == "debut"), {})
        fin = next((e for e in events if e.get("type") == "fin"), None)
        tests = [e for e in events if e.get("type") == "test"]
        comptes = collections.Counter(e.get("statut", "?") for e in tests)
        part = debut.get("part") or (tests[0].get("part") if tests else "") or chemin
        if fin is None:
            etat = "inachevée (pas de ligne de fin)"
        else:
            etat = {"succes": "verte", "echec": "rouge"}.get(fin.get("statut"), fin.get("statut"))
            etat += f", {_duration(fin.get('duree_s', 0))}"
        check_output.append(f"### Tests {part} : {etat}\n")
        check_output.append(", ".join(f"{n} {s}" for s, n in sorted(comptes.items())) or "aucun test")
        if illisibles:
            check_output[-1] += f" ; {illisibles} ligne(s) illisible(s)"
        check_output.append("")
        # #899 : la part du plafond consommée, mesure de la croissance de la suite
        publication = etat_publication(chemin)
        plafond = _nombre(publication.get("plafond_min"))
        alerte = alerte_de(publication)
        fin_ecoule = _nombre(publication.get("fin_ecoule_s"))
        ecoule_alerte = _nombre(alerte.get("ecoule_s")) if alerte else None
        if plafond and fin_ecoule is not None:
            part_du_plafond = fin_ecoule / (plafond * 60.0)
            ligne = ("%sPlafond du job : %s à la fin de la part sur %s min (%.0f %%)"
                     % ("⚠ " if alerte or part_du_plafond >= ALERTE_SEUIL else "",
                        _duration(fin_ecoule), _minutes(plafond), 100 * part_du_plafond))
            if alerte:
                ligne += " — alerte à %d %% publiée dans le check run." % seuil_pourcent(alerte)
            elif part_du_plafond >= ALERTE_SEUIL:
                ligne += " — seuil de %d %% atteint." % round(ALERTE_SEUIL * 100)
            else:
                ligne += "."
            check_output.append(ligne + "\n")
        elif plafond and ecoule_alerte is not None:
            # #1044 : sans `fin_ecoule_s`, la durée de l'alerte n'atteste pas la
            # fin de la part (interruption, annulation) : ne pas la présenter
            # comme telle.
            part_du_plafond = ecoule_alerte / (plafond * 60.0)
            check_output.append(
                "⚠ Plafond du job : %s écoulées au moment de l'alerte sur %s min (%.0f %%)"
                " — durée finale non attestée (part sans ligne de fin).\n"
                % (_duration(ecoule_alerte), _minutes(plafond), 100 * part_du_plafond))
        classes = debut.get("classes", {})
        touches = [m for m in debut.get("modules", []) if classes.get(m, "P3") != "P3"]
        impacted = next((e for e in events if e.get("type") == "impacted"), None)
        if impacted is not None:
            when = ""
            t0, t1 = _instant(debut.get("ts")), _instant(impacted.get("ts"))
            if t0 and t1:
                when = f" after {_duration((t1 - t0).total_seconds())}"
            check_output.append(f"Impacted modules (P0, P1): "
                          f"{'green' if impacted.get('statut') == 'vert' else 'red'}{when}\n")
        if touches:
            check_output.append("Passés d'abord (touchés par la PR) : "
                          + ", ".join(f"`{m}` ({classes[m]})" for m in touches) + "\n")
        echecs = [e for e in tests if e.get("statut") in FAILURES]
        if echecs:
            check_output.append("| statut | test | durée | rejouer |\n|---|---|---|---|")
            for e in echecs[:LIMIT]:
                rejouer = f"`{e['rejouer']}`" if e.get("rejouer") else ""
                check_output.append(f"| {e['statut']} | `{e.get('test', '?')}` | "
                              f"{e.get('duree_s', 0):.1f} s | {rejouer} |")
            if len(echecs) > LIMIT:
                check_output.append(f"\n… et {len(echecs) - LIMIT} autre(s).")
            check_output.append("")
            for e in echecs[:LIMIT]:
                if e.get("extrait"):
                    check_output.append(f"<details><summary><code>{e.get('test', '?')}</code></summary>"
                                  f"\n\n```\n{e['extrait']}\n```\n</details>\n")
        lents = sorted(tests, key=lambda e: -e.get("duree_s", 0))[:5]
        if lents:
            check_output.append("Tests les plus lents : " + ", ".join(
                f"`{e.get('test', '?').removeprefix('tests.')}` {e.get('duree_s', 0):.1f} s"
                for e in lents) + "\n")
    return "\n".join(check_output) + "\n"


# --------------------------------------------------------------------------
# L161c : check run « tests (flux) » tenu pendant la part
# --------------------------------------------------------------------------

#: début du bloc lu par `ameesh notify` (ameesh.ci.events_of)
MARKER = "<!-- ameesh-test-event/1 -->"
#: taille maximale de `output.text` d'un check run (GitHub : 65 535)
MAX_TEXT = 65000
#: échecs dont l'extrait est gardé entier, puis lignes gardées pour les suivants
FULL_EXCERPTS = 5
FOLLOWING_LINES = 10
#: annotations au plus par requête (limite de GitHub)
MAX_ANNOTATIONS = 50
CONCLUSIONS = {"succes": "success", "echec": "failure", "interrompu": "cancelled"}
#: préfixe des checks tenus par `publish` (#856 : le job porte le nom sans lui)
FLUX_PREFIX = "tests (flux) "
#: conclusion d'un check orphelin : le publisher est mort sans lire de ligne
#: `fin`, donc jamais `success` (#856)
ORPHAN_CONCLUSION = "cancelled"
#: #1393 : préfixe de l'`external_id` posé par `publish` à la création du
#: check — `ameesh-ci-flow/<exécution>/<tentative>`. Le sweep y lit l'identité
#: exacte du job publiant, sans se fier au repli `check_suite` (qui peut
#: nommer une autre exécution de `tests.yml` sur la même tête).
EXTERNAL_PREFIX = "ameesh-ci-flow/"
#: #1393 : durée maximale du balayage, en secondes. L'étape CI l'entoure de
#: `timeout 120` : sous le plafond de 5 min du job, un balayage lent (appels
#: HTTP de 20 s) ne peut pas faire expirer le job et rougir le check requis.
SWEEP_MAX_DURATION = 100.0


def _failure_line(ev: dict, lignes: int | None, attempt: int) -> str:
    ev = dict(ev)
    # #901 : la provenance (l'attempt qui a publié cette ligne) est écrite avec
    # l'événement ; le relevé enregistre chaque échec sous son attempt et
    # n'impute à une relance que les lignes de l'attempt courant.
    ev["attempt"] = int(attempt)
    if lignes == 0:
        ev.pop("extrait", None)
    elif lignes is not None and ev.get("extrait"):
        coupe = ev["extrait"].splitlines()
        if len(coupe) > lignes:
            ev["extrait"] = "\n".join(coupe[:lignes] + ["… (%d ligne(s) de plus)"
                                                        % (len(coupe) - lignes)])
    return json.dumps(ev, ensure_ascii=False)


def check_output(events: list[dict], attempt: int = 1,
                 alerte: dict | None = None) -> dict:
    """`output` du check run : titre, résumé, et le bloc des échecs.

    `attempt` est l'attempt GitHub de la part (défaut 1) : il est porté par
    chaque ligne d'échec publiée (#901).
    `alerte` (#899) : l'alerte de plafond, une fois publiée ; elle se voit dans
    le titre et le résumé, et voyage en ligne JSON `type: alerte` du bloc, que
    `ameesh.ci.events_of` ignore (seuls les échecs de test le concernent)."""
    attempt = int(attempt or 1)
    debut = next((e for e in events if e.get("type") == "debut"), {})
    fin = next((e for e in events if e.get("type") == "fin"), None)
    tests = [e for e in events if e.get("type") == "test"]
    echecs = [e for e in tests if e.get("statut") in FAILURES]
    attendus = sum(1 for _ in debut.get("modules", ())) if debut else 0
    etat = "en cours" if fin is None else {"succes": "verte", "echec": "rouge"}.get(
        fin.get("statut"), fin.get("statut") or "?")
    titre = "%d échec(s), %d test(s) passé(s), %s" % (len(echecs), len(tests), etat)
    if alerte:
        titre = "⚠ %d %% du plafond — %s" % (seuil_pourcent(alerte), titre)
    resume_ = ["Part %s : %s." % (debut.get("part") or "?", etat)]
    if alerte:
        resume_.append(texte_alerte(alerte))
    if attendus:
        resume_.append("%d module(s) dans cette part." % attendus)
    if echecs:
        resume_.append("")
        resume_ += ["- `%s` (%s)" % (e.get("test"), e.get("statut")) for e in echecs[:LIMIT]]
        if len(echecs) > LIMIT:
            resume_.append("- … et %d autre(s)" % (len(echecs) - LIMIT))
    lignes, taille, omis = [MARKER, "```jsonl"], len(MARKER) + 20, 0
    if alerte:
        ligne = json.dumps(alerte, ensure_ascii=False)
        lignes.append(ligne)
        taille += len(ligne) + 1
    for i, e in enumerate(echecs):
        for garde in ((None if i < FULL_EXCERPTS else FOLLOWING_LINES), 0):
            ligne = _failure_line(e, garde, attempt)
            if taille + len(ligne) + 1 < MAX_TEXT - 200:
                lignes.append(ligne)
                taille += len(ligne) + 1
                break
        else:
            omis += 1
    lignes.append("```")
    if omis:
        lignes.append("%d échec(s) de plus, hors de la limite de taille de GitHub." % omis)
    return {"title": titre[:255], "summary": "\n".join(resume_)[:60000],
            "text": "\n".join(lignes)}


def annotations(echecs: list[dict]) -> list[dict]:
    """Une annotation par échec, sur le fichier du module de test."""
    out = []
    for e in echecs:
        module = e.get("module") or ""
        message = (e.get("extrait") or e.get("statut") or "échec")[-60000:]
        if e.get("rejouer"):
            message += "\n\nrejouer : %s" % e["rejouer"]
        out.append({"path": "tests/%s.py" % module if module else "tests",
                    "start_line": 1, "end_line": 1, "annotation_level": "failure",
                    "title": str(e.get("test") or "?")[:255], "message": message})
    return out


class GitHub:
    """Les deux appels utiles de l'API des check runs, avec le jeton du job."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.tete = env.get("AMEESH_CI_TETE") or env.get("GITHUB_SHA", "")
        run = env.get("GITHUB_RUN_ID", "")
        # #901 : l'attempt de la part (relance `gh run rerun`), publié avec
        # chaque échec pour que le relevé ne relise pas l'attempt précédent.
        try:
            self.attempt = max(1, int(env.get("GITHUB_RUN_ATTEMPT") or 1))
        except (TypeError, ValueError):
            self.attempt = 1
        self.lien = ("%s/%s/actions/runs/%s" % (env.get("GITHUB_SERVER_URL", "https://github.com"),
                                                self.depot, run) if run else None)
        #: #1393 : l'exécution et la tentative du job qui publie — posées dans
        #: l'`external_id` du check pour que le sweep les retrouve exactement.
        self.execution = _entier(env.get("GITHUB_RUN_ID"))
        self.tentative = _entier(env.get("GITHUB_RUN_ATTEMPT"))

    @property
    def external_id(self) -> str:
        """L'identité du job publiant (`ameesh-ci-flow/<exécution>/<tentative>`),
        ou "" quand le numéro d'exécution manque (#1393)."""
        if self.execution is None:
            return ""
        return "%s%d/%d" % (EXTERNAL_PREFIX, self.execution, self.tentative or 1)

    def ready(self) -> bool:
        return bool(self.jeton and self.depot and self.tete)

    def _request(self, methode: str, chemin: str, corps: dict | None = None) -> dict:
        req = urllib.request.Request(
            "%s/repos/%s/%s" % (self.api, self.depot, chemin), method=methode,
            data=None if corps is None else json.dumps(corps).encode("utf-8"),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as rep:
            return json.loads(rep.read().decode("utf-8") or "{}")

    def call(self, methode: str, chemin: str, corps: dict) -> dict:
        return self._request(methode, chemin, corps)

    def read(self, chemin: str) -> dict:
        """Lecture (GET) : checks d'une tête, exécution, jobs d'une exécution."""
        return self._request("GET", chemin)

    def check_runs(self, tete: str) -> list[dict]:
        """Tous les checks d'une tête (paginés)."""
        checks, page = [], 1
        while True:
            rep = self.read("commits/%s/check-runs?per_page=100&page=%d" % (tete, page))
            page_checks = rep.get("check_runs") or []
            checks += page_checks
            if not page_checks or len(checks) >= int(rep.get("total_count") or 0):
                return checks
            page += 1

    def run(self, execution: int) -> dict:
        """L'exécution GitHub qui a publié un check (details_url ou check_suite)."""
        return self.read("actions/runs/%d" % execution)

    def runs_of_suite(self, suite: int) -> list[dict]:
        """Les exécutions d'une suite de checks (#856).

        GitHub laisse le `details_url` par défaut `…/runs/<id_du_check>` sur
        les checks publiés par un jeton d'exécution : il ne nomme alors pas
        l'exécution, mais le `check_suite` du check la retrouve exactement."""
        return self._listed("actions/runs?check_suite_id=%d" % suite,
                            "workflow_runs", maximum=1000)

    def _listed(self, path: str, key: str, *, maximum: int | None = None) -> list[dict]:
        """Une liste complète et stable, sinon une erreur (pas une liste vide)."""
        items, page, total = [], 1, None
        separator = "&" if "?" in path else "?"
        while True:
            response = self.read("%s%sper_page=100&page=%d" % (path, separator, page))
            page_items = response.get(key)
            count = response.get("total_count")
            if (not isinstance(page_items, list) or type(count) is not int
                    or count < 0 or (maximum is not None and count > maximum)
                    or (total is not None and count != total)
                    or any(not isinstance(item, dict) for item in page_items)):
                raise ValueError("liste %s incomplète" % key)
            total = count
            items.extend(page_items)
            if len(items) == total:
                return items
            if not page_items or len(items) > total:
                raise ValueError("page de %s manquante" % key)
            page += 1

    def jobs(self, execution: int) -> list[dict]:
        """Les jobs d'une exécution : leur état terminal fait foi (#856)."""
        return self._listed("actions/runs/%d/jobs" % execution, "jobs")

    def run_attempt(self, execution: int, tentative: int) -> dict:
        """L'état d'une tentative d'exécution (#1393) : c'est *cette* tentative
        qui fait foi quand le check la nomme, pas la tentative courante du run."""
        return self.read("actions/runs/%d/attempts/%d" % (execution, tentative))

    def jobs_attempt(self, execution: int, tentative: int) -> list[dict]:
        """Les jobs d'une tentative d'exécution (#1393) : le job homonyme d'une
        tentative passée ne dépend pas de l'état de la tentative courante."""
        return self._listed("actions/runs/%d/attempts/%d/jobs" % (execution, tentative),
                            "jobs")

    def create(self, nom: str, output: dict) -> int:
        # #5298 : `started_at` explicite en UTC. Sans lui, la forge date le
        # check de l'heure locale du runner ; la lecture `filter=latest` (et
        # `statusCheckRollup`) choisit alors le check de l'attempt précédent,
        # dont le `startedAt` est plus grand, et l'ancien rouge masque le vert
        # conclu par la relance.
        corps = {"name": nom, "head_sha": self.tete, "status": "in_progress",
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "output": output}
        if self.lien:
            corps["details_url"] = self.lien
        if self.external_id:
            # #1393 : l'identité exacte du job publiant, relue par le sweep.
            corps["external_id"] = self.external_id
        return int(self.call("POST", "check-runs", corps)["id"])

    def update(self, ident: int, output: dict, conclusion: str | None = None) -> None:
        corps: dict = {"output": output}
        if conclusion:
            corps.update(status="completed", conclusion=conclusion)
        self.call("PATCH", "check-runs/%d" % ident, corps)


class Publication:
    """L'état d'une publication, partagé entre `publish` et `close` par un
    fichier à côté du flux (`F.jsonl.check`)."""

    def __init__(self, flux: str, nom: str, gh: GitHub, log=None):
        self.flux, self.nom, self.gh = pathlib.Path(flux), nom, gh
        self.etat_chemin = pathlib.Path(str(flux) + ".check")
        self.log = log or (lambda texte: print(texte, file=sys.stderr, flush=True))
        try:
            self.etat = json.loads(self.etat_chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.etat = {}

    def _keep(self) -> None:
        """Écrit l'état par un temporaire propre à l'écrivain (#3336).

        `publish` (arrière-plan) et `close` (`if: always()`) écrivent tous deux
        l'état de la même part. Avec un nom fixe (`F.jsonl.tmp`), le renommage
        de l'un consommait le temporaire de l'autre et l'étape échouait en
        FileNotFoundError (main, run 40084134277) ; chaque écrivain garde donc
        son propre temporaire, renommé atomiquement dans le même dossier."""
        fd, tmp = tempfile.mkstemp(prefix=self.etat_chemin.name + ".", suffix=".tmp",
                                   dir=str(self.etat_chemin.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.etat))
            os.replace(tmp, self.etat_chemin)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def events(self) -> list[dict]:
        return read(self.flux)[0] if self.flux.is_file() else []

    def push(self, events: list[dict], conclusion: str | None = None,
             alerte: dict | None = None) -> bool:
        """Crée ou met à jour le check run ; False si GitHub refuse."""
        attempt = int(getattr(self.gh, "attempt", 1) or 1)
        output = check_output(events, attempt=attempt, alerte=alerte)
        echecs = [e for e in events if e.get("type") == "test"
                  and e.get("statut") in FAILURES]
        vus = set(self.etat.get("annotes") or ())
        nouveaux = [e for e in echecs if e.get("test") not in vus][:MAX_ANNOTATIONS]
        if nouveaux:
            output["annotations"] = annotations(nouveaux)
        try:
            if not self.etat.get("id"):
                self.etat["id"] = self.gh.create(self.nom, output)
                if conclusion:
                    self.gh.update(self.etat["id"],
                                   check_output(events, attempt=attempt, alerte=alerte),
                                   conclusion)
            else:
                self.gh.update(int(self.etat["id"]), output, conclusion)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            self.log("ci-flow : check run « %s » non publié (%s)" % (self.nom, exc))
            return False
        self.etat["annotes"] = sorted(vus | {e.get("test") for e in nouveaux})
        self.etat["echecs"] = len(echecs)
        if alerte:
            self.etat["alerte"] = alerte
        if conclusion:
            self.etat["conclusion"] = conclusion
        try:
            self._keep()
        except OSError as exc:
            # #3336 : le check run est publié ; un état non écrit ne rougit pas
            # la part (même contrat que le refus de GitHub), il est consigné.
            self.log("ci-flow : état « %s » non écrit (%s)" % (self.etat_chemin, exc))
        return True


def conclusion_of(events: list[dict], defaut: str = "cancelled") -> str | None:
    fin = next((e for e in events if e.get("type") == "fin"), None)
    return CONCLUSIONS.get(fin.get("statut"), "failure") if fin else defaut


def publish(args, gh: GitHub | None = None, dormir=time.sleep, horloge=time.monotonic,
            maintenant=time.time) -> int:
    """Suit le flux et tient le check run jusqu'à la ligne `fin`.

    #899 : avec le plafond du job (`--plafond-min`, défaut `AMEESH_PLAFOND_MIN`)
    et son début (`--debut`, défaut `AMEESH_JOB_DEBUT`), publie une fois
    l'alerte à `ALERTE_SEUIL` du plafond ; sans eux, rien ne change."""
    gh = gh or GitHub()
    if not gh.ready():
        print("ci-flow : pas de jeton, de dépôt ou de tête : rien n'est publié", file=sys.stderr)
        return 0
    plafond = _nombre(getattr(args, "plafond_min", None))
    debut = _nombre(getattr(args, "debut", None))
    if plafond is None:
        plafond = _nombre(os.environ.get(ENV_PLAFOND))
    if debut is None:
        debut = _nombre(os.environ.get(ENV_DEBUT))
    pub = Publication(args.file, args.name, gh)
    if plafond:
        pub.etat.setdefault("plafond_min", plafond)
    if debut:
        pub.etat.setdefault("debut", debut)
    alerte = alerte_de(pub.etat)
    limite = horloge() + args.max_duration
    if not pub.push([], alerte=alerte):
        return 0  # refus (jeton en lecture seule) : on n'insiste pas
    publies, dernier, alerte_publiee = (0, 0), horloge(), False
    while horloge() < limite:
        if pub.etat_chemin.with_suffix(".stop").exists():
            return 0
        events = pub.events()
        tests = [e for e in events if e.get("type") == "test"]
        echecs = sum(1 for e in tests if e.get("statut") in FAILURES)
        if alerte is None and debut and plafond:
            alerte = alerte_plafond(debut, plafond, maintenant(), contexte_de(events))
            if alerte:
                # le log est relu par l'étape `fermer` (cat) : l'annotation
                # apparaît dans le job, en plus du résumé du check run
                pub.log("::warning title=plafond du job à %d %%::%s"
                        % (seuil_pourcent(alerte), texte_alerte(alerte)))
        fin = conclusion_of(events, defaut=None)
        if fin:
            if debut:
                pub.etat["fin_ecoule_s"] = round(maintenant() - debut, 1)
            pub.push(events, fin, alerte=alerte)
            return 0
        # le check run change à chaque échec et à la première alerte de plafond ;
        # sinon, toutes les minutes au plus (avancement), pour ménager le quota
        if (alerte is not None and not alerte_publiee) or echecs != publies[0] \
                or (len(tests) != publies[1] and horloge() - dernier > 60):
            if pub.push(events, alerte=alerte):
                alerte_publiee = alerte_publiee or alerte is not None
                publies, dernier = (echecs, len(tests)), horloge()
        dormir(args.interval)
    # arrêt de sûreté sans ligne `fin` : le publisher vit encore, le check
    # reste ouvert — `close` (job vivant) ou `sweep` (job terminal) conclura #856
    print("ci-flow : arrêt de sûreté après %.0f s sans ligne fin — check « %s » "
          "laissé ouvert" % (args.max_duration, args.name), file=sys.stderr)
    return 0


def close(args, gh: GitHub | None = None) -> int:
    """Termine le check run (le crée au besoin) avec le bilan de la part."""
    gh = gh or GitHub()
    if not gh.ready():
        return 0
    pub = Publication(args.file, args.name, gh)
    try:
        pub.etat_chemin.with_suffix(".stop").touch()
    except OSError:
        pass
    if pub.etat.get("conclusion"):
        return 0
    events = pub.events()
    pub.push(events, conclusion_of(events), alerte=alerte_de(pub.etat))
    return 0


def _execution_of(check: dict) -> int | None:
    """Le numéro d'exécution lu dans le `details_url`, quand il le nomme.

    Les checks publiés par un jeton d'exécution portent l'URL par défaut
    `…/runs/<id_du_check>`, qui ne nomme pas l'exécution : c'est alors le
    `check_suite` qui la retrouve (`_executions_of`)."""
    trouve = re.search(r"/actions/runs/(\d+)", check.get("details_url") or "")
    return int(trouve.group(1)) if trouve else None


def _suite_of(check: dict) -> int | None:
    """Le `check_suite` du check : l'exécution s'y retrouve par l'API (#856)."""
    ident = (check.get("check_suite") or {}).get("id")
    try:
        return int(ident) if ident is not None else None
    except (TypeError, ValueError):
        return None


def _external_of(check: dict) -> tuple[int, int | None] | None:
    """L'exécution et la tentative lues dans l'`external_id` du check (#1393),
    ou None quand il est absent ou d'une autre forme : le sweep retombe alors
    sur le `details_url` puis le `check_suite`, sans jamais exiger cette
    identité des checks publiés avant #1393."""
    texte = str(check.get("external_id") or "")
    if not texte.startswith(EXTERNAL_PREFIX):
        return None
    morceaux = texte[len(EXTERNAL_PREFIX):].split("/")
    if len(morceaux) != 2:
        return None
    execution, tentative = _entier(morceaux[0]), _entier(morceaux[1])
    if execution is None or tentative is None:
        return None
    return (execution, tentative)


def _executions_of(check: dict, gh: GitHub,
                   suites: dict[int, list[int]]) -> list[tuple[int, int | None]]:
    """Les exécutions (et tentatives) qui ont pu publier le check (#856).

    #1393 : d'abord l'`external_id` du check — l'exécution et la tentative
    exactes, sans ambiguïté ; c'est lui qui empêche le repli `check_suite` de
    nommer une autre exécution de `tests.yml` sur la même tête et de conclure
    un check vivant. Sinon celle du `details_url` quand il porte
    `/actions/runs/<id>` ; sinon celles de son `check_suite`, dont la
    terminalité reste exigée de toutes les candidates. Une liste vide veut dire
    « non identifiable », jamais « orphelin » : sans exécution prouvée
    terminale, on ne conclut pas."""
    identite = _external_of(check)
    if identite is not None:
        return [identite]
    execution = _execution_of(check)
    if execution is not None:
        return [(execution, None)]
    suite = _suite_of(check)
    if suite is None:
        return []
    if suite not in suites:
        suites[suite] = [int(run["id"]) for run in gh.runs_of_suite(suite)]
    return [(run_id, None) for run_id in suites[suite]]


def orphaned(check: dict, jobs: list[dict], execution_status: str) -> bool:
    """Le check « tests (flux) » est-il orphelin ? (#856)

    Il l'est quand il est encore `in_progress` et que le job qui l'a publié est
    terminal : le publisher est mort avec le runner (saturation #747) — ou il a
    expiré sans lire de ligne `fin` — et l'étape `close` n'a pas tourné. Le job
    est retrouvé par son nom (`tests (flux) X` pour le job `X`) ; s'il a
    disparu, l'exécution terminale fait foi. Un job encore actif — y compris
    celui de la tentative courante d'un rerun du même run, qui porte le même
    nom — et une autre tête ne sont jamais touchés."""
    if check.get("status") != "in_progress":
        return False
    nom = check.get("name") or ""
    if not nom.startswith(FLUX_PREFIX):
        return False
    job = next((j for j in jobs if j.get("name") == nom[len(FLUX_PREFIX):]), None)
    if job is not None:
        return job.get("status") == "completed"
    return execution_status == "completed"


def _proved_terminal(check: dict, executions: list[tuple[int, int | None]], gh: GitHub,
                     etats: dict[tuple[int, int | None],
                                 tuple[dict, list[dict]] | None]) -> bool:
    """Le publisher est-il mort dans *toutes* les exécutions candidates ? (#856)

    Le job homonyme doit être terminal dans chaque candidate, même si d'autres
    jobs de l'exécution tournent encore. Si ce job est absent d'une liste lue
    complètement, seule une exécution terminale fait foi. Une réponse illisible
    ou une liste incomplète ne permet jamais de conclure.

    #1393 : une candidate qui nomme une tentative est relue sur *elle*
    (`…/attempts/<n>`) — l'état de la tentative courante d'un rerun ne décide
    pas du check d'une tentative passée, et réciproquement."""
    for identite in executions:
        if identite not in etats:
            execution, tentative = identite
            try:
                if tentative is None:
                    run, jobs = gh.run(execution), gh.jobs(execution)
                else:
                    run, jobs = (gh.run_attempt(execution, tentative),
                                 gh.jobs_attempt(execution, tentative))
                if (not isinstance(run, dict) or not isinstance(run.get("status"), str)
                        or not run["status"] or not isinstance(jobs, list)
                        or any(not isinstance(job, dict) for job in jobs)):
                    raise ValueError("réponse d'exécution ou de jobs illisible")
                etats[identite] = (run, jobs)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                print("ci-flow : exécution %d%s illisible (%s)"
                      % (execution, " tentative %d" % tentative if tentative else "", exc),
                      file=sys.stderr)
                etats[identite] = None
        etat = etats[identite]
        if etat is None or not orphaned(check, etat[1], etat[0].get("status", "")):
            return False
    return True


def sweep(args, gh: GitHub | None = None, horloge=time.monotonic) -> int:
    """Conclut les checks « tests (flux) » laissés en cours par un job terminal.

    Réconciliation indépendante du publisher (#856) : chaque check encore en
    cours est jugé sur l'état terminal réel du job et de l'exécution qui l'ont
    publié (retrouvée par son `external_id` #1393, sinon par son `details_url`
    ou, à défaut, par son `check_suite`). Sans ligne `fin`, la conclusion est
    `cancelled`, jamais `success`. Ne ferme aucun check dont l'exécution est
    encore active, ni ceux d'une autre tête. Ne fait jamais échouer le job : une
    erreur d'API est tracée et la fonction rend 0.

    #1393 : le balayage est borné (`--max-duration`) — un check non atteint
    reste en cours et sera vu au prochain passage, jamais conclu à l'aveugle."""
    try:
        return _sweep(args, gh, horloge)
    except Exception as exc:  # la réconciliation n'échoue jamais le job
        print("ci-flow : réconciliation interrompue (%s)" % exc, file=sys.stderr)
        return 0


def _sweep(args, gh: GitHub | None = None, horloge=time.monotonic) -> int:
    gh = gh or GitHub()
    if not gh.ready():
        print("ci-flow : pas de jeton, de dépôt ou de tête : rien à réconcilier",
              file=sys.stderr)
        return 0
    budget = _nombre(getattr(args, "max_duration", None))
    budget = SWEEP_MAX_DURATION if budget is None else budget
    limite = horloge() + budget if budget > 0 else horloge()
    tete = getattr(args, "sha", "") or gh.tete
    try:
        checks = gh.check_runs(tete)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print("ci-flow : checks de %s illisibles (%s)" % (tete[:8], exc), file=sys.stderr)
        return 0
    etats: dict[tuple[int, int | None], tuple[dict, list[dict]] | None] = {}
    suites: dict[int, list[int]] = {}
    for check in checks:
        if horloge() >= limite:
            print("ci-flow : balayage arrêté au budget de %.0f s ; les checks restants "
                  "seront vus au prochain passage" % budget, file=sys.stderr)
            break
        nom = check.get("name") or ""
        if check.get("status") != "in_progress" or not nom.startswith(FLUX_PREFIX):
            continue
        try:
            executions = _executions_of(check, gh, suites)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print("ci-flow : exécution du check %s illisible (%s)"
                  % (check.get("id"), exc), file=sys.stderr)
            continue
        if not executions:
            # publié hors CI, ni details_url ni check_suite exploitables : on
            # ne juge pas (#856)
            print("ci-flow : check %s « %s » sans exécution identifiable : non conclu"
                  % (check.get("id"), nom), file=sys.stderr)
            continue
        if not _proved_terminal(check, executions, gh, etats):
            continue
        sortie = check.get("output") or {}
        resume = (sortie.get("summary") or "").rstrip()
        sortie = {
            "title": sortie.get("title") or "tests (flux) : check réconcilié (#856)",
            "summary": (resume + "\n\nCheck conclu par réconciliation (#856) : le job "
                        "d'origine est terminal et aucune ligne `fin` n'a été lue — "
                        "conclusion `cancelled`, jamais `success`.").strip(),
            "text": sortie.get("text") or "",
        }
        try:
            gh.update(int(check["id"]), sortie, ORPHAN_CONCLUSION)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print("ci-flow : check %s non conclu (%s)" % (check.get("id"), exc),
                  file=sys.stderr)
            continue
        print("ci-flow : check %s « %s » conclu %s — job terminal sans ligne fin"
              % (check.get("id"), nom, ORPHAN_CONCLUSION), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    p = sous.add_parser("summary", help="résumé Markdown du job")
    p.add_argument("files", nargs="+")
    for nom, aide in (("publish", "tient le check run pendant la part (arrière-plan)"),
                      ("close", "termine le check run (étape if: always())")):
        p = sous.add_parser(nom, help=aide)
        p.add_argument("file")
        p.add_argument("--name", required=True, help="nom du check run")
        if nom == "publish":
            p.add_argument("--interval", type=float, default=5.0,
                           help="secondes entre deux lectures du flux (défaut 5)")
            p.add_argument("--max-duration", type=float, default=3600.0,
                           help="arrêt de sûreté (secondes, défaut 3600)")
            p.add_argument("--plafond-min", type=_nombre, default=None,
                           help="plafond du job en minutes (= timeout-minutes ; "
                                "défaut : %s)" % ENV_PLAFOND)
            p.add_argument("--debut", type=_nombre, default=None,
                           help="début du job, epoch en secondes (défaut : %s)" % ENV_DEBUT)
    p = sous.add_parser("sweep", help="conclut les checks « tests (flux) » laissés "
                         "en cours par un job terminal (#856)")
    p.add_argument("--sha", default="",
                   help="tête à réconcilier (défaut : AMEESH_CI_TETE ou GITHUB_SHA)")
    p.add_argument("--max-duration", type=_nombre, default=SWEEP_MAX_DURATION,
                   help="durée maximale du balayage en secondes (défaut %g) ; "
                        "l'étape CI l'entoure de `timeout 120`" % SWEEP_MAX_DURATION)
    args = ap.parse_args(argv)
    if args.commande == "publish":
        return publish(args)
    if args.commande == "close":
        return close(args)
    if args.commande == "sweep":
        return sweep(args)
    sys.stdout.write(summary(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
