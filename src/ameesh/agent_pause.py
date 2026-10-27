# SPDX-License-Identifier: AGPL-3.0-only
"""L87b: a human's durable pause, separate from budget/pressure and leases.

The authenticated API supplies actor/principal; the local CLI acts for its
own human. Clearing this flag never changes status or adds a resume prompt.
"""
from __future__ import annotations

import math
import re
import time

from . import storage
from .config import NAME_RE

REASON_MAX = 2000
#: #3961 (issue #3014) : une pause posée sans échéance ni `indefinite` est
#: plafonnée à 4 h ; à l'échéance, l'exécuteur la lève (`expire`).
DEFAULT_MAX_S = 4 * 3600.0
HUMAN_RE = re.compile(r"human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")


class PauseRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def change(db, name: str, *, paused: bool, reason: str, actor: str,
           principal: str | None = None, expect_reason: str | None = None,
           not_after: float | None = None, until: float | None = None,
           indefinite: bool = False, now: float | None = None) -> dict:
    """Change the hold and record by whom/for whom, once per transition.

    Native operator operation, absent from the mediated executor contract.
    Authorization belongs to the authenticated caller, never a request body
    or the server's agent identity.

    `expect_reason` (#2152) : avec paused=False, ne lève que la pause dont la
    raison est exactement celle-là (reprise conditionnelle, atomique côté
    base) : une pause étrangère qui aurait remplacé la nôtre n'est jamais
    levée. Refusé pour poser une pause.

    `not_after` (#2184/R3) : avec paused=True, échéance absolue (epoch) que la
    base compare à son horloge sous le verrou de ligne ; au-delà, la pose est
    refusée (`changed=False`, `refusal='not_after'`) au lieu de committer en
    retard. Refusé pour lever une pause.

    `until` (#3961) : échéance absolue (epoch) de la pause posée ; la base
    refuse une échéance déjà passée (`refusal='until_past'`) et l'exécuteur
    lève la pause à l'échéance (`expire`). Sans `until` ni `indefinite`,
    l'échéance vaut `now + DEFAULT_MAX_S` et le résultat porte
    `capped=True`. `indefinite` pose une pause sans fin, choix explicite que
    l'alerte `operator_pause_long` signale.
    """
    principal = actor if principal is None else principal
    if (not isinstance(actor, str) or not HUMAN_RE.fullmatch(actor)
            or not isinstance(principal, str) or not HUMAN_RE.fullmatch(principal)):
        raise PauseRefused("bad_actor", "a human actor and principal are required")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PauseRefused("bad_agent", "a valid agent name is required")
    if not isinstance(paused, bool):
        raise PauseRefused("bad_pause", "paused must be a boolean")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        raise PauseRefused("bad_reason", "reason: 1 to %d characters required" % REASON_MAX)
    if expect_reason is not None:
        if paused:
            raise PauseRefused("bad_expect", "expect_reason only lifts a pause")
        if (not isinstance(expect_reason, str) or not expect_reason.strip()
                or len(expect_reason) > REASON_MAX):
            raise PauseRefused(
                "bad_expect", "expect_reason: 1 to %d characters required" % REASON_MAX)
    if not_after is not None:
        if not paused:
            raise PauseRefused("bad_not_after", "not_after only applies to a pause")
        if (isinstance(not_after, bool) or not isinstance(not_after, (int, float))
                or not math.isfinite(not_after) or not_after <= 0):
            raise PauseRefused("bad_not_after", "not_after must be a positive epoch")
    if not paused and (until is not None or indefinite):
        raise PauseRefused("bad_until", "until/indefinite only apply to a pause")
    if until is not None:
        if indefinite:
            raise PauseRefused("bad_until", "until and indefinite are exclusive")
        if (isinstance(until, bool) or not isinstance(until, (int, float))
                or not math.isfinite(until) or until <= 0):
            raise PauseRefused("bad_until", "until must be a positive epoch")
    capped = paused and until is None and not indefinite
    if capped:
        until = (time.time() if now is None else float(now)) + DEFAULT_MAX_S
    if storage.is_remote(db):
        raise PauseRefused("native_only", "operator pause needs a native operator connection")
    result = storage.of(db).agents.operator_pause(
        name, paused=paused, reason=reason.strip(), actor=actor, principal=principal,
        expect_reason=expect_reason.strip() if expect_reason is not None else None,
        not_after=not_after, until=until if paused else None,
        indefinite=bool(paused and indefinite))
    if result is None:
        raise PauseRefused("unknown_agent", "unknown agent: %s" % name)
    if paused:
        result = dict(result, capped=capped and bool(result.get("changed")))
    # #2184/N1 : une levée conditionnelle doit encore aboutir si l'agent est
    # devenu `externe` pendant l'attente du drain (0459). La pose, elle, reste
    # interdite sur un agent non-execute.
    levee_conditionnelle = not paused and expect_reason is not None
    if result.get("mode") != "execute" and not levee_conditionnelle:
        raise PauseRefused("external_agent", "an external session cannot be paused by the runner")
    return result


