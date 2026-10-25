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


def retire(cfg: Config, db, name: str, *, reason: str, actor: str | None = None,
           env: dict | None = None) -> dict:
    """Retire `name` ; RetireError si un bail, un lot ou une veille le retient."""
    from . import watch

    reason = " ".join((reason or "").split())
    if not reason:
        raise RetireError("agent retire: --reason is required (the decision or the lot)")
    who = work.corrector(cfg, db, actor, what="agent retire", auditor=True)
    with db.transaction() as tx:
        result = storage.of(tx).agents.retire(name, actor=who, reason=reason)
        if result is None:
            raise RetireError("agent retire: unknown agent: %s" % name)
        agent = result["name"]
        if result["blockers"]:
            raise RetireError("agent retire refused: %s %s" % (
                agent, "; ".join(_blocker_text(agent, b) for b in result["blockers"])))
        event = None
        if result["changed"]:
            event = storage.of(tx).events.record(
                RETIRED_EVENT, "agent:%s" % agent, None, {"agent": agent, "actor": who})
    warnings = []
    if not result["changed"]:
        warnings.append("%s was already retired: nothing changed" % agent)
    if agent == watch.SENDER:
        units = legacy_units(env)
        if units:
            warnings.append(
                "legacy units of this host still send as %s (they are not the agent; "
                "their fate is another decision): %s" % (watch.SENDER, ", ".join(units)))
    return {"agent": agent, "changed": bool(result["changed"]), "actor": who,
            "event": event, "warnings": warnings}


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
