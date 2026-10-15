#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Contrôle d'inclusion d'un paquet §12, avant la CI complète et le feu (#1644).

    scripts/paquet-inclusion.py --manifest reviews/2026-10-15-paquet-23h.md \
        --head <sha|ref> --expected-file admissions-23h.txt
    scripts/paquet-inclusion.py --manifest … --head … --expected 1124,1056 --json

Le constat (qa-01, 15/10) : des lots admis ou routés vers la coupure ont été
omis à la composition, sans note de retrait ni revert — cinq au paquet 11h,
puis huit, puis une neuvième. Le manifeste seul ne le dit pas ; l'ascendance
non plus, puisqu'un revert laisse la tête du lot ancêtre de la tête du paquet
(#1638). Ce contrôle confronte donc **trois sources** :

* la liste des lots **attendus** à la coupure (admissions et routages du
  pilote) : `--expected`, `--expected-file`, ou une section « admissions » du
  manifeste (titre contenant « admission ») ;
* les lignes de **composition du manifeste** (table dont l'en-tête porte une
  colonne « lot » et une colonne « commit »), et les **retraits** consignés
  (lignes de la table de composition dont la note dit retrait/revert/report,
  tables dont l'en-tête dit « retiré/revert », mentions en prose sous un titre
  « Retrait »/« Report ») ;
* le **contenu réel de l'arbre** à la tête du paquet : pour chaque entrée de
  composition, les lignes ajoutées par son commit (diff `commit^..commit`,
  premier parent) doivent encore être présentes à la tête, fichier par
  fichier. Un contenu absent ou partiel n'est pas une preuve d'omission — une
  adaptation d'union peut réécrire une ligne — mais c'est un signal à lire
  **avant** le feu.

Verdicts d'une entrée de composition : `présent` (toutes les lignes ajoutées
sont là, ou le blob est identique), `partiel` (une partie seulement), `absent`
(aucune ligne, fichier disparu, ou suppression annulée), `vide` (aucun delta :
remise fraîche déjà portée par une autre entrée). Une ligne de retrait est
consignée `retrait_consigné` : son effet se lit sur l'entrée de composition du
lot (contenu absent), et un contenu qui réapparaît pour un lot pourtant retiré
donne un avertissement.

Sortie : un rapport lisible, ou `--json` (schéma `ameesh-paquet-inclusion/1`).
Code de retour : 0 si aucun défaut, 1 si une omission ou un contenu manquant
n'est expliqué par aucune note de retrait/report, 2 si l'entrée est invalide.
Le script est en lecture seule : aucun réseau, aucune écriture dans le dépôt.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "ameesh-paquet-inclusion/1"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
ID_RE = re.compile(r"#(\d+)")
SHA_RE = re.compile(r"`?([0-9a-f]{7,40})`?")
EXPLAIN_RE = re.compile(r"retir|revert|report|exclu", re.IGNORECASE)
NEGATIVE_RE = re.compile(
    r"(sans|aucun|aucune|pas de|jamais)\s+(note\s+de\s+)?(retrait|revert|report|exclusion)",
    re.IGNORECASE,
)
EXCLUSION_ROW_RE = re.compile(r"retrait|retir[ée]e?s?|revert|report", re.IGNORECASE)
ADMISSION_HEADING_RE = re.compile(r"admission", re.IGNORECASE)
RETRAIT_HEADING_RE = re.compile(r"retrait|report|exclusion", re.IGNORECASE)


class GitError(RuntimeError):
    """Une commande git de lecture a échoué."""


