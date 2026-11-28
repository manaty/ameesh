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
  manifeste (titre contenant « admission », liste ou tableau) ; et la liste
  **calculée** des lots prêts (#2404) : `--ready`, `--ready-file`, produite par
  `scripts/paquet-candidats.py` (état qa, gel = tête publiée, verdicts requis
  acquis, merge-tree propre) ;
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
  **avant** le feu ;
* les **trailers `ameesh-work:`** des commits de composition : chaque entrée
  non retirée doit porter un trailer par lot qu'elle compose (#4240). Sans
  lui, `packet record` échoue et la porte des familles d'auteurs voit tous les
  auteurs de la tête : le composeur s'arrête ici, avant la CI complète, au
  lieu de recomposer après une CI annulée (paquet Nexlink 229, 28/10).

Verdicts d'une entrée de composition : `présent` (toutes les lignes ajoutées
sont là, ou le blob est identique), `partiel` (une partie seulement), `absent`
(aucune ligne, fichier disparu, ou suppression annulée), `vide` (aucun delta :
remise fraîche déjà portée par une autre entrée). Une ligne de retrait est
consignée `retrait_consigné` : son effet se lit sur l'entrée de composition du
lot (contenu absent), et un contenu qui réapparaît pour un lot pourtant retiré
donne un avertissement.

Un lot **prêt** (liste calculée) absent de la composition n'est pas une
omission s'il figure dans une table « Écartés » avec un motif : c'est la forme
que le composeur doit écrire, et le contrôle la lit comme une explication
forte. Seul le lot en tête de ligne est le sujet d'une exclusion ; un id cité
dans le motif reste une référence faible. Une ligne « écartée » sans motif ne
lève pas l'omission (#2404).

