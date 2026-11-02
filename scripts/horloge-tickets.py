#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5427 : les tickets d'admission d'un rouge de la passe horloge/fuseau.

Un rouge de `.github/workflows/horloge.yml` est un défaut de test dépendant de
l'environnement (canon §3), pas un incident de production. Le workflow ne peut
pas ouvrir un lot ameesh (aucune base sur le runner, cf. PILOT-ci-vm) ; il
ouvre un **ticket** dont le titre est le nom du test et le corps le scénario
(horloge simulée ou fuseau), le run et la preuve. La veille GitHub du projet
lit les nouveaux tickets et les convertit en lot dans l'heure, `issue_ref`
compris (canon, demandes externes). Un ticket par test, jamais de doublon :
un test déjà rouge garde son ticket ouvert.

Usage :
  scripts/horloge-tickets.py RAPPORT.json [--repo OWNER/NAME] [--dry-run]
Sortie : 0 les tickets sont ouverts (ou déjà là), 2 rapport illisible.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

#: le préfixe stable qui identifie un ticket d'admission de cette passe
TITLE_PREFIX = "test dépendant de l'environnement : "
#: au-delà, le titre n'est plus un titre GitHub utile (limite 256)
MAX_TITLE = 240
#: le corps ne cite jamais un secret ; le run est public
BODY = """\
La passe horloge/fuseau (#5427) a rougi ce test hors de la porte de fusion.

- test : `{test}`
- scénario(s) : {scenarios}
- run : {run}
- tête : `{sha}`

Canon §3 : un test dépendant de l'environnement est un défaut **du test**,
corrigé à la source (horloge injectée, fixture déterministe, valeur fixée) ;
il ne se relance pas. Ce ticket est l'admission d'un lot « test dépendant de
l'environnement » : la veille GitHub le convertit en lot avec son `issue_ref`.

Scénario(s) et extraits :

{details}
"""


class TicketError(RuntimeError):
    """Un rapport illisible ou un `gh` en échec."""


def collect(payload: dict) -> dict:
    """`{test: [scénario, …]}` depuis le rapport de la passe, sans doublon.

    Un même test qui rougit sous deux scénarios reste un seul ticket : le
    corps les nomme tous, pour ne pas multiplier les lots."""
    out: dict[str, list[dict]] = {}
    for scenario in payload.get("scenarios") or ():
        for failure in scenario.get("failures") or ():
            if not isinstance(failure, str):
                continue
            test = failure.strip()
            if not test:
                continue
            out.setdefault(test, []).append(scenario)
    return out


def title_for(test: str) -> str:
    """Le titre stable d'un test ; au-delà de la limite, le milieu est coupé
    mais le suffixe (module.classe.test, unique) reste lisible."""
    title = TITLE_PREFIX + test
    if len(title) <= MAX_TITLE:
        return title
    room = MAX_TITLE - len(TITLE_PREFIX)
    keep = max(1, room // 2)
    tail = max(1, room - keep - 1)
    return TITLE_PREFIX + test[:keep] + "…" + test[-tail:]


def scenarios_text(scenarios: list[dict]) -> str:
    return ", ".join("%s (TZ=%s)" % (scenario.get("name"), scenario.get("tz"))
                     for scenario in scenarios)


def details_text(scenarios: list[dict]) -> str:
    blocks = []
    for scenario in scenarios:
        fake = scenario.get("fake_at")
        when = ("horloge simulée à %s" % fake) if fake else "horloge réelle"
        tail = (scenario.get("output_tail") or "").strip()
        blocks.append("- **%s** : %s, %s\n\n```\n%s\n```"
                      % (scenario.get("name"), when,
                         scenario.get("why") or "", tail[-2000:]))
    return "\n\n".join(blocks)


def body_for(test: str, scenarios: list[dict], *, run: str, sha: str) -> str:
    return BODY.format(test=test, scenarios=scenarios_text(scenarios),
                       run=run, sha=sha, details=details_text(scenarios))


def _gh(args: list[str], *, input_text: str | None = None) -> str:
    try:
        proc = subprocess.run(["gh", *args], input=input_text,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TicketError("gh %s : %s" % (" ".join(args[:3]), exc)) from exc
    if proc.returncode != 0:
        raise TicketError("gh %s : %s" % (" ".join(args[:3]),
                                          (proc.stderr or proc.stdout).strip()[:300]))
    return proc.stdout


def existing(dry_run: bool, repo: str) -> dict[str, int]:
    """Les tickets ouverts de la passe, par titre exact.

    La liste est lue sans recherche plein texte (dont la syntaxe peut changer
    ou rendre zéro) puis filtrée sur le titre exact ; une lecture en échec
    lève : mieux vaut ne pas ouvrir que dupliquer un ticket déjà ouvert."""
    if dry_run:
        return {}
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--limit", "2000", "--json", "number,title"])
    rows = json.loads(raw or "[]")
    if not isinstance(rows, list):
        raise TicketError("liste des tickets illisible")
    return {row["title"]: int(row["number"]) for row in rows
            if isinstance(row, dict) and row.get("title") and row.get("number")}


def open_tickets(payload: dict, *, repo: str, run: str, sha: str,
                 dry_run: bool = False, say=print) -> list[dict]:
    """Ouvre un ticket par test rouge, sauf s'il existe déjà ; rend les gestes."""
    actions = []
    try:
        known = existing(dry_run, repo)
    except (TicketError, ValueError) as exc:
        say("liste des tickets illisible, aucun ticket ouvert : %s" % exc,
            file=sys.stderr)
        return [{"action": "failed", "why": str(exc)}]
    for test, scenarios in sorted(collect(payload).items()):
        title = title_for(test)
        if title in known:
            actions.append({"test": test, "action": "existing",
                            "issue": known[title]})
            say("déjà ouvert : #%d %s" % (known[title], title))
            continue
        body = body_for(test, scenarios, run=run, sha=sha)
        if dry_run:
            say("DRY %s\n%s" % (title, body))
            actions.append({"test": test, "action": "would-open", "title": title})
            continue
        try:
            url = _gh(["issue", "create", "--repo", repo, "--title", title,
                       "--body-file", "-"], input_text=body).strip()
        except TicketError as exc:
            # le rouge de la passe reste le verdict ; l'admission est signalée
            say("ticket non ouvert pour %s : %s" % (test, exc), file=sys.stderr)
            actions.append({"test": test, "action": "failed", "why": str(exc)})
            continue
        say("ouvert : %s" % url)
        actions.append({"test": test, "action": "opened", "url": url})
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="horloge-tickets.py",
        description="tickets d'admission des rouges de la passe horloge (#5427)")
    parser.add_argument("report", help="rapport JSON de scripts/test-horloge-passe.py")
    parser.add_argument("--repo", default="",
                        help="dépôt GitHub (défaut : GITHUB_REPOSITORY)")
    parser.add_argument("--dry-run", action="store_true",
                        help="montrer les tickets sans les ouvrir")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(pathlib.Path(args.report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("rapport illisible : %s" % exc, file=sys.stderr)
        return 2
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or ""
    if not repo:
        print("dépôt GitHub inconnu : --repo ou GITHUB_REPOSITORY", file=sys.stderr)
        return 2
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    run = "%s/%s/actions/runs/%s" % (server, repo, run_id)
    sha = os.environ.get("GITHUB_SHA") or "inconnu"
    open_tickets(payload, repo=repo, run=run, sha=sha, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
