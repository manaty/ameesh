# SPDX-License-Identifier: AGPL-3.0-only
"""#2442 : un réalignement mécanique sur la cible ne redemande pas de verdict.

Cas du 18/10 (paquet 67 Nexlink, #2431) : deux avis favorables sur la tête
gelée, puis la branche est réalignée sur la cible (fusion de `develop`), gelée
à nouveau — et la porte (`merge_queue.review_gate`, qui ne compte que les
verdicts rendus **sur la tête gelée**) redemande deux verdicts, sans rien de
nouveau à relire. Le canon le dit déjà (§11 bis : « verdicts acquis : ni gel
répété, ni second avis sur delta ») ; ce module le fait sans jugement (§7).

**Critère** — déterministe et vérifiable, deux preuves sur la pointe
**fraîche** de la cible `T` :

1. *même atterrissage* : la fusion d'essai de la tête relue `R` et celle de la
   nouvelle tête gelée `F` sont toutes deux propres et donnent **le même
   arbre** — atterrir `F`, c'est atterrir `R` aujourd'hui ;
2. *même delta de lot* (R1 cx1, P1-2) : le diff de chaque tête depuis sa base
   de fusion avec `T` a **le même patch-id verbatim** (`plan_git.
   rebase_identity`, #1905, la preuve des candidats de paquet). C'est la preuve
   structurelle qui rend le report durable : un contenu que l'auteur ajoute à
   l'identique de la cible, sans en descendre, est masqué par `T` dans la
   preuve 1 mais change le delta — et réapparaîtrait si la cible le retirait.
   N1 (#2486) : un commit relu que la cible porte **déjà** — sa fusion dans `T`
   ne change pas l'arbre de `T` — peut manquer à `F`, un rebase l'ayant
   abandonné comme déjà appliqué, sans que le delta du lot change. Il est alors
   ignoré ; tout commit relu ni rejoué ni déjà atterri refuse le report.

Tout autre écart laisse la porte exiger ses verdicts : un commit de l'auteur,
une fusion « maligne », le contenu d'une branche qui n'est pas dans la cible,
une résolution de conflit (la tête relue ne fusionne plus proprement : la
résolution est un jugement, que seule la dispense du pilote couvre, #1979), et
par prudence un réalignement dont les lignes de contexte ont bougé près des
morceaux du lot. Une cible illisible, un commit absent ou une fusion d'essai
ratée ne prouvent rien : pas de report. Un **clone superficiel** ne prouve rien
non plus (N2 #2486) : `mechanical_history` dépend de l'histoire complète, comme
`train_history` (`rev-parse --is-shallow-repository`).

**Report** — pour chaque relecteur, son **dernier** verdict, s'il porte sur une
tête réalignée, est recopié en jalon `verdict` sur la tête gelée : au nom du
relecteur (sa famille compte à la porte, #1374), sans preuve de tour (#1465 :
ce n'est pas un tour du relecteur), **daté exactement de son original**.
Tout geste réel ultérieur du même relecteur reste plus récent, même si sa
transaction a commencé avant l'écriture du report (`now()` date le début de
transaction). L'identifiant départage la copie de l'original à date égale.
La copie porte la note `verdict carried: alignment …`, avec une
ligne au journal du lot. Un **blocage se reporte** comme un avis
favorable : un réalignement ne lave jamais un blocage. Un relecteur qui a déjà
un verdict sur la tête gelée n'en reçoit pas ; le dernier verdict d'un
relecteur rendu sur une autre tête (non réalignée) n'est pas remplacé par un
plus ancien.

Le report se fait au gel (`ameesh work milestone <lot> frozen`, `carry`) et à
chaque relevé des branches de l'exécuteur (`run_pass`). Les calculs git
(récupération de la pointe, fusions d'essai, patch-ids) sont **en lecture
seule** — clone du radar et objets jetables (L230) — et peuvent donc se
chevaucher entre passages et hôtes : le bail `fire:<lot>` ne les couvre pas.
L'écriture (R1 cx1, P1-1) est sérialisée : `carry` prend ce bail juste avant
d'écrire, et la transaction tient **le verrou de ligne du lot** — celui que
prend `add_milestone` pour tout gel et tout verdict — dans une seule
transaction : gel et verdicts y sont relus, et un gel ou un verdict concurrent
passe entièrement avant ou après.
Une issue définitive (reporté ou non) est journalisée une fois par
couple (tête relue, tête gelée) ; le passage suivant ne la recalcule pas.
"""
from __future__ import annotations