#3813 : la liste calculée peut nommer des lots **en erreur** (`!<lot> :
<motif>`) sans devenir inutilisable. Un tel lot ne peut pas être composé :
le trouver dans une entrée du manifeste est un défaut bloquant. S'il est
absent du manifeste, il est consigné comme écarté avec ce motif, sans recopie
manuelle ; les nombres du motif ne sont jamais relus comme des ids.

Sortie : un rapport lisible, ou `--json` (schéma `ameesh-paquet-inclusion/1`).
La liste des lots prêts est **exigée** : sans source `--ready`/`--ready-file`,
le contrôle s'arrête en 2 ; `--no-ready` est refusé (#2464 : une recomposition
l'a utilisé et a laissé passer un lot prêt). Un fichier fourni mais vide
(« aucun lot prêt ») est valide, un fichier absent ou illisible est une entrée
invalide. Sur une recomposition, la liste est recalculée sur la cible de la
recomposition (`scripts/paquet-candidats.py --ready-out`).
Codes de retour : 0 si aucun défaut ; 1 si une inclusion manquante, un lot
prêt ni composé ni écarté avec un motif, un lot composé alors que la liste
calculée le dit en erreur (#3813), un contenu absent non expliqué, un commit
introuvable, ou un commit de composition sans trailer `ameesh-work:` (#4240),
ou bien, sous `--strict`, un contenu partiel ou vide non expliqué ; 2 si
l'entrée est invalide (manifeste ou tête introuvable, fichier de lots absent
ou illisible, ligne `!lot` sans motif, aucune source de liste prête, ou aucune
source de liste attendue sans `--no-expected`).
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
NO_ADMISSION_HEADING_RE = re.compile(
    r"(sans|aucun|aucune|pas de|pas d['’])\s*(section\s+)?(d['’]\s*)?admissions?",
    re.IGNORECASE,
)
EXCLUSION_HEADING_RE = re.compile(
    r"écart|ecart|retrait|retir|revert|report|exclu|différ", re.IGNORECASE)
READY_ERROR_RE = re.compile(r"^!(\d+)\s*(?::\s*(.*))?$")
#: le trailer d'une unité de composition, même forme que `packet_link.TRAILER`
TRAILER_RE = re.compile(r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?(\d+)[ \t]*$")


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


def blob_size(repo: Path, rev: str, path: str) -> int | None:
    """Taille du blob à cette révision, ou None s'il est absent/illisible."""
    proc = git(repo, "cat-file", "-s", f"{rev}:{path}", check=False)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.decode("ascii", "replace").strip())
    except ValueError:
        return None


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
    #: lots « écartés » au manifeste, ligne → motif ("" : sans motif)
    ecartes: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def split_exclusion_line(line: str) -> tuple[list[int], list[int], str]:
    """Ids sujets en tête de ligne, ids cités dans le motif, motif restant.

    Sous « Écartés »/« Retraits », seul le lot en tête de ligne est le sujet de
    l'exclusion. Un id cité dans le motif (« #500 dépend de #600 ») est une
    référence : elle reste une explication faible (#2404, relecture cx2 B1).
    """
    match = re.match(r"^[\s>*\-]*((?:#\d+[\s/,]*)+)", line)
    if not match:
        return [], [int(found) for found in ID_RE.findall(line)], line.strip()
    subjects = [int(found) for found in ID_RE.findall(match.group(1))]
    rest = line[match.end():].strip(" :—-\t")
    references = [int(found) for found in ID_RE.findall(rest)]
    return subjects, references, rest


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


def parse_manifest(path: Path, *, text: str | None = None) -> Manifest:
    if text is None:
        text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    manifest = Manifest(path=path)
    composed_found = False
    seen_lines: set[int] = set()
    admissions: list[int] = []

    for heading, block in iter_tables(lines):
        if ADMISSION_HEADING_RE.search(heading) \
                and not NO_ADMISSION_HEADING_RE.search(heading):
            # Une section d'admissions écrite en tableau fournit la liste
            # attendue ; elle n'est jamais lue comme une composition, même si
            # ses colonnes s'appellent « lot » et « commit » (#1655 N2).
            for line_no, line in block[2:]:
                admissions.extend(int(found) for found in ID_RE.findall(line))
                seen_lines.add(line_no)
            continue
        header = [cell.lower() for cell in split_row(block[0][1])]
        is_retrait = any("retir" in cell or "revert" in cell for cell in header)
        is_compo = (
            not is_retrait
            and any("lot" in cell for cell in header)
            and any("commit" in cell for cell in header)
        )
        is_ecarte = bool(EXCLUSION_HEADING_RE.search(heading))
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
        elif is_ecarte:
            # La table « Écartés (ce véhicule) » du manifeste : chaque lot
            # écarté y porte un motif. Une ligne sans motif ne lève pas
            # l'omission d'un lot prêt (#2404) — elle reste visible à part.
            lot_index = next(
                (index for index, cell in enumerate(header) if "lot" in cell), 0
            )
            for line_no, line in block[2:]:
                cells = split_row(line)
                cell = cells[lot_index] if lot_index < len(cells) else cells[0]
                lots = [int(found) for found in ID_RE.findall(cell)]
                reason = " | ".join(
                    value for index, value in enumerate(cells)
                    if index != lot_index and value.strip()
                ).strip(" —-?")
                for lot in lots:
                    manifest.ecartes[lot] = reason
                    if reason:
                        manifest.explained[lot] = (
                            f"écarté au manifeste, ligne {line_no} : {reason}"
                        )
                seen_lines.add(line_no)

    if not composed_found:
        manifest.warnings.append(
            "aucune table de composition reconnue (en-tête « lot » + « commit »)"
        )

    # Sections : admissions de la coupure (liste attendue) et retraits en prose.
    heading = ""
    in_admissions = False
    for line_no, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            heading = match.group(2)
            in_admissions = bool(
                ADMISSION_HEADING_RE.search(heading)
                and not NO_ADMISSION_HEADING_RE.search(heading)
            )
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
            # Une mention en prose ne vaut explication forte que pour les lots
            # sujets (ids en tête, après marqueurs de liste) ; un id cité dans
            # le motif reste une référence faible : « #500 dépend de #600 » ne
            # retire pas #600 (#2404, relecture cx2 B1).
            subjects, references, _rest = split_exclusion_line(line)
            strong = bool(subjects and EXCLUSION_HEADING_RE.search(heading))
            for lot in subjects:
                if strong:
                    manifest.explained.setdefault(lot, reason)
                else:
                    manifest.weak.setdefault(lot, reason)
            for lot in references:
                manifest.weak.setdefault(lot, reason)
        elif EXCLUSION_HEADING_RE.search(heading):
            # Sous « Écartés »/« Retraits », le lot doit être le sujet de la
            # ligne (id en tête) pour être écarté ; une phrase qui le cite au
            # passage reste une mention faible. Un id seul n'est pas un motif
            # et ne lève pas l'omission d'un lot prêt (#2404).
            subjects, references, rest = split_exclusion_line(line)
            strong = bool(subjects and rest)
            for lot in subjects:
                if strong:
                    manifest.ecartes.setdefault(lot, rest)
                    manifest.explained.setdefault(
                        lot,
                        f"écarté sous « {heading} », ligne {line_no} : {rest[:160]}",
                    )
                else:
                    manifest.ecartes.setdefault(lot, "")
            for lot in references:
                manifest.weak.setdefault(
                    lot, f"sous « {heading} », ligne {line_no}"
                )
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
    """{chemin: lignes ajoutées} (None = binaire) depuis un diff unifié -U0.

    Les en-têtes `---`/`+++` ne sont lus qu'**hors hunk** : une ligne ajoutée
    qui commence par « ++ » produit une ligne de diff « +++ … » et ne doit pas
    être prise pour un en-tête de fichier (#1655 N3).
    """
    result: dict[str, list[str] | None] = {}
    path: str | None = None
    in_hunk = False
    lines = patch.decode("utf-8", "surrogateescape").splitlines()
    for line in lines:
        if line.startswith("diff --git "):
            path = None
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                if path is not None:
                    result.setdefault(path, []).append(line[1:])
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
    return result


def parse_removed(patch: bytes) -> dict[str, list[str] | None]:
    """{chemin: lignes retirées} (None = binaire) depuis un diff unifié -U0.

    Même lecture que :func:`parse_added` : les en-têtes ``---``/``+++`` ne sont
    lus qu'**hors hunk** ; dans un hunk, toute ligne commençant par ``-`` est
    du contenu retiré, même si elle ressemble à un en-tête.
    """
    result: dict[str, list[str] | None] = {}
    path: str | None = None
    in_hunk = False
    lines = patch.decode("utf-8", "surrogateescape").splitlines()
    for line in lines:
        if line.startswith("diff --git "):
            path = None
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("-"):
                if path is not None:
                    result.setdefault(path, []).append(line[1:])
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
    return result


def check_entry(repo: Path, entry: Entry, head: str, line_cache: dict,
                exempt: dict[str, set[str]] | None = None) -> dict:
    """Vérifie qu'une entrée de composition est encore dans l'arbre de tête.

    #8395 : ``exempt`` porte, par chemin, les lignes que le commit de pliage
    du contrat a **retirées** (en-tête, révision renumérotée,
    ``CONTRACT_VERSION``, surface, fragments). Ces lignes-là ne peuvent pas
    être exigées telles quelles ; les autres lignes ajoutées par le lot
    restent exigées à la tête (B1 : une exemption de fichier entier masquait
    une perte dans ``public/__init__.py`` ou une section du contrat). Un
    chemin dont toutes les lignes sont repliées est lu « replié », ce qui
    n'est ni une omission ni un retrait.
    """
    exempt = dict(exempt or {})
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
        "trailers": [],
        "trailers_missing": [],
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

    # #4240 : un merge de composition sans trailer `ameesh-work:` pour un lot
    # qu'il porte n'est pas une unité de paquet. `packet record` l'ignore ou
    # échoue, et la porte des familles d'auteurs lit alors les auteurs de la
    # tête entière. On l'arrête ici, avant la CI complète et le feu, plutôt
    # qu'après une CI annulée et une recomposition (paquet Nexlink 229). Seuls
    # les commits de fusion sont des unités : un manifeste peut citer un commit
    # de contenu (remise, adaptation) sans lui demander de trailer.
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.decode(
        "ascii", "replace").split()
    if len(parents) >= 3:
        body = git(repo, "show", "-s", "--format=%B", commit).stdout.decode(
            "utf-8", "replace")
        trailers = sorted({int(match.group(1))
                           for match in TRAILER_RE.finditer(body)})
        missing = [lot for lot in entry.lots if lot not in trailers]
        result["trailers"] = trailers
        result["trailers_missing"] = missing
        if missing:
            result["messages"].append(
                "commit de composition sans trailer `ameesh-work:` pour %s"
                % ", ".join("#%d" % lot for lot in missing))

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
        if added is None and path not in added_by_path and (
            blob_id(repo, parent, path) == blob_id(repo, commit, path)
            or blob_size(repo, commit, path) == 0
        ):
            # Aucun hunk pour ce chemin : un delta vide n'est pas un binaire.
            # Blobs identiques : changement de mode seul (bit exécutable,
            # #1655 N1). Blob vide au commit : fichier nouveau et vide, statut
            # A sans blob parent ni ligne (#1684).
            added = []
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
        folded = exempt.get(path, set())
        excused = [line for line in significant if line in folded]
        required = [line for line in significant if line not in folded]
        notes = []
        if ignored:
            notes.append(f"{ignored} ligne(s) vide(s) ignorée(s)")
        if excused:
            notes.append(f"{len(excused)} ligne(s) repliée(s) par le commit "
                         "de composition du contrat")
        note = " ; ".join(notes)
        if not required and excused:
            # #8395 : toutes les lignes ajoutées par le lot ont été retirées
            # par le pliage (fragment supprimé, en-tête remplacé) : le chemin
            # est replié, ni omis ni perdu.
            checks.append(
                PathCheck(
                    path=path, status=status, added=len(significant), present=0,
                    verdict="replié", note=note,
                )
            )
            continue
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
                    present=len(significant), verdict="identique", note=note,
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
        missing = [line for line in required if line not in head_lines]
        present = len(required) - len(missing)
        # B2 (#8395) : un lot qui ne fait que retirer des lignes n'a rien à
        # retrouver ; sans `required`, present=0 ne dit pas « absent ».
        if present == 0 and required:
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
                note=note,
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


CONTRACT_HEADING_RE = re.compile(r"contrat module v1", re.IGNORECASE)
CONTRACT_COMMIT_RE = re.compile(
    r"Commit de composition\s*:\s*`([0-9a-f]{7,40})`")
CONTRACT_REVISION_LOT_RE = re.compile(
    r"(\d+\.\d+)\s*\(#(\d+)(?:,\s*([^)]+))?\)")

#: le contrat module v1 et les deux satellites que le pliage aligne
CONTRACT_REL = "docs/api/module-contract-v1.md"
SURFACE_REL = "docs/api/module-contract-v1.surface.json"
VERSION_REL = "src/ameesh/public/__init__.py"
CONTRACT_DIR_REL = "docs/api/module-contract-v1.d"
CONTRACT_FILE_RELS = frozenset({CONTRACT_REL, SURFACE_REL, VERSION_REL})
CONTRACT_HEADER_LINE_RE = re.compile(
    r"^Contract revision: \*\*(\d+\.\d+), ")
CONTRACT_HEADER_ONLY_RE = re.compile(r"^Contract revision: \*\*\d+\.\d+, ")
CONTRACT_ITEM_RE = re.compile(r"^Revision \d+\.\d+ adds\b")
CONTRACT_VERSION_RE = re.compile(r'^CONTRACT_VERSION = "(\d+\.\d+)"$')
CONTRACT_SURFACE_RE = re.compile(r'^\s*"contract"\s*:\s*"(\d+\.\d+)",?\s*$')
CONTRACT_ADDITIVE_RE = re.compile(
    r"\badditive to (?:revision )?\d+\.\d+\b")
FRAGMENT_FILE_RE = re.compile(r"^([1-9][0-9]*)\.md$")
CONTRACT_ITEM_NUMBER_RE = re.compile(r"^Revision \d+\.\d+ adds\b")


def _revision_list_lines(text: str) -> list[str]:
    """Les lignes de la liste des révisions, sans les sections qui suivent.

    La liste commence à la première ligne « Revision 1.N adds » et s'arrête au
    premier titre ou à « The [IMA mapping] », comme le pliage. Une ligne
    « Revision 1.8 adds … » en tête de section n'en fait donc pas partie (N).
    """
    lines = str(text).splitlines()
    start = None
    for index, line in enumerate(lines):
        if CONTRACT_ITEM_RE.match(line):
            start = index
            break
    if start is None:
        return []
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if (lines[index].startswith("## ")
                or lines[index].startswith("The [IMA mapping]")):
            end = index
            break
    return lines[start:end]


def _norm_revision_line(line: str) -> str:
    """Une ligne de révision, numéro et dernière référence additive neutralisés.

    Le pliage ne réécrit qu'un numéro de révision et la dernière mention
    « additive to 1.N » d'un paragraphe ; le reste doit être identique pour
    prouver que le contenu a été renuméroté, pas supprimé.
    """
    text = CONTRACT_ITEM_NUMBER_RE.sub("Revision X adds", line, count=1)
    matches = list(CONTRACT_ADDITIVE_RE.finditer(text))
    if matches:
        match = matches[-1]
        text = (text[:match.start()] + "additive to X" + text[match.end():])
    return text


def _contract_section(text: str) -> str | None:
    """Le corps de la section « Contrat module v1 » du manifeste, ou None."""
    lines = str(text).splitlines()
    section: list[str] | None = None
    level = 0
    for line in lines:
        match = HEADING_RE.match(line)
        if not match:
            if section is not None:
                section.append(line)
            continue
        if section is None:
            if CONTRACT_HEADING_RE.search(match.group(2)):
                section = []
                level = len(match.group(1))
            continue
        if len(match.group(1)) <= level:
            break
        section.append(line)
    if section is None:
        return None
    return "\n".join(section)


def contract_fold(text: str) -> tuple[str, list[int]] | None:
    """#8395 : le commit de pliage du contrat et ses lots, lus au manifeste.

    La section « Contrat module v1 » cite le commit de composition qui a replié
    les fragments de révision et les lots concernés ; ``None`` quand le
    manifeste n'en porte pas (paquet sans changement de contrat).
    """
    body = _contract_section(text)
    if body is None:
        return None
    commit = CONTRACT_COMMIT_RE.search(body)
    if not commit:
        return None
    lots = [int(match.group(2))
            for match in CONTRACT_REVISION_LOT_RE.finditer(body)]
    return commit.group(1), lots


def _fragment_parts(text: str) -> tuple[str, str]:
    """La ligne ``adds:`` d'un fragment et ses sections, comme le composeur.

    ``("", "")`` quand le fragment n'a pas la forme attendue : le contrôle
    refuse alors le pliage cité plutôt que de dispenser son contenu.
    """
    body = str(text).replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
    while body.startswith("<!--"):
        end = body.find("-->")
        if end < 0:
            return "", ""
        body = body[end + 3:].lstrip("\n")
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("adds:"):
        return "", ""
    adds = lines[0][len("adds:"):].strip()
    if not adds:
        return "", ""
    index = 1
    while index < len(lines) and lines[index].strip():
        adds += "\n" + lines[index].rstrip()
        index += 1
    return adds, "\n".join(lines[index:]).strip("\n")


def _render_fragment_adds(adds: str, previous: str) -> str:
    """Le texte ``adds`` après pliage : ``{previous}`` et référence finale."""
    text = str(adds).replace("{previous}", previous)
    matches = list(CONTRACT_ADDITIVE_RE.finditer(text))
    if matches:
        match = matches[-1]
        text = (text[:match.start()] + "additive to " + previous
                + text[match.end():])
    return text


def contract_fold_paths(repo: Path, head: str, text: str
                        ) -> tuple[dict[int, dict[str, set[str]]], list[str]]:
    """#8395 : les lignes que le pliage a retirées, par lot et par chemin.

    Une entrée de composition ne peut pas exiger de retrouver telles quelles
    les lignes que le composeur a réécrites : ce sont les lignes « - » du
    commit de pliage, et elles seules. Les autres lignes ajoutées par le lot
    restent exigées à la tête (B1 : une exemption de fichier entier masquait
    une perte dans ``public/__init__.py`` ou une section du contrat).

    Le commit cité est borné : commit simple, ancêtre de la tête, qui ne touche
    que le contrat, sa surface, ``CONTRACT_VERSION`` et les fragments des lots
    cités. Le pliage est vérifié : seules l'en-tête et les révisions changent
    dans le contrat, seul ``CONTRACT_VERSION`` dans le module, seul le champ
    ``contract`` dans la surface, les trois versions s'alignent, et le contenu
    de chaque fragment se retrouve à la tête. Toute autre forme refuse le
    manifeste (cx2 : un commit cité qui retire du contenu hors contrat ne vaut
    pas exemption).
    """
    fold = contract_fold(text)
    if fold is None:
        return {}, []
    sha, lots = fold
    if len(set(lots)) != len(lots):
        return {}, ["section « Contrat module v1 » : un lot est cité deux fois"]
    commit = rev_parse(repo, sha)
    if commit is None:
        return {}, [f"commit de pliage du contrat {sha} introuvable"]
    if git(repo, "merge-base", "--is-ancestor", commit, head,
           check=False).returncode != 0:
        return {}, [f"commit de pliage du contrat {sha} non ancêtre de la tête"]
    parents = git(repo, "rev-list", "--parents", "-n", "1",
                  commit).stdout.decode("ascii", "replace").split()
    if len(parents) != 2:
        return {}, [f"commit de pliage du contrat {sha} n'est pas un commit simple"]
    parent = rev_parse(repo, f"{commit}^")
    if parent is None:
        return {}, [f"commit de pliage du contrat {sha} sans parent"]
    status = parse_name_status(git(
        repo, "diff", "--no-renames", "--name-status", "-z", parent,
        commit).stdout)
    if not status:
        return {}, [f"commit de pliage du contrat {sha} sans fichier"]
    fragments = {f"{CONTRACT_DIR_REL}/{lot}.md" for lot in lots}
    changed = {path for _status, path in status}
    outside = sorted(changed - CONTRACT_FILE_RELS - fragments)
    if outside:
        return {}, [
            "commit de pliage du contrat %s touche %s, hors du contrat "
            "module v1 : refusé" % (sha, path) for path in outside]

    patch = git(repo, "-c", "core.quotePath=false", "diff", "--no-renames",
                "-U0", "--no-color", "--no-ext-diff", parent, commit).stdout
    added = parse_added(patch)
    removed = parse_removed(patch)
    errors: list[str] = []

    def lines_for(store: dict[str, list[str] | None], path: str,
                  kind: str) -> list[str]:
        value = store.get(path)
        if value is None:
            errors.append("commit de pliage %s : %s binaire ou illisible (%s)"
                          % (sha, path, kind))
            return []
        return [line for line in value if line.strip()]

    header_value = None
    if CONTRACT_REL in changed:
        added_lines = lines_for(added, CONTRACT_REL, "ajoutées")
        parent_contract = git(repo, "show", "%s:%s" % (parent, CONTRACT_REL),
                              check=False)
        parent_revisions = (
            _revision_list_lines(parent_contract.stdout.decode("utf-8", "replace"))
            if parent_contract.returncode == 0 else [])
        added_set = set(added_lines)
        added_norms = {_norm_revision_line(line) for line in added_lines}
        for line in lines_for(removed, CONTRACT_REL, "retirées"):
            # Le pliage ne retire que : l'en-tête ; une ligne de la liste des
            # révisions, renumérotée et/ou réécrite (même contenu, numéro ou
            # référence finale neutralisés) ; ou une ligne réécrite ailleurs à
            # l'identique (paragraphe ancien déplacé dans l'ordre du
            # manifeste). Une ligne de section, même commençant par
            # « Revision 1.N adds », n'est pas une révision (N) ; sans
            # réécriture correspondante, c'est un contenu perdu (B1 legacy).
            if CONTRACT_HEADER_ONLY_RE.match(line):
                continue
            if line in added_set:
                continue
            if (line in parent_revisions
                    and _norm_revision_line(line) in added_norms):
                continue
            if line in parent_revisions:
                errors.append(
                    "commit de pliage %s : %s retire une ligne de révision "
                    "sans réécriture correspondante : %s"
                    % (sha, CONTRACT_REL, line.strip()[:80]))
            else:
                errors.append(
                    "commit de pliage %s : %s retire une ligne hors de la "
                    "liste des révisions : %s"
                    % (sha, CONTRACT_REL, line.strip()[:80]))
        headers = [CONTRACT_HEADER_LINE_RE.match(line)
                   for line in added_lines]
        headers = [match for match in headers if match]
        if len(headers) != 1:
            errors.append("commit de pliage %s : %s n'écrit pas exactement un "
                          "en-tête" % (sha, CONTRACT_REL))
        else:
            header_value = headers[0].group(1)

    version_value = None
    if VERSION_REL in changed:
        added_versions = []
        for kind, store in (("retirée", removed), ("ajoutée", added)):
            for line in lines_for(store, VERSION_REL, kind):
                match = CONTRACT_VERSION_RE.match(line)
                if not match:
                    errors.append(
                        "commit de pliage %s : %s %s une ligne hors "
                        "CONTRACT_VERSION : %s"
                        % (sha, VERSION_REL, kind, line.strip()[:80]))
                elif kind == "ajoutée":
                    added_versions.append(match.group(1))
        if len(added_versions) != 1:
            errors.append("commit de pliage %s : %s n'écrit pas exactement "
                          "une CONTRACT_VERSION" % (sha, VERSION_REL))
        else:
            version_value = added_versions[0]

    surface_value = None
    if SURFACE_REL in changed:
        added_surfaces = []
        for kind, store in (("retirée", removed), ("ajoutée", added)):
            for line in lines_for(store, SURFACE_REL, kind):
                match = CONTRACT_SURFACE_RE.match(line)
                if not match:
                    errors.append(
                        "commit de pliage %s : %s %s une ligne hors du champ "
                        "« contract » : %s"
                        % (sha, SURFACE_REL, kind, line.strip()[:80]))
                elif kind == "ajoutée":
                    added_surfaces.append(match.group(1))
        if len(added_surfaces) != 1:
            errors.append("commit de pliage %s : %s n'écrit pas exactement un "
                          "champ « contract »" % (sha, SURFACE_REL))
        else:
            surface_value = added_surfaces[0]

    known = {"en-tête": header_value, "version": version_value,
             "surface": surface_value}
    known = {name: value for name, value in known.items() if value}
    if len(set(known.values())) > 1:
        errors.append("commit de pliage %s : versions désalignées (%s)"
                      % (sha, ", ".join("%s %s" % item
                                        for item in sorted(known.items()))))

    body = _contract_section(text) or ""
    revisions = [(match.group(1), int(match.group(2)),
                  (match.group(3) or "").strip().casefold())
                 for match in CONTRACT_REVISION_LOT_RE.finditer(body)]
    head_contract = blob_lines(repo, head, CONTRACT_REL)
    if head_contract is None:
        errors.append("contrat %s illisible à la tête %s"
                      % (CONTRACT_REL, head[:12]))
    # Le pliage ajoute une liste de révisions qui doit tenir à la tête : une
    # ligne de liste perdue après le pliage est un contenu disparu, même si
    # elle avait été retirée puis réécrite par le pliage (« déplacée »).
    fold_head = git(repo, "show", "%s:%s" % (commit, CONTRACT_REL), check=False)
    if fold_head.returncode == 0 and head_contract is not None:
        for line in _revision_list_lines(
                fold_head.stdout.decode("utf-8", "replace")):
            if line.strip() and line not in head_contract:
                errors.append(
                    "commit de pliage %s : ligne de la liste des révisions "
                    "absente à la tête : %s"
                    % (sha, line.strip()[:80]))
                break
    head_text = git(repo, "show", "%s:%s" % (head, CONTRACT_REL), check=False)
    head_revisions = (
        _revision_list_lines(head_text.stdout.decode("utf-8", "replace"))
        if head_text.returncode == 0 else [])
    for number, lot, kind in revisions:
        if kind.startswith("fragment"):
            continue
        if kind and not any(line.startswith("Revision %s adds" % number)
                            for line in head_revisions):
            errors.append(
                "révision écrite %s (#%d) absente de la liste des révisions "
                "à la tête" % (number, lot))
    header_before = None
    base_contract = git(repo, "show", "%s:%s" % (parent, CONTRACT_REL),
                        check=False)
    if base_contract.returncode == 0:
        match = CONTRACT_HEADER_LINE_RE.search(
            base_contract.stdout.decode("utf-8", "replace"))
        if match:
            header_before = match.group(1)
    for path in sorted(changed & fragments):
        name = Path(path).name
        match = FRAGMENT_FILE_RE.match(name)
        if not match:
            continue
        lot = int(match.group(1))
        content = git(repo, "show", "%s:%s" % (parent, path), check=False)
        if content.returncode != 0:
            errors.append("fragment %s illisible au parent du pliage" % path)
            continue
        adds, sections = _fragment_parts(
            content.stdout.decode("utf-8", "replace"))
        if not adds:
            errors.append("fragment %s : premier paragraphe « adds: » "
                          "illisible" % path)
            continue
        positions = [index for index, (_number, cited, _kind)
                     in enumerate(revisions) if cited == lot]
        if len(positions) != 1:
            errors.append("fragment %s : la section du manifeste ne cite pas "
                          "le lot #%d (ou le cite deux fois)" % (path, lot))
            continue
        index = positions[0]
        previous = revisions[index - 1][0] if index else header_before
        if previous is None:
            errors.append("fragment %s : révision précédente illisible" % path)
            continue
        expected = "Revision %s adds %s" % (
            revisions[index][0], _render_fragment_adds(adds, previous))
        blocks = expected + ("\n" + sections if sections else "")
        missing = [line for line in blocks.splitlines()
                   if line.strip() and head_contract is not None
                   and line not in head_contract]
        if missing:
            errors.append("fragment %s : ligne(s) pliée(s) absente(s) du "
                          "contrat à la tête : %s"
                          % (path, missing[0].strip()[:80]))

    if errors:
        return {}, errors
    shared = {path: set(removed.get(path) or [])
              for path in sorted(changed & CONTRACT_FILE_RELS)}
    table: dict[int, dict[str, set[str]]] = {}
    for lot in lots:
        entry = {path: set(lines) for path, lines in shared.items()}
        fragment = f"{CONTRACT_DIR_REL}/{lot}.md"
        if fragment in changed:
            entry[fragment] = set(removed.get(fragment) or [])
        table[lot] = entry
    return table, []


def entry_exemptions(table: dict[int, dict[str, set[str]]], lots
                     ) -> dict[str, set[str]]:
    """Les lignes repliées qui couvrent une entrée de composition (ses lots)."""
    merged: dict[str, set[str]] = {}
    for lot in lots:
        for path, lines in (table.get(lot) or {}).items():
            merged.setdefault(path, set()).update(lines)
    return merged



# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> tuple[int, dict]:
    repo = Path(args.repo).resolve()
    manifest_path = Path(args.manifest).resolve()
    head = rev_parse(repo, args.head)
    if head is None:
        raise InputError(f"tête introuvable : {args.head}")
    receipt = None
    if manifest_path.name.startswith("integration-"):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from ameesh import packet_snapshot
        branch = "integration/" + manifest_path.stem.removeprefix("integration-")
        receipt = packet_snapshot.lookup(checkout=str(repo), head=head, branch=branch,
            target=getattr(args, "target", None) or None,
            package_id=getattr(args, "carrier", 0) or None)
    if receipt:
        manifest_text = receipt["payload"]["manifest"]
        manifest = parse_manifest(manifest_path, text=manifest_text)
    else:
        if not manifest_path.is_file():
            raise InputError(f"manifeste introuvable : {manifest_path}")
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = parse_manifest(manifest_path, text=manifest_text)
    # #8395 : le commit de pliage du contrat, cité au manifeste, explique les
    # chemins du contrat que le composeur a réécrits pour les lots composés.
    fold_exempt, fold_errors = contract_fold_paths(repo, head, manifest_text)

    expected = collect_ids(args.expected, args.expected_file)
    if not expected and manifest.admissions:
        expected = list(manifest.admissions)
    ready, ready_errors = collect_ready(
        getattr(args, "ready", []), getattr(args, "ready_file", []))
    if getattr(args, "no_ready", False):
        # #2464 : sur la recomposition de h-1300, le composeur a gardé le
        # manifeste et sauté le contrôle « prêts × manifeste » avec --no-ready ;
        # #2384, prêt depuis 14:29, a été omis sans ligne « écarté ». Une
        # recomposition recalcule la liste sur sa cible, elle ne s'en dispense
        # pas.
        raise InputError(
            "--no-ready est refusé (#2464) : une recomposition l'a utilisé et a "
            "laissé passer un lot prêt (#2384). Recalculer la liste sur la cible "
            "de la recomposition (scripts/paquet-candidats.py --ready-out …) et "
            "la passer avec --ready ou --ready-file"
        )
    # Une source fournie mais vide est un calcul valide (« aucun lot prêt »).
    ready_source = bool(
        getattr(args, "ready", None) or getattr(args, "ready_file", None)
    )
    expected_source = bool(args.expected or args.expected_file or manifest.admissions)
    if not ready_source:
        # La composition part de la liste calculée (#2404) : sans elle, le
        # contrôle ne peut pas dire qu'un lot prêt a été oublié.
        raise InputError(
            "aucune liste de lots prêts : fournir --ready ou --ready-file "
            "(sortie de scripts/paquet-candidats.py --ready-out), recalculée "
            "sur la cible de la composition ou de la recomposition"
        )
    if not expected_source and not args.no_expected:
        # Une liste prête contrôle les omissions de lots prêts, pas les
        # admissions du pilote : sauter ce second contrôle reste explicite
        # (#1655 N1), même quand la liste calculée est fournie.
        raise InputError(
            "aucun lot attendu : fournir --expected, --expected-file ou une "
            "section « admissions » au manifeste, ou assumer le contrôle "
            "partiel avec --no-expected"
        )
    excused = {
        lot: "reporté (déclaré hors manifeste)"
        for lot in collect_ids(args.excused, args.excused_file)
    }

    line_cache: dict = {}
    entry_results = [
        check_entry(
            repo, entry, head, line_cache,
            exempt=entry_exemptions(fold_exempt, entry.lots))
        for entry in manifest.entries
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
    # #8395 : un commit de pliage cité mais introuvable ou hors de la tête est
    # un défaut du manifeste, pas une exemption silencieuse.
    errors.extend(fold_errors)
    # #4240 : les commits de composition dont le message n'a pas le trailer du
    # lot qu'ils portent. Le défaut est bloquant : la composition n'est pas
    # déclarable, `packet record` échoue ou ignore l'unité.
    trailers_missing: list[dict] = [
        {"row": result["row"], "line": result["line"], "lot": lot,
         "commit": result["commit"] or ""}
        for result in entry_results
        for lot in result.get("trailers_missing") or ()
    ]
    # #3813 : un lot en erreur n'est pas composable. Absent du manifeste, il y
    # est consigné comme écarté avec le motif du calcul, sans recopie manuelle.
    computed_errors: dict[int, str] = {
        int(lot): " ".join(str(motive).split())
        for lot, motive in ready_errors.items()
    }
    ready_error_exclusions: dict[int, str] = {}
    for lot, motive in sorted(computed_errors.items()):
        if lot in composed:
            continue
        reason = explained.get(lot) or excused.get(lot)
        ready_error_exclusions[lot] = reason or (
            "lot en erreur dans la liste calculée : %s" % motive)

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

    def ecarte_sans_motif(lot: int) -> bool:
        """La ligne « écarté » existe-t-elle sans motif ? (#2404)"""
        return (lot in manifest.ecartes and lot not in manifest.explained
                and lot not in excused)

    # #3813 : les lots que le calcul n'a pas pu lire sont des exclusions
    # fortes, avec le motif du calcul ; ils ne comptent pas comme omissions.
    for lot, reason in sorted(ready_error_exclusions.items()):
        exclusions.append({"lot": lot, "reason": reason, "verdict": "erreur",
                           "computed_error": True})

    # 1. Chaque lot attendu doit apparaître au manifeste (ou être expliqué).
    # Une mention faible (prose sans retrait du lot) ne lève pas l'omission :
    # « retour à l'état #1333 » sous « Retraits » ne dit pas que #1333 est exclu.
    for lot in expected:
        if lot in composed or lot in ready_error_exclusions:
            continue
        if ecarte_sans_motif(lot):
            omissions.append({"lot": lot, "reason": "écarté au manifeste sans "
                              "motif", "ready": False})
            continue
        if lot in manifest.weak and lot not in explained and lot not in excused:
            omissions.append({"lot": lot, "reason": "absent du manifeste "
                              f"(mention faible seulement : {manifest.weak[lot]})"})
            continue
        if explain(lot, "absent"):
            continue
        omissions.append({"lot": lot, "reason": "absent du manifeste"})

    # 1 bis. Tout lot **prêt** (liste calculée) doit être composé ou écarté
    # avec un motif. C'est le défaut de #2404 : #2377, prêt, n'était ni dans
    # le manifeste ni dans ses « écartés », donc invisible avant la CI.
    omitted = {item["lot"] for item in omissions}
    for lot in ready:
        if lot in composed or lot in omitted:
            continue
        if ecarte_sans_motif(lot):
            omissions.append({"lot": lot, "reason": "lot prêt écarté au "
                              "manifeste sans motif", "ready": True})
            omitted.add(lot)
            continue
        if lot in manifest.weak and lot not in explained and lot not in excused:
            omissions.append({"lot": lot, "reason": "lot prêt absent du "
                              "manifeste (mention faible seulement : "
                              f"{manifest.weak[lot]})", "ready": True})
            omitted.add(lot)
            continue
        if explain(lot, "prêt"):
            exclusions[-1]["ready"] = True
            continue
        omissions.append({"lot": lot, "reason": "lot prêt ni composé ni "
                          "écarté au manifeste", "ready": True})
        omitted.add(lot)

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

    # #3813 : une unité du manifeste que la liste calculée dit en erreur est
    # un défaut bloquant — sa préparation n'a pas pu être lue.
    for lot, motive in sorted(computed_errors.items()):
        if lot in composed:
            errors.append(
                f"#{lot} : composé alors que la liste calculée le dit en "
                f"erreur ({motive})")
    errors.extend(f"#{item['lot']} : {item['reason']}" for item in omissions)
    errors.extend(f"#{item['lot']} : {item['reason']}" for item in removed)
    for item in trailers_missing:
        errors.append(
            "entrée %s #%d : commit de composition %s sans trailer "
            "`ameesh-work: %d` (poser le trailer sur le message, à arbre "
            "identique, avant la CI complète et le feu)"
            % (item["row"], item["lot"], (item["commit"] or "")[:12],
               item["lot"]))
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
        "ready": ready,
        "ready_errors": {str(lot): motive
                         for lot, motive in sorted(computed_errors.items())},
        "ready_source": (
            "options" if ready_source else None
        ),
        "ecartes": {str(lot): reason for lot, reason in sorted(manifest.ecartes.items())},
        "excused": sorted(excused),
        "composed": sorted(composed),
        "explained": {str(lot): reason for lot, reason in sorted(explained.items())},
        "weak": {str(lot): reason for lot, reason in sorted(manifest.weak.items())},
        "entries": entry_results,
        "trailers_missing": trailers_missing,
        "omissions": omissions,
        "ready_omissions": [item for item in omissions if item.get("ready")],
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
            "(--no-expected ; fournir --expected, --expected-file ou une section "
            "« admissions »)"
        )
    if payload.get("ready_source"):
        liste = ", ".join(f"#{lot}" for lot in payload["ready"]) or "aucun"
        out.append(
            f"Prêts : {len(payload['ready'])} lots (liste calculée, "
            f"{payload['ready_source']}) — {liste}"
        )
        if payload.get("ready_errors"):
            erreurs = ", ".join(f"#{lot}" for lot in payload["ready_errors"])
            out.append(
                f"En erreur : {len(payload['ready_errors'])} lot(s) de la liste "
                f"calculée, à écarter — {erreurs}"
            )
    out.append("")

    if payload.get("trailers_missing"):
        out.append(
            f"TRAILERS DE COMPOSITION MANQUANTS "
            f"({len(payload['trailers_missing'])})"
        )
        for item in payload["trailers_missing"]:
            out.append(
                f"  entrée {item['row']} #{item['lot']} — commit "
                f"{(item['commit'] or '')[:12]} sans `ameesh-work: "
                f"{item['lot']}` (ligne {item['line']})"
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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        # Un export absent (ou retiré parce que l'inventaire était incomplet)
        # n'est pas une liste vide : c'est une entrée invalide (#2404, cx2 B3).
        raise InputError(f"fichier de lots illisible : {path} ({error})") from error
    ids: list[int] = []
    for line in text.splitlines():
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


