# SPDX-License-Identifier: AGPL-3.0-only
"""L539 — le lot de relecture ouvert après la fusion d'un lot léger.

Canon `processus/lots.md` §2 : un lot léger fusionne sans verdict préalable
(tests ciblés de l'auteur et CI verte), puis reçoit « une relecture par une
autre famille de modèle, dans la journée ». Ce module ouvre ce lot de
relecture au moment où la fusion est **confirmée** : `intake`, sans assigné,
dans la file de l'équipe du projet — la famille opposée à celle de l'auteur et
l'échéance du jour sont écrites dans le corps, à charge du relecteur qui prend
le lot.

Rien ici n'autorise ni ne bloque une fusion : la classe est relue sur le diff
gelé par l'appelant (`lot_class`), la fusion est un fait déjà enregistré, et
toute erreur de cette écriture est **rendue**, jamais levée — le lot d'origine
reste fusionné. L'idempotence tient à deux marques écrites **dans la même
transaction** que la création (revue #544 r1) : le lot de relecture lui-même
(`issue_ref = review-after-merge:#<lot>`, **quel que soit son état**) et une
ligne de journal sur le lot fusionné, qui reste un confort. Deux appels
**concurrents** sont sérialisés par le verrou de la ligne marquée, qui est
posé avant l'insertion : le perdant rend le lot du gagnant (revue #544 r2).

#1053 (suite #1037 N1) : la famille de l'auteur est celle du **gel**, pas
celle de la session courante à la fusion. Le gel est le geste de livraison :
son acteur est l'auteur du lot produit, et les tours comptés de cet acteur
(`turn_costs`, 0014) jusqu'à l'instant du gel sont la trace du harnais qui a
réellement tourné. Entre le gel et la fusion, une session peut être remplacée
ou changer de harnais (rotation, `adopt`, L242b) : le registre courant ne
prouve rien du passé, et quand aucun tour n'est tracé la famille reste
inconnue (`codex ou claude`) — elle n'est jamais devinée du registre.
#1104 : l'ouverture ne dépend plus de la **voie** de fusion. La file l'ouvre à
l'atterrissage (`land`), et toute fusion **enregistrée** ailleurs l'ouvre par
`open_for_merge` — le point d'entrée de `work.close_merged`, donc du relevé
des branches (L118), de `sync-merges`, de `sync-github`, de la porte
`git-merge` et de `ameesh work merged`. La classe est alors relue sur le diff
que la fusion a fait entrer (`class_at_merge`, `lot_measures.diff_range`) —
jamais `cible..gel`, vide dès que le gel est un ancêtre de la cible — avec les
indices de l'appelant ; seule `light` ouvre, un doute n'ouvre rien, et
l'idempotence reste celle de `open_after_merge`.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from . import lot_class, storage, work

#: la référence qui identifie le lot de relecture d'un lot fusionné
PREFIX = "review-after-merge:"
#: le marqueur de journal d'une tentative de rattrapage sans création ; le
#: premier mot de la note est `ATTEMPT_PREFIX + <id>` (reconnu par la sélection)
ATTEMPT_PREFIX = "review-after-merge:attempt:#"
#: le délai (s) avant de réessayer une tentative sans création : le temps que
#: les candidats plus anciens passent, qu'un doute se résolve ou qu'une panne
#: se reprenne. Délai, jamais suppression : le journal garde l'historique.
ATTEMPT_RETRY_S = 3600
#: le marqueur de journal d'un candidat **terminal** : un lot non-light
#: confirmé (le canon §2 ne demande une relecture après fusion que pour un lot
#: léger) ou un doute épuisé après `CATCH_UP_MAX_ATTEMPTS` tentatives. Le
#: premier mot de la note est `TERMINAL_PREFIX + <id>` ; la sélection l'exclut
#: toujours — la marque reste au journal, visible, mais aucune tentative
#: horaire perpétuelle (arbitrage pilote du 15/10 sur le P2 de la R2 #1104).
TERMINAL_PREFIX = "review-after-merge:closed:#"
#: le nombre de tentatives bornées d'un candidat **douteux** (classe non
#: confirmée : lecture impossible, dépôt absent) avant sa marque terminale.
#: Un non-light confirmé, lui, est terminal dès la première lecture.
CATCH_UP_MAX_ATTEMPTS = 3
#: l'échéance du canon §2 : le jour même de la fusion, heure de Paris
TZ = "Europe/Paris"
HOUR = "23:59"
#: la famille de relecture opposée à celle de l'auteur (règle du pilote,
#: 2026-10-13) : codex si l'auteur est Claude ou DeepSeek, sinon Claude.
OPPOSITE = {"claude": "codex", "deepseek": "codex", "codex": "claude"}
UNKNOWN_FAMILY = "codex ou claude"
#: la règle du canon §2, recopiée dans le corps du lot (consigne injectée au
#: relecteur, avec la route du défaut non bloquant — owner du 15/10/2026)
BLOCKING = ("un défaut **bloquant** (B, P1) trouvé après la fusion ouvre aussitôt un lot de "
            "correction, qui passe avant tout nouveau travail ; un défaut **non bloquant** "
            "(N, P2, P3) ne crée pas de lot : il va au backlog "
            "(`ameesh work backlog add --title … --value … --score …`), regroupé avec les "
            "autres défauts mineurs du même chantier (canon §2, owner du 15/10/2026 ; sur "
            "ameesh, un P2 de la classe sensible bloque encore)")


def ref_of(lot: int) -> str:
    """La référence du lot de relecture de `lot`."""
    return "%s#%d" % (PREFIX, int(lot))


def deadline(now: Optional[datetime] = None) -> str:
    """Le jour même de la fusion, à 23:59 heure de Paris."""
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(TZ))
        except Exception:  # noqa: BLE001 - fuseau absent : l'échéance reste écrite
            return "%s %s UTC" % (datetime.now(tz=None).date().isoformat(), HOUR)
    return "%s %s %s" % (now.date().isoformat(), HOUR, TZ)


def reviewer_family(harness: Optional[str]) -> str:
    """La famille de relecture opposée au harnais de l'auteur."""
    return OPPOSITE.get((harness or "").strip().lower(), UNKNOWN_FAMILY)