import os
import re
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
        order = merge_queue._mark_order
        if key not in latest or order(mark) > order(latest[key]):
            latest[key] = mark
    return {key: mark for key, mark in latest.items()
            if key not in current and not _same(mark.get("sha"), frozen)}


def merged_tree(repo: str, target: str, head: str, *, base: str | None = None,
                attributes: str | None = None) -> str | None:
    """L'arbre de la fusion d'essai **propre** de `head` dans `target`, ou None
    (conflit, commit inconnu, git en échec). `base` impose la base de fusion
    (un cherry-pick : `base` = parent du commit repris). Les objets écrits vont
    dans un dossier jetable (le dépôt en alternates) : rien n'entre dans `.git`
    (L230). Les attributs de fusion se lisent dans l'arbre de `attributes`, la
    cible par défaut (#1517)."""
    from . import radar
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        return None
    extra = ["--merge-base", base] if base else []
    scratch = tempfile.mkdtemp(prefix="ameesh-carry-")
    try:
        proc = radar._scratch_git(repo, objects, scratch,
                                  "-c", "attr.tree=%s" % (attributes or target),
                                  "merge-tree", "--write-tree", "-z", "--name-only",
                                  "--no-messages", *extra, target, head)
    except radar.GitError:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return None
    lines = radar._lines(proc)
    return lines[0].strip() if lines else None


#: commits nouveaux examinés au plus pour une preuve d'historique (borne de coût)
MAX_NEW_COMMITS = 200


def _lines_of(repo: str, *args: str) -> list[str] | None:
    proc = plan_git._git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").split()


def _complete_history(repo: str) -> bool:
    """L'histoire complète est-elle lisible ? Un clone superficiel ne prouve
    rien (N2 qa-01 #2486) : ses `rev-list` omettent des commits sans le dire.
    Même garde que `train_history` (`rev-parse --is-shallow-repository`)."""
    return plan_git._out(repo, "rev-parse", "--is-shallow-repository") == "false"


#: commits relus abandonnés examinés au plus pour la preuve « déjà atterri »
MAX_DROPPED_COMMITS = 200


def _own_delta(repo: str, head: str, tip: str) -> dict | None:
    """{patch-id verbatim: commit} des commits non-fusions propres à `head`
    depuis sa base de fusion avec `tip`, ou None si le delta est illisible."""
    base = plan_git._out(repo, "merge-base", head, tip)
    if not base:
        return None
    try:
        return plan_git._patch_ids(repo, "%s..%s" % (base, head))
    except Exception:  # noqa: BLE001 - patch-id illisible : rien de prouvé
        return None


def _landed(repo: str, tip: str, commit: str) -> bool | None:
    """Le commit `commit` est-il **déjà atterri** dans la cible `tip` ? Vrai si
    sa fusion dans `tip` laisse l'arbre de `tip` intact, None si la preuve est
    illisible (conflit, objet absent). Un commit de même patch-id dans
    l'historique ne suffit pas : si la cible l'a annulé depuis, la fusion le
    réintroduit et la réponse est non (N1 #2486)."""
    tree = plan_git._out(repo, "rev-parse", tip + "^{tree}")
    if not tree:
        return None
    merged = merged_tree(repo, tip, commit)
    if merged is None:
        return None
    return merged == tree