class InputError(RuntimeError):
    """L'entrée du contrôle est invalide (manifeste ou tête introuvable)."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} : {detail}")
    return proc


def rev_parse(repo: Path, rev: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", "replace").strip() or None


def commit_subject(repo: Path, rev: str) -> str:
    proc = git(repo, "log", "-1", "--format=%h %s", rev, check=False)
    return proc.stdout.decode("utf-8", "replace").strip()


def blob_id(repo: Path, rev: str, path: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}:{path}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", "replace").strip() or None


def blob_lines(repo: Path, rev: str, path: str) -> set[str] | None:
    """Les lignes du fichier à cette révision, ou None s'il est absent/binaire."""
    proc = git(repo, "show", f"{rev}:{path}", check=False)
    if proc.returncode != 0:
        return None
    data = proc.stdout
    if b"\0" in data[:8000]:
        return None
    return set(data.decode("utf-8", "replace").splitlines())


# --------------------------------------------------------------------------
# Manifeste
# --------------------------------------------------------------------------


@dataclass
class Entry:
    """Une ligne de la table de composition."""

    row: str
    line_no: int
    lots: list[int]
    source: str | None
    commit: str | None
    note: str
    retrait: bool

    def label(self) -> str:
        lots = "/".join(f"#{lot}" for lot in self.lots) or "?"
        return f"{lots} (ligne {self.line_no})"


@dataclass
class Manifest:
    path: Path
    entries: list[Entry] = field(default_factory=list)
    explained: dict[int, str] = field(default_factory=dict)
    weak: dict[int, str] = field(default_factory=dict)
    admissions: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def sha_in(cell: str) -> str | None:
    match = SHA_RE.search(cell)
    return match.group(1) if match else None


def iter_tables(lines: list[str]):
    """(titre courant, [(numéro de ligne, texte)]) pour chaque table markdown."""
    heading = ""
    index = 0
    while index < len(lines):
        match = HEADING_RE.match(lines[index])
        if match:
            heading = match.group(2)
        if TABLE_ROW_RE.match(lines[index]) and index + 1 < len(lines) \
                and TABLE_SEP_RE.match(lines[index + 1]):
            block: list[tuple[int, str]] = []
            cursor = index
            while cursor < len(lines) and TABLE_ROW_RE.match(lines[cursor]):
                block.append((cursor + 1, lines[cursor]))
                cursor += 1
            yield heading, block
            index = cursor
            continue
        index += 1


def parse_manifest(path: Path) -> Manifest:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    manifest = Manifest(path=path)
    composed_found = False
    seen_lines: set[int] = set()

    for heading, block in iter_tables(lines):
        header = [cell.lower() for cell in split_row(block[0][1])]
        is_retrait = any("retir" in cell or "revert" in cell for cell in header)
        is_compo = (
            not is_retrait
            and any("lot" in cell for cell in header)
            and any("commit" in cell for cell in header)
        )
        if is_compo:
            composed_found = True
            for line_no, line in block[2:]:
                cells = split_row(line)
                if len(cells) < 4:
                    manifest.warnings.append(
                        f"ligne {line_no} : table de composition illisible, ignorée"
                    )
                    continue
                lots = [int(found) for found in ID_RE.findall(cells[1])]
                note = " | ".join(cells[4:]).strip()
                entry = Entry(
                    row=cells[0],
                    line_no=line_no,
                    lots=lots,
                    source=sha_in(cells[2]),
                    commit=sha_in(cells[3]),
                    note=note,
                    retrait=bool(EXCLUSION_ROW_RE.search(note)),
                )
                if not entry.lots:
                    manifest.warnings.append(
                        f"ligne {line_no} : aucun lot reconnu dans « {cells[1]} »"
                    )
                manifest.entries.append(entry)
                seen_lines.add(line_no)
                if entry.retrait:
                    for lot in entry.lots:
                        manifest.explained[lot] = (
                            f"retrait consigné au manifeste, ligne {line_no}"
                        )
        elif is_retrait:
            lot_index = next(
                (index for index, cell in enumerate(header) if "lot" in cell), 0
            )
            for line_no, line in block[2:]:
                cells = split_row(line)
                cell = cells[lot_index] if lot_index < len(cells) else cells[0]
                lots = [int(found) for found in ID_RE.findall(cell)]
                commits = SHA_RE.findall(line)
                reason = f"retrait consigné, ligne {line_no}"
                if commits:
                    reason += f" (revert {', '.join(commits)})"
                for lot in lots:
                    manifest.explained[lot] = reason
                seen_lines.add(line_no)

    if not composed_found:
        manifest.warnings.append(
            "aucune table de composition reconnue (en-tête « lot » + « commit »)"
        )

    # Sections : admissions de la coupure (liste attendue) et retraits en prose.
    heading = ""
    admissions: list[int] = []
    in_admissions = False
    for line_no, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            heading = match.group(2)
            in_admissions = bool(ADMISSION_HEADING_RE.search(heading))
            continue
        if line_no in seen_lines or TABLE_ROW_RE.match(line):
            continue
        if in_admissions:
            admissions.extend(int(found) for found in ID_RE.findall(line))
            continue
        if not ID_RE.search(line) or NEGATIVE_RE.search(line):
            continue
        if EXPLAIN_RE.search(line):
            reason = f"mention ligne {line_no} : {line.strip()[:160]}"
            # Une mention en prose ne vaut explication forte que si le lot est
            # le sujet de la ligne (id en tête, après marqueurs de liste) :
            # « #987 × #1468 ... et retire celui de #1256 » ne retire pas #987.
            leading = bool(re.match(r"^[\s>*\-]*#\d+", line))
            weak = not (leading and RETRAIT_HEADING_RE.search(heading))
            for lot in (int(found) for found in ID_RE.findall(line)):
                if weak:
                    manifest.weak.setdefault(lot, reason)
                else:
                    manifest.explained.setdefault(lot, reason)
        elif RETRAIT_HEADING_RE.search(heading):
            reason = f"sous « {heading} », ligne {line_no}"
            for lot in (int(found) for found in ID_RE.findall(line)):
                manifest.weak.setdefault(lot, reason)
    if admissions:
        # dédoublonner en gardant l'ordre
        manifest.admissions = list(dict.fromkeys(admissions))
    return manifest