def _author(db, name: str) -> dict:
    """Le harnais de l'auteur du lot, si le registre le connaît.

    #1037 : c'est celui de sa session VIVANTE (`registry.live_harness`), pas
    seulement son harnais déclaré — L242b fait tourner un agent sous un
    harnais emprunté, et la relecture doit venir d'une autre famille que
    celle qui a réellement produit le travail."""
    name = (name or "").strip()
    if not name:
        return {"name": "", "harness": ""}
    try:
        from . import registry
        row = registry.get(db, name) or {}
    except Exception:  # noqa: BLE001 - registre illisible : famille inconnue
        return {"name": name, "harness": ""}
    return {"name": name, "harness": registry.live_harness(row)}


def _repo_of_agent(db, name: str) -> str:
    """Le dossier de travail déclaré de l'agent, s'il existe (indice de dépôt).

    Même source que le relevé des branches (`sync_branches`) : le `cwd` du
    registre est le worktree où le gel vit encore. Un nom inconnu, un chemin
    disparu ou un registre illisible ne rendent rien — l'appelant doubtera,
    jamais ne devinera."""
    name = (name or "").strip()
    if not name or ":" in name:
        return ""
    try:
        from . import registry
        row = registry.get(db, name) or {}
    except Exception:  # noqa: BLE001 - registre illisible : pas d'indice
        return ""
    path = os.path.expanduser(str(row.get("cwd") or ""))
    return path if path and os.path.isdir(path) else ""


def _open(ops, ref: str) -> Optional[dict]:
    """Le lot de relecture déjà écrit pour `ref`, **quel que soit son état**.

    L'identité tient à la `issue_ref` (`review-after-merge:#<lot>`), pas à
    l'état du lot : un lot de relecture fermé reste celui de sa fusion, et la
    marque au journal peut manquer (écriture ratée avant cette version, journal
    tronqué). `open_by_ref` reste le repli d'un magasin sans `by_issue_ref`.
    """
    for name in ("by_issue_ref", "open_by_ref"):
        reader = getattr(ops, name, None)
        if reader is None:
            continue
        try:
            rows = reader(ref) or []
        except Exception:  # noqa: BLE001 - magasin incomplet : on essaie l'autre
            continue
        if rows:
            return rows[0]
    return None