def read_ready_file(path: Path) -> tuple[list[int], dict[int, str]]:
    """Lit l'export de `paquet-candidats.py --ready-out` (#2404, #3813).

    Les lots prêts sont `#123` (ou un id nu, format antérieur) ; un lot en
    erreur est `!123 : motif`. Seule cette ligne nomme un lot en erreur : les
    nombres du motif ne sont jamais relus comme des ids. Les en-têtes
    (`# paquet-candidats …`, `# prêts`, `# erreurs`) sont ignorés.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        # Un export absent (ou retiré parce que l'inventaire était incomplet)
        # n'est pas une liste vide : c'est une entrée invalide (#2404, cx2 B3).
        raise InputError(f"fichier de lots illisible : {path} ({error})") from error
    ids: list[int] = []
    errors: dict[int, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = READY_ERROR_RE.match(line)
        if match:
            lot = int(match.group(1))
            motive = " ".join((match.group(2) or "").split())
            if not motive:
                raise InputError(
                    f"lot en erreur sans motif dans {path} : {line!r}")
            errors.setdefault(lot, motive)
            continue
        if line.startswith("!"):
            raise InputError(
                f"ligne de lot en erreur illisible dans {path} : {line!r}")
        if line.startswith("#"):
            # Un id prêt est `#123` (le reste de la ligne est une note) ; un
            # en-tête `# prêts`/`# erreurs` ou un commentaire n'en est pas un.
            ids.extend(int(found) for found in re.findall(r"#(\d+)\b", line))
            continue
        ids.extend(parse_id_list(line))
    both = sorted(set(ids) & set(errors))
    if both:
        raise InputError(
            "lots à la fois prêts et en erreur dans %s : %s"
            % (path, ", ".join("#%d" % lot for lot in both)))
    return list(dict.fromkeys(ids)), errors


def collect_ready(values, files) -> tuple[list[int], dict[int, str]]:
    """Ids prêts et lots en erreur, depuis les options et les fichiers (#3813)."""
    ids: list[int] = []
    for value in values:
        if isinstance(value, int):
            ids.append(value)
        else:
            ids.extend(parse_id_list(str(value)))
    errors: dict[int, str] = {}
    for name in files:
        file_ids, file_errors = read_ready_file(Path(name))
        ids.extend(file_ids)
        for lot, motive in file_errors.items():
            if lot in errors and errors[lot] != motive:
                raise InputError(
                    "motifs contradictoires pour #%d dans les listes prêtes : "
                    "%r / %r" % (lot, errors[lot], motive))
            errors[lot] = motive
    both = sorted(set(ids) & set(errors))
    if both:
        raise InputError(
            "lots à la fois prêts et en erreur : %s"
            % ", ".join("#%d" % lot for lot in both))
    return list(dict.fromkeys(ids)), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contrôle d'inclusion des lots d'un paquet §12 avant le feu."
    )
    parser.add_argument("--manifest", required=True,
                        help="manifeste de composition du paquet (reviews/…)")
    parser.add_argument("--head", required=True,
                        help="tête du paquet (SHA ou réf)")
    parser.add_argument("--repo", default=".", help="dépôt git (défaut : .)")
    parser.add_argument("--target", help="branche cible de l’instantané")
    parser.add_argument("--carrier", type=int, default=0, help="lot porteur de l’instantané")
    parser.add_argument("--expected", action="append", default=[],
                        help="lots attendus à la coupure (ids, virgules ou espaces)")
    parser.add_argument("--expected-file", action="append", default=[],
                        help="fichier de lots attendus (un id par ligne)")
    parser.add_argument("--ready", action="append", default=[],
                        help="lots prêts calculés (#2404 : paquet-candidats.py), "
                             "ids ou virgules")
    parser.add_argument("--ready-file", action="append", default=[],
                        help="fichier des lots prêts (paquet-candidats.py "
                             "--ready-out : ids prêts, puis !lot : motif pour "
                             "les lots en erreur)")
    parser.add_argument("--excused", action="append", default=[],
                        help="lots reportés/retirés hors manifeste (ids)")
    parser.add_argument("--excused-file", action="append", default=[],
                        help="fichier de lots reportés/retirés hors manifeste")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--strict", action="store_true",
                        help="un contenu partiel ou vide compte comme un défaut "
                             "bloquant")
    parser.add_argument("--no-expected", dest="no_expected", action="store_true",
                        help="assumer un contrôle sans liste attendue (admissions "
                             "non vérifiées)")
    parser.add_argument("--no-ready", dest="no_ready", action="store_true",
                        help="refusé (#2464) : la liste calculée des lots prêts "
                             "est exigée, y compris sur une recomposition")
    args = parser.parse_args(argv)

    for value in args.expected:
        if isinstance(value, str) and not parse_id_list(value):
            parser.error(f"--expected illisible : {value!r}")
    for value in args.excused:
        if isinstance(value, str) and not parse_id_list(value):
            parser.error(f"--excused illisible : {value!r}")
    for value in args.ready:
        if isinstance(value, str) and not parse_id_list(value):
            parser.error(f"--ready illisible : {value!r}")

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