# --------------------------------------------------------------------------
# Contenu de l'arbre
# --------------------------------------------------------------------------


@dataclass
class PathCheck:
    path: str
    status: str
    added: int
    present: int
    verdict: str
    missing_sample: list[str] = field(default_factory=list)
    note: str = ""


def parse_name_status(data: bytes) -> list[tuple[str, str]]:
    """[(statut, chemin)] depuis `git diff --name-status -z`."""
    tokens = data.decode("utf-8", "surrogateescape").split("\0")
    pairs: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(tokens):
        status = tokens[index].strip()
        path = tokens[index + 1]
        if not status:
            break
        pairs.append((status[:1], path))
        index += 2
    return pairs


def parse_added(patch: bytes) -> dict[str, list[str] | None]:
    """{chemin: lignes ajoutées} (None = binaire) depuis un diff unifié -U0."""
    result: dict[str, list[str] | None] = {}
    path: str | None = None
    lines = patch.decode("utf-8", "surrogateescape").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            candidate = line[4:].strip()
            if candidate != "/dev/null":
                path = candidate[2:] if candidate.startswith("b/") else candidate
                result.setdefault(path, [])
        elif line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate != "/dev/null" and path is None:
                path = candidate[2:] if candidate.startswith("a/") else candidate
                result.setdefault(path, [])
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            if path is not None:
                result[path] = None
        elif line.startswith("+") and not line.startswith("+++"):
            if path is not None:
                result.setdefault(path, []).append(line[1:])
        index += 1
    return result


