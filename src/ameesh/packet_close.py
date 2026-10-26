# SPDX-License-Identifier: AGPL-3.0-only
"""#2426 — fermer les lots unitaires d'un paquet à sa fusion (manifeste git).

Un paquet est composé en git seulement : sa branche `integration/**` porte un
commit de manifeste et les unités sont des commits de composition qui citent
leur lot (`ameesh-work: <id>`). Le paquet n'a pas de fiche et les unités n'ont
pas de `package_id` : ni la file de fusion ni `sync-github` ne peuvent les
fermer comme les lots d'une PR ordinaire. Le manifeste est pourtant sur la
cible après la fusion : c'est la liste des unités livrées.

    ameesh work sync-packets --git-dir D [--target T] [--dry-run] [--json]
    packet_close.sync(db, D, target=…)

Le relevé lit les manifestes `reviews/**paquet**.md` de la cible, retient les
lots de leur table de composition (une ligne de retrait ne livre rien), puis,
pour chacun encore ouvert, examine **son dernier gel** — le même que
`sync-merges` : un gel antérieur déjà fusionné ne prouve rien du travail
courant. La fusion est constatée par le contenu (`plan_git.probe` : ancêtre,
patch-id, squash, revert #1825) ; la fermeture passe par
`work.close_merged` avec le jalon de gel examiné (`frozen_id`), donc un nouveau
gel déclaré entre l'examen et l'écriture n'est jamais fermé (`refrozen`).

Seuls les lots du manifeste sont examinés : c'est le balayage ciblé qui manque
à `sync-merges` (tous les lots ouverts, plus de 120 s sur ameesh). Un lot sans
gel est rendu `no-freeze` et laissé aux autres relevés.

Le même balayage tourne chez l'exécuteur (`runner.packet_sweep_once`) sur les
projets `radar` de l'hôte : il rafraîchit la cible dans le clone privé du
radar (comme l'atterrissage) puis lit les manifestes de cette tête. Une cible
que personne ne fetch ne fermait jamais les unités d'un paquet fusionné.
"""
from __future__ import annotations

import re

from . import plan_git, storage, work

#: un manifeste de paquet : `reviews/integration-paquet-h-….md` (la convention
#: des paquets horaires ; le nom seul suffit, le contenu est vérifié ensuite)
MANIFEST = re.compile(r"(?i)^reviews/(?:[^/]+/)*[^/]*paquet[^/]*\.md$")
#: manifestes lus au plus (borne de coût ; une cible en porte des dizaines)
MAX_MANIFESTS = 200

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_LOT = re.compile(r"#(\d+)")
_SHA = re.compile(r"`?([0-9a-f]{7,64})`?")
#: une table ou une section de retrait exclut ses lots ; une ligne de la table
#: de composition reste une unité, même si sa note parle d'un revert : le
#: contenu est alors examiné (et un revert le fait refuser par `probe` #1825)
_RETRAIT_HEADER = re.compile(r"retir|revert", re.IGNORECASE)
#: un titre de section qui annonce des lots écartés (« Écartés (ce véhicule) »)
_RETRAIT_HEADING = re.compile(r"retir|revert|écart|ecart|exclu", re.IGNORECASE)
#: un titre qui dit explicitement l'absence de retrait (« Reports d'union
#: (aucun retrait) ») n'écarte rien
_NO_RETRAIT_HEADING = re.compile(
    r"(aucun|aucune|sans|pas de|pas d['’])\s+(note\s+de\s+)?"
    r"(retrait|revert|exclusion)", re.IGNORECASE)


class PacketError(RuntimeError):
    """Cible ou manifeste illisible : message actionnable."""


#: #3629 : l'avertissement d'une table de lots sans colonne « commit », avec
#: la forme que lisent `parse_manifest` et `release_cutoff` (section
#: `## Composition`) ; `scripts/paquet-compose.py --units` imprime ce gabarit.
GAP_HINT = ("a 'lot' column without a 'commit' column: these units are not read; "
            "use the '## Composition' table | unité | lot(s) | source | commit | note | "
            "(scripts/paquet-compose.py --units prints it), or close them with "
            "`ameesh work sync-merges --target <branch>`")


