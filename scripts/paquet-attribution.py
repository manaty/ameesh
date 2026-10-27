#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3931 — attribuer un test ciblé rouge de composition à une unité (même
lecture que `packet_red`, #3801).

    scripts/paquet-attribution.py --repo <worktree> [--slug owner/repo] \\
        [--state-dir DIR] < demande.json

La demande est lue sur l'entrée standard :

    {"units": [{"lot": 1, "unit_sha": "<sha>"}],
     "failures": [{"test": "tests.test_cluster.T.t", "module": "test_cluster"}]}

La réponse porte la projection `packet_red.attribute_tests` telle quelle :
pour chaque échec, l'unité désignée et ses preuves (fichier du test, support
importé, liste attendue, migration, module couvert), ou `unattributable`.
Aucune décision de retrait ici : le script ne modifie ni lot, ni branche, ni
base. `scripts/paquet-auto.py` applique le seuil de preuve forte et retire ;
sans attribution sûre, il garde son échec actuel.

La carte d'impact est lue **localement** (clone ou cache de l'hôte, sans
téléchargement) : un tic de composition ne dépend pas du réseau. Sans carte,
seules les preuves directes (fichier du test, support importé) sont nommées,
et le reste est `unattributable`. Aucune donnée de la base n'est lue.

Codes de retour : 0 relevé produit (même sans carte), 2 entrée invalide,
1 lecture impossible (le relevé n'est pas une attribution).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ameesh import packet_red  # noqa: E402

SCHEMA = "ameesh-paquet-attribution/1"
REQUEST_SCHEMA = "ameesh-paquet-attribution-request/1"
SLUG_RE = re.compile(r"(?:github\.com|gitlab\.com)[:/]([^/]+/[^/]+?)(?:\.git)?/?$")
DEFAULT_STATE = Path.home() / ".local" / "state" / "ameesh"


class InputError(RuntimeError):
    """La demande ou les arguments ne permettent pas de lire la composition."""


def _text(value) -> str:
    return str(value or "").strip()


def repository_slug(repo: Path) -> str:
    """`owner/repo` du remote `origin`, ou "" (la carte n'est alors pas lue)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    found = SLUG_RE.search(_text(proc.stdout))
    return found.group(1) if found else ""


def head_of(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{commit}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return _text(proc.stdout) if proc.returncode == 0 else ""


def members(payload) -> list[dict]:
    """Les unités de la demande, contrôlées (`lot` positif, `unit_sha` hex)."""
    if not isinstance(payload, dict):
        raise InputError("la demande doit être un objet JSON")
    units = payload.get("units")
    if not isinstance(units, list):
        raise InputError("la demande doit porter une liste `units`")
    out: list[dict] = []
    for entry in units:
        if not isinstance(entry, dict):
            raise InputError("unité illisible : %r" % (entry,))
        try:
            lot = int(entry["lot"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError("unité sans lot valide : %r" % (entry,)) from exc
        sha = _text(entry.get("unit_sha"))
        if lot <= 0 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise InputError("unité invalide (lot %r, sha %r)" % (lot, sha))
        out.append({"lot": lot, "unit_sha": sha})
    return out


def failures(payload) -> list[dict]:
    """Les échecs de la demande, au format attendu par `packet_red`."""
    raw = payload.get("failures")
    if not isinstance(raw, list):
        raise InputError("la demande doit porter une liste `failures`")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("échec illisible : %r" % (entry,))
        test = _text(entry.get("test"))
        module = _text(entry.get("module"))
        if not test and not module:
            raise InputError("échec sans test ni module : %r" % (entry,))
        out.append({"test": test, "module": module})
    return out


def read_card(repo: Path, slug: str, state_dir: Path):
    """La carte du clone ou du cache de l'hôte, sans réseau (`download=False`)."""
    cfg = types.SimpleNamespace(state_dir=str(state_dir))
    return packet_red.carte_for(
        cfg, slug, repo=str(repo), download=False)


def attribute(repo: Path, unit_rows: list[dict], rows: list[dict],
              slug: str, state_dir: Path) -> dict:
    """Le relevé complet, ou un statut qui dit pourquoi il ne conclut pas."""
    head = head_of(repo)
    units = packet_red.unit_files(unit_rows, checkout=str(repo))
    card, card_prov = read_card(repo, slug, state_dir)

    def source(module: str) -> str:
        return packet_red._source_text(str(repo), head, module, repo="")

    tests = packet_red.attribute_tests(
        rows, units, packet_red.card_index(card), sources=source)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "repo": str(repo),
        "head": head,
        "slug": slug,
        "card": card_prov,
        "units": units,
        "tests": tests,
        "unattributable": sum(1 for entry in tests
                              if entry.get("status") != "probable"),
        "why": "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Attribue un test ciblé rouge de composition (#3931).")
    parser.add_argument("--repo", default=".",
                        help="worktree composé à lire (défaut : .)")
    parser.add_argument("--slug", default="",
                        help="dépôt owner/repo de la carte (défaut : remote origin)")
    parser.add_argument("--state-dir", default=os.environ.get("AGENT_MESH_STATE")
                        or str(DEFAULT_STATE),
                        help="état ameesh, pour le cache de la carte")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        try:
            payload = json.load(sys.stdin)
        except ValueError as exc:
            raise InputError("demande JSON illisible : %s" % exc) from exc
        if not isinstance(payload, dict):
            raise InputError("la demande doit être un objet JSON")
        if payload.get("schema") not in (None, REQUEST_SCHEMA):
            raise InputError("schéma de demande inconnu : %r"
                             % payload.get("schema"))
        unit_rows = members(payload)
        rows = failures(payload)
        slug = _text(args.slug) or repository_slug(repo)
        out = attribute(repo, unit_rows, rows, slug, Path(args.state_dir))
    except InputError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "invalid",
                          "tests": [], "why": str(exc)}, ensure_ascii=False))
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - le tic garde son échec actuel
        print(json.dumps({"schema": SCHEMA, "status": "error",
                          "tests": [], "why": "%s: %s"
                          % (type(exc).__name__, str(exc)[:300])},
                         ensure_ascii=False))
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