def check_entry(repo: Path, entry: Entry, head: str, line_cache: dict) -> dict:
    """Vérifie qu'une entrée de composition est encore dans l'arbre de tête."""
    result: dict = {
        "row": entry.row,
        "line": entry.line_no,
        "lots": entry.lots,
        "source": entry.source,
        "commit": entry.commit,
        "note": entry.note,
        "retrait": entry.retrait,
        "verdict": None,
        "ancestor": None,
        "paths": [],
        "messages": [],
    }
    if not entry.commit:
        result["verdict"] = "non_vérifiable"
        result["messages"].append("aucun commit au manifeste : contenu non vérifiable")
        return result
    commit = rev_parse(repo, entry.commit)
    if commit is None:
        result["verdict"] = "commit_inconnu"
        result["messages"].append(
            f"commit {entry.commit} introuvable localement (fetch nécessaire ?)"
        )
        return result
    result["commit"] = commit
    ancestor = git(repo, "merge-base", "--is-ancestor", commit, head, check=False)
    result["ancestor"] = ancestor.returncode == 0
    if not result["ancestor"]:
        result["messages"].append("commit de composition non ancêtre de la tête")

    if entry.retrait:
        # Une ligne de retrait consigne l'exclusion : son effet est vérifié par
        # l'entrée de composition du lot (contenu absent) et par l'alerte
        # « retrait consigné mais contenu présent » le cas échéant.
        result["verdict"] = "retrait_consigné"
        return result

    parent = rev_parse(repo, f"{commit}^")
    if parent is None:
        result["verdict"] = "non_vérifiable"
        result["messages"].append("commit racine : aucun parent pour le delta")
        return result

    status_proc = git(
        repo, "diff", "--no-renames", "--name-status", "-z", parent, commit
    )
    name_status = parse_name_status(status_proc.stdout)
    patch_proc = git(
        repo,
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-renames",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        parent,
        commit,
    )
    added_by_path = parse_added(patch_proc.stdout)

    checks: list[PathCheck] = []
    for status, path in name_status:
        if status == "D":
            present = blob_id(repo, head, path) is None
            checks.append(
                PathCheck(
                    path=path,
                    status=status,
                    added=0,
                    present=1 if present else 0,
                    verdict="suppression_tenue" if present else "réintroduit",
                )
            )
            continue
        added = added_by_path.get(path)
        if added is None:
            checks.append(
                PathCheck(
                    path=path, status=status, added=0, present=0,
                    verdict="binaire", note="fichier binaire : contenu non comparé",
                )
            )
            continue
        significant = [line for line in added if line.strip()]
        ignored = len(added) - len(significant)
        head_blob = blob_id(repo, head, path)
        commit_blob = blob_id(repo, commit, path)
        if head_blob is None:
            checks.append(
                PathCheck(
                    path=path, status=status, added=len(significant), present=0,
                    verdict="absent", note="fichier absent de la tête",
                )
            )
            continue
        if head_blob == commit_blob:
            checks.append(
                PathCheck(
                    path=path, status=status, added=len(significant),
                    present=len(significant), verdict="identique",
                )
            )
            continue
        key = (head, path)
        if key not in line_cache:
            line_cache[key] = blob_lines(repo, head, path)
        head_lines = line_cache[key]
        if head_lines is None:
            checks.append(
                PathCheck(
                    path=path, status=status, added=len(significant), present=0,
                    verdict="binaire", note="tête binaire ou illisible",
                )
            )
            continue
        missing = [line for line in significant if line not in head_lines]
        present = len(significant) - len(missing)
        if present == 0 and significant:
            verdict = "absent"
        elif missing:
            verdict = "partiel"
        else:
            verdict = "adapté"
        checks.append(
            PathCheck(
                path=path, status=status, added=len(significant), present=present,
                verdict=verdict,
                missing_sample=[line.strip()[:80] for line in missing[:3]],
                note=f"{ignored} ligne(s) vide(s) ignorée(s)" if ignored else "",
            )
        )
    result["paths"] = [check.__dict__ for check in checks]

    if not checks:
        result["verdict"] = "vide"
        result["messages"].append("aucun fichier dans le delta du commit")
        return result

    absent = [check for check in checks if check.verdict in ("absent", "réintroduit")]
    partial = [
        check for check in checks
        if check.verdict in ("partiel", "binaire", "non_vérifiable")
    ]
    if absent and partial:
        result["verdict"] = "partiel"
    elif absent:
        result["verdict"] = "absent"
    elif partial:
        result["verdict"] = "partiel"
    else:
        result["verdict"] = "présent"
    return result