# --------------------------------------------------------------------------
# manifeste
# --------------------------------------------------------------------------

def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tables(lines: list[str]):
    """(titre courant, [(numéro de ligne, texte)]) pour chaque table markdown."""
    heading = ""
    index = 0
    while index < len(lines):
        found = _HEADING.match(lines[index])
        if found:
            heading = found.group(2)
        if _TABLE_ROW.match(lines[index]) and index + 1 < len(lines) \
                and _TABLE_SEP.match(lines[index + 1]):
            block: list[tuple[int, str]] = []
            cursor = index
            while cursor < len(lines) and _TABLE_ROW.match(lines[cursor]):
                block.append((cursor + 1, lines[cursor]))
                cursor += 1
            yield heading, block
            index = cursor
            continue
        index += 1


def _shas(cell: str) -> list[str]:
    """Les commits nommés dans une cellule (le premier est la source, les
    suivants peuvent être une résolution ou un re-gel)."""
    return [match.group(1) for match in _SHA.finditer(cell or "")]


def parse_manifest(text: str) -> list[dict]:
    """Les lignes de composition d'un manifeste.

    Rend `[{lots, sources, commits, withdrawn, line_no, note}]` ; `sources` et
    `commits` gardent tous les commits de leur cellule, dans l'ordre. Une table
    de retrait exclut ses lots même s'ils figurent ailleurs. Aucune table de
    composition : liste vide (un fichier qui n'est pas un manifeste n'échoue
    pas le relevé).
    """
    lines = (text or "").splitlines()
    entries: list[dict] = []
    withdrawn: set[int] = set()
    for heading, block in _tables(lines):
        if len(block) < 2:
            continue
        header = [cell.lower() for cell in _split_row(block[0][1])]
        lots_at = next((index for index, cell in enumerate(header) if "lot" in cell), None)
        commit_at = next((index for index, cell in enumerate(header) if "commit" in cell),
                         None)
        if lots_at is None:
            continue
        withdrawn_table = bool(
            (_RETRAIT_HEADING.search(heading)
             and not _NO_RETRAIT_HEADING.search(heading))
            or any(_RETRAIT_HEADER.search(cell) for cell in header))
        if withdrawn_table:
            for line_no, line in block[2:]:
                cells = _split_row(line)
                if lots_at < len(cells):
                    withdrawn.update(int(found) for found in _LOT.findall(cells[lots_at]))
            continue
        if commit_at is None:
            continue
        source_at = next((index for index, cell in enumerate(header) if "source" in cell),
                         None)
        note_at = next((index for index, cell in enumerate(header) if "note" in cell),
                       None)
        for line_no, line in block[2:]:
            cells = _split_row(line)
            if lots_at >= len(cells) or commit_at >= len(cells):
                continue
            lots = [int(found) for found in _LOT.findall(cells[lots_at])]
            if not lots:
                continue
            sources = _shas(cells[source_at]) if source_at is not None \
                and source_at < len(cells) else []
            commits = _shas(cells[commit_at])
            note = " | ".join(cells[note_at:]) if note_at is not None \
                and note_at < len(cells) else " | ".join(cells)
            entries.append({"lots": lots, "sources": sources, "commits": commits,
                            "withdrawn": False, "line_no": line_no, "note": note})
    for entry in entries:
        entry["withdrawn"] = entry["withdrawn"] or any(
            lot in withdrawn for lot in entry["lots"])
    return entries


