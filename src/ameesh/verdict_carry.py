# SPDX-License-Identifier: AGPL-3.0-only
"""#2442 : un réalignement mécanique sur la cible ne redemande pas de verdict.

Cas du 18/10 (paquet 67 Nexlink, #2431) : deux avis favorables sur la tête
gelée, puis la branche est réalignée sur la cible (fusion de `develop`), gelée
à nouveau — et la porte (`merge_queue.review_gate`, qui ne compte que les
verdicts rendus **sur la tête gelée**) redemande deux verdicts, sans rien de
nouveau à relire. Le canon le dit déjà (§11 bis : « verdicts acquis : ni gel
répété, ni second avis sur delta ») ; ce module le fait sans jugement (§7).

**Critère** — déterministe et vérifiable : sur la pointe **fraîche** de la
cible `T`, la fusion d'essai de la tête relue `R` et celle de la nouvelle tête
gelée `F` sont toutes deux propres et donnent **le même arbre**. Atterrir `F`,
c'est alors atterrir `R` : rien de ce qui entre dans la cible n'a changé. Tout
autre écart laisse la porte exiger ses verdicts : un commit de l'auteur, une
fusion « maligne », le contenu d'une branche qui n'est pas dans la cible, et
une résolution de conflit (la tête relue ne fusionne plus proprement : la
résolution est un jugement, que seule la dispense du pilote couvre, #1979).
Une cible illisible, un commit absent ou une fusion d'essai ratée ne prouvent
rien : pas de report.

**Report** — pour chaque relecteur, son **dernier** verdict, s'il porte sur une
tête réalignée, est recopié en jalon `verdict` sur la tête gelée : au nom du
relecteur (sa famille compte à la porte, #1374), sans preuve de tour (#1465 :
ce n'est pas un tour du relecteur), avec la note `verdict carried: alignment
…`, et une ligne au journal du lot. Un **blocage se reporte** comme un avis
favorable : un réalignement ne lave jamais un blocage. Un relecteur qui a déjà
un verdict sur la tête gelée n'en reçoit pas ; le dernier verdict d'un
relecteur rendu sur une autre tête (non réalignée) n'est pas remplacé par un
plus ancien.

Le report se fait au gel (`ameesh work milestone <lot> frozen`, `carry`) et à
chaque relevé des branches de l'exécuteur (`run_pass`), sous le bail du feu du
lot (`fire:<lot>`) : jalons relus sous le bail, gel revérifié avant
l'écriture. Une issue définitive (reporté ou non) est journalisée une fois par
couple (tête relue, tête gelée) ; le passage suivant ne la recalcule pas.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from . import merge_queue, plan_git, storage, work

#: préfixe de la note du jalon reporté et de sa ligne de journal
CARRIED = "verdict carried: alignment"
#: préfixe de la ligne de journal d'un réalignement qui n'en est pas un
NOT_CARRIED = "verdict not carried:"
#: résultats qu'un passage de l'exécuteur dit dans son journal
LOUD = ("carried", "not-carried", "error")
#: lots examinés par passage
MAX_LOTS = 1000


def _norm(sha) -> str:
    return str(sha or "").strip().lower()


def _same(a, b) -> bool:
    """Même commit sans dépôt : égaux, ou l'un abrégé de l'autre."""
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _key(actor) -> str:
    return str(actor or "").strip().lower() or "?"


def pending(marks, frozen) -> dict:
    """{relecteur: dernier verdict} des relecteurs dont le dernier verdict porte
    sur une **autre** tête que `frozen` — les candidats au report. Un relecteur
    qui a déjà un verdict sur la tête gelée n'en est pas un : le sien fait foi.
    Sans dépôt : les têtes se comparent à l'égalité ou au préfixe."""
    frozen = _norm(frozen)
    if not frozen:
        return {}
    latest: dict = {}
    current: set = set()
    for mark in marks or ():
        if mark.get("kind") != "verdict" or not _norm(mark.get("sha")):
            continue
        key = _key(mark.get("actor"))
        if _same(mark.get("sha"), frozen):
            current.add(key)
        if key not in latest or merge_queue._mark_order(mark) > merge_queue._mark_order(latest[key]):
            latest[key] = mark
    return {key: mark for key, mark in latest.items()
            if key not in current and not _same(mark.get("sha"), frozen)}


