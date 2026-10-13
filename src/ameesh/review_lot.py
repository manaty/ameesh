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
reste fusionné. L'idempotence tient à deux marques : le lot de relecture ouvert
(`issue_ref = review-after-merge:#<lot>`) et une ligne de journal sur le lot
fusionné.
"""
from __future__ import annotations

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
#: la règle du canon §2, recopiée dans le corps du lot
BLOCKING = ("un défaut **bloquant** (B, P1) trouvé après la fusion ouvre aussitôt un lot de "
            "correction, qui passe avant tout nouveau travail ; les autres deviennent des "
            "lots de suite (canon §2)")


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
    """Le harnais de l'auteur du lot, si le registre le connaît."""
    name = (name or "").strip()
    if not name:
        return {"name": "", "harness": ""}
    try:
        from . import registry
        row = registry.get(db, name) or {}
    except Exception:  # noqa: BLE001 - registre illisible : famille inconnue
        return {"name": name, "harness": ""}
    return {"name": name, "harness": str(row.get("harness") or "")}


def _open(ops, ref: str) -> Optional[dict]:
    """Le lot de relecture déjà ouvert pour `ref`, ou None."""
    try:
        rows = ops.open_by_ref(ref) or []
    except Exception:  # noqa: BLE001 - pas d'idempotence plutôt qu'une erreur
        return None
    return rows[0] if rows else None


def _marked(ops, lot: int, ref: str) -> bool:
    """Vrai si le journal du lot `lot` porte déjà la marque `ref`."""
    try:
        events = ops.events(int(lot), 200) or []
    except Exception:  # noqa: BLE001 - journal illisible : on retombe sur `_open`
        return False
    return any(ref in str(e.get("note") or "") for e in events)


def title_of(item: dict, lot: int) -> str:
    """Le titre du lot de relecture."""
    return "REVUE #%d : relecture après fusion — %s" % (
        int(lot), " ".join(str(item.get("title") or "").split())[:90])


def body_of(item: dict, lot: int, *, klass: dict, merged_sha: str = "", pr=None,
            target: str = "", author: Optional[dict] = None, family: str = "") -> str:
    """Le corps du lot de relecture : quoi relire, par qui, avant quand."""
    author = author or {}
    who = author.get("name") or "inconnu"
    harness = author.get("harness") or "inconnu"
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
    ne demande une relecture après fusion que pour les lots légers)."""
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
                    "why": "lot de relecture #%d déjà ouvert" % int(existing["id"])})
        return out
    if _marked(ops, lot, ref):
        out["why"] = "lot de relecture déjà marqué au journal de #%d" % lot
        return out
    author = _author(db, str(item.get("assignee") or ""))
    family = reviewer_family(author.get("harness"))
    try:
        created = work.add(
            db, title=title_of(item, lot), type="evolution", source=ref,
            app=str(item.get("app") or ""),
            body=body_of(item, lot, klass=klass, merged_sha=merged_sha, pr=pr,
                         target=target, author=author, family=family),
            issue_ref=ref, actor=actor)
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        out["why"] = "lot de relecture non ouvert : %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        return out
    new_id = int(created["id"])
    out.update({"id": new_id, "created": True, "why": "ouvert"})
    try:
        work.note(db, lot, "%s lot #%d ouvert (classe light, relecture due %s)"
                  % (ref, new_id, deadline()), actor=actor)
    except Exception as exc:  # noqa: BLE001 - la marque est un confort
        out["why"] = "ouvert ; marque non écrite : %s" % str(exc)[:120]
    return out