def manifest_gaps(text: str) -> list[dict]:
    """#3629 : les tables qui citent des lots sans pouvoir les livrer.

    Un manifeste dont aucune table n'est lue comme composition (colonne
    « lot » sans colonne « commit ») rendait 0 unité en silence : ses lots
    restaient en qa après la fusion du paquet (#3514). Rend, pour un tel
    manifeste seulement, `[{line_no, heading, lots}]` de chaque table à
    colonne « lot » hors retrait ; un manifeste qui livre au moins une unité
    ne rend rien (ses autres tables — avis, admissions — ne sont pas des
    compositions).
    """
    if parse_manifest(text):
        return []
    gaps: list[dict] = []
    for heading, block in _tables((text or "").splitlines()):
        if len(block) < 2:
            continue
        header = [cell.lower() for cell in _split_row(block[0][1])]
        lots_at = next((index for index, cell in enumerate(header) if "lot" in cell), None)
        if lots_at is None or any("commit" in cell for cell in header):
            continue
        if (_RETRAIT_HEADING.search(heading) and not _NO_RETRAIT_HEADING.search(heading)) \
                or any(_RETRAIT_HEADER.search(cell) for cell in header):
            continue
        lots: list[int] = []
        for _line_no, line in block[2:]:
            cells = _split_row(line)
            if lots_at < len(cells):
                lots.extend(int(found) for found in _LOT.findall(cells[lots_at]))
        if lots:
            gaps.append({"line_no": block[0][0], "heading": heading,
                         "lots": list(dict.fromkeys(lots))})
    return gaps


def manifest_paths(repo: str, target: str) -> list[str]:
    """Les manifestes de paquet présents sur `target` (chemins, ordre stable)."""
    listing = plan_git._out(repo, "ls-tree", "-r", "--name-only", target, "reviews") or ""
    paths = [path for path in listing.splitlines() if MANIFEST.match(path)]
    return sorted(paths)[-MAX_MANIFESTS:]


def units(repo: str, target: str, gaps: list | None = None
          ) -> tuple[dict[int, list[dict]], int]:
    """{lot: [{manifest, sources, commits}…]} des unités livrées, et le nombre
    de manifestes lus.

    Une ligne de retrait ne livre pas son lot ; un lot peut apparaître dans
    plusieurs manifestes (paquet recomposé, lot rejoué) et garde toutes ses
    preuves (`sources` : têtes composées, `commits` : unités de composition).

    #3629 : `gaps`, si donné, reçoit `{manifest, line_no, heading, lots}` des
    manifestes qui ne livrent rien alors qu'une table cite des lots
    (`manifest_gaps`).
    """
    paths = manifest_paths(repo, target)
    lots: dict[int, list[dict]] = {}
    for path in paths:
        text = plan_git._out(repo, "show", "%s:%s" % (target, path))
        if text is None:
            continue
        if gaps is not None:
            gaps.extend(dict(gap, manifest=path) for gap in manifest_gaps(text))
        for entry in parse_manifest(text):
            if entry["withdrawn"]:
                continue
            for lot in entry["lots"]:
                lots.setdefault(int(lot), []).append(
                    {"manifest": path, "sources": entry["sources"],
                     "commits": entry["commits"]})
    return lots, len(paths)


def _delivering_unit(repo: str, target: str, units_rows: list[dict], lot: int) -> str | None:
    """Le commit de composition qui a livré `lot`, s'il est ancêtre de la cible
    et porte son trailer `ameesh-work: <lot>`.

    Preuve de secours d'un lot **sans gel** (une composition déclarée mais
    aucun jalon `frozen`) : le trailer nomme le lot, la composition est sur la
    cible, et un revert de ce commit l'annule (#1825). Un lot qui a un gel
    n'utilise pas cette voie : le gel courant fait seul autorité."""
    for unit in units_rows:
        for candidate in unit["commits"]:
            resolved = plan_git._commit(repo, candidate)
            if resolved is None:
                continue
            if plan_git._git(repo, "merge-base", "--is-ancestor", resolved,
                             target).returncode != 0:
                continue
            if plan_git._merge_reverted(repo, resolved, target) is not None:
                continue
            message = plan_git._out(repo, "log", "-1", "--format=%B", resolved) or ""
            if lot in plan_git.cited_lots(message):
                return resolved
    return None