def _marked(ops, lot: int, ref: str) -> bool:
    """Vrai si le journal du lot `lot` porte déjà la marque `ref`."""
    try:
        events = ops.events(int(lot), 200) or []
    except Exception:  # noqa: BLE001 - journal illisible : on retombe sur `_open`
        return False
    return any(ref in str(e.get("note") or "") for e in events)


#: ce qu'écrit le corps quand le harnais de l'auteur n'est pas tracé (#1053)
UNTRACED = "non tracé au gel"


def _freeze(db, lot: int) -> Optional[dict]:
    """Le dernier gel du lot (jalon `frozen` le plus récent), ou None.

    Même règle que `plan_git.latest_freeze` : c'est le gel le plus récent qui
    fait autorité. Une lecture impossible rend None (famille inconnue), jamais
    une exception : la fusion est un fait, l'attribution reste descriptive."""
    try:
        rows = work.milestones(db, int(lot)) or []
        freezes = [r for r in rows if r.get("kind") == "frozen"]
        if not freezes:
            return None
        return max(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))
    except Exception:  # noqa: BLE001 - jalons illisibles : pas de trace
        return None


def _turns_harness(db, agent: str, at_ts: float) -> str:
    """Le harnais du dernier tour écrit de `agent` jusqu'à `at_ts` (epoch).

    `turn_costs` (0014) est la seule trace du harnais qui a réellement tourné :
    une ligne par tour fini, écrite avec le harnais du flux. Le registre, lui,
    ne dit que l'état courant. Les lignes écartées (0043) sont gardées : le
    tour a eu lieu, seule sa comptabilisation a été corrigée."""
    rows = db.query(
        "SELECT harness FROM turn_costs"
        " WHERE agent = %s AND recorded_at <= to_timestamp(%s)"
        " ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (agent, float(at_ts)))
    return str((rows[0].get("harness") if rows else "") or "")


def _author_at_freeze(db, lot: int, item: dict) -> dict:
    """L'auteur du lot produit, et le harnais sous lequel il tournait au gel.

    #1053 : le gel est le geste de livraison — son acteur est l'auteur du lot
    produit, et le registre de ses tours est relu **jusqu'à l'instant du gel**
    (`_turns_harness`). L'assigné courant ne le remplace pas : la relecture
    réassigne le lot après le gel. Sans gel, sans acteur ou sans tour tracé, la
    famille reste inconnue : le registre courant n'est jamais consulté pour la
    deviner, il ne prouve rien d'historique (L242b, rotation, `adopt`)."""
    assignee = str(item.get("assignee") or "").strip()
    out = {"name": assignee, "harness": ""}
    try:
        freeze = _freeze(db, lot)
        if freeze is None:
            return out
        name = str(freeze.get("actor") or "").strip() or assignee
        out["name"] = name
        at_ts = freeze.get("at_ts")
        if not name or at_ts is None:
            return out
        out["harness"] = _turns_harness(db, name, at_ts)
    except Exception:  # noqa: BLE001 - toute trace illisible : famille inconnue
        pass
    return out


def title_of(item: dict, lot: int) -> str:
    """Le titre du lot de relecture."""
    return "REVUE #%d : relecture après fusion — %s" % (
        int(lot), " ".join(str(item.get("title") or "").split())[:90])


def body_of(item: dict, lot: int, *, klass: dict, merged_sha: str = "", pr=None,
            target: str = "", author: Optional[dict] = None, family: str = "") -> str:
    """Le corps du lot de relecture : quoi relire, par qui, avant quand."""
    author = author or {}
    who = author.get("name") or "inconnu"
    harness = author.get("harness") or UNTRACED
    where = "PR #%s" % pr if pr else (merged_sha[:12] if merged_sha else "?")
    why = " ".join(str((klass or {}).get("why") or "").split())
    lines = [
        "Relecture après fusion (canon `processus/lots.md` §2, lot léger).",
        "",
        "Lot fusionné : #%d « %s »" % (int(lot), " ".join(str(item.get("title") or "").split())),
        "- Classe : %s%s." % ((klass or {}).get("class") or "?",
                              " (%s)" % why if why else ""),
        "- Fusion : %s%s sur %s." % (merged_sha[:12] if merged_sha else "?",
                                     " (%s)" % where if pr else "",
                                     target or item.get("branch_target") or "sa cible"),
        "- Auteur : %s (harnais %s)." % (who, harness),
        "- Relecteur : une **autre famille de modèle** — %s (règle du pilote du 13/10)."
        % (family or reviewer_family(harness)),
        "- Échéance : **%s**, le jour même de la fusion." % deadline(),
        "",
        "À faire :",
        "- lire le diff fusionné (`git show %s`) et vérifier les cas limites ;"
        % (merged_sha[:12] if merged_sha else "<sha>"),
        "- livrer le verdict (`ameesh work milestone %d verdict ok|blocked --sha %s`) ;"
        % (int(lot), merged_sha or "<sha>"),
        "- %s." % BLOCKING,
        "",
        "Ce lot n'est assigné à personne : il est dans la file de l'équipe, le premier "
        "relecteur disponible de la bonne famille le prend.",
    ]
    return "\n".join(lines)


def open_after_merge(db, *, lot: int, klass: dict, item: Optional[dict] = None,
                     merged_sha: str = "", pr=None, target: str = "",
                     actor: str = work.SYSTEM_SENDER) -> dict:
    """Ouvre le lot de relecture du lot léger `lot`, s'il n'existe pas déjà.

    Rend `{lot, id, created, class, why}` — ne lève jamais. `klass` est la
    classe lue sur le diff gelé (`lot_class`) : seule `light` ouvre un lot de
    relecture ; un doute ou une classe supérieure n'en ouvre pas (le canon §2
    ne demande une relecture après fusion que pour les lots légers).

    La création et la marque au journal de `lot` sont **une seule écriture**
    (revue #544 r1) : un échec de la marque annule la création, donc un nouvel
    appel ne peut pas ouvrir un second lot de relecture."""
    lot = int(lot)
    klass = klass or {}
    out = {"lot": lot, "id": None, "created": False, "class": klass.get("class") or "",
           "why": ""}
    if klass.get("class") != lot_class.LIGHT:
        out["why"] = ("class %s: pas de lot de relecture après fusion (§2)"
                      % (klass.get("class") or "?"))
        return out
    if item is None:
        item = work.get(db, lot)
    if item is None:
        out["why"] = "lot #%d introuvable" % lot
        return out
    ref = ref_of(lot)
    ops = storage.of(db).work
    existing = _open(ops, ref)
    if existing is not None:
        out.update({"id": int(existing["id"]),
                    "why": "lot de relecture #%d déjà ouvert (état %s)"
                           % (int(existing["id"]), existing.get("state") or "?")})
        return out
    if _marked(ops, lot, ref):
        out["why"] = "lot de relecture déjà marqué au journal de #%d" % lot
        return out
    author = _author_at_freeze(db, lot, item)
    family = reviewer_family(author.get("harness"))
    try:
        created = work.add(
            db, title=title_of(item, lot), type="evolution", source=ref,
            app=str(item.get("app") or ""),
            body=body_of(item, lot, klass=klass, merged_sha=merged_sha, pr=pr,
                         target=target, author=author, family=family),
            issue_ref=ref, actor=actor, mark_item_id=lot,
            mark_note="%s lot #{lot} ouvert (classe light, relecture due %s)"
                      % (ref, deadline()))
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        # revue #544 r2 : un appel concurrent a pu créer le lot pendant
        # l'attente du verrou ; on rend l'identifiant du gagnant, pas un échec
        existing = _open(ops, ref)
        if existing is not None:
            out.update({"id": int(existing["id"]),
                        "why": "lot de relecture #%d déjà ouvert (course perdue : %s)"
                               % (int(existing["id"]), type(exc).__name__)})
            return out
        out["why"] = "lot de relecture non ouvert : %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        return out
    out.update({"id": int(created["id"]), "created": True, "why": "ouvert"})
    return out


def class_at_merge(db, lot: int, *, item: Optional[dict] = None,
                   repo: Optional[str] = None, target: str = "",
                   sha: str = "") -> dict:
    """La classe du lot au moment d'une fusion constatée (L539, canon §8).

    Le diff lu est celui que la fusion a fait entrer : `sha` (le commit de la
    fusion constatée, le gel à défaut) est ramené à son intervalle de
    changement par `lot_measures.diff_range` — commit de fusion, fusion qui a
    introduit le gel, ou gel hors cible. Lire `cible..gel` serait un piège :
    une fois la fusion faite, le gel est un ancêtre de la cible, ce diff est
    vide et toute classe y serait « light ».

    `repo` est le dépôt où la fusion a été constatée (le relevé des branches
    le connaît), sinon le dossier de travail de l'assigné au registre, sinon
    le dépôt du projet (`lot_class.repository`) — toujours pour y lire le diff
    de la fusion, jamais `cible..gel`. `target` la cible déjà résolue.

    Sans intervalle **prouvé** (diff illisible, commit de fusion absent du
    dépôt, ni gel ni commit), la classe n'est pas demandée à `of_lot` : son
    repli gel/cible rendrait `cible..gel`, vide dès que le gel est un ancêtre
    de la cible — « light » sur un lot SQL fusionné (R1 #1104 P2-1). On rend un
    doute conservateur, et d'une classe inconnue on n'ouvre pas de relecture."""
    from . import lot_measures, plan_git
    if item is None:
        item = work.get(db, lot)
    if item is None:
        return {"class": "", "why": "lot #%d introuvable" % int(lot), "doubt": True}
    where = repo or _repo_of_agent(db, str(item.get("assignee") or ""))
    if not where:
        where = lot_class.repository(getattr(db, "cfg", None), item) or ""
    base = head = ""
    proven = False
    why = "aucun intervalle de fusion lisible"
    try:
        frozen = ""
        if where:
            freeze = plan_git.latest_freeze(work.milestones(db, int(lot)))
            frozen = str((freeze or {}).get("sha") or "").strip()
        tips = [t for t in ((sha or "").strip(), frozen) if t]
        if not where:
            why = "dépôt inconnu : aucun intervalle de fusion lisible"
        elif not tips:
            why = "ni commit de fusion ni gel : aucun intervalle de fusion lisible"
        # le commit de la fusion constatée d'abord ; à défaut le gel (fusion
        # déclarée sans commit lisible, `ameesh work merged` à la main)
        for tip in tips:
            rng = lot_measures.diff_range(where, tip, target or None)
            if rng:
                base, head = rng
                proven = True
                break
            why = "le dépôt %s ne contient pas le commit %s" % (where, tip[:12])
    except Exception as exc:  # noqa: BLE001 - pas d'intervalle : doute, jamais de classe
        proven = False
        why = "lecture du diff impossible : %s: %s" % (type(exc).__name__, str(exc)[:160])
    if not proven:
        # R1 #1104 P2-1 : `of_lot` ne reçoit jamais ce repli (base/head vides),
        # qui relirait `cible..gel` — vide après fusion — et classerait light.
        return {"lot": int(lot), "class": lot_class.SENSITIVE, "why": why, "files": [],
                "policy_ref": "", "repo": where or "", "target": target or "",
                "frozen_sha": "", "doubt": True}
    try:
        return lot_class.of_lot(db, int(lot), cfg=getattr(db, "cfg", None), item=item,
                                repo=where or None, base=base or None, head=head or None)
    except Exception as exc:  # noqa: BLE001 - un doute, jamais une exception
        return {"class": "", "why": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "doubt": True}


def open_for_merge(db, lot: int, *, merged_sha: str = "", pr=None,
                   repo: Optional[str] = None, target: str = "",
                   actor: str = work.SYSTEM_SENDER) -> dict:
    """L539 : le lot de relecture d'une fusion enregistrée **hors file**.

    Toute voie qui enregistre une fusion (`work.close_merged`) passe ici : le
    relevé des branches (L118), `sync-merges`, `sync-github`, la porte
    `git-merge` et `ameesh work merged`. La classe est relue sur le diff de la
    fusion constatée (`class_at_merge`) avec les indices de l'appelant ; seule
    `light` ouvre, un doute n'ouvre rien. Idempotent : un lot de relecture déjà
    écrit, **quel que soit son état**, est rendu sans relire la classe. Ne lève
    jamais — la fusion est un fait déjà enregistré."""
    lot = int(lot)
    out = {"lot": lot, "id": None, "created": False, "class": "", "why": ""}
    try:
        item = work.get(db, lot)
        if item is None:
            out["why"] = "lot #%d introuvable" % lot
            return out
        existing = _open(storage.of(db).work, ref_of(lot))
        if existing is not None:
            out.update({"id": int(existing["id"]),
                        "why": "lot de relecture #%d déjà ouvert (état %s)"
                               % (int(existing["id"]), existing.get("state") or "?")})
            return out
        where_target = target or str(item.get("branch_target") or "")
        klass = class_at_merge(db, lot, item=item, repo=repo, target=where_target,
                               sha=merged_sha)
        out["class"] = klass.get("class") or ""
        out["doubt"] = bool(klass.get("doubt"))
        opened = open_after_merge(
            db, lot=lot, klass=klass, item=item, merged_sha=merged_sha, pr=pr,
            target=where_target, actor=actor)
        out.update({"id": opened.get("id"), "created": bool(opened.get("created")),
                    "class": opened.get("class") or out["class"],
                    "why": opened.get("why") or ""})
        return out
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        out["why"] = "lot de relecture non ouvert : %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        return out


#: le nombre de relectures **manquantes** examinées par un rattrapage (borné)
CATCH_UP_LIMIT = 20


def merged_sha(db, lot: int) -> str:
    """Le commit du dernier jalon `merged` du lot, ou "" (best effort)."""
    try:
        rows = [m for m in work.milestones(db, int(lot)) if m.get("kind") == "merged"]
    except Exception:  # noqa: BLE001 - pas de jalon lisible : pas de commit
        return ""
    if not rows:
        return ""
    last = max(rows, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))
    return str(last.get("sha") or "").strip()