def _same_delta_with_landed(repo: str, reviewed: str, frozen: str,
                            tip: str) -> tuple:
    """N1 qa-01 #2486 : le delta propre du lot est constant si chaque commit
    relu est **rejoué** dans `frozen` (même patch-id verbatim) ou **déjà
    atterri** dans la cible (sa fusion ne change pas l'arbre de `tip`). Un
    rebase abandonne alors un commit déjà appliqué sans changer l'atterrissage.
    Rend `(oui, raison)` ; aucun report sur un doute ni sur un commit relu
    disparu sans preuve d'atterrissage."""
    old = _own_delta(repo, reviewed, tip)
    new = _own_delta(repo, frozen, tip)
    if old is None or new is None:
        return False, "the lot's own delta cannot be read"
    if not old:
        return False, "the lot has no commit of its own"
    added = [pid for pid in new if pid not in old]
    if added:
        return False, ("the new head brings %d commit(s) outside the reviewed delta"
                       % len(added))
    dropped = [commit for pid, commit in old.items() if pid not in new]
    if not dropped:
        return False, "no reviewed commit is missing from the new head"
    if len(dropped) > MAX_DROPPED_COMMITS:
        return False, "too many dropped commits to prove (%d)" % len(dropped)
    for commit in sorted(dropped):
        landed = _landed(repo, tip, commit)
        if landed is None:
            return False, ("the merge of reviewed commit %s into the target cannot be "
                           "read" % commit[:12])
        if not landed:
            return False, ("reviewed commit %s is neither replayed nor already landed "
                           "in the target" % commit[:12])
    return True, "%d reviewed commit(s) already landed in the target ignored" % len(dropped)


def mechanical_history(repo: str, reviewed: str, frozen: str, tip: str) -> tuple:
    """R1 cx1/qa-01 : chaque commit atteignable depuis `frozen` mais ni depuis
    `reviewed` ni depuis la cible `tip` est **mécanique** — rien de ce que la
    nouvelle tête apporte à l'historique n'est un contenu neuf :

    * une fusion à deux parents dont l'arbre est **exactement** la fusion
      d'essai propre de ses parents (ni conflit résolu, ni retouche) ;
    * un commit à un parent qui reprend proprement (cherry-pick, rebase) un
      commit de l'historique relu de même patch-id : son arbre est la fusion
      d'essai du commit repris sur son parent, avec pour base le parent repris.

    Une copie à la main d'un fichier de la cible, un ajout suivi d'un retrait
    (un secret resterait dans l'historique), une fusion « maligne » ou le
    contenu d'une autre branche sont refusés. Un clone superficiel l'est aussi
    (N2 #2486) : l'histoire manquante ferait passer un commit pour neuf. Rend
    `(oui, raison)`."""
    if not _complete_history(repo):
        return False, "complete Git history required; shallow clone refused"
    new = _lines_of(repo, "rev-list", "--topo-order", "--reverse", frozen, "--not",
                   reviewed, tip)
    if new is None:
        return False, "the new commits cannot be listed"
    if len(new) > MAX_NEW_COMMITS:
        return False, "too many new commits to prove (%d)" % len(new)
    sources = None
    for commit in new:
        parents = _lines_of(repo, "rev-list", "--parents", "-n", "1", commit)
        tree = plan_git._out(repo, "rev-parse", commit + "^{tree}")
        if not parents or not tree:
            return False, "commit %s cannot be read" % commit[:12]
        parents = parents[1:]
        if len(parents) == 2:
            if merged_tree(repo, parents[0], parents[1], attributes=parents[0]) != tree:
                return False, ("merge %s is not the clean, unmodified merge of its parents "
                               "(conflict resolved or content added)" % commit[:12])
            continue
        if len(parents) != 1:
            return False, "commit %s has %d parents" % (commit[:12], len(parents))
        if sources is None:
            try:
                sources = plan_git._patch_ids(repo, reviewed, "--not", tip)
            except Exception:  # noqa: BLE001 - patch-id illisible : rien de prouvé
                return False, "the reviewed commits' patch-ids cannot be read"
        try:
            pid = plan_git._diff_patch_id(repo, parents[0], commit)
        except Exception:  # noqa: BLE001 - idem
            pid = None
        source = sources.get(pid) if pid else None
        origin = _lines_of(repo, "rev-list", "--parents", "-n", "1", source) if source else None
        if not origin or len(origin) != 2:
            return False, ("commit %s is not a replay of a reviewed commit: new content "
                           "in the history" % commit[:12])
        if merged_tree(repo, parents[0], source, base=origin[1],
                       attributes=parents[0]) != tree:
            return False, ("commit %s is not the clean replay of reviewed commit %s"
                           % (commit[:12], source[:12]))
    return True, "%d mechanical commit(s)" % len(new)