def expire(db, name: str | None = None) -> list[dict]:
    """#3961 : lève les pauses échues (toutes, ou celle de `name`), une fois
    chacune, avec un événement au nom de l'humain qui les avait posées."""
    if storage.is_remote(db):
        return []
    return storage.of(db).agents.operator_pause_expire(name)


def parse_until(text: str, *, now: float | None = None) -> float:
    """`--until` : epoch, ou date ISO 8601 (sans fuseau = UTC)."""
    import datetime as dt
    text = (text or "").strip()
    try:
        value = float(text)
    except ValueError:
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("--until: an epoch or an ISO 8601 date is required: %r"
                             % text) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        value = moment.timestamp()
    if not math.isfinite(value) or value <= 0:
        raise ValueError("--until must be a positive epoch: %r" % text)
    return value


def until_label(ts) -> str:
    """Échéance lisible (UTC, à la minute)."""
    import datetime as dt
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def cmd_cli(cfg, args, *, paused: bool) -> int:
    """Local human CLI. No client-supplied deputyship, no agent impersonation."""
    import json
    import sys
    from . import db as db_mod, identity, serve

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            binding = identity.resolve_binding(cfg, db)
            if binding.name or binding.ok:
                raise PauseRefused("bad_actor", "an agent session cannot act as the local human")
            human = serve.owner(cfg, args.human)
            expect = getattr(args, "expect_reason", None)
            not_after = getattr(args, "not_after", None)
            # #3961 : `--for`, `--until` ou `--indefinite` ; aucun des trois :
            # échéance par défaut (DEFAULT_MAX_S), dite sur la sortie d'erreur.
            until = getattr(args, "until", None)
            pour = getattr(args, "for_s", None)
            if pour is not None:
                until = time.time() + pour
            out = change(db, args.agent, paused=paused, reason=args.reason,
                         actor=human, principal=human, expect_reason=expect,
                         not_after=not_after, until=until,
                         indefinite=bool(getattr(args, "indefinite", False)))
        except (PauseRefused, ValueError) as exc:
            # Le même chemin sert « resume --operator » : nommer le geste
            # réellement demandé (N3 de la R2 sur #1462).
            print("ameesh operator %s: %s" % ("pause" if paused else "resume", exc),
                  file=sys.stderr)
            return 1
        if out.get("capped"):
            print("%s: no --for/--until/--indefinite: the pause ends at %s (default "
                  "%dh); it is lifted then by the runner" % (
                      args.agent, until_label(out["until_ts"]),
                      int(DEFAULT_MAX_S // 3600)), file=sys.stderr)
        if out.get("refusal"):
            # #2184/R3 : dire pourquoi la pose a été refusée ; le JSON la porte
            # aussi (refusal=not_after), le drain la lit sans la confondre avec
            # une pause étrangère. En texte, le refus remplace le compte rendu
            # habituel (la pause n'est pas posée) et rend un code non nul
            # (#2498) ; en JSON, le refus est une réponse lue (rc 0).
            print("%s: operator pause refused (%s)" % (args.agent, out["refusal"]),
                  file=sys.stderr)
            if not args.json:
                return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        elif not paused and expect is not None:
            # #2152 : dire ce qui a réellement été levé — une pause étrangère
            # reste en place et le script de drain le lit.
            if out["changed"]:
                print("%s: operator pause cleared (expected reason matched)" % args.agent)
            elif out["operator_paused"]:
                print("%s: operator pause kept: the pause in place is not the "
                      "expected one" % args.agent)
            else:
                print("%s: operator pause already cleared" % args.agent)
        else:
            echeance = ""
            if paused and out["operator_paused"]:
                echeance = (" until %s" % until_label(out["until_ts"])
                            if out.get("until_ts") else " with no deadline")
            print("%s: operator pause %s%s%s" % (
                args.agent, "set" if out["operator_paused"] else "cleared", echeance,
                "" if out["changed"] else " (unchanged)"))
        return 0
    finally:
        db.close()
