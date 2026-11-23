# SPDX-License-Identifier: AGPL-3.0-only
"""#3460 (décision #135690 a) : retirer un agent du registre actif, et l'inverse.

  ameesh agent retire NOM --reason RAISON [--actor A] [--json]
  ameesh agent restore NOM [--actor A] [--json]

Le retrait marque la ligne (0484) : l'agent n'est plus désignable comme
relecteur, ni réclamable par un exécuteur (`canon_claim_predicate_sql`), ni
destinataire d'un courrier (`undeliverable.check_send`) ; son historique et
son courrier restent lisibles. Statut, mode et bail ne changent pas : la
restauration rend exactement l'état d'avant. Retrait et restauration sont
journalisés (`agent.retired`, `agent.restored`) dans la transaction du geste.

Le retrait ferme aussi le courrier encore EN ATTENTE de l'agent (#8113) :
chaque message non remis — sauf une demande de décision, qui attend un
humain — passe remis avec `meta.retired`, et chaque expéditeur qui a encore
une boîte lue en reçoit un `event` qui le lui rend. Une boîte retirée n'est
donc jamais annoncée comme un agent arrêté à relancer : l'alerte
`mail_undeliverable` ne propose pas `ameesh resume` pour un retiré.

Le retrait est refusé, avec chaque cause nommée, tant que l'agent tient un
bail vivant, un lot ouvert assigné ou réservé comme exécutant externe, ou une
veille `ameesh watch` qui lui écrit : l'agent les rend d'abord.
`watch.SENDER` (« veille ») reste une identité
d'émission système, comme « ameesh » : les veilles et les réveils émettent
toujours sous ce nom, retiré ou non (#3342). Les unités héritées
`ameesh-veille-<agent>` (L121) de l'hôte émettent elles aussi sous ce nom ;
elles sont nommées en avertissement, sans refus : leur sort relève d'une autre
décision.

Le geste est celui d'un humain, d'un orchestrateur, d'un agent de conception
ou de l'auditeur (`work.corrector`, `auditor=True`).
"""
from __future__ import annotations

import hashlib
import os
import time

from . import storage, work
from .config import Config

RETIRED_EVENT = "agent.retired"
RESTORED_EVENT = "agent.restored"


class RetireError(ValueError):
    """Retrait ou restauration refusés ; le texte dit pourquoi."""


def _blocker_text(name: str, blocker: dict) -> str:
    kind = blocker.get("kind")
    if kind == "lease":
        return ("holds a live lease (%s): stop its runner, or wait for the lease "
                "to expire" % (blocker.get("owner") or "?"))
    if kind == "lot":
        return ("is assigned lot #%s (%s), not closed: hand it over "
                "(ameesh work assign %s <agent>) or close it"
                % (blocker.get("id"), blocker.get("state"), blocker.get("id")))
    if kind == "watch":
        return ("receives an ameesh watch on %s: ameesh watch disable --agent %s"
                % (blocker.get("host") or "?", name))
    if kind == "external_executor":
        return ("is external executor of lot #%s (%s), not closed: clear the "
                "reservation (ameesh work external-executor %s --clear) or "
                "reserve it to another executor "
                "(ameesh work external-executor %s <executor>)"
                % (blocker.get("id"), blocker.get("state"), blocker.get("id"),
                   blocker.get("id")))
    return str(blocker)


def legacy_units(env: dict | None = None) -> list[str]:
    """Les minuteries héritées `ameesh-veille-<agent>.timer` (L121) de cet hôte."""
    from . import watch

    try:
        names = os.listdir(watch.unit_dir(env))
    except OSError:
        return []
    return sorted(n for n in names
                  if n.startswith(watch.LEGACY_PREFIX) and n.endswith(".timer"))


