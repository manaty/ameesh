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


def _return_notices(db, agent: str, closed: list[dict], *,
                    actor: str, reason: str) -> dict:
    """#8113 : rend à chaque expéditeur VIVANT le courrier clos au retrait.

    Rend `{closed, senders, informed, unreachable, warnings}`. Un expéditeur
    retiré, arrêté, humain, système (`ameesh`, `veille`) ou absent du registre
    n'a pas de boîte lue : il est dit `unreachable`, et le courrier reste
    lisible dans le fil. Best effort : une notice ratée ne défait pas le
    retrait déjà committé."""
    from . import mail, registry, undeliverable

    summary: dict = {"closed": len(closed), "senders": [], "informed": [],
                     "unreachable": [], "warnings": []}
    groups: dict[str, list[dict]] = {}
    for row in closed:
        groups.setdefault(str(row.get("sender") or "").strip(), []).append(row)
    summary["senders"] = sorted(groups)
    for sender, rows in sorted(groups.items()):
        if (not sender or sender in ("ameesh", agent) or sender.startswith("human:")
                or undeliverable.watch_sender(sender)):
            summary["unreachable"].append(sender or "?")
            continue
        try:
            target = registry.get(db, sender)
        except Exception as exc:  # noqa: BLE001 - un avis ne défait pas le retrait
            summary["warnings"].append("sender %s not read (%s)"
                                       % (sender, type(exc).__name__))
            summary["unreachable"].append(sender)
            continue
        if (target is None or target.get("retired_ts") is not None
                or target.get("status") == "stopped"):
            summary["unreachable"].append(sender)
            continue
        lots = {str(row.get("work_item_id") or "").strip() for row in rows}
        lot = next(iter(lots)) if len(lots) == 1 else ""
        try:
            mail.send(db, "ameesh", sender,
                      _returned_body(agent, rows, actor=actor, reason=reason),
                      kind="event",
                      payload={"returned": {
                          "recipient": agent,
                          "messages": [int(row["id"]) for row in rows],
                          "by": actor, "reason": reason}},
                      work_item_id=lot or None)
        except Exception as exc:  # noqa: BLE001 - idem
            summary["warnings"].append("return notice to %s not sent (%s)"
                                       % (sender, type(exc).__name__))
            summary["unreachable"].append(sender)
        else:
            summary["informed"].append(sender)
    return summary


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
            agent, {"by": who, "reason": reason})
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
        returned = _return_notices(db, agent, closed, actor=who, reason=reason)
    except Exception as exc:  # noqa: BLE001 - le retrait est déjà committé
        returned = {"closed": len(closed), "senders": [], "informed": [],
                    "unreachable": [], "warnings": []}
        warnings.append("return notices not sent (%s)" % type(exc).__name__)
    warnings.extend(returned.pop("warnings", []))
    if returned["unreachable"]:
        warnings.append(
            "%d pending message(s) closed; sender(s) with no live mailbox: %s"
            % (len(closed), ", ".join(returned["unreachable"])))
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