def realigned(repo: str, reviewed: str, frozen: str, tip: str) -> tuple:
    """`frozen` est-elle un réalignement mécanique de `reviewed` sur la cible
    de pointe `tip` ? Trois preuves : même atterrissage, historique mécanique,
    même delta de lot. Rend `(oui, raison)` ; la raison dit pourquoi non. Un
    clone superficiel est refusé d'emblée (N2 #2486)."""
    if not _complete_history(repo):
        return False, "complete Git history required; shallow clone refused"
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
    history, why = mechanical_history(repo, reviewed, frozen, tip)
    if not history:
        return False, why
    # R1 cx1, P1-2 : le delta propre du lot est le même des deux côtés
    # (`plan_git.rebase_identity`, #1905 : patch-id verbatim du diff écrasé
    # depuis la base de chaque tête avec la cible).
    same = plan_git.rebase_identity(repo, reviewed, frozen, tip)
    reason = "same merge result, mechanical history and same lot delta"
    if not same.get("mechanical"):
        # N1 qa-01 #2486 : un rebase peut abandonner un commit relu déjà
        # atterri dans la cible ; il ne compte alors pas dans le delta.
        ok, why = _same_delta_with_landed(repo, reviewed, frozen, tip)
        if not ok:
            return False, ("the lot's own delta differs from the reviewed one (%s): %s"
                           % (same.get("detail") or "?", why))
        reason += " (%s)" % why
    return True, reason


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


_ARROW_RE = re.compile(r"\b([0-9a-f]{4,64}) → ([0-9a-f]{4,64})\b")