def merged_tree(repo: str, target: str, head: str) -> str | None:
    """L'arbre de la fusion d'essai **propre** de `head` dans `target`, ou None
    (conflit, commit inconnu, git en échec). Les objets écrits vont dans un
    dossier jetable (le dépôt en alternates) : rien n'entre dans `.git` (L230).
    Les attributs de fusion se lisent dans l'arbre de la cible (#1517)."""
    from . import radar
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        return None
    scratch = tempfile.mkdtemp(prefix="ameesh-carry-")
    try:
        proc = radar._scratch_git(repo, objects, scratch, "-c", "attr.tree=%s" % target,
                                  "merge-tree", "--write-tree", "-z", "--name-only",
                                  "--no-messages", target, head)
    except radar.GitError:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return None
    lines = radar._lines(proc)
    return lines[0].strip() if lines else None


def realigned(repo: str, reviewed: str, frozen: str, tip: str) -> tuple:
    """`frozen` est-elle un réalignement mécanique de `reviewed` sur la cible
    de pointe `tip` ? Rend `(oui, raison)` ; la raison dit pourquoi non."""
    if _same(reviewed, frozen):
        return False, "same head"
    theirs = merged_tree(repo, tip, frozen)
    if theirs is None:
        return False, "the frozen head does not merge cleanly into the target"
    ours = merged_tree(repo, tip, reviewed)
    if ours is None:
        return False, ("the reviewed head no longer merges cleanly into the target: "
                       "the new head resolves a conflict (a judgment, not a realignment)")
    if ours != theirs:
        return False, ("landing the frozen head would not land the reviewed content: "
                       "the merge results differ")
    return True, "same merge result"


def _outcomes(db, lots) -> dict:
    """{lot: notes de journal des issues déjà rendues}, en une lecture."""
    out: dict = {int(lot): [] for lot in lots}
    try:
        rows = storage.of(db).work.events_noted("verdict ", 0.0, ids=sorted(out))
    except Exception:  # noqa: BLE001 - journal illisible : on recalcule
        return out
    for r in rows:
        note = str(r.get("note") or "")
        if note.startswith((CARRIED, NOT_CARRIED)) and int(r["work_item_id"]) in out:
            out[int(r["work_item_id"])].append(note)
    return out


def _arrow(reviewed: str, frozen: str) -> str:
    return "%s → %s" % (_norm(reviewed)[:12], _norm(frozen)[:12])


def _decided(notes, reviewed: str, frozen: str) -> bool:
    arrow = _arrow(reviewed, frozen)
    return any(arrow in note for note in notes)


def _clone(cfg, row: dict, repo: str):
    from . import radar, resolutions
    name = str(row.get("app") or "").strip() or os.path.basename(repo.rstrip("/"))
    return resolutions.Replayer(repo, radar.clone_dir(cfg, name))


def _passive(db, to, lot: int, text: str, data: dict) -> None:
    """Le mot au fil du lot, sans réveiller personne."""
    from . import mail
    if not to or str(to).startswith(work.HUMAN_PREFIX):
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                  payload={"radar": dict(data, kind="verdict-carry")})
    except Exception:  # noqa: BLE001 - un mot n'est jamais fatal
        pass


