#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3251 — l'agrégat requis `tests` distingue une annulation interne d'un échec.

Décision owner #128512 (signée le 23/10/2026, option a) : « Agrégat requis
jamais conclu = preuve absente, donc bloquant […]. Les non-concluants à
l'intérieur d'un agrégat qui a conclu restent non bloquants (§3). »

Avant ce lot, l'étape « bilan des parts » exigeait `success` de chaque job
interne : une annulation de capacité (job hébergé jamais pris, runner perdu)
rougissait le check requis `tests`, donc le paquet et la release, alors que
le canon §3 range une panne d'infra dans les non-concluants. #940 interdit
la correction inverse — un `tests` SUCCESS muet avec une part annulée : le
check requis ne doit jamais verdir sans le dire.

Ce script sépare donc les deux :

* un **échec** (`failure`, ou une part sautée qui devait tourner) est un
  négatif : l'agrégat conclut rouge (code retour 1) ;
* une **annulation interne** (`cancelled`, `timed_out`, `startup_failure`,
  `stale`, `action_required`) est un non concluant : l'agrégat conclut vert
  — il a conclu — mais publie le check de détail `tests (bilan des parts)`
  en `neutral`, avec la part et son état. `fire` et `radar` le consignent
  (lot de correction #3209) sans retenir le paquet ni la release. Le détail
  est aussi écrit en annotation `::warning::`, jamais muet.

Le check de détail est publié à **chaque** exécution (vert quand toutes les
parts ont conclu) : une tentative ultérieure sur la même tête remplace ainsi
l'ancien `neutral` au lieu de laisser une consignation périmée
(`radar.latest_rollup` garde le dernier essai par nom).

L'agrégat lui-même ne publie jamais `success` sans ce détail : si le check
ne peut pas être publié alors qu'une annulation doit être consignée (jeton
en lecture seule d'une PR de fourche, API en panne), l'étape échoue (code
retour 2), et le check requis reste rouge. Une annulation ne devient donc
jamais un vert silencieux.

Usage (étape « bilan des parts » de .github/workflows/tests.yml) :

    scripts/ci-agregat.py --event pull_request --plan success \
        --version success --version-only false --docs-only false \
        --doc-readers skipped \
        --psycopg success --psycopg-parts '[1, 2]' \
        --psql cancelled --psql-parts '[1]' \
        --plateforme-macos success --autre-fs skipped --publish

Sans `--publish` (tests locaux), la classification seule est rendue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

#: nom du check de détail, partagé avec `ameesh.radar.AGGREGATE_DETAIL_CHECK`
#: (un témoin de tests/test_3251_agregat_annulation.py verrouille l'égalité).
DETAIL_CHECK = "tests (bilan des parts)"

#: conclusions de job qui ne prouvent rien (canon §3 : non concluant)
NON_CONCLUSIVE = ("cancelled", "timed_out", "startup_failure", "stale",
                  "action_required")

#: état d'une part
GREEN = "green"
RED = "red"
INCONCLUSIVE = "inconclusive"


class AgregatError(RuntimeError):
    """Lecture impossible ou publication refusée : l'agrégat ne se suppose
    pas vert."""


def _state(value) -> str:
    """La conclusion d'un job, normalisée (casse libre, valeur absente)."""
    return str(value or "").strip().lower()


def _flag(value) -> bool:
    """Un booléen de `needs.plan.outputs` (`true`/`false`, casse libre)."""
    return str(value or "").strip().lower() == "true"


def _json_list(text) -> list | None:
    """La liste de parts d'un plan (`[1, 2]`), ou None si elle est illisible."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def classify(*, event, plan, version, version_only, docs_only, doc_readers,
             psycopg, psycopg_parts, psql, psql_parts,
             platform_macos=None, fs_class=None) -> dict:
    """Classe l'agrégat sans réseau : `{state, red, consigned, parts}`.

    `state` vaut `green` quand l'agrégat a conclu (même avec des annulations
    internes), `red` sur un négatif. `red` liste les manques prouvés,
    `consigned` les parts non concluantes (dans l'ordre du workflow), `parts`
    le détail de chaque part exigée (pour le check publié)."""
    red: list[str] = []
    consigned: list[dict] = []
    parts: list[dict] = []

    def add(name: str, result, parts_text: str, *, skipped_ok: bool) -> None:
        value = _state(result)
        row = {"part": name, "result": value or "absent", "parts": parts_text}
        if value == "success":
            row["status"] = GREEN
        elif value in NON_CONCLUSIVE:
            row["status"] = INCONCLUSIVE
            consigned.append(row)
        elif value == "skipped" and skipped_ok:
            # un saut prévu par le plan n'a rien à consigner
            row["status"] = GREEN
        else:
            row["status"] = RED
            detail = value or "absent"
            if value == "skipped" and parts_text:
                detail = "skipped, parts %s" % parts_text
            red.append("%s (%s)" % (name, detail))
        parts.append(row)

    event = _state(event)
    plan_state = _state(plan)
    if plan_state != "success":
        red.append("plan (%s)" % (plan_state or "absent"))
    version_state = _state(version)
    if event == "pull_request":
        if version_state != "success":
            red.append("version (%s)" % (version_state or "absent"))
    elif version_state not in ("success", "skipped"):
        red.append("version (%s)" % (version_state or "absent"))
    if event == "pull_request":
        # #1343 : une demande de gel ne prend jamais les voies version seule
        # (L157) ou documentation seule (L161a) — un `tests` vert sans tests.
        if _flag(version_only):
            red.append("version_only (voie interdite sur une demande de gel)")
        if _flag(docs_only):
            red.append("docs_only (voie interdite sur une demande de gel)")

    # L157 : une PR qui ne change que le numéro de version a déjà été validée
    # sur main ; aucune part ne tourne. L161a : ses lecteurs déclarés seulement.
    if _flag(version_only) and not red:
        return {"state": GREEN, "red": [], "consigned": [], "parts": []}
    if _flag(docs_only):
        readers = _state(doc_readers)
        if readers != "success":
            red.append("doc-readers (%s)" % (readers or "absent"))
        if not red:
            return {"state": GREEN, "red": [], "consigned": [], "parts": []}

    add("psycopg", psycopg, str(psycopg_parts or "").strip(), skipped_ok=False)
    # psql n'a pas de part quand le plan n'en a sélectionné aucune : c'est le
    # seul saut légitime. Une liste absente ou illisible ne l'est jamais.
    add("psql", psql, str(psql_parts or "").strip(),
        skipped_ok=(_json_list(psql_parts) == []))
    if platform_macos is not None:
        # L68b (#3026) : le job court macOS entre dans les needs de `tests` ;
        # son annulation est une annulation interne comme les autres.
        add("plateforme macOS", platform_macos, "", skipped_ok=False)
    if fs_class is not None:
        # #748 : la part « autre système de fichiers » ne tourne qu'à la
        # release (ou à la main) ; sautée ailleurs, elle n'a rien à prouver.
        add("autre système de fichiers", fs_class, "", skipped_ok=True)

    return {"state": RED if red else GREEN, "red": red,
            "consigned": consigned, "parts": parts}


def _summary(event: str, parts: list[dict], result: dict) -> str:
    lines = ["Agrégat requis **tests** — événement : %s." % (event or "?")]

    def display(row: dict) -> str:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        return "- %s : %s%s" % (row["part"], row["result"], suffix)

    lines += [display(row) for row in parts]
    if result["consigned"]:
        lines.append("")
        lines.append("Non concluant (non bloquant, canon §3 ; lot de correction "
                     "ouvert par fire/radar) : %s."
                     % ", ".join(row["part"] for row in result["consigned"]))
    if result["red"]:
        lines.append("")
        lines.append("Négatif prouvé (bloquant) : %s." % ", ".join(result["red"]))
    return "\n".join(lines)


class GitHub:
    """La publication du check de détail, avec le jeton du job (aucun `gh`)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.tete = env.get("AMEESH_CI_TETE") or env.get("GITHUB_SHA", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.execution = env.get("GITHUB_RUN_ID", "")
        self.tentative = env.get("GITHUB_RUN_ATTEMPT", "")
        self.serveur = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")

    def ready(self) -> bool:
        return bool(self.jeton and self.depot and self.tete)

    def create(self, name: str, conclusion: str, title: str, summary: str) -> int:
        """Publie le check de détail sur la tête poussée ; l'identifiant rendu."""
        if not self.ready():
            raise AgregatError("no token, repository or head: the detail check "
                              "cannot be published")
        payload = {"name": name, "head_sha": self.tete, "status": "completed",
                   "conclusion": conclusion,
                   "output": {"title": title[:255], "summary": summary[:60000]}}
        if self.execution:
            payload["details_url"] = "%s/%s/actions/runs/%s" % (
                self.serveur, self.depot, self.execution)
            payload["external_id"] = "ameesh-ci-agregat/%s/%s" % (
                self.execution, self.tentative or "1")
        request = urllib.request.Request(
            "%s/repos/%s/check-runs" % (self.api, self.depot),
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AgregatError("check-runs: HTTP %d %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AgregatError("check-runs: %s" % exc) from exc
        if not isinstance(body, dict) or not isinstance(body.get("id"), int):
            raise AgregatError("check-runs: unreadable response")
        return body["id"]


def _parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="bilan des parts de l'agrégat requis « tests » (#3251)")
    parser.add_argument("--event", default="")
    parser.add_argument("--plan", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--version-only", default="false")
    parser.add_argument("--docs-only", default="false")
    parser.add_argument("--doc-readers", default="")
    parser.add_argument("--psycopg", default="")
    parser.add_argument("--psycopg-parts", default="")
    parser.add_argument("--psql", default="")
    parser.add_argument("--psql-parts", default="")
    parser.add_argument("--plateforme-macos", dest="platform_macos", default=None)
    parser.add_argument("--autre-fs", dest="fs_class", default=None)
    parser.add_argument("--publish", action="store_true",
                        help="publier le check de détail sur la tête poussée")
    return parser.parse_args(argv)


def main(argv=None, gh=None) -> int:
    args = _parse(argv)
    try:
        result = classify(
            event=args.event, plan=args.plan, version=args.version,
            version_only=args.version_only, docs_only=args.docs_only,
            doc_readers=args.doc_readers,
            psycopg=args.psycopg, psycopg_parts=args.psycopg_parts,
            psql=args.psql, psql_parts=args.psql_parts,
            platform_macos=args.platform_macos, fs_class=args.fs_class)
    except (TypeError, ValueError) as exc:  # pragma: no cover - garde-fou
        print("ci-agregat : %s" % exc, file=sys.stderr)
        return 2

    for row in result["parts"]:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        print("%-24s: %s%s" % (row["part"], row["result"], suffix))
    for gap in result["red"]:
        print("::error title=tests : négatif prouvé::%s" % gap)
    for row in result["consigned"]:
        print("::warning title=tests : part non concluante::%s a été %s%s ; "
              "consignée, non bloquante (canon §3, décision #128512)."
              % (row["part"], row["result"],
                 " (parts %s)" % row["parts"] if row.get("parts") else ""))

    conclusion = ("failure" if result["state"] == RED
                  else "neutral" if result["consigned"] else "success")
    if result["state"] == RED:
        title = "tests : négatif prouvé"
    elif result["consigned"]:
        title = "tests : %d part(s) non concluante(s)" % len(result["consigned"])
    else:
        title = "tests : toutes les parts ont conclu"
    summary = _summary(args.event, result["parts"], result)

    if args.publish:
        try:
            (gh or GitHub()).create(DETAIL_CHECK, conclusion, title, summary)
        except AgregatError as exc:
            print("ci-agregat : %s" % exc, file=sys.stderr)
            # Une annulation qui ne peut pas être consignée ne verdit jamais :
            # ce serait rouvrir #940 (un `tests` SUCCESS muet). Un vert complet
            # reste vert (une PR de fourche n'a pas `checks: write`).
            if result["consigned"] or result["state"] == RED:
                return 2
            print("::warning title=tests : détail non publié::%s" % exc)
    return 0 if result["state"] == GREEN else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
