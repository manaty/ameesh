#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3449 — l'agrégat requis `tests` n'accorde pas un vert sans les parts Linux.

Décision owner #128512 (signée le 23/10/2026, option a) : « Agrégat requis
jamais conclu = preuve absente, donc bloquant […]. Les non-concluants à
l'intérieur d'un agrégat qui a conclu restent non bloquants (§3). »

#3251 avait lu cette décision comme « au moins une part réussie suffit » : un
run annulé où seuls la part macOS (2 min) et l'agrégat concluaient a publié
`tests` SUCCESS alors que les 8 psycopg et les 16 psql étaient annulés (run
40144843565, paquet h-13, fusionné sans suite Linux). #3449 borne la
tolérance : les familles **Linux planifiées** sont la preuve de la suite ;
une famille sans **aucune part réussie** est une preuve absente, tandis
qu'une part perdue parmi d'autres réussies reste consignée (15/16).

Ce script sépare donc trois cas :

* un **échec** (`failure`, ou une part sautée qui devait tourner) est un
  négatif : l'agrégat conclut rouge (code retour 1) ;
* une **famille Linux planifiée** (`psycopg`, `psql` avec au moins une part)
  qui n'a **aucune part réussie** (toutes annulées, absentes ou le job
  sauté) est une **preuve absente** : l'agrégat conclut rouge (code retour 1)
  et le détail la nomme. C'est le cas du run annulé de h-13 : macOS vert,
  0/8 psycopg et 0/16 psql réussies ;