def lot_verdict(entries: list[dict]) -> str:
    """Meilleur verdict d'un lot sur ses entrées (présent > partiel > absent)."""
    order = ["présent", "partiel", "absent", "non_vérifiable",
             "commit_inconnu", "vide", "retrait_consigné"]
    for name in order:
        if any(entry["verdict"] == name for entry in entries):
            return name
    return "non_vérifiable"


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> tuple[int, dict]:
    repo = Path(args.repo).resolve()
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        raise InputError(f"manifeste introuvable : {manifest_path}")
    manifest = parse_manifest(manifest_path)

    head = rev_parse(repo, args.head)
    if head is None:
        raise InputError(f"tête introuvable : {args.head}")

    expected = collect_ids(args.expected, args.expected_file)
    if not expected and manifest.admissions:
        expected = list(manifest.admissions)
    excused = {
        lot: "reporté (déclaré hors manifeste)"
        for lot in collect_ids(args.excused, args.excused_file)
    }

    line_cache: dict = {}
    entry_results = [
        check_entry(repo, entry, head, line_cache) for entry in manifest.entries
    ]

    composed: dict[int, list[dict]] = {}
    for entry, result in zip(manifest.entries, entry_results):
        if entry.retrait:
            continue
        for lot in entry.lots:
            composed.setdefault(lot, []).append(result)

    explained = dict(manifest.explained)
    omissions: list[dict] = []
    removed: list[dict] = []
    exclusions: list[dict] = []
    contents: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []

    def explain(lot: int, verdict: str) -> bool:
        """Consigne une exclusion (forte, faible ou déclarée) si elle existe."""
        if lot in explained:
            exclusions.append({"lot": lot, "reason": explained[lot],
                               "verdict": verdict})
            return True
        if lot in excused:
            exclusions.append({"lot": lot, "reason": excused[lot],
                               "verdict": verdict})
            return True
        if lot in manifest.weak:
            exclusions.append({"lot": lot, "reason": manifest.weak[lot],
                               "verdict": verdict, "weak": True})
            warnings.append(
                f"#{lot} : contenu {verdict}, explication faible "
                f"({manifest.weak[lot]}) — à confirmer"
            )
            return True
        return False

    # 1. Chaque lot attendu doit apparaître au manifeste (ou être expliqué).
    # Une mention faible (prose sans retrait du lot) ne lève pas l'omission :
    # « retour à l'état #1333 » sous « Retraits » ne dit pas que #1333 est exclu.
    for lot in expected:
        if lot in composed:
            continue
        if lot in manifest.weak and lot not in explained and lot not in excused:
            omissions.append({"lot": lot, "reason": "absent du manifeste "
                              f"(mention faible seulement : {manifest.weak[lot]})"})
            continue
        if explain(lot, "absent"):
            continue
        omissions.append({"lot": lot, "reason": "absent du manifeste"})

    # 2. Contenu réel à la tête, pour chaque lot composé (attendu ou non).
    for lot in sorted(composed):
        verdict = lot_verdict(composed[lot])
        if lot in expected:
            contents.append({"lot": lot, "verdict": verdict})
        if verdict == "présent":
            if lot in explained:
                warnings.append(
                    f"#{lot} : retrait/report consigné mais contenu présent "
                    "à la tête (recomposition ?)"
                )
            continue
        if verdict in ("partiel", "non_vérifiable", "vide"):
            if explain(lot, verdict):
                continue
            warnings.append(
                f"#{lot} : contenu {verdict} à la tête — à lire avant le feu"
            )
            if args.strict:
                errors.append(f"#{lot} : contenu {verdict} à la tête (--strict)")
            continue
        # absent, commit_inconnu
        if explain(lot, verdict):
            continue
        removed.append({
            "lot": lot,
            "verdict": verdict,
            "reason": f"composé mais contenu {verdict} à la tête, "
                      "aucun retrait/report consigné",
        })

    errors.extend(f"#{item['lot']} : {item['reason']}" for item in omissions)
    errors.extend(f"#{item['lot']} : {item['reason']}" for item in removed)
    for result in entry_results:
        if not result["lots"] and result["verdict"] in ("commit_inconnu",
                                                        "non_vérifiable"):
            message = f"entrée {result['row']} : {result['messages'][0]}"
            if result["verdict"] == "commit_inconnu":
                errors.append(message)
            else:
                warnings.append(message)

    extras = sorted(
        lot for lot in composed if expected and lot not in expected
    )

    payload = {
        "schema": SCHEMA,
        "repo": str(repo),
        "manifest": str(manifest_path),
        "head": head,
        "head_subject": commit_subject(repo, head),
        "expected_source": (
            "options" if (args.expected or args.expected_file) else
            "section du manifeste" if manifest.admissions else None
        ),
        "expected": expected,
        "excused": sorted(excused),
        "composed": sorted(composed),
        "explained": {str(lot): reason for lot, reason in sorted(explained.items())},
        "weak": {str(lot): reason for lot, reason in sorted(manifest.weak.items())},
        "entries": entry_results,
        "omissions": omissions,
        "removed": removed,
        "exclusions": exclusions,
        "contents": contents,
        "extras": extras,
        "warnings": warnings + manifest.warnings,
        "errors": errors,
    }
    return (1 if errors else 0), payload


