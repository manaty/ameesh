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
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from . import lot_class, storage, work

#: la référence qui identifie le lot de relecture d'un lot fusionné
PREFIX = "review-after-merge:"
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


def _freezes(db, lot: int) -> list:
    """Les gels du lot (jalons `frozen`), du plus récent au plus ancien.

    Même règle que `plan_git.latest_freeze` : c'est le gel le plus récent qui
    fait autorité. Une lecture impossible rend [] (famille inconnue), jamais
    une exception : la fusion est un fait, l'attribution reste descriptive."""
    try:
        rows = work.milestones(db, int(lot)) or []
        freezes = [r for r in rows if r.get("kind") == "frozen"]
        return sorted(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)),
                      reverse=True)
    except Exception:  # noqa: BLE001 - jalons illisibles : pas de trace
        return []


#: #1525 : les notes du journal qui nomment un détenteur du lot
_HELD = re.compile(r"(?:^|; )(?:assigned to|delegated to) ([^\s,;()]+)|\(before: ([^\s,;()]+)")


def _holders(db, lot: int, item: dict) -> set:
    """Les agents qui ont tenu le lot : l'assigné courant et ceux que le
    journal nomme (`assigned to X`, `delegated to X`, `(before: X)`).

    #1525 : un pilote ou le bureau qui repose le gel n'a jamais tenu le lot ;
    il n'en est pas l'auteur, et sa famille ne doit pas être celle du lot."""
    names = {str(item.get("assignee") or "").strip()}
    try:
        for e in storage.of(db).work.events(int(lot), 500) or []:
            for m in _HELD.finditer(str(e.get("note") or "")):
                names.add(m.group(1) or m.group(2))
    except Exception:  # noqa: BLE001 - journal illisible : l'assigné seul
        pass
    names.discard("")
    names.discard("nobody")
    return names


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
    deviner, il ne prouve rien d'historique (L242b, rotation, `adopt`).

    #1525 : l'acteur d'un gel ne compte que s'il a tenu le lot (`_holders`).
    Un tiers qui repose le gel (pilote, bureau) est écarté : on remonte au gel
    précédent d'un détenteur ; s'il n'y en a pas, l'auteur est inconnu et le
    corps nomme l'acteur écarté (`refroze`)."""
    assignee = str(item.get("assignee") or "").strip()
    out = {"name": assignee, "harness": ""}
    try:
        freezes = _freezes(db, lot)
        if not freezes:
            return out
        holders = _holders(db, lot, item)
        third = ""
        for freeze in freezes:
            name = str(freeze.get("actor") or "").strip() or assignee
            if name and name not in holders:
                third = third or name
                continue
            out["name"] = name
            at_ts = freeze.get("at_ts")
            if name and at_ts is not None:
                out["harness"] = _turns_harness(db, name, at_ts)
            break
        else:
            out["name"] = ""
        if third:
            out["refroze"] = third
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
    if author.get("refroze"):
        # #1525 : un tiers a reposé le gel ; il n'est pas l'auteur
        harness += "; gel reposé par %s, qui n'a pas tenu le lot" % author["refroze"]
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