# --------------------------------------------------------------------------
# fermeture
# --------------------------------------------------------------------------

def sync(db, repo_dir: str, *, target: str | None = None, dry_run: bool = False,
         actor: str = "") -> dict:
    """Ferme les lots unitaires des manifestes de paquet présents sur la cible.

    Seuls les lots encore ouverts et cités par un manifeste sont examinés ;
    chacun par son **dernier gel** (`plan_git.latest_freeze`), comme
    `sync-merges` mais sans balayer les autres lots. La cible est lue telle
    que le clone la connaît (aucun fetch ici ; le CLI reste en lecture seule).

    #2508 : les lots sont lus **par leur identifiant de manifeste**, jamais par
    `items(limit=1000)` : la borne globale pouvait laisser une unité du paquet
    au-delà de la limite, et la fermeture n'a besoin que des unités listées.
    """
    if target is None:
        try:
            target = plan_git.default_target(repo_dir)
        except (plan_git.GitError, plan_git.MergeProbeError) as exc:
            raise PacketError(str(exc)) from exc
        if not target:
            raise PacketError("no target: `git config %s <branch>` in %s, or --target"
                               % (plan_git.TARGET_GIT_KEY, repo_dir))
    try:
        if plan_git._commit(repo_dir, target) is None:
            raise PacketError("target %r not found in %s" % (target, repo_dir))
    except plan_git.GitError as exc:
        raise PacketError(str(exc)) from exc
    gaps: list[dict] = []
    lots, manifests = units(repo_dir, target, gaps)
    st = storage.of(db)
    open_rows: dict[int, dict] = {}
    for lot in lots:
        row = st.work.get(lot)
        if row is not None and row["state"] not in work.MERGED_STATES \
                and row["state"] != "closed":
            open_rows[int(row["id"])] = row
    examined = sorted(open_rows)
    declared: dict = {}
    for milestone in st.progress.lot_milestones(examined):
        declared.setdefault(int(milestone["work_item_id"]), []).append(milestone)
    results: list[dict] = []
    for lot in examined:
        row = open_rows[lot]
        freeze = plan_git.latest_freeze(declared.get(lot, []))
        sha = ((freeze or {}).get("sha") or "").strip()
        sources = ", ".join(sorted({unit["manifest"] for unit in lots[lot]}))
        entry = {"work_item": lot, "state": row["state"], "manifest": sources,
                 "sha": sha or None, "how": None, "ref": None, "result": "no-freeze",
                 "detail": "no freeze declared (milestone frozen --sha): left to the "
                           "other sweeps"}
        if not sha:
            unit = _delivering_unit(repo_dir, target, lots[lot], lot)
            if unit is None:
                results.append(entry)
                continue
            entry.update(how="trailer", ref=unit,
                         detail="composition %s carries `ameesh-work: %d`"
                                % (unit[:12], lot))
            if dry_run:
                retention = plan_git.dry_run_retention(db, st, row, sha=unit,
                                                       repo=repo_dir)
                if retention is not None and not retention.get("lift"):
                    plan_git.mark_retained(entry, retention, target=target,
                                           dry_run=True)
                    results.append(entry)
                    continue
                if retention is not None:
                    plan_git.mark_lifted(entry, retention)
                entry["result"] = "would-merge"
                results.append(entry)
                continue
            done = work.close_merged(
                db, lot, sha=unit, actor=actor or "packet:%s" % target,
                source="packet manifest (%s)" % sources, repo=repo_dir,
                content_guard=plan_git.content_retention)
            entry["result"] = done["result"]
            if done["result"] in ("withheld", "error"):
                plan_git.mark_retained(entry, done["retention"], target=target)
                if done["result"] == "withheld":
                    plan_git.record_content_withheld(
                        db, st, row, entry, done["retention"], target=target,
                        actor=actor or "packet:%s" % target)
            else:
                entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
            results.append(entry)
            continue
        try:
            found = plan_git.probe(repo_dir, sha, target, lot=lot)
        except (plan_git.MergeProbeError, plan_git.GitError) as exc:
            entry.update(result="error", detail=str(exc))
            results.append(entry)
            continue
        entry.update(how=found["how"], ref=found["ref"], detail=found["detail"])
        if not found["merged"]:
            entry["result"] = "reverted" if found["how"] == "reverted" else "open"
            results.append(entry)
            continue
        if dry_run:
            retention = plan_git.dry_run_retention(db, st, row,
                                                   sha=found["ref"] or sha, repo=repo_dir)
            if retention is not None and not retention.get("lift"):
                plan_git.mark_retained(entry, retention, target=target, dry_run=True)
                results.append(entry)
                continue
            if retention is not None:
                plan_git.mark_lifted(entry, retention)
            entry["result"] = "would-merge"
            results.append(entry)
            continue
        done = work.close_merged(
            db, lot, sha=found["ref"] or sha, actor=actor or "packet:%s" % target,
            source="packet manifest (%s)" % sources,
            frozen_id=int(freeze["id"]) if freeze and freeze.get("id") else None,
            repo=repo_dir, content_guard=plan_git.content_retention)
        entry["result"] = done["result"]
        if done["result"] in ("withheld", "error"):
            plan_git.mark_retained(entry, done["retention"], target=target)
            if done["result"] == "withheld":
                plan_git.record_content_withheld(
                    db, st, row, entry, done["retention"], target=target,
                    actor=actor or "packet:%s" % target)
        else:
            entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
        results.append(entry)
    return {"git_dir": repo_dir, "target": target, "dry_run": bool(dry_run),
            "manifests": manifests, "units": len(lots), "results": results,
            "merged": sum(1 for row in results if row["result"] == "merged"),
            "warnings": _gap_warnings(st, gaps)}