def catch_up(db, *, repo: str = "", target: str = "",
             limit: int = CATCH_UP_LIMIT,
             retry_s: float = ATTEMPT_RETRY_S,
             max_attempts: int = CATCH_UP_MAX_ATTEMPTS) -> dict:
    """L539 (#1104) : rattrapage des relectures manquantes après une panne.

    Les relevés ne sélectionnent que des lots **ouverts** : un lot déjà `merged`
    dont l'ouverture de relecture a échoué (panne d'écriture, base indisponible)
    ne serait plus jamais revu — la relecture manquerait définitivement (R1
    #1104 P2-2). Ici les relectures manquantes sont sélectionnées **comme
    telles** (`WorkItems.items_missing_ref`) puis `open_for_merge` (idempotent,
    best effort, jamais d'exception) les ouvre.

    R2 #1104 P2-2 : la borne porte sur les **manquants examinés**, pas sur un
    balayage préalable de lots déjà pourvus — un parent ancien dont
    l'ouverture a échoué n'est plus caché par `limit` parents plus récents
    déjà pourvus. `merged` et `promoted` sont un seul ensemble ordonné par
    récence : un `promoted` manquant n'est pas affamé par des `merged`.

    Borne d'activation (arbitrage pilote du 15/10, P2 de la R2 #1104) : le
    rattrapage ne remonte **jamais** avant `review_catch_up_bound()` — la ligne
    écrite par la migration 0438 à l'activation du lot, commune aux exécuteurs
    et conservée après redémarrage. Un lot déjà fusionné avant cette activation
    ne devient pas une relecture due aujourd'hui ; sans borne lisible, le
    rattrapage est fermé (aucun balayage historique, qui reste une décision du
    pilote). Le contenu d'un candidat non-light **confirmé** est terminal : une
    seule lecture, aucune tentative horaire perpétuelle. Un candidat **douteux**
    (classe non confirmée) est réessayé au plus `max_attempts` fois, espacées de
    `retry_s`, puis marqué terminal à son tour — le journal garde la trace.

    Ne lève jamais ; rend `{examined, missing, opened, already, attempted,
    terminal, failed, bound}` (`attempted` : tentatives sans création marquées,
    `already` : relecture ouverte entre la sélection et le geste, `terminal` :
    candidats clos par la marque terminale)."""
    out = {"examined": 0, "missing": 0, "opened": 0, "already": 0,
           "attempted": 0, "terminal": 0, "failed": 0, "bound": ""}
    try:
        ops = storage.of(db).work
        bound = ops.review_catch_up_bound()
        if bound is None:  # fail-closed : jamais de balayage historique
            return out
        out["bound"] = bound.isoformat() if hasattr(bound, "isoformat") else str(bound)
        rows = ops.items_missing_ref(states=work.MERGED_STATES,
                                     ref_prefix=PREFIX + "#", limit=int(limit),
                                     attempt_prefix=ATTEMPT_PREFIX,
                                     retry_s=float(retry_s),
                                     terminal_prefix=TERMINAL_PREFIX,
                                     max_attempts=int(max_attempts),
                                     since=bound)
    except Exception:  # noqa: BLE001 - pas de base : rien à rattraper
        return out

    def mark(lot, state, detail):
        """Marque la tentative sans création (best effort : le lot reste)."""
        try:
            ops.log_review_attempt(lot, state, "%s%d %s"
                                   % (ATTEMPT_PREFIX, int(lot), detail),
                                   work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    def terminal(lot, state, detail):
        """Marque le candidat terminal (best effort : le journal le garde)."""
        try:
            ops.log_review_attempt(lot, state, "%s%d %s"
                                   % (TERMINAL_PREFIX, int(lot), detail),
                                   work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    def attempts(lot):
        """Le nombre de tentatives déjà marquées au journal du lot."""
        try:
            events = ops.events(int(lot), 200) or []
        except Exception:  # noqa: BLE001 - journal illisible : compteur à zéro
            return 0
        token = "%s%d" % (ATTEMPT_PREFIX, int(lot))
        return sum(1 for e in events
                   if str(e.get("note") or "").split(" ")[0] == token)

    for row in rows:
        lot = int(row["id"])
        state = str(row.get("state") or "")
        out["examined"] += 1
        try:
            # la sélection a exclu les pourvus ; le recontrôle couvre une
            # ouverture concurrente entre la sélection et ici
            if _open(ops, ref_of(lot)) is not None:
                continue
            out["missing"] += 1
            done = open_for_merge(db, lot, merged_sha=merged_sha(db, lot), repo=repo,
                                  target=target or str(row.get("branch_target") or ""))
            if done.get("created"):
                out["opened"] += 1
                continue
            if done.get("id") or "déjà" in str(done.get("why") or ""):
                # un autre exécuteur a ouvert la relecture entre la sélection
                # et ici : rien à marquer, le lot est pourvu
                out["already"] += 1
                continue
            klass = str(done.get("class") or "")
            if not done.get("doubt") and klass and klass != lot_class.LIGHT:
                # classe confirmée non-light : le canon §2 ne demande une
                # relecture après fusion que pour un lot léger — terminal
                out["terminal"] += 1
                terminal(lot, state, "classe %s confirmée non-light" % klass)
                continue
            seen = attempts(lot) + 1
            if seen >= int(max_attempts):
                out["terminal"] += 1
                terminal(lot, state, "doute après %d tentatives" % seen)
            else:
                out["attempted"] += 1
                mark(lot, state, "sans ouverture: %s" % (done.get("why") or "?"))
        except Exception:  # noqa: BLE001 - un lot en panne n'arrête pas le rattrapage
            out["failed"] += 1
            mark(lot, state, "erreur")
    return out


def catch_up_eligible(db, lot: int, *, retry_s: float = ATTEMPT_RETRY_S,
                      max_attempts: int = CATCH_UP_MAX_ATTEMPTS) -> bool:
    """L539 (#1104, B1 de la R1) : ce lot déjà fusionné serait-il encore
    sélectionné par le rattrapage borné (`catch_up`) ?

    La même vérité que le balayage (filtre partagé, `missing_ref_eligible`) :
    borne d'activation lisible (migration 0438, fail-closed sinon) et jalon
    `merged` à elle ou après, relecture toujours manquante, aucune marque
    terminale, tentatives sous le plafond et espacées du délai. Sert au chemin
    `already` de `close_merged` : sans cette borne, chaque passage de
    `sync-github` relancerait l'ouverture pour les ~1019 fusions antérieures à
    l'activation (B1). Best effort : toute panne de lecture ferme la porte, le
    lot reste au rattrapage et n'est jamais rouvert par ce chemin."""
    try:
        ops = storage.of(db).work
        bound = ops.review_catch_up_bound()
        if bound is None:      # fail-closed : jamais de balayage historique
            return False
        reader = getattr(ops, "missing_ref_eligible", None)
        if reader is None:     # magasin d'une version antérieure : rien tenté
            return False
        return bool(reader(int(lot), ref_prefix=PREFIX + "#",
                           attempt_prefix=ATTEMPT_PREFIX, retry_s=float(retry_s),
                           terminal_prefix=TERMINAL_PREFIX,
                           max_attempts=int(max_attempts), since=bound))
    except Exception:  # noqa: BLE001 - pas de base : rien à reprendre
        return False
