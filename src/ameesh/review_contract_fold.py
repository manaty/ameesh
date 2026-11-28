# SPDX-License-Identifier: AGPL-3.0-only
"""#8395/#8417 — la vérification du commit de pliage du contrat module v1.

Le composeur (``scripts/paquet-auto.py``) plie les fragments de révision dans
``docs/api/module-contract-v1.md`` par un commit simple cité au manifeste du
paquet (section « Contrat module v1 »). Deux lecteurs vérifient ce commit avec
le même code :

* le contrôle d'inclusion (``scripts/paquet-inclusion.py``, #8395) : les lignes
  que le pliage a retirées ne sont pas exigées à la tête ;
* la porte du véhicule (``packet_reviews.own_changes``, #8417) : un pliage
  vérifié n'est pas un delta propre du véhicule (§7 : code déterministe ;
  §11 bis : pas de second avis sur un delta acquis). Un pliage non vérifiable
  reste un delta propre, sans repli silencieux.

Code de la porte : le nom ``review_*`` le garde critique (deux familles,
``merge_queue.CRITICAL_SCOPE``) comme ``scripts/paquet-inclusion.py`` d'où il
vient. Lecture seule : aucun réseau, aucune écriture dans le dépôt.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


class GitError(RuntimeError):
    pass


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


def blob_lines(repo: Path, rev: str, path: str) -> set[str] | None:
    """Les lignes du fichier à cette révision, ou None s'il est absent/binaire."""
    proc = git(repo, "show", f"{rev}:{path}", check=False)
    if proc.returncode != 0:
        return None
    data = proc.stdout
    if b"\0" in data[:8000]:
        return None
    return set(data.decode("utf-8", "replace").splitlines())


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

    La liste commence à la première ligne « Revision 1.N adds » et s'arrête à
    la première ligne vide, comme le pliage (#8395). Une ligne
    « Revision 1.8 adds … » en tête de section, ou le paragraphe de mapping qui
    suit le bloc, n'en fait donc pas partie (N).
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
        if not lines[index].strip() or lines[index].startswith("## "):
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


def _revision_parts(number: str) -> tuple[int, int] | None:
    """``(majeur, mineur)`` d'un numéro de révision ``1.N``, ou None."""
    major, _dot, minor = str(number).partition(".")
    if not major.isdigit() or not minor.isdigit():
        return None
    return int(major), int(minor)


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

    # #8417 (B cx2, dev5) : un fragment cité est retiré par le pliage, jamais
    # réécrit ni ajouté ; sa ligne garderait sinon un contenu non relu.
    statuses = dict((path, kind) for kind, path in status)
    for path in sorted(changed & fragments):
        if statuses.get(path) != "D":
            return {}, ["commit de pliage du contrat %s : le fragment %s n'est "
                        "pas retiré (statut %s) : refusé"
                        % (sha, path, statuses.get(path) or "?")]

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
    # #8417 (B dev5) : les trois versions de l'arbre plié valent la dernière
    # révision citée au manifeste, pas seulement entre elles : un pliage qui
    # ne change qu'un composant, ou vers un autre numéro, est refusé.
    if not revisions:
        errors.append("commit de pliage %s : aucune révision citée au "
                      "manifeste" % sha)
    else:
        cited = revisions[-1][0]
        for rel, pattern, name in ((CONTRACT_REL, CONTRACT_HEADER_LINE_RE,
                                    "en-tête"),
                                   (VERSION_REL, CONTRACT_VERSION_RE,
                                    "CONTRACT_VERSION"),
                                   (SURFACE_REL, CONTRACT_SURFACE_RE,
                                    "surface")):
            folded_lines = blob_lines(repo, commit, rel)
            if folded_lines is None:
                if rel == CONTRACT_REL:
                    errors.append("commit de pliage %s : %s illisible"
                                  % (sha, rel))
                continue
            values = {match.group(1) for match in
                      (pattern.match(line) for line in folded_lines) if match}
            if values != {cited}:
                errors.append(
                    "commit de pliage %s : %s %s, la révision citée est %s"
                    % (sha, name, ", ".join(sorted(values)) or "absent",
                       cited))
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
    # #8417 : le composeur attribue des révisions consécutives depuis l'en-tête
    # de la base du paquet ; la révision qui précède la première citée se
    # déduit donc de son numéro. L'en-tête du parent du pliage ne la donne
    # pas : il est en ligne 3 du contrat (``^`` sans MULTILINE ne le trouvait
    # jamais), et un lot à l'ancienne manière l'a peut-être déjà avancé.
    numbers = [_revision_parts(number) for number, _lot, _kind in revisions]
    for before, after in zip(numbers, numbers[1:]):
        if (before is None or after is None or after[0] != before[0]
                or after[1] != before[1] + 1):
            errors.append("commit de pliage %s : révisions citées non "
                          "consécutives au manifeste" % sha)
            break
    header_before = None
    if numbers and numbers[0] is not None and numbers[0][1] > 0:
        header_before = "%d.%d" % (numbers[0][0], numbers[0][1] - 1)
    expected_added: list[str] = []
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
        expected_added.extend(line for line in blocks.splitlines()
                              if line.strip())
        missing = [line for line in blocks.splitlines()
                   if line.strip() and head_contract is not None
                   and line not in head_contract]
        if missing:
            errors.append("fragment %s : ligne(s) pliée(s) absente(s) du "
                          "contrat à la tête : %s"
                          % (path, missing[0].strip()[:80]))

    # #8417 (B cx2, dev5) : chaque ligne ajoutée au contrat est justifiée par
    # le pliage, compte tenu de ses répétitions : l'en-tête, une ligne pliée
    # d'un fragment cité, ou une ligne retirée réécrite (renumérotée, même
    # contenu au numéro et à la dernière référence additive près, ou
    # déplacée). Toute autre ligne est un contenu que personne n'a relu.
    if CONTRACT_REL in changed and not errors:
        pool = Counter(expected_added)
        moved = Counter(_norm_revision_line(line)
                        for line in removed.get(CONTRACT_REL) or []
                        if line.strip())
        for line in added.get(CONTRACT_REL) or []:
            if not line.strip() or CONTRACT_HEADER_LINE_RE.match(line):
                continue
            if pool[line] > 0:
                pool[line] -= 1
                continue
            norm = _norm_revision_line(line)
            if moved[norm] > 0:
                moved[norm] -= 1
                continue
            errors.append(
                "commit de pliage %s : %s ajoute une ligne hors du pliage "
                "attendu : %s" % (sha, CONTRACT_REL, line.strip()[:80]))
            break

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


def verified_fold_commit(repo: Path, head: str, text: str
                         ) -> tuple[str | None, list[str]]:
    """#8417 : le commit de pliage cité au manifeste, s'il est vérifié.

    Rend ``(sha complet, [])`` quand le manifeste ``text`` cite un commit de
    pliage et que :func:`contract_fold_paths` le vérifie sur ``head`` (commit
    simple, ancêtre, borné au contrat, à la surface, à ``CONTRACT_VERSION`` et
    aux fragments cités ; contenu identique au numéro et à la dernière
    référence additive près). ``(None, [])`` sans pliage cité ;
    ``(None, erreurs)`` quand le pliage cité ne se vérifie pas.
    """
    fold = contract_fold(text)
    if fold is None:
        return None, []
    _table, errors = contract_fold_paths(repo, head, text)
    if errors:
        return None, errors
    commit = rev_parse(repo, fold[0])
    if commit is None:
        return None, [f"commit de pliage du contrat {fold[0]} introuvable"]
    return commit, []