def _excerpt(row: dict, limit: int = 120) -> str:
    """Une ligne du corps d'un message clos, pour le rendre à son expéditeur."""
    from . import fil

    text = " ".join(fil.clean_text(row.get("excerpt") or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _returned_body(agent: str, rows: list[dict], *, actor: str, reason: str) -> str:
    """Le texte du retour : ce qui n'a pas été remis, quand, et les gestes."""
    lines = [
        "%d message(s) addressed to %s were not delivered: %s was retired by %s "
        "(reason: %s), so nothing is delivered to it any more."
        % (len(rows), agent, agent, actor, reason),
    ]
    for row in rows:
        lot = str(row.get("work_item_id") or "").strip()
        when = time.strftime("%Y-%m-%d %H:%M UTC",
                             time.gmtime(float(row.get("created_ts") or 0.0)))
        lines.append("  - #%s, sent %s%s: %s" % (
            row.get("id"), when, ", lot #%s" % lot if lot else "",
            _excerpt(row) or "(empty body)"))
    lines.append(
        "They are closed and stay readable in the thread. Re-address what still "
        "matters to a live agent (`ameesh show %s` names who carries its work); "
        "`ameesh agent restore %s` brings it back." % (agent, agent))
    return "\n".join(lines)


def _single_lot(rows: list[dict]) -> str | None:
    lots = {str(row.get("work_item_id") or "").strip() for row in rows}
    return next(iter(lots)) if len(lots) == 1 and next(iter(lots)) else None


def _return_key(agent: str, part: str) -> str:
    """Une clé d'idempotence stable : rejouer un retour ne double jamais l'avis.

    La clé ne dépend que du retiré, de l'expéditeur (ou du relais pilote) et
    de l'instant du lot de fermeture, pas du passage qui l'envoie."""
    digest = hashlib.sha1(("%s\0%s" % (agent, part)).encode()).hexdigest()[:16]
    return "retire-return-%s" % digest


def _sender_state(db, agent: str, sender: str) -> str:
    """`""` si l'expéditeur a une boîte lue, sinon la raison, en clair."""
    from . import registry, undeliverable

    if not sender:
        return "no sender"
    if sender in ("ameesh", agent):
        return "system sender"
    if sender.startswith("human:"):
        return "human sender"
    if undeliverable.watch_sender(sender):
        return "watch sender"
    try:
        target = registry.get(db, sender)
    except Exception as exc:  # noqa: BLE001 - la file durable garde le retour
        return "registry unreadable (%s)" % type(exc).__name__
    if target is None:
        return "absent from the registry"
    if target.get("retired_ts") is not None:
        return "retired"
    if target.get("status") == "stopped":
        return "stopped"
    return ""


def _pilots(cfg, db, agent: str) -> list[str]:
    """Les pilotes du projet (règle partagée des briefs), à défaut `ameesh`."""
    from . import freeze_packet

    try:
        names = freeze_packet.pilot_names(
            cfg, db, getattr(cfg, "project", "") or "")
    except Exception:  # noqa: BLE001 - le relais retombe sur l'orchestrateur
        names = []
    live = sorted({str(name) for name in names or ()
                   if name and str(name) != agent
                   and not str(name).startswith("human:")})
    return live or ["ameesh"]


def _unreachable_body(agent: str, rows: list[dict], *,
                      actor: str, reason: str) -> str:
    """Le relais au pilote : le retour est dû, aucun expéditeur n'est joignable."""
    lines = [
        "%d message(s) addressed to %s were closed at its retirement, but their "
        "sender(s) have no live mailbox: this relay is the durable return "
        "(retired by %s; reason: %s)."
        % (len(rows), agent, actor, reason),
    ]
    for row in rows:
        when = time.strftime("%Y-%m-%d %H:%M UTC",
                             time.gmtime(float(row.get("created_ts") or 0.0)))
        lines.append("  - from %s, message #%s, sent %s%s: %s" % (
            row.get("sender") or "?", row.get("id"), when,
            ", lot #%s" % row["work_item_id"] if row.get("work_item_id") else "",
            _excerpt(row) or "(empty body)"))
    lines.append(
        "The pilot holds this return; `ameesh mail forward %s <live-agent>` "
        "hands the messages on. `ameesh mail returns --replay` retries a failed "
        "notice or relay, never twice (stable idempotency key)." % agent)
    return "\n".join(lines)


def _deliver_batch(db, cfg, agent: str, closed_ts: float, rows: list[dict], *,
                   actor: str, reason: str) -> dict:
    """#8113 : rend UN lot de fermeture (retiré, instant, messages).

    Chaque expéditeur vivant reçoit son avis ; un expéditeur sans boîte lue
    (ou dont l'envoi échoue) part au pilote du projet. Le lot n'est marqué
    rendu (`meta.retired.notified`) que si aucun envoi n'a échoué : sinon il
    reste dans la file durable, et un rejeu le reprend sous la même clé."""
    from . import mail, storage

    summary: dict = {"closed": len(rows), "senders": [], "informed": [],
                     "unreachable": [], "pilots": [], "warnings": [],
                     "marked": 0}
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get("sender") or "").strip(), []).append(row)
    summary["senders"] = sorted(groups)
    unreachable_rows: list[dict] = []
    batch = "batch:%d" % int(float(closed_ts or 0.0))
    for sender, messages in sorted(groups.items()):
        state = _sender_state(db, agent, sender)
        if state:
            summary["unreachable"].append(sender or "?")
            unreachable_rows.extend(messages)
            continue
        key = _return_key(agent, "%s:%s" % (batch, sender))
        try:
            mail.send(db, "ameesh", sender,
                      _returned_body(agent, messages, actor=actor, reason=reason),
                      kind="event",
                      payload={"returned": {
                          "recipient": agent,
                          "messages": [int(row["id"]) for row in messages],
                          "by": actor, "reason": reason}},
                      work_item_id=_single_lot(messages),
                      idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - la file durable garde le retour
            summary["warnings"].append("return notice to %s not sent (%s)"
                                       % (sender, type(exc).__name__))
            summary["unreachable"].append(sender)
            unreachable_rows.extend(messages)
        else:
            summary["informed"].append(sender)
    if unreachable_rows:
        body = _unreachable_body(agent, unreachable_rows, actor=actor, reason=reason)
        payload = {"returned_unreachable": {
            "recipient": agent,
            "messages": [int(row["id"]) for row in unreachable_rows],
            "by": actor, "reason": reason}}
        for pilot in _pilots(cfg, db, agent):
            try:
                mail.send(db, "ameesh", pilot, body, kind="event",
                          payload=payload,
                          work_item_id=_single_lot(unreachable_rows),
                          idempotency_key=_return_key(agent, "%s:pilot:%s"
                                                      % (batch, pilot)))
            except Exception as exc:  # noqa: BLE001 - le lot reste en file
                summary["warnings"].append("pilot relay to %s not sent (%s)"
                                           % (pilot, type(exc).__name__))
            else:
                summary["pilots"].append(pilot)
    if not summary["warnings"]:
        summary["marked"] = storage.of(db).mailbox.mark_returned(
            [int(row["id"]) for row in rows],
            {"at": time.time(), "by": actor,
             "to": summary["informed"] + summary["pilots"]})
    return summary


def replay_returns(cfg: Config, db, *, agent: str | None = None,
                   limit: int = 500) -> dict:
    """#8113 : rejoue la file durable des retours de courrier.

    Lit `returns_pending` (courrier clos au retrait, non encore rendu) et
    refait chaque lot : avis directs aux expéditeurs vivants, relais au pilote
    pour les injoignables. Les clés d'idempotence sont stables : un rejeu
    après un envoi réussi ne dépose aucun doublon. Rend le compte agrégé
    (`closed`, `batches`, `senders`, `informed`, `unreachable`, `pilots`,
    `marked`, `warnings`)."""
    out: dict = {"closed": 0, "batches": 0, "senders": [], "informed": [],
                 "unreachable": [], "pilots": [], "marked": 0, "warnings": []}
    rows = storage.of(db).mailbox.returns_pending(agent, limit=limit)
    batches: dict[tuple[str, float], list[dict]] = {}
    for row in rows:
        batches.setdefault(
            (str(row.get("recipient") or ""), float(row.get("closed_ts") or 0.0)),
            []).append(row)
    for (retired, closed_ts), batch_rows in sorted(
            batches.items(), key=lambda item: (item[0][1], item[0][0])):
        delivered = _deliver_batch(
            db, cfg, retired, closed_ts, batch_rows,
            actor=str(batch_rows[0].get("retired_by") or "?"),
            reason=str(batch_rows[0].get("retired_reason") or ""))
        out["batches"] += 1
        out["closed"] += delivered["closed"]
        out["marked"] += delivered["marked"]
        for key in ("senders", "informed", "unreachable", "pilots"):
            out[key].extend(delivered[key])
        out["warnings"].extend(delivered["warnings"])
    for key in ("senders", "informed", "unreachable", "pilots"):
        out[key] = sorted(set(out[key]))
    return out


def retire(cfg: Config, db, name: str, *, reason: str, actor: str | None = None,
           env: dict | None = None) -> dict:
    """Retire `name` ; RetireError si un bail, un lot ou une veille le retient."""
    from . import watch

    reason = " ".join((reason or "").split())
    if not reason:
        raise RetireError("agent retire: --reason is required (the decision or the lot)")
    who = work.corrector(cfg, db, actor, what="agent retire", auditor=True)
    closed: list[dict] = []
    with db.transaction() as tx:
        result = storage.of(tx).agents.retire(name, actor=who, reason=reason)
        if result is None:
            raise RetireError("agent retire: unknown agent: %s" % name)
        agent = result["name"]
        if result["blockers"]:
            raise RetireError("agent retire refused: %s %s" % (
                agent, "; ".join(_blocker_text(agent, b) for b in result["blockers"])))
        # #8113 : la fermeture du courrier est DANS la transaction du retrait,
        # sous le verrou exclusif de la ligne du registre. Elle vaut aussi
        # pour un agent déjà retiré : une boîte laissée en attente par la
        # version d'avant est soldée par le geste suivant.
        closed = storage.of(tx).mailbox.close_for_retired(
            agent, {"by": who, "reason": reason, "at": time.time()})
        event = None
        if result["changed"]:
            event = storage.of(tx).events.record(
                RETIRED_EVENT, "agent:%s" % agent, None,
                {"agent": agent, "actor": who, "messages_closed": len(closed)})
    warnings: list[str] = []
    if not result["changed"]:
        warnings.append("%s was already retired: registry unchanged%s" % (
            agent, ", %d pending message(s) closed" % len(closed) if closed else ""))
    if agent == watch.SENDER:
        units = legacy_units(env)
        if units:
            warnings.append(
                "legacy units of this host still send as %s (they are not the agent; "
                "their fate is another decision): %s" % (watch.SENDER, ", ".join(units)))
    try:
        # La file durable est écrite (meta.retired) ; ce passage la solde. Un
        # échec laisse le lot en file : `ameesh mail returns --replay`, ou le
        # prochain retrait du même agent, le reprend sous la même clé.
        returned = replay_returns(cfg, db, agent=agent)
    except Exception as exc:  # noqa: BLE001 - le retrait est déjà committé
        returned = {"closed": len(closed), "batches": 0, "senders": [],
                    "informed": [], "unreachable": [], "pilots": [],
                    "marked": 0, "warnings": []}
        warnings.append("return notices not sent (%s)" % type(exc).__name__)
    warnings.extend(returned.pop("warnings", []))
    if returned["unreachable"]:
        warnings.append(
            "%d pending message(s) closed; sender(s) with no live mailbox: %s "
            "(relayed to the pilot)" % (
                returned["closed"], ", ".join(returned["unreachable"])))
    return {"agent": agent, "changed": bool(result["changed"]), "actor": who,
            "event": event, "warnings": warnings, "mail": returned}


def restore(cfg: Config, db, name: str, *, actor: str | None = None) -> dict:
    """L'inverse de `retire` : l'agent redevient désignable, réclamable et joignable."""
    who = work.corrector(cfg, db, actor, what="agent restore", auditor=True)
    with db.transaction() as tx:
        result = storage.of(tx).agents.restore(name, actor=who)
        if result is None:
            raise RetireError("agent restore: unknown agent: %s" % name)
        agent = result["name"]
        event = None
        if result["changed"]:
            event = storage.of(tx).events.record(
                RESTORED_EVENT, "agent:%s" % agent, None, {"agent": agent, "actor": who})
    warnings = [] if result["changed"] else ["%s was not retired: nothing changed" % agent]
    return {"agent": agent, "changed": bool(result["changed"]), "actor": who,
            "event": event, "warnings": warnings}