def _decided(notes, reviewed: str, frozen: str) -> bool:
    """Une issue est-elle déjà journalisée pour ce couple de têtes ? Les têtes
    se comparent au préfixe (N1 qa-01) : un verdict qui cite un SHA abrégé
    retrouve l'issue écrite avec le SHA résolu, sans nouveau calcul."""
    for note in notes or ():
        for left, right in _ARROW_RE.findall(note):
            if _same(left, reviewed) and _same(right, frozen):
                return True
    return False


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
          dry_run: bool = False, lock: bool = True, notes=None,
          fetched: dict | None = None) -> dict:
    """Reporte sur la tête gelée du lot les verdicts rendus sur une tête dont
    elle n'est qu'un réalignement mécanique (critère du module).

    Rend `{lot, result, frozen, target, tip, carried, refused, detail}` ;
    `result` : `idle` (rien à reporter), `carried`, `not-carried`,
    `no-repository`, `busy` (bail tenu ailleurs), `moved` (gel changé
    pendant le calcul), `error` (cible illisible…). `would-carry` en essai.
    `notes` : les issues déjà journalisées du lot, lues par l'appelant ;
    `fetched` : les pointes de cible déjà récupérées pendant ce passage, par
    `(dépôt, cible)` (N2 qa-01 : une récupération par projet, pas par lot) ;
    `lock` : prend le bail `fire:<lot>` pour l'écriture **seule** — les calculs
    git sont des lectures concurrentes (N3 qa-01 #2486).
    Ne lève jamais."""
    lot = int(lot)
    out: dict = {"lot": lot, "result": "idle", "frozen": "", "target": "", "tip": "",
                 "carried": [], "refused": [], "detail": ""}
    try:
        return _carry(cfg, db, lot, out, repo=repo, clone=clone, dry_run=dry_run, lock=lock,
                      notes=notes, fetched={} if fetched is None else fetched)
    except Exception as exc:  # noqa: BLE001 - jamais fatal : retenté au passage suivant
        out.update(result="error", detail="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out


def _carry(cfg, db, lot: int, out: dict, *, repo, clone, dry_run: bool, lock: bool,
           notes, fetched: dict) -> dict:
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
    clone = clone or _clone(cfg, row, repo)
    key = (repo, out["target"])
    if key not in fetched:
        tips, where = merge_queue.fetch_fresh(repo, [target], clone)
        fetched[key] = (tips.get(target), where)
    tip, where = fetched[key]
    if not tip:
        out.update(result="error", detail="target %s not found on origin" % out["target"])
        return out
    out["tip"] = tip
    if merge_queue.pinned(where, declared) is None and row.get("branch"):
        merge_queue.fetch_fresh(repo, [row["branch"]], clone)   # objets de la tête gelée
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
    # N3 qa-01 #2486 : le bail ne couvre que l'écriture. Les preuves ci-dessus
    # sont des lectures (clone du radar, objets jetables) et peuvent se
    # chevaucher entre passages ; la transaction de `_write` sérialise la copie.
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


_MARKS_SQL = ("SELECT id, kind, sha, actor, verdict,"
              "       extract(epoch from at)::float8 AS at_ts"
              "  FROM work_item_milestones"
              " WHERE work_item_id = %s AND kind IN ('frozen', 'verdict')")

_COPY_SQL = ("INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, verdict, note, at)"
             " SELECT work_item_id, 'verdict', %s, actor, verdict, %s,"
             "        at"
             "   FROM work_item_milestones"
             "  WHERE id = %s AND work_item_id = %s AND kind = 'verdict'"
             " RETURNING id")


def _write(db, lot: int, out: dict, declared: str, plan: list, refused: list,
           where: str) -> dict:
    """Écrit les jalons reportés dans **une** transaction qui tient le verrou de
    ligne du lot — celui de `add_milestone` (R1 cx1, P1-1) : gel et verdicts y
    sont relus, un gel plus récent annule tout (`moved`), et un relecteur dont
    le dernier verdict a changé entre-temps garde le sien. Un gel ou un verdict
    concurrent attend le COMMIT, ou est vu s'il l'a précédé. Les lignes de
    journal et les mots suivent le COMMIT."""
    from . import train_events
    frozen, tip, target = out["frozen"], out["tip"], out["target"]
    carried = []
    with db.transaction() as tx:
        rows = tx.query("SELECT id, state, title, app, assignee, branch_target"
                        "  FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(lot),))
        marks = tx.query(_MARKS_SQL, (int(lot),)) if rows else []
        freeze = plan_git.latest_freeze(marks)
        if not rows or not freeze or not merge_queue._same_sha(freeze.get("sha"), frozen, where):
            out.update(result="moved", detail="the freeze changed meanwhile: retried next pass")
            return out
        row = rows[0]
        still = pending(marks, declared)
        for mark, reviewed in plan:
            now = still.get(_key(mark.get("actor")))
            if now is None or int(now.get("id") or 0) != int(mark.get("id") or 0):
                continue              # verdict rendu ou reporté entre-temps : le sien vaut
            note = ("%s %s on %s@%s, same merge result and same lot delta as the reviewed "
                    "head (#2442)" % (CARRIED, _arrow(reviewed, frozen), target, tip[:12]))
            if tx.query(_COPY_SQL, (frozen, note, int(mark["id"]), int(lot))):
                carried.append(_entry(mark, reviewed))
        if carried:
            try:
                train_events.emit(tx, kind="verdict", project=row.get("app") or "",
                                  target=merge_queue._remote_branch(row.get("branch_target") or ""))
            except ValueError:
                pass                  # lot sans sujet de train : le jalon reste valide
    st = storage.of(db).work
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
                "(same merge result and same lot delta as the reviewed head); verdicts "
                "carried: %s. No re-review is due (#2442)."
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
    fetched: dict = {}
    out = []
    for lot in wanted:
        result = carry(cfg, db, lot, dry_run=dry_run, notes=notes.get(lot), fetched=fetched)
        if result.get("result") != "idle":
            out.append(result)
    return out