def _gap_warnings(st, gaps: list[dict]) -> list[dict]:
    """#3629 : les tables sans colonne « commit » qui citent un lot encore
    ouvert — le relevé ne les livre pas, et le dit. Un manifeste ancien dont
    les lots sont tous fermés ne rend plus d'avertissement."""
    out: list[dict] = []
    for gap in gaps:
        open_lots = []
        for lot in gap["lots"]:
            row = st.work.get(lot)
            if row is not None and row["state"] not in work.MERGED_STATES \
                    and row["state"] != "closed":
                open_lots.append(int(lot))
        if open_lots:
            out.append({"manifest": gap["manifest"], "line_no": gap["line_no"],
                        "heading": gap["heading"], "lots": open_lots,
                        "message": GAP_HINT})
    return out


def sync_project(cfg, db, project, *, dry_run: bool = False, actor: str = "") -> dict:
    """Le même relevé sur un projet `radar`, à la pointe fraîche de sa cible.

    Le clone privé du radar est rafraîchi par `land.fresh_target` (un fetch de
    la branche cible, comme un atterrissage) ; les manifestes sont lus à cette
    tête. Appelé sous le verrou de passage du projet (`radar.pass_lock`), le
    clone n'est pas modifié par un autre exécutant de l'hôte.
    """
    from . import land, radar, resolutions
    repo = getattr(project, "repo", "")
    target = getattr(project, "target", "") or None
    if not repo:
        raise PacketError("project %s has no repository" % getattr(project, "name", "?"))
    if not target:
        target = plan_git.default_target(repo)
    if not target:
        raise PacketError("no target for project %s: declare `target` or "
                           "`git config %s <branch>`" % (project.name,
                                                         plan_git.TARGET_GIT_KEY))
    # `land.fresh_target` fetche `refs/heads/<branche>` : une cible écrite
    # `origin/main`, `refs/remotes/origin/main`… perd ses préfixes
    target = plan_git.short_branch(target) or target
    replayer = resolutions.Replayer(repo, radar.clone_dir(cfg, project.name),
                                    target=project.target)
    replayer.ensure()
    tip = land.fresh_target(replayer, project, target)
    return sync(db, replayer.path, target=tip, dry_run=dry_run, actor=actor)