* une **annulation partielle** d'une famille Linux (15 parts sur 16
  réussies, une annulée) reste un non concluant : l'agrégat conclut vert, le
  détail `tests (bilan des parts)` passe en `neutral` et nomme le compte
  `15/16`. `needs.psycopg.result` seul ne suffit pas — une matrice vaut
  `cancelled` dès qu'une part l'est —, les jobs du run sont donc lus par
  l'API (`actions: read`) ; ce cas ne durcit pas #128512 pour une part
  perdue (B1 de la relecture claude sur #3449) ;
* une **annulation d'une part non requise** (macOS, autre système de
  fichiers) reste un non concluant : l'agrégat conclut vert s'il garde la
  preuve Linux, et publie le check de détail en `neutral`, avec la part et
  son état. `fire` et `radar` le consignent (lot de correction #3209) sans
  retenir le paquet ni la release — canon §3. Le détail est aussi écrit en
  annotation `::warning::`, jamais muet. Si **aucune** part due n'a réussi
  (toutes annulées ou sautées), c'est l'absence de toute preuve : l'agrégat
  rougit (décision #128512, #940).
* un **run annulé** ne conclut pas : l'étape `if: cancelled()` du workflow
  échoue explicitement avant le bilan, et une famille Linux sans part
  réussie est vue par la classification (`needs.*.result` et jobs du run),
  pas seulement par cette garde — un job `always()` démarré après
  l'annulation du run ne voit pas toujours `cancelled()` (#3449, run
  40144843565).

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

    scripts/ci-agregat.py --event pull_request --run-id 123 --plan success \
        --version success --version-only false --docs-only false \
        --doc-readers skipped \
        --psycopg success --psycopg-parts '[1, 2]' \
        --psql success --psql-parts '[1]' \
        --plateforme-macos cancelled --autre-fs skipped --publish

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
MISSING = "missing"
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


def _family_jobs(jobs, family: str) -> list:
    """Les jobs d'une famille (`psycopg 1/8`, `psql 1/16`) parmi les jobs lus.

    Appelée par `paquet.yml`, `tests.yml` est un workflow réutilisable : l'API
    préfixe le nom de chaque job appelé par celui du job appelant
    (`suite complète du paquet / psycopg 1/8`). On lit donc la feuille après
    le dernier ` / `, sans dépendre du préfixe."""
    prefix = family + " "
    return [job for job in (jobs or []) if isinstance(job, dict)
            and str(job.get("name", "")).rsplit(" / ", 1)[-1].startswith(prefix)]


def _job_stats(jobs, expected):
    """`(réussies, total)` d'une famille, ou None si ses jobs sont illisibles.

    `jobs` est la liste des jobs du run (`name`, `status`, `conclusion`),
    `expected` le nombre de parts du plan (les jobs jamais démarrés ne sont
    pas toujours listés). Une liste vide est une lecture valide : zéro part
    réussie."""
    if jobs is None:
        return None
    rows = [job for job in jobs if isinstance(job, dict)]
    successes = sum(1 for job in rows if _state(job.get("conclusion")) == "success")
    total = max(int(expected or 0), len(rows))
    return successes, total


def classify(*, event, plan, version, version_only, docs_only, doc_readers,
             psycopg, psycopg_parts, psql, psql_parts,
             platform_macos=None, fs_class=None,
             psycopg_jobs=None, psql_jobs=None) -> dict:
    """Classe l'agrégat sans réseau : `{state, red, missing, consigned, parts}`.

    `state` vaut `green` quand la preuve Linux est acquise (chaque famille
    planifiée garde au moins une part réussie), `red` sur un négatif ou une
    preuve absente. `red` liste les négatifs prouvés, `missing` les familles
    Linux planifiées sans aucune part réussie (et l'absence de toute part
    réussie), `consigned` les annulations partielles et les parts non
    requises non concluantes (dans l'ordre du workflow), `parts` le détail
    de chaque part exigée (pour le check publié).

    `psycopg_jobs` / `psql_jobs` sont les jobs du run lus par l'API : une
    matrice `cancelled` avec 15 parts réussies sur 16 est consignée, pas
    bloquante ; sans jobs lisibles, une famille annulée reste une preuve
    absente (jamais un vert sur une lecture manquante)."""
    red: list[str] = []
    missing: list[str] = []
    consigned: list[dict] = []
    parts: list[dict] = []

    def add(name: str, result, parts_text: str, *, skipped_ok: bool,
            required: bool, jobs=None, expected=None) -> None:
        value = _state(result)
        row = {"part": name, "result": value or "absent", "parts": parts_text}
        if value == "success":
            row["status"] = GREEN
        elif value in NON_CONCLUSIVE:
            stats = _job_stats(jobs, expected) if required else None
            if not required:
                row["status"] = INCONCLUSIVE
                consigned.append(row)
            elif stats is not None and stats[0] > 0:
                # #3449/B1 : une part perdue dans une famille qui en garde
                # 15/16 reste une annulation partielle non bloquante.
                row["status"] = INCONCLUSIVE
                row["parts_ok"] = "%d/%d" % stats
                consigned.append(row)
            else:
                # Aucune part réussie (ou jobs illisibles) : preuve absente.
                row["status"] = MISSING
                if stats is not None:
                    missing.append("%s (%s, %d/%d part(s) réussie(s), preuve "
                                   "absente)" % (name, value, stats[0], stats[1]))
                else:
                    missing.append("%s (%s, preuve absente)" % (name, value))
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
        return {"state": GREEN, "red": [], "missing": [], "consigned": [],
                "parts": []}
    if _flag(docs_only):
        readers = _state(doc_readers)
        if readers != "success":
            red.append("doc-readers (%s)" % (readers or "absent"))
        if not red:
            return {"state": GREEN, "red": [], "missing": [], "consigned": [],
                    "parts": []}

    # #3449 : psycopg est toujours planifiée (les voies version/doc sont
    # sorties plus haut) ; un job sauté, absent ou sans part réussie y est un
    # manque. psql n'est requise que si le plan sélectionne des parts : une
    # liste explicitement vide (`[]`) est son seul saut légitime.
    psycopg_expected = len(_json_list(psycopg_parts) or [])
    psql_expected = len(_json_list(psql_parts) or [])
    add("psycopg", psycopg, str(psycopg_parts or "").strip(),
        skipped_ok=False, required=True,
        jobs=psycopg_jobs, expected=psycopg_expected)
    add("psql", psql, str(psql_parts or "").strip(),
        skipped_ok=(_json_list(psql_parts) == []),
        required=(_json_list(psql_parts) != []),
        jobs=psql_jobs, expected=psql_expected)
    if platform_macos is not None:
        # L68b (#3026) : le job court macOS entre dans les needs de `tests` ;
        # son annulation reste une annulation interne : la preuve Linux suffit
        # (#3449).
        add("plateforme macOS", platform_macos, "", skipped_ok=False,
            required=False)
    if fs_class is not None:
        # #748 : la part « autre système de fichiers » ne tourne qu'à la
        # release (ou à la main) ; sautée ailleurs, elle n'a rien à prouver.
        add("autre système de fichiers", fs_class, "", skipped_ok=True,
            required=False)

    # Décision #128512 : « agrégat requis jamais conclu = preuve absente, donc
    # bloquant ». #3449 : une famille Linux sans part réussie est déjà une
    # preuve absente ci-dessus ; ici on ferme le cas où **aucune** part due
    # n'a réussi (toutes annulées ou sautées), y compris si seules des parts
    # non requises ont conclu — #940 reste fermé (jamais de tests SUCCESS
    # sans test).
    if parts and not any(row["result"] == "success" for row in parts):
        missing.append("aucune part n'a conclu (preuve absente)")

    return {"state": RED if red or missing else GREEN, "red": red,
            "missing": missing, "consigned": consigned, "parts": parts}


def _summary(event: str, parts: list[dict], result: dict) -> str:
    lines = ["Agrégat requis **tests** — événement : %s." % (event or "?")]

    def display(row: dict) -> str:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        if row.get("parts_ok"):
            suffix += " — %s part(s) réussie(s)" % row["parts_ok"]
        return "- %s : %s%s" % (row["part"], row["result"], suffix)

    lines += [display(row) for row in parts]
    if result["consigned"]:
        lines.append("")
        lines.append("Non concluant (non bloquant, canon §3 ; lot de correction "
                     "ouvert par fire/radar) : %s."
                     % ", ".join(row["part"] for row in result["consigned"]))
    if result["missing"]:
        lines.append("")
        lines.append("Preuve absente (bloquant, décision #128512, #3449) : %s."
                     % ", ".join(result["missing"]))
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

    def jobs(self, run_id) -> list[dict]:
        """Les jobs du run (parts comprises), lus par l'API Actions.

        `actions: read` est accordé au job `tests` ; une lecture impossible ou
        tronquée lève `AgregatError` — le classifieur retombe alors sur une
        preuve absente pour une famille annulée, jamais un vert muet."""
        if not self.ready():
            raise AgregatError("no token, repository or head: the run jobs "
                              "cannot be read")
        if not str(run_id).isdigit() or int(run_id) <= 0:
            raise AgregatError("run jobs: invalid run id %r" % (run_id,))
        request = urllib.request.Request(
            "%s/repos/%s/actions/runs/%s/jobs?per_page=100&filter=latest"
            % (self.api, self.depot, int(run_id)),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AgregatError("run jobs: HTTP %d %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AgregatError("run jobs: %s" % exc) from exc
        if (not isinstance(body, dict)
                or type(body.get("total_count")) is not int
                or not isinstance(body.get("jobs"), list)
                or body["total_count"] != len(body["jobs"])
                or body["total_count"] >= 100):
            raise AgregatError("run jobs: malformed, truncated or wrong response")
        rows = []
        for job in body["jobs"]:
            if (not isinstance(job, dict) or not isinstance(job.get("name"), str)
                    or not job["name"]):
                raise AgregatError("run jobs: invalid job")
            rows.append({"name": job["name"],
                         "status": job.get("status") or "",
                         "conclusion": job.get("conclusion") or ""})
        return rows

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
        description="bilan des parts de l'agrégat requis « tests » "
                    "(#3251, #3449)")
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
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""),
                        help="run dont lire les jobs (défaut : GITHUB_RUN_ID)")
    parser.add_argument("--publish", action="store_true",
                        help="publier le check de détail sur la tête poussée")
    return parser.parse_args(argv)