def render(payload: dict) -> str:
    out: list[str] = []
    out.append(f"Paquet  : {payload['head'][:12]} — {payload['head_subject']}")
    out.append(f"Manifeste : {payload['manifest']}")
    entries = payload["entries"]
    retraits = sum(1 for entry in entries if entry["retrait"])
    out.append(
        f"Composition : {len(entries)} entrées "
        f"({len(entries) - retraits} compositions, {retraits} retraits)"
    )
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
    out.append(
        "Verdicts   : "
        + ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    )
    if payload["expected"]:
        out.append(
            f"Attendus : {len(payload['expected'])} lots "
            f"({payload['expected_source']})"
        )
    else:
        out.append(
            "Attendus : aucun — contrôle « admissions × manifeste » NON effectué "
            "(fournir --expected, --expected-file ou une section « admissions »)"
        )
    out.append("")

    if payload["omissions"]:
        out.append(f"INCLUSIONS MANQUANTES ({len(payload['omissions'])})")
        for item in payload["omissions"]:
            out.append(f"  #{item['lot']} — {item['reason']}")
        out.append("")
    if payload["removed"]:
        out.append(f"CONTENUS ABSENTS À LA TÊTE ({len(payload['removed'])})")
        for item in payload["removed"]:
            out.append(f"  #{item['lot']} — {item['reason']}")
        out.append("")
    if payload["exclusions"]:
        out.append(f"EXCLUSIONS CONSIGNÉES ({len(payload['exclusions'])})")
        for item in payload["exclusions"]:
            marker = " [explication faible]" if item.get("weak") else ""
            out.append(f"  #{item['lot']} — {item['reason']}{marker}")
        out.append("")
    if payload["extras"]:
        out.append(f"COMPOSÉS HORS LISTE ATTENDUE ({len(payload['extras'])})")
        out.append("  " + ", ".join(f"#{lot}" for lot in payload["extras"]))
        out.append("")
    if payload["contents"]:
        present = [item for item in payload["contents"]
                   if item["verdict"] == "présent"]
        other = [item for item in payload["contents"] if item not in present]
        out.append(
            f"CONTENUS À LA TÊTE ({len(payload['contents'])} lots attendus : "
            f"{len(present)} présents, {len(other)} à lire)"
        )
        for item in other:
            out.append(f"  #{item['lot']} — {item['verdict']}")
        out.append("")

    severity = {
        "absent": 0, "commit_inconnu": 0,
        "partiel": 1, "réintroduit": 1,
        "non_vérifiable": 2, "vide": 2, "binaire": 2,
    }
    flagged = sorted(
        (entry for entry in entries if entry["verdict"] in severity),
        key=lambda entry: severity[entry["verdict"]],
    )
    if flagged:
        out.append(f"ENTRÉES DE COMPOSITION À LIRE ({len(flagged)})")
        for entry in flagged:
            lots = "/".join(f"#{lot}" for lot in entry["lots"]) or "?"
            out.append(
                f"  entrée {entry['row']} {lots} — {entry['verdict']} "
                f"(commit {str(entry['commit'])[:12]}, ligne {entry['line']})"
            )
            for message in entry["messages"]:
                out.append(f"      {message}")
            for check in entry["paths"]:
                if check["verdict"] in ("absent", "partiel", "réintroduit", "binaire"):
                    detail = f"{check['present']}/{check['added']} lignes"
                    if check["note"]:
                        detail += f" ; {check['note']}"
                    out.append(f"      {check['path']} : {check['verdict']} ({detail})")
                    for line in check["missing_sample"]:
                        out.append(f"          absente : {line}")
        out.append("")

    if payload["warnings"]:
        out.append(f"AVERTISSEMENTS ({len(payload['warnings'])})")
        for warning in payload["warnings"]:
            out.append(f"  - {warning}")
        out.append("")

    if payload["errors"]:
        out.append(f"DÉFAUTS BLOQUANTS ({len(payload['errors'])})")
        for error in payload["errors"]:
            out.append(f"  - {error}")
        out.append("")
        out.append("VERDICT : à corriger avant la CI complète et le feu.")
    else:
        out.append("VERDICT : aucun défaut détecté par ce contrôle.")
    return "\n".join(out)


