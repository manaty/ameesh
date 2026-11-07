# SPDX-License-Identifier: AGPL-3.0-only
"""#6089 : le quorum d'avis a validé un lot que retient un hold de pilote.

Depuis #5758, la transition automatique `qa → validated` ne lève plus un hold
de pilote : c'est voulu, le pilote seul sait si la cause du hold tient
encore. Mais rien ne le lui disait, et le lot restait hors paquet en silence
(#5993 : 11 h 48 entre le quorum et la levée à la main). À cette transition,
le détenteur de chaque hold de pilote actif reçoit un courrier : avis acquis,
hold maintenu ? Le hold ne change pas, la garde du composeur (#3375) non plus.

L'avis est une suite de la transition, jamais sa condition : une lecture ou
un envoi raté ne défait pas la validation ; il laisse une note au lot, et le
débit relève de toute façon le hold après son seuil (`hold_validated`).
"""
from __future__ import annotations

import time

from . import fil, mail, planned_waits, storage


def _moment(since) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(since)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "date inconnue"


def _key_moment(since) -> str:
    """L'instant du hold dans la clé d'idempotence : un même hold, une clé."""
    try:
        return "%.6f" % float(since)
    except (TypeError, ValueError):
        return "unknown"


def notify(db, lot: int) -> list[str]:
    """Écrit au détenteur de chaque hold de pilote actif du lot validé.

    Rend les détenteurs avertis. Un humain n'est pas relancé par courrier
    (canon §9 : jamais l'owner) : une note au lot le dit, et le débit porte
    la relance au pilote. Le courrier est idempotent par hold : une seconde
    validation sous le même hold ne le renvoie pas (son corps ne dépend que
    du hold, une clé rejouée garde le même corps)."""
    from . import work                 # import tardif : work importe le stockage
    lot = int(lot)
    if storage.is_remote(db):
        return []
    try:
        events = storage.of(db).progress.lot_hold_events(
            [lot], planned_waits.HOLD_NOTE_PATTERNS)
        holds = planned_waits.active_holds(db, events).get(lot) or {}
    except Exception as exc:  # noqa: BLE001 - l'avis ne retient pas la validation
        _warn(db, lot, "pilot hold not read after the review quorum (%s)" % exc)
        return []
    told: list[str] = []
    for who, hold in sorted(holds.items()):
        if not hold.get("pilot") or not who:
            continue
        cause = fil.excerpt(str(hold.get("note") or ""), 300)
        if who.startswith(work.HUMAN_PREFIX):
            _warn(db, lot, "review quorum reached under the pilot hold of %s "
                  "(%s, since %s): the hold stays; lift it or restate its cause"
                  % (who, cause, _moment(hold.get("since"))))
            continue
        body = ("Lot #%d is validated: the review quorum is reached "
                "(qa → validated). Your pilot hold stays active (#5758) and "
                "keeps the lot out of the packets: \"%s\" (since %s). Is the "
                "hold still needed? If its condition is met, lift it "
                "(`ameesh work note %d \"unhold: …\"`); otherwise restate its "
                "cause (`ameesh work note %d \"hold: …\"`). Without a gesture, "
                "`ameesh debit --check` reports it after its threshold (#6089)."
                % (lot, cause,
                   _moment(hold.get("since")), lot, lot))
        try:
            mail.send(db, work.SYSTEM_SENDER, who, body, work_item_id=str(lot),
                      # #6096 : une demande de geste, pas une information
                      # passive (`mail.passive_reason`) : elle réveille le
                      # détenteur au repos.
                      payload={"validated_hold": {"lot": lot, "by": who,
                                                  "since": hold.get("since")}},
                      idempotency_key="validated-hold:%d:%s:%s"
                                      % (lot, who, _key_moment(hold.get("since"))))
            told.append(who)
        except Exception as exc:  # noqa: BLE001 - l'envoi raté est dit au lot
            _warn(db, lot, "validated-hold notice not delivered to %s (%s): "
                  "tell them" % (who, exc))
    return told


def _warn(db, lot: int, text: str) -> None:
    from . import work
    try:
        work.note(db, lot, text, actor=work.SYSTEM_SENDER)
    except Exception:  # noqa: BLE001 - la note ratée ne retient rien
        pass