def carry(cfg, db, lot: int, *, repo: str | None = None, clone=None,
          dry_run: bool = False, lock: bool = True, notes=None) -> dict:
    """Reporte sur la tête gelée du lot les verdicts rendus sur une tête dont
    elle n'est qu'un réalignement mécanique (critère du module).

    Rend `{lot, result, frozen, target, tip, carried, refused, detail}` ;
    `result` : `idle` (rien à reporter), `carried`, `not-carried`,
    `no-repository`, `busy` (bail tenu ailleurs), `moved` (gel changé
    pendant le calcul), `error` (cible illisible…). `would-carry` en essai.
    `notes` : les issues déjà journalisées du lot, lues par l'appelant.
    Ne lève jamais."""
    lot = int(lot)
    out: dict = {"lot": lot, "result": "idle", "frozen": "", "target": "", "tip": "",
                 "carried": [], "refused": [], "detail": ""}
    try:
        return _carry(cfg, db, lot, out, repo=repo, clone=clone, dry_run=dry_run, lock=lock,
                      notes=notes)
    except Exception as exc:  # noqa: BLE001 - jamais fatal : retenté au passage suivant
        out.update(result="error", detail="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out


def _carry(cfg, db, lot: int, out: dict, *, repo, clone, dry_run: bool, lock: bool,
           notes) -> dict:
    from . import fire, lot_class
    row = work.get(db, lot)
    if row is None or row.get("state") in tuple(work.MERGED_STATES) + ("closed",):
        out["detail"] = "lot not open"
        return out
    marks = work.milestones(db, lot)
    freeze = plan_git.latest_freeze(marks)
    declared = _norm((freeze or {}).get("sha"))
    if not declared:
        out["detail"] = "not frozen"
        return out
    out["frozen"] = declared
    candidates = pending(marks, declared)
    if not candidates:
        out["detail"] = "no verdict to carry"
        return out
    if notes is None:
        notes = _outcomes(db, [lot])[lot]
    if all(_decided(notes, m.get("sha"), declared) for m in candidates.values()):
        out["detail"] = "already decided for this freeze"
        return out
    repo = repo or lot_class.repository(cfg, row)
    if not repo:
        out.update(result="no-repository", detail="no repository known for the lot on this "
                   "host: the executor's branch sweep retries")
        return out
    target = lot_class.default_base(cfg, row, repo)
    if not target:
        out.update(result="error", detail="no target branch known for the lot")
        return out
    out["target"] = merge_queue._remote_branch(target)
    names = [target] + ([row["branch"]] if row.get("branch") else [])
    tips, where = merge_queue.fetch_fresh(repo, names, clone or _clone(cfg, row, repo))
    tip = tips.get(target)
    if not tip:
        out.update(result="error", detail="target %s not found on origin" % out["target"])
        return out
    out["tip"] = tip
    frozen = merge_queue.pinned(where, declared)
    if not frozen:
        out.update(result="error", detail="the freeze %s is not a commit of %s"
                   % (declared[:12], where))
        return out
    out["frozen"] = frozen
    verdicts: dict = {}          # tête relue complète -> (oui, raison)
    plan: list = []              # (relecteur, verdict reporté)
    refused: list = []           # (tête relue, raison)
    for key, mark in sorted(candidates.items()):
        if _decided(notes, mark.get("sha"), frozen):
            continue
        reviewed = merge_queue.pinned(where, mark.get("sha"))
        if not reviewed:
            out["detail"] = "reviewed head %s is not a commit of %s" % (
                _norm(mark.get("sha"))[:12], where)
            continue                  # objet absent : rien de prouvé, retenté
        if reviewed not in verdicts:
            verdicts[reviewed] = realigned(where, reviewed, frozen, tip)
        ok, why = verdicts[reviewed]
        if ok:
            plan.append((mark, reviewed))
        elif (reviewed, why) not in refused:
            refused.append((reviewed, why))
    if not plan and not refused:
        if not out["detail"]:
            out["detail"] = "nothing decidable"
        return out
    if dry_run:
        out.update(result="would-carry" if plan else "not-carried",
                   carried=[_entry(m, r) for m, r in plan],
                   refused=[{"reviewed": r, "why": w} for r, w in refused])
        return out
    handle = fire._lock(db, lot) if lock else None
    if lock and handle is None:
        out.update(result="busy", detail="another host holds the lease of lot #%d" % lot)
        return out
    try:
        return _write(db, lot, out, declared, plan, refused, where)
    finally:
        if handle is not None:
            fire._unlock(handle)


def _entry(mark: dict, reviewed: str) -> dict:
    return {"actor": str(mark.get("actor") or ""), "verdict": mark.get("verdict") or "ok",
            "from": reviewed}


def _write(db, lot: int, out: dict, declared: str, plan: list, refused: list,
           where: str) -> dict:
    """Écrit les jalons reportés et les issues, sous le bail : jalons relus,
    gel revérifié — un gel plus récent ou un verdict rendu entre-temps gagnent."""
    row = work.get(db, lot)
    marks = work.milestones(db, lot)
    freeze = plan_git.latest_freeze(marks)
    frozen, tip, target = out["frozen"], out["tip"], out["target"]
    if row is None or not freeze or not merge_queue._same_sha(freeze.get("sha"), frozen, where):
        out.update(result="moved", detail="the freeze changed meanwhile: retried next pass")
        return out
    still = pending(marks, declared)
    st = storage.of(db).work
    carried = []
    for mark, reviewed in plan:
        key = _key(mark.get("actor"))
        now = still.get(key)
        if now is None or now.get("id") != mark.get("id"):
            continue                  # verdict rendu ou reporté entre-temps
        verdict = mark.get("verdict") or "ok"
        st.add_milestone(
            lot, "verdict", sha=frozen, actor=str(mark.get("actor") or ""), verdict=verdict,
            note="%s %s on %s@%s, same merge result as the reviewed head (#2442)"
                 % (CARRIED, _arrow(reviewed, frozen), target, tip[:12]))
        carried.append(_entry(mark, reviewed))
    state = row.get("state") or ""
    by_head: dict = {}
    for entry in carried:
        by_head.setdefault(entry["from"], []).append(entry)
    for reviewed, entries in by_head.items():
        st.note(lot, state, "%s %s on %s@%s: %s — no re-review due (#2442)"
                % (CARRIED, _arrow(reviewed, frozen), target, tip[:12],
                   ", ".join("%s %s" % (e["verdict"], e["actor"]) for e in entries)),
                work.SYSTEM_SENDER, once=True)
    for reviewed, why in refused:
        st.note(lot, state, "%s %s on %s@%s: %s — verdicts are due on the frozen head (#2442)"
                % (NOT_CARRIED, _arrow(reviewed, frozen), target, tip[:12], why),
                work.SYSTEM_SENDER, once=True)
    out.update(carried=carried, refused=[{"reviewed": r, "why": w} for r, w in refused],
               result="carried" if carried else "not-carried" if refused else "idle")
    if carried:
        text = ("Lot #%d « %s »: the new freeze %s is a mechanical realignment on %s "
                "(same merge result as the reviewed head); verdicts carried: %s. "
                "No re-review is due (#2442)."
                % (lot, row.get("title") or "", frozen[:12], target,
                   ", ".join("%s %s from %s" % (e["verdict"], e["actor"], e["from"][:12])
                             for e in carried)))
        data = {"project": row.get("app") or "", "target": target, "sha": frozen}
        for to in [row.get("assignee")] + [e["actor"] for e in carried]:
            if to:
                _passive(db, to, lot, text, data)
    return out


def describe(result: dict) -> str:
    """Une ligne lisible du résultat de `carry` (CLI, journal de l'exécuteur)."""
    kind = result.get("result")
    if kind in ("carried", "would-carry"):
        return "verdicts carried (alignment on %s@%s): %s — no re-review due" % (
            result.get("target"), _norm(result.get("tip"))[:12],
            ", ".join("%s %s from %s" % (e["verdict"], e["actor"], e["from"][:12])
                      for e in result.get("carried") or ()))
    if kind == "not-carried":
        return "verdicts not carried: %s" % "; ".join(
            "%s: %s" % (e["reviewed"][:12], e["why"]) for e in result.get("refused") or ())
    return "verdicts not carried (%s): %s" % (kind, result.get("detail") or "")


def run_pass(cfg, db, *, dry_run: bool = False, limit: int = MAX_LOTS) -> list[dict]:
    """Un passage sur les lots ouverts dont la tête gelée attend un verdict
    qu'une tête précédente porte (`pending`). Le tri sans git écarte tous les
    autres : seuls ces lots coûtent une récupération et deux fusions d'essai.
    Ne lève jamais pour un lot."""
    st = storage.of(db)
    rows = [r for r in st.work.open_for_sweep(limit)
            if r.get("state") not in tuple(work.MERGED_STATES) + ("closed",)]
    if not rows:
        return []
    marks: dict = {}
    for mark in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    wanted = []
    for row in rows:
        lot = int(row["id"])
        freeze = plan_git.latest_freeze(marks.get(lot, []))
        if freeze and pending(marks.get(lot, []), freeze.get("sha")):
            wanted.append(lot)
    if not wanted:
        return []
    notes = _outcomes(db, wanted)
    out = []
    for lot in wanted:
        result = carry(cfg, db, lot, dry_run=dry_run, notes=notes.get(lot))
        if result.get("result") != "idle":
            out.append(result)
    return out