def parse_id_list(value: str) -> list[int]:
    ids: list[int] = []
    for token in re.split(r"[,\s]+", value):
        token = token.strip().lstrip("#")
        if token.isdigit():
            ids.append(int(token))
    return ids


def read_id_file(path: Path) -> list[int]:
    ids: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"\s+#.*$", "", line)
        ids.extend(parse_id_list(line))
    return ids


def collect_ids(values, files) -> list[int]:
    """Ids depuis les options (entiers ou listes) et les fichiers, sans doublon."""
    ids: list[int] = []
    for value in values:
        if isinstance(value, int):
            ids.append(value)
        else:
            ids.extend(parse_id_list(str(value)))
    for name in files:
        ids.extend(read_id_file(Path(name)))
    return list(dict.fromkeys(ids))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contrôle d'inclusion des lots d'un paquet §12 avant le feu."
    )
    parser.add_argument("--manifest", required=True,
                        help="manifeste de composition du paquet (reviews/…)")
    parser.add_argument("--head", required=True,
                        help="tête du paquet (SHA ou réf)")
    parser.add_argument("--repo", default=".", help="dépôt git (défaut : .)")
    parser.add_argument("--expected", action="append", default=[],
                        help="lots attendus à la coupure (ids, virgules ou espaces)")
    parser.add_argument("--expected-file", action="append", default=[],
                        help="fichier de lots attendus (un id par ligne)")
    parser.add_argument("--excused", action="append", default=[],
                        help="lots reportés/retirés hors manifeste (ids)")
    parser.add_argument("--excused-file", action="append", default=[],
                        help="fichier de lots reportés/retirés hors manifeste")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--strict", action="store_true",
                        help="un contenu partiel compte comme un défaut bloquant")
    args = parser.parse_args(argv)

    for value in args.expected:
        if isinstance(value, str) and not parse_id_list(value):
            parser.error(f"--expected illisible : {value!r}")
    for value in args.excused:
        if isinstance(value, str) and not parse_id_list(value):
            parser.error(f"--excused illisible : {value!r}")

    try:
        code, payload = run(args)
    except (GitError, InputError) as error:
        print(f"paquet-inclusion : {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(render(payload))
    return code


if __name__ == "__main__":
    sys.exit(main())