def main(argv=None, gh=None, jobs=None) -> int:
    args = _parse(argv)
    if jobs is None and args.run_id:
        try:
            jobs = (gh or GitHub()).jobs(args.run_id)
        except AgregatError as exc:
            print("ci-agregat : jobs du run illisibles : %s" % exc, file=sys.stderr)
    psycopg_jobs = _family_jobs(jobs, "psycopg") if jobs is not None else None
    psql_jobs = _family_jobs(jobs, "psql") if jobs is not None else None
    try:
        result = classify(
            event=args.event, plan=args.plan, version=args.version,
            version_only=args.version_only, docs_only=args.docs_only,
            doc_readers=args.doc_readers,
            psycopg=args.psycopg, psycopg_parts=args.psycopg_parts,
            psql=args.psql, psql_parts=args.psql_parts,
            platform_macos=args.platform_macos, fs_class=args.fs_class,
            psycopg_jobs=psycopg_jobs, psql_jobs=psql_jobs)
    except (TypeError, ValueError) as exc:  # pragma: no cover - garde-fou
        print("ci-agregat : %s" % exc, file=sys.stderr)
        return 2

    for row in result["parts"]:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        print("%-24s: %s%s" % (row["part"], row["result"], suffix))
    for gap in result["missing"]:
        print("::error title=tests : preuve absente::%s" % gap)
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
        title = ("tests : preuve absente" if result["missing"]
                 else "tests : négatif prouvé")
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
