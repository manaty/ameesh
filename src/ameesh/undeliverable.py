# SPDX-License-Identifier: AGPL-3.0-only
"""Courrier en souffrance : refusé à l'envoi, signalé, renvoyé.

Constat du 2026-10-10 sur la base du mesh : 42 messages entre agents jamais
livrés, sans qu'aucun expéditeur le sache.

* 27 étaient adressés à « orchestrator » (faute de frappe : l'agent est
  « orchestrateur ») : l'envoi avait CRÉÉ un agent fantôme de ce nom, et
  rapports, SHA de fusion et gels s'y sont accumulés toute la journée ;
* 15 étaient adressés à un agent arrêté à la main : son ancienne session,
  liée depuis à un autre agent, écrivait encore sous l'ancien nom
  (`--from`), et les réponses partaient vers une boîte que personne ne lit.

Les parades, appelées par `ameesh mail send|forward`, `ameesh alerts` /
`ameesh notify` et `ameesh projects` :

* `check_send`, avant tout dépôt (base seulement) : un destinataire absent du
  registre est refusé sans rien créer, avec les noms proches (distance
  d'édition, préfixe ; pour un rôle — « orchestrator », « orchestrateur » —
  les orchestrateurs de l'équipe de l'expéditeur) ; un destinataire ARRÊTÉ
  (`stopped` : ni en pause, ni au repos) est refusé sauf `--queue`, avec
  depuis quand, la raison, le responsable et l'agent qui a repris son
  travail s'il est connu ; une identité d'expéditeur arrêtée est refusée,
  avec l'identité liée à la session (`agent-mail whoami`) ; un courrier
  adressé à l'identité d'ÉMISSION de la veille (`watch.SENDER`, « veille »)
  est refusé : elle n'a pas de lecteur, les comptes rendus qu'on lui
  envoyait s'y perdaient (#3342) ;
* `alerts` : `mail_undeliverable`, du courrier en attente depuis plus de
  15 min chez un agent arrêté ou inexistant, pour le responsable humain et,
  par courrier (`orchestrator_briefs`), les orchestrateurs de l'équipe ;
* un humain DÉCLARÉ (`human:<id>` : `humans` de la configuration, Member du
  canon, ou humain déjà résolu en base) n'est pas une boîte d'agent : il lit
  par `ameesh decide` et par le fil. Son courrier n'est donc ni alerté
  `mail_undeliverable` ni expiré (#4519) ; un `human:<id>` non déclaré reste
  un nom inconnu, et l'alerte le dit (faute de frappe probable) ;
* `forward` : `ameesh mail forward <ancien> <nouveau>` re-livre le courrier en
  attente d'une boîte morte à un agent vivant, expéditeur et date d'origine
  gardés, renvoi noté ; l'original n'est plus en attente.
* `sweep` (L976) : le ménage de l'exécuteur re-livre le courrier d'un agent
  arrêté dont le successeur est connu, et expire au-delà de
  `mail_dead_letter_days` celui dont personne ne lit la boîte (arrêté sans
  successeur, nom absent du registre), avec une trace au fil : l'alerte
  `mail_undeliverable` peut ainsi se vider sans geste humain. `ameesh mail
  sweep` fait le même passage à la main.

Le SQL est dans le stockage (`storage.of(db).mailbox` : `dead_letters`,
`forward`, `expire_dead_letters`).
"""
from __future__ import annotations

import os
import shlex
import time

from . import db as db_mod
from . import fastmail, fil, registry, storage

#: `mail_undeliverable` : courrier en attente depuis 15 min
DEFAULT_THRESHOLD_S = 900.0
#: L976 : rétention du courrier en souffrance avant expiration, en jours
#: (`cfg.mail_dead_letter_days`, 0 = aucune expiration)
DEFAULT_DEAD_LETTER_DAYS = 7
#: noms proches proposés au plus
MAX_SUGGESTIONS = 5
#: orchestrateurs déclarés (comme `ameesh alerts --orchestrators`)
ORCHESTRATORS_ENV = "AMEESH_ALERT_ORCHESTRATORS"
#: raisons de `mail_undeliverable` : destinataire arrêté, ou absent du registre
REASONS = ("arrete", "inconnu")


class Refused(Exception):
    """Envoi ou renvoi refusé : rien n'est déposé ; le texte dit pourquoi et
    quoi faire."""


class RefusedOld(Refused):
    """Renvoi refusé à cause de l'ancien destinataire — relancé sous le verrou
    (« X is not stopped ») : sa boîte est de nouveau lue, aucun successeur n'y
    changera rien (#1615/N2). Le balayage quitte alors la boucle des
    successeurs au lieu de répéter le même refus pour chacun."""


# --------------------------------------------------------------------------
# petits outils
# --------------------------------------------------------------------------

def _moment(ts) -> str:
    return time.strftime("%Y-%m-%d at %H:%M", time.localtime(float(ts)))


#: `old_state` of `forward` (a JSON value, kept as is) → its English label
_STATE_LABELS = {"arrêté": "stopped", "absent du registre": "not in the registry"}


def state_label(state: str) -> str:
    """The display label of a `forward` `old_state` value."""
    return _STATE_LABELS.get(state, state)


def _span(seconds) -> str:
    from .sous_utilisation import _duration
    return _duration(max(60.0, float(seconds)))


def team_of(row: dict | None) -> str:
    """L'équipe d'un agent (`team`), à défaut son chantier ; vide si aucun."""
    if not row:
        return ""
    return str(row.get("team") or "").strip() or str(row.get("chantier") or "").strip()


def distance(a: str, b: str) -> int:
    """Distance d'édition entre deux noms, sans tenir compte de la casse :
    insertions, suppressions, substitutions, et transposition de deux
    lettres voisines (« cluade3 » est à 1 de « claude3 »)."""
    a, b = a.lower(), b.lower()
    before: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], before[j - 2] + 1)
        before, previous = previous, current
    return previous[len(b)]


def role_of(name: str) -> str | None:
    """Le rôle que désigne un nom inconnu (« orchestrator », « orchestrateur »,
    « orch ») : l'envoi vise alors l'orchestrateur de l'équipe."""
    low = (name or "").strip().lower()
    if low.startswith("orchestr") or low == "orch":
        return "orchestrateur"
    return None


def declared_orchestrators() -> tuple:
    return tuple(n.strip() for n in (os.environ.get(ORCHESTRATORS_ENV) or "").split(",")
                 if n.strip())


def known_orchestrators(cfg, db, rows: list, *, declared=None, canons=None) -> list[str]:
    """Les orchestrateurs destinataires d'alerte
    (`sous_utilisation.alert_orchestrators` : déclarés ou rôle
    `orchestrateur` au canon) parmi `rows` (lignes du registre). L'historique
    des lots confiés ne confère pas ce rôle (#1114)."""
    from . import sous_utilisation as su
    if declared is None:
        declared = declared_orchestrators()
    try:
        return su.alert_orchestrators(cfg, db, rows, declared=declared, canons=canons)
    except db_mod.SchemaMissing:
        raise
    except Exception:  # un canon illisible ne fait pas tomber l'envoi ni l'alerte
        return [n for n in {su._bare(d) for d in declared} if n in {r["name"] for r in rows}]


def team_orchestrators(rows: list, known: list[str], teams, *, exclude=()) -> list[str]:
    """Parmi les orchestrateurs `known`, ceux qui servent l'une des `teams` :
    même équipe, ou sans équipe (un hub explicitement déclaré, #1114) ;
    `teams` vide : tous. Jamais un agent arrêté (il ne lit pas son courrier),
    ni `exclude`."""
    by_name = {row["name"]: row for row in rows}
    wanted = {team for team in teams or () if team}
    out = []
    for name in known:
        row = by_name.get(name) or {}
        if name in exclude or row.get("status") == "stopped":
            continue
        team = team_of(row)
        if wanted and team and team not in wanted:
            continue
        out.append(name)
    return sorted(out)


def suggestions(dest: str, rows: list, *, role_names=(), team: str = "") -> list[tuple]:
    """Les noms proches de `dest` dans le registre (`rows`), avec la raison :
    d'abord les orchestrateurs de l'équipe si `dest` désigne un rôle
    (`role_names`, déjà filtrés), puis la distance d'édition (2 lettres au
    plus, 1 pour un nom de 4 lettres ou moins), puis le même début. Au plus
    MAX_SUGGESTIONS ; un agent arrêté est signalé comme tel."""
    by_name = {row["name"]: row for row in rows}
    out: list[tuple] = []
    seen = {dest}

    def add(name: str, why: str) -> None:
        if name in seen:
            return
        seen.add(name)
        if (by_name.get(name) or {}).get("status") == "stopped":
            why += ", stopped"
        out.append((name, why))

    for name in role_names:
        add(name, "orchestrator of team %s" % team
            if team and team_of(by_name.get(name)) == team else "orchestrator")
    limit = 2 if len(dest) >= 5 else 1
    for gap, name in sorted((distance(dest, name), name) for name in by_name):
        if gap <= limit:
            add(name, "%d letter%s apart" % (gap, "s" if gap > 1 else ""))
    low = dest.lower()
    if len(low) >= 3:
        for name in sorted(by_name):
            other = name.lower()
            if other.startswith(low) or (len(other) >= 3 and low.startswith(other)):
                add(name, "same prefix")
    return out[:MAX_SUGGESTIONS]


def _known_human(cfg, db, name: str, *, canons=None) -> str | None:
    from . import work
    try:
        return work.known_human(db, name, cfg, canons=canons)
    except db_mod.SchemaMissing:
        raise
    except Exception:
        return None


def human_recipient(cfg, db, name: str, *, canons=None) -> bool:
    """#4519 : `name` désigne un humain DÉCLARÉ (`human:<id>`), pas une boîte
    d'agent. Un humain lit par `ameesh decide` et par le fil : son courrier
    n'est ni « non distribuable » ni expiré. Un `human:<id>` non déclaré reste
    un nom inconnu (faute de frappe probable) ; un nom nu n'est jamais un
    destinataire humain ici."""
    prefix = "human:"
    name = (name or "").strip()
    if not name.startswith(prefix):
        return False
    bare = name[len(prefix):].strip()
    if not bare or ":" in bare:
        return False
    return _known_human(cfg, db, bare, canons=canons) == name


def _canons_once(cfg):
    """Un chargeur de canon lu au plus une fois par passage (`known_human`)."""
    from . import work
    loaded: list = []

    def canons() -> list:
        if not loaded:
            loaded.append(work.configured_canons(cfg))
        return loaded[0]
    return canons


def without_human_boxes(cfg, db, rows: list[dict], *, canons=None) -> list[dict]:
    """#4519 : écarte les lignes de courrier en souffrance dont le destinataire
    est un humain DÉCLARÉ : il lit par `ameesh decide` et par le fil, pas par
    une boîte d'agent — son courrier n'est pas perdu et n'a pas à alerter.
    `canons` : un chargeur déjà en cache chez l'appelant, sinon lu ici une
    fois."""
    if not rows:
        return rows
    loader = canons if canons is not None else _canons_once(cfg)
    cache: dict = {}

    def declared(name: str) -> bool:
        if name not in cache:
            cache[name] = human_recipient(cfg, db, name, canons=loader)
        return cache[name]

    return [row for row in rows if not declared(str(row.get("recipient") or ""))]


def dead_letters(cfg, db, *, canons=None) -> list[dict]:
    """Les lignes de `mailbox.dead_letters` qui sont vraiment des boîtes
    mortes : un humain déclaré n'en est pas une (#4519)."""
    return without_human_boxes(cfg, db, storage.of(db).mailbox.dead_letters(),
                               canons=canons)


# --------------------------------------------------------------------------
# arrêt : depuis quand, pourquoi, qui, et qui a repris
# --------------------------------------------------------------------------

def stop_facts(row: dict, now: float | None = None) -> str:
    """« depuis le 2026-10-07 à 14:02 (3 j 2 h) ; raison : manuel — arrêté à la
    main ; responsable : human:x » — ce que dit un refus ou une alerte."""
    now = time.time() if now is None else float(now)
    parts = []
    since = row.get("status_since_ts")
    if since:
        parts.append("since %s (%s)" % (_moment(since), _span(now - float(since))))
    reason = " — ".join(str(p) for p in (row.get("stop_reason"), row.get("status_text")) if p)
    parts.append("reason: %s" % (reason or "unknown"))
    parts.append("responsible: %s" % (row.get("responsible") or "none"))
    return "; ".join(parts)


def successor(db, row: dict | None, *, bindings: list | None = None) -> list[str]:
    """Les agents qui ont repris le travail d'un agent arrêté, s'il en est
    connu : ceux auxquels sa dernière session est désormais liée (`ameesh mail
    bind`, L41).

    #1615/N3 : une liste, la liaison la plus récente d'abord (`listing` rend
    les liaisons récentes d'abord) — et non une chaîne triée alphabétiquement
    que chaque appelant redécoupait. Un agent lié plusieurs fois ne figure
    qu'une fois, à la place de sa liaison la plus récente."""
    session = (row or {}).get("session_id")
    if not session:
        return []
    if bindings is None:
        try:
            bindings = storage.of(db).session_bindings.listing()
        except db_mod.SchemaMissing:
            raise
        except db_mod.DbError:
            return []
    heirs: list[str] = []
    for binding in bindings:
        agent = binding.get("agent")
        if not agent or binding.get("session_id") != session or agent == row["name"] \
                or binding.get("revoked_ts"):
            continue
        if agent not in heirs:
            heirs.append(agent)
    return heirs


# --------------------------------------------------------------------------
# à l'envoi (`agent-mail send`)
# --------------------------------------------------------------------------

def watch_sender(name: str) -> bool:
    """L'identité qui ÉMET la consigne de veille (`watch.SENDER`, « veille »)
    n'a pas de boîte lue : un courrier qui lui est adressé est perdu (#3342).

    Nom comparé à `watch.SENDER`, importé ici : `watch` importe ce module
    depuis `send()`, un import de module serait circulaire."""
    from . import watch
    return name == watch.SENDER


def watch_sender_text(name: str) -> str:
    """Le refus d'un courrier adressé à l'identité d'émission de la veille,
    avec le geste qui, lui, laisse une trace lisible."""
    return "\n  ".join([
        "%s is the sender of the `ameesh watch` instruction: nobody reads its "
        "mailbox, so a reply sent to it is lost." % name,
        "write what you did in the readable thread instead: report to your pilot or "
        "orchestrator (`agent-mail send <pilot> …` lands in `ameesh fil`), and send "
        "anything that needs a decision there — never to %s" % name,
    ])


def unknown_text(cfg, db, dest: str, rows: list | None = None, *, sender: str | None = None,
                 canons=None) -> str:
    """Le refus d'un destinataire absent du registre, avec les noms proches."""
    rows = registry.overview(db) if rows is None else rows
    by_name = {row["name"]: row for row in rows}
    lines = ["\"%s\" is not in the registry (unknown agent): nobody would read this "
             "message, and no agent is created." % dest]
    human = _known_human(cfg, db, dest)
    if human:
        lines.append("\"%s\" designates a human (%s): the mailbox only reaches "
                     "agents." % (dest, human))
    team = team_of(by_name.get(sender)) if sender else ""
    role_names: list[str] = []
    if role_of(dest):
        known = known_orchestrators(cfg, db, rows, canons=canons)
        role_names = team_orchestrators(rows, known, {team} if team else (),
                                        exclude=(sender,) if sender else ())
    near = suggestions(dest, rows, role_names=role_names, team=team)
    if near:
        lines.append("close names: " + ", ".join("%s (%s)" % pair for pair in near))
    else:
        lines.append("no close name in the registry")
    lines.append("known agents: agent-mail list; a new agent registers first "
                 "(agent-runner register <name> <harness> …)")
    return "\n  ".join(lines)


def retired_text(row: dict, *, sender: bool) -> str:
    """#3460 : le refus d'un agent retiré (`ameesh agent retire`), destinataire
    ou expéditeur : depuis quand, par qui, pourquoi, et le geste inverse."""
    name = row["name"]
    facts = "since %s, by %s: %s" % (
        _moment(row["retired_ts"]) if row.get("retired_ts") else "?",
        row.get("retired_by") or "?", row.get("retired_reason") or "no reason given")
    what = ("this identity no longer sends mail" if sender
            else "nobody reads its mailbox, and nothing is queued for it")
    return "%s is retired (%s): %s.\n  to bring it back: ameesh agent restore %s" % (
        name, facts, what, name)


def stopped_recipient_text(db, row: dict, now: float | None = None) -> str:
    """Le refus d'un destinataire arrêté : depuis quand, pourquoi, qui, qui a
    repris, et les deux gestes possibles."""
    name = row["name"]
    lines = ["%s is stopped (%s): nobody reads its mailbox." % (name, stop_facts(row, now))]
    heirs = successor(db, row)
    if heirs:
        lines.append("its work was taken over by %s (its last session is bound to it): "
                     "agent-mail send %s \"…\"" % (", ".join(heirs), heirs[0]))
    lines.append("to drop it anyway (read when it resumes): --queue; to restart it: "
                 "ameesh resume %s" % name)
    return "\n  ".join(lines)


def _whoami(binding) -> str:
    """L'identité d'une session, comme l'écrit `agent-mail whoami`."""
    if binding.source == "session":
        return "%s [session] %s" % (binding.name, binding.describe_session())
    if binding.source == "explicit":
        return "%s (AGENT_MAIL_NAME)" % binding.name
    return "%s [%s]" % (binding.name, binding.source)


def stopped_sender_text(cfg, db, row: dict, *, explicit: bool, binding=None,
                        now: float | None = None) -> str:
    """Le refus d'une identité d'expéditeur arrêtée, avec l'identité liée à
    cette session (`agent-mail whoami`) et l'agent qui a repris, s'il est connu."""
    from . import identity

    name = row["name"]
    lines = ["sender %s is stopped (%s): replies would go to a mailbox that "
             "nobody reads." % (name, stop_facts(row, now))]
    heirs = successor(db, row)
    if heirs:
        lines.append("its work was taken over by %s" % ", ".join(heirs))
    if binding is None:
        binding = identity.resolve_binding(cfg, db)
    if explicit and binding.ok and binding.name != name:
        lines.append("identity bound to this session (agent-mail whoami): %s — resend "
                     "without --from (or --from %s)" % (_whoami(binding), binding.name))
    elif binding.ok and binding.name == name:
        if binding.source == "session":
            lines.append("this session is bound to %s (agent-mail whoami: %s): unbind it "
                         "(agent-mail unbind --session %s --harness %s) and bind it to the agent "
                         "it now carries (agent-mail bind <agent> …)"
                         % (name, _whoami(binding), (binding.session or {}).get("session_id"),
                            (binding.session or {}).get("harness")))
        else:
            lines.append("this session speaks on behalf of %s (agent-mail whoami: %s): set "
                         "AGENT_MAIL_NAME=<live agent>, or pass --from <live agent>"
                         % (name, _whoami(binding)))
    else:
        lines.append("identity of this session (agent-mail whoami): %s — pass --from "
                     "<live agent>" % binding.describe())
    return "\n  ".join(lines)


def registry_rows(db, names: list[str]) -> dict:
    """`{name: registry row or None}` for the send checks.

    Path B: a mediated harness session has no `agents.get` (contract 1.1),
    only the reduced directory (`agents.overview`: name, team, status). The
    same checks then run on it, with fewer facts in the refusal texts (no
    stop date, reason or responsible)."""
    from .mediated_executor.contract import NotSupportedRemotely

    try:
        return dict(zip(names, db_mod.batched(db, lambda d: [registry.get(d, n)
                                                               for n in names])))
    except NotSupportedRemotely:
        directory = {row["name"]: row for row in registry.overview(db)}
        return {name: directory.get(name) for name in names}


def _mail_lifts_sleep(cfg, db, row: dict, sender: str) -> bool:
    """#4619 : ce courrier lèverait-il le sommeil automatique du destinataire ?

    Même règle que le découpage (#4610) : le courrier du pilote d'un projet de
    l'agent ou d'un orchestrateur déclaré lève le sommeil, celui d'un autre
    expéditeur attend. Une lecture en échec ne promet pas un réveil : on
    avertit, comme le sommeil qui ne se lève pas non plus dans ce cas."""
    from . import file_decoupage
    try:
        listing = registry.overview(db)
        senders, error = file_decoupage.mail_lifters(cfg, db, listing, row)
    except Exception:  # noqa: BLE001 - le doute laisse l'avertissement
        return False
    return not error and sender in senders


def wake_note(cfg, db, name: str, row: dict, *, sender: str | None = None) -> list[str]:
    """#858 : avertissement d'un destinataire inscrit mais NON réveillable à
    l'instant (profil divergé en attente du prochain canon sync, responsable
    manquant, éphémère échu…). Le message est déposé quand même — c'est lui le
    réveil durable : il ouvrira un tour dès que l'agent redeviendra réclamable
    (la passe de l'exécuteur relit `claimable` en continu). Rend `[]` quand un
    réveil est possible, ou quand aucun n'est attendu : une identité `externe`
    (session humaine) est lue à son prochain tour, et un destinataire arrêté
    est déjà refusé ou averti par l'appelant. Le répertoire réduit du contrat
    1.1 (pas de `mode`) ne permet pas de juger : on n'avertit pas.

    #4610 : le sommeil automatique du découpage est réveillable pour qu'une
    attribution passe, mais un courrier ordinaire ne le lève pas ; l'expéditeur
    reçoit donc un avertissement, comme pour une pause d'opérateur.

    #4619 : l'avertissement tient compte de l'expéditeur — le courrier du
    pilote ou d'un orchestrateur lève le sommeil, donc il ne s'avertit pas
    lui-même d'un réveil qui n'aura pas lieu."""
    if "mode" not in row or (row.get("mode") or "execute") == "externe":
        return []
    try:
        ok, why, _note = registry.wake_status(db, cfg, name)
    except db_mod.DbError as exc:
        # #5099 : ce diagnostic est consultatif ; le dépôt durable de
        # l'alerte de migration ne doit pas attendre le schéma de la tête.
        if exc.sqlstate not in ("42703", "42P01"):
            raise
        return ["%s: wake status unavailable (schema not aligned); message stored"
                % name]
    if ok:
        # #4610 : seuls une attribution, un lot démarrable, un courrier du
        # pilote ou d'un orchestrateur, ou l'échéance lèvent ce sommeil ; un
        # message d'un autre expéditeur attend. Le taire ferait croire à un
        # réveil qui n'arrivera pas.
        from . import file_decoupage
        if (row.get("operator_paused")
                and file_decoupage.is_sleep_reason(row.get("operator_pause_reason"))):
            if sender and _mail_lifts_sleep(cfg, db, row, sender):
                return []
            return ["%s is under the automatic roadmap sleep: the message is stored, "
                    "but it does not open a turn by itself; the sleep is lifted by a "
                    "lot assigned to it, a startable lot in its project, mail from its "
                    "pilot or an orchestrator, or its deadline; if the request cannot "
                    "wait, address another agent" % name]
        return []
    if row.get("operator_paused"):
        # #2200 : la pause humaine a sa propre sortie ; « prochain canon sync »
        # serait faux et ferait attendre un réveil qui n'arrivera pas seul.
        # #3961 : `why` porte l'échéance ; la pause se lève seule à l'échéance
        return ["%s is under operator pause (%s): the message is stored, and no turn "
                "opens until the pause ends at its deadline or a human lifts it "
                "(ameesh resume %s --operator "
                "--reason <reason>); if the request cannot wait, address another agent"
                % (name, why, name)]
    return ["%s is not wakeable right now (%s): the message is stored, but no turn "
            "opens until it is claimable again (next canon sync on its host); if the "
            "request cannot wait, address another agent" % (name, why)]


def check_send(cfg, db, *, sender: str, dest: str, queue: bool = False,
               explicit: bool = False, binding=None, now: float | None = None) -> list[str]:
    """Contrôle d'un envoi `agent-mail send`, AVANT tout dépôt (base seulement).

    Lève `Refused` (rien n'est déposé, aucun agent créé) : identité
    d'expéditeur arrêtée ou retirée ; destinataire absent du registre ;
    destinataire retiré, même avec `queue` (#3460) ; destinataire arrêté sans
    `queue` ; courrier adressé à l'identité d'émission de la veille
    (`veille`, qui ne lit aucune boîte — #3342). `veille` reste une identité
    d'émission système, comme « ameesh » : retirée, elle émet encore les
    veilles et les réveils (#3460). Rend les
    avertissements à dire (destinataire arrêté avec `queue` ; #858 :
    destinataire inscrit mais non réveillable à l'instant — le message part
    quand même, le réveil attend). `explicit` : l'expéditeur vient de
    `--from` ; `binding` : l'identité déjà résolue de la session, s'il y en a
    une. « all » ne contrôle que l'expéditeur (la diffusion écarte elle-même
    les arrêtés)."""
    names = [sender] if dest in ("all", sender) else [sender, dest]
    rows = registry_rows(db, names)
    row = rows.get(sender)
    if row is not None and row.get("status") == "stopped":
        raise Refused(stopped_sender_text(cfg, db, row, explicit=explicit, binding=binding,
                                          now=now))
    if row is not None and row.get("retired_ts") and not watch_sender(sender):
        raise Refused(retired_text(row, sender=True))
    if dest == "all":
        return []
    if watch_sender(dest):
        raise Refused(watch_sender_text(dest))
    row = rows.get(dest)
    if row is None:
        raise Refused(unknown_text(cfg, db, dest, sender=sender))
    if row.get("retired_ts"):
        raise Refused(retired_text(row, sender=False))
    if row.get("status") != "stopped":
        return wake_note(cfg, db, dest, row, sender=sender)
    if not queue:
        raise Refused(stopped_recipient_text(db, row, now))
    return ["%s is stopped (%s): message queued until it resumes (ameesh resume %s) "
            "or is forwarded (ameesh mail forward %s <agent>)"
            % (dest, stop_facts(row, now), dest, dest)]


# --------------------------------------------------------------------------
# alerte `mail_undeliverable`
# --------------------------------------------------------------------------

def by_recipient(rows: list) -> list[dict]:
    """Les lignes de `mailbox.dead_letters` (une par destinataire et
    expéditeur) regroupées par destinataire : `count`, `oldest_ts`,
    `newest_ts`, `unknown`, `senders` [(nom, nombre)] du plus fréquent au
    moins fréquent."""
    groups: dict = {}
    for row in rows:
        group = groups.setdefault(row["recipient"], {
            "recipient": row["recipient"], "unknown": bool(row.get("unknown")), "count": 0,
            "oldest_ts": None, "newest_ts": None, "senders": {}})
        n = int(row.get("n") or 0)
        group["count"] += n
        group["senders"][row["sender"]] = group["senders"].get(row["sender"], 0) + n
        for key, pick in (("oldest_ts", min), ("newest_ts", max)):
            value = row.get(key)
            if value is not None:
                group[key] = float(value) if group[key] is None else pick(group[key],
                                                                         float(value))
    out = []
    for group in groups.values():
        group["senders"] = sorted(group["senders"].items(), key=lambda kv: (-kv[1], kv[0]))
        out.append(group)
    return sorted(out, key=lambda g: g["recipient"])


def _senders(group: dict) -> str:
    return ", ".join("%s (%d)" % pair for pair in group["senders"])


def alerts(cfg, db, listing: list, now: float, *, threshold_s: float = DEFAULT_THRESHOLD_S,
           orchestrators_declared=(), canons=None) -> list[dict]:
    """`mail_undeliverable` : du courrier non remis depuis plus de `threshold_s`
    chez un agent arrêté (`reason: arrete`) ou absent du registre
    (`inconnu`). Une alerte par destinataire ; `responsible` : le responsable
    de l'agent arrêté, ou le plus fréquent des expéditeurs pour un nom inconnu ;
    `orchestrators` : ceux de l'équipe (le courrier de `ameesh notify` les
    prévient). Un seuil nul ou négatif désactive l'alerte. Un humain déclaré
    n'est pas une boîte morte (#4519)."""
    if threshold_s <= 0:
        return []
    due = [g for g in by_recipient(dead_letters(cfg, db, canons=canons))
           if g["oldest_ts"] is not None and now - g["oldest_ts"] >= threshold_s]
    if not due:
        return []
    from . import sous_utilisation as su

    agents = {row["name"]: row for row in listing}
    known = known_orchestrators(cfg, db, listing, declared=tuple(orchestrators_declared or ()),
                                canons=canons)
    bindings: list | None = None
    out = []
    for group in due:
        name = group["recipient"]
        row = agents.get(name)
        senders = [agents.get(sender) for sender, _n in group["senders"]]
        age = _span(now - group["oldest_ts"])
        extra: dict = {"senders": [sender for sender, _n in group["senders"]],
                       "count": group["count"]}
        if row is not None:
            if row.get("status") != "stopped":
                continue                  # relancé entre les deux lectures
            if bindings is None:
                bindings = _bindings(db)
            heirs = successor(db, row, bindings=bindings)
            heir_text = ", ".join(heirs)
            teams = {team_of(row)}
            reason = "arrete"
            responsible = row.get("responsible") or None
            detail = ("%d message(s) pending for %s for %s, stopped (%s): nobody will "
                      "read them — from %s. Re-deliver: ameesh mail forward %s %s; or restart "
                      "the agent: ameesh resume %s"
                      % (group["count"], age, name, stop_facts(row, now), _senders(group),
                         name, heirs[0] if heirs else "<live agent>", name))
            if heirs:
                detail += " (work taken over by %s)" % heir_text
            detail += _retention_note(cfg)
            extra.update(stop_reason=row.get("stop_reason") or None,
                         successor=heir_text or None, host=row.get("host") or None)
        else:
            teams = {team_of(r) for r in senders if r}
            reason = "inconnu"
            responsible = su._common_responsible([r for r in senders if r])
            role_names = (team_orchestrators(listing, known, teams, exclude=(name,))
                          if role_of(name) else [])
            near = suggestions(name, listing, role_names=role_names,
                               team=next(iter(teams)) if len(teams) == 1 else "")
            detail = ("%d message(s) pending for %s for \"%s\", not in the registry "
                      "(misspelled name?) — from %s. %sRe-deliver: ameesh mail forward %s <agent>, "
                      "and tell the senders the right name"
                      % (group["count"], age, name, _senders(group),
                         "Close names: %s. " % ", ".join(n for n, _why in near) if near else "",
                         shlex.quote(name)))
            detail += _retention_note(cfg)
            extra.update(suggestions=[n for n, _why in near])
        orchestras = team_orchestrators(listing, known, {t for t in teams if t},
                                        exclude=(name,))
        out.append(_alert(
            "mail_undeliverable", name, group["oldest_ts"], group["count"], threshold_s,
            detail, reason=reason, responsible=responsible, orchestrators=orchestras,
            **extra))
    return out


def _alert(kind, agent, since, value, threshold, detail, **extra) -> dict:
    from .exploitation import _alert as make
    return make(kind, agent, since, value, threshold, detail, **extra)


def brief_text(alert: dict) -> str:
    """#3036 : le texte du courrier en souffrance, sans destinataire.

    `alert_briefs` le route vers les orchestrateurs vivants, puis vers les
    auditeurs, le bureau ou le fil quand aucun ne l'est ; le texte ne dépend
    pas du destinataire."""
    name = alert.get("agent") or "?"
    count = int(alert.get("value") or 0)
    senders = ", ".join(alert.get("senders") or ()) or "?"
    if alert.get("reason") == "inconnu":
        near = alert.get("suggestions") or []
        text = ("Undeliverable mail: %d message(s) addressed to \"%s\", not in the "
                "registry (misspelled name?), waiting for more than %s, from %s; nobody will "
                "read them. %sRe-deliver them: ameesh mail forward %s <agent> (to yourself if they "
                "were meant for you), then tell the senders the right name."
                % (count, name, _span(alert.get("threshold") or 0), senders,
                   "Close names: %s. " % ", ".join(near) if near else "", shlex.quote(name)))
    else:
        heir = alert.get("successor")
        text = ("Undeliverable mail: %d message(s) addressed to %s, stopped, waiting "
                "for more than %s, from %s; nobody will read them. Re-deliver them to the agent "
                "carrying its work: ameesh mail forward %s %s; or have the agent restarted by "
                "its owner (ameesh resume %s)."
                % (count, name, _span(alert.get("threshold") or 0), senders, name,
                   heir.split(", ")[0] if heir else "<agent>", name))
    return text


def orchestrator_briefs(alert: dict) -> dict:
    """`mail_undeliverable` part aux orchestrateurs de l'équipe
    (`alert["orchestrators"]`), en courrier `event` : ce sont eux qui peuvent
    re-livrer (`ameesh mail forward`) ou faire relancer l'agent. Rend
    `{orchestrateur: texte}` ; jamais au destinataire mort.

    #3036 : le filtrage de vivacité et les replis sont faits par
    `alert_briefs.briefs` (auditeurs, bureau, fil) ; cette fonction garde le
    contrat historique pour ses appelants directs et les tests."""
    if alert.get("type") != "mail_undeliverable":
        return {}
    name = alert.get("agent") or "?"
    text = brief_text(alert)
    return {orch: text for orch in alert.get("orchestrators") or () if orch != name}


# --------------------------------------------------------------------------
# renvoi (`ameesh mail forward <ancien> <nouveau>`)
# --------------------------------------------------------------------------

def _refus_du_stockage(db, old: str, text: str) -> Refused:
    """Classe le refus du stockage pendant un renvoi (#1615/N2).

    `RefusedOld` quand l'ancien destinataire est relancé : le refus porte sur
    lui (sa boîte est de nouveau lue), et le balayage doit quitter la boucle
    des successeurs. `Refused` sinon (successeur inconnu, ou arrêté). Une
    course reste possible — l'ancien arrêté juste après la relecture — et le
    refus est alors classé comme celui du successeur ; l'essai suivant
    retombe sur le refus de l'ancien."""
    try:
        row = registry.get(db, old)
    except db_mod.DbError:
        return Refused(text)
    if row is not None and row.get("status") != "stopped":
        return RefusedOld(text)
    return Refused(text)


def forward(cfg, db, old: str, new: str, *, actor: str, dry_run: bool = False,
            now: float | None = None) -> dict:
    """Re-livre à `new` le courrier en attente de `old`, une boîte morte.

    Les boîtes `human:*` sont refusées ; les demandes de décision ne sont
    jamais copiées (y compris en aperçu).
    `old` doit être arrêté (`stopped`) ou absent du registre : on ne vide pas
    la boîte d'un agent qui la lit. `new` doit être inscrit, ni arrêté ni
    retiré (#3460).
    Chaque message garde son expéditeur, sa date, sa nature et son lot ; la
    copie porte `meta.forwarded_from` (montré au destinataire), l'original
    passe remis avec `meta.forwarded` ; le renvoi est écrit dans le fil.
    `dry_run` : ce qui serait renvoyé, rien n'est écrit. Lève `Refused`.

    L976 (relecture codex3, B1) : le statut de `old` et `new` est relu sous
    verrou du registre DANS la transaction qui copie ; une relance de l'un ou
    un arrêt de l'autre, committé entre la lecture et la copie, lève `Refused`
    au lieu de copier le courrier d'un vivant vers un mort."""
    if old.startswith("human:"):
        raise RefusedOld("%s is a human mailbox: use ameesh decisions, not forward" % old)
    if old == new:
        raise Refused("old and new recipient are the same (%s)" % old)
    old_row, new_row = db_mod.batched(db, lambda d: [registry.get(d, old),
                                                     registry.get(d, new)])
    if old_row is not None and old_row.get("status") != "stopped":
        # #1615/N2 : le refus porte sur l'ancien, pas sur le successeur —
        # `RefusedOld` le dit pour que le balayage n'essaie pas les suivants.
        raise RefusedOld("%s is not stopped (status %s): its mailbox is read, its mail will "
                         "be delivered to it. To empty it to another agent, stop it first "
                         "(agent-runner stop %s), then forward."
                         % (old, old_row.get("status") or "?", old))
    if new_row is None:
        raise Refused(unknown_text(cfg, db, new))
    if new_row.get("status") == "stopped":
        raise Refused("%s is stopped (%s): forward to a live agent"
                      % (new, stop_facts(new_row, now)))
    if new_row.get("retired_ts"):
        raise Refused(retired_text(new_row, sender=False))
    state = "arrêté" if old_row is not None else "absent du registre"
    if dry_run:
        rows = storage.of(db).mailbox.unread(old, 10000)
        return {"old": old, "new": new, "old_state": state, "dry_run": True,
                "messages": [{"id": int(r["id"]), "sender": r.get("sender"),
                              "kind": r.get("kind"), "created_ts": r.get("created_ts")}
                             for r in rows if not (r.get("kind") == "request"
                                 and (r.get("payload") or {}).get("decision") is not None)],
                "forwarded": []}
    try:
        moved = storage.of(db).mailbox.forward(old, new, actor)
    except storage.MailboxRefused as exc:
        # L976 (relecture codex3, B1) : le statut de `old`/`new` est relu sous
        # verrou dans la transaction qui copie — une relance ou un arrêt
        # concurrent gagne ici, jamais entre la lecture et la copie.
        # #1615/N2 : le refus du stockage ne dit pas lequel des deux a gagné ;
        # une relecture de l'ancien le dit (relancé → `RefusedOld`), sans quoi
        # le balayage réessaierait le même refus sur chaque successeur.
        raise _refus_du_stockage(db, old, str(exc)) from None
    if moved:
        # #1689 (N1) : les copies sont déposées dans la boîte du nouveau
        # destinataire sans passer par `mail.send` ; son chemin rapide doit les
        # voir sans attendre le TTL. Un destinataire d'un autre hôte n'a pas de
        # marqueur ici : `bump` ne crée rien.
        fastmail.bump(cfg, new)
        project = fil.project_for(cfg, team_of(new_row), team_of(old_row))
        fil.record(
            cfg, db, sender=actor, recipients=[new],
            text=("Forwarded %d undeliverable message(s): the mailbox of %s (%s) was no longer "
                  "read; re-delivered to %s, original sender and date kept (#%s)."
                  % (len(moved), old, state_label(state), new, ", ".join(
                      "%d → %d" % (int(r["original_id"]), int(r["new_id"])) for r in moved))),
            project=project,
            meta={"audit": "forward", "from": old, "to": new,
                  "messages": [int(r["original_id"]) for r in moved],
                  "copies": [int(r["new_id"]) for r in moved]})
    return {"old": old, "new": new, "old_state": state, "dry_run": False,
            "messages": [], "forwarded": moved}


# --------------------------------------------------------------------------
# balayage (`ameesh mail sweep`, ménage de l'exécuteur) — L976
# --------------------------------------------------------------------------

def _bindings(db) -> list:
    """Les liaisons de session, une fois : []; un schéma absent remonte."""
    try:
        return storage.of(db).session_bindings.listing()
    except db_mod.SchemaMissing:
        raise
    except db_mod.DbError:
        return []


def _retention_days(cfg, days) -> int:
    days = (getattr(cfg, "mail_dead_letter_days", DEFAULT_DEAD_LETTER_DAYS)
            if days is None else days)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("mail retention must be a whole number of days, >= 0")
    if days < 0:
        raise ValueError("mail retention must be a whole number of days, >= 0")
    return days


def _retention_note(cfg) -> str:
    """Ce que l'alerte dit du balayage automatique, si la rétention est active."""
    try:
        days = _retention_days(cfg, None)
    except ValueError:
        return ""
    return ("; expires automatically after %d day(s) without a live successor "
            "(ameesh mail sweep)" % days) if days > 0 else ""


def _trace_expiry(cfg, db, name, row, expired, days, actor) -> None:
    """Trace d'une expiration, au fil du projet du mort (ou de l'inconnu)."""
    senders: dict = {}
    for message in expired:
        sender = message.get("sender") or "?"
        senders[sender] = senders.get(sender, 0) + 1
    fil.record(
        cfg, db, sender=actor, recipients=[name],
        text=("Expired %d undeliverable message(s) addressed to %s, pending for more than "
              "%d day(s): %s; nobody read them. From %s. The messages stay in the mailbox, "
              "marked expired."
              % (len(expired), name, days,
                 "the agent is stopped" if row is not None
                 else "the name is not in the registry",
                 ", ".join("%s (%d)" % kv for kv in sorted(senders.items(),
                                                           key=lambda kv: (-kv[1], kv[0]))))),
        project=fil.project_for(cfg, team_of(row)),
        meta={"audit": "expire", "recipient": name, "days": days,
              "messages": [int(m["id"]) for m in expired]})


def sweep(cfg, db, *, days=None, now=None, dry_run=False, actor="ameesh:mail-sweep") -> dict:
    """L976 : re-livre ou expire le courrier en souffrance, sans geste humain.

    Destinataires visés : ceux de `dead_letters` (agent arrêté, ou nom absent
    du registre) — personne ne lit leur boîte. Un humain déclaré n'en est pas
    une (#4519) : son courrier se lit par `ameesh decide` et par le fil, ni
    expiré ni alerté.

    * un agent arrêté dont le successeur est connu (`successor` : la session de
      l'arrêté est liée à un agent vivant) : le courrier lui est re-livré
      (`forward`, tracé au fil), quel que soit son âge ; #1396/N3 : les
      successeurs sont essayés dans l'ordre, un premier arrêté ne fait plus
      expirer le courrier quand un suivant est vivant ; si tous sont arrêtés,
      chaque refus est noté et le courrier suit la rétention ci-dessous au lieu
      d'attendre indéfiniment ; #1615/N3 : liaison la plus récente d'abord ;
      #1615/N2 : un refus portant sur l'ancien (relancé sous le verrou) quitte
      la boucle après une seule entrée, et rien n'est expiré — sa boîte est de
      nouveau lue ;
    * sinon, le courrier attendu depuis plus de `days` jours
      (`mail_dead_letter_days`, 0 = aucune expiration) est expiré
      (`mailbox.expire_dead_letters` : remis, `meta.expired`), et le fil porte
      la trace — l'alerte `mail_undeliverable` peut alors se vider sans
      intervention humaine.

    `dry_run` : ce qui serait fait, rien n'est écrit. Rend `{days, cutoff,
    dry_run, forwarded, expired, refused}` ; les listes disent le
    destinataire, le nombre et les ids. Une boîte relancée entre la lecture et
    l'écriture garde son courrier (le stockage revérifie sous verrou)."""
    days = _retention_days(cfg, days)
    now = time.time() if now is None else float(now)
    cutoff = now - days * 86400.0 if days > 0 else None
    mail = storage.of(db).mailbox
    groups = sorted(by_recipient(dead_letters(cfg, db)), key=lambda g: g["recipient"])
    rows = registry_rows(db, [g["recipient"] for g in groups]) if groups else {}
    bindings: list | None = None
    report = {"days": days, "cutoff": cutoff, "dry_run": bool(dry_run),
              "forwarded": [], "expired": [], "refused": []}
    for group in groups:
        name = group["recipient"]
        row = rows.get(name)
        if row is not None and row.get("status") != "stopped":
            continue                  # relancé entre les deux lectures : sa boîte est lue
        heirs: list[str] = []
        if row is not None:
            if bindings is None:
                bindings = _bindings(db)
            # #1615/N3 : la liste des successeurs, liaison la plus récente d'abord
            heirs = successor(db, row, bindings=bindings)
        # #1396/N3 : tous les successeurs sont essayés dans l'ordre ; un premier
        # successeur arrêté ne fait plus expirer le courrier quand un suivant est
        # vivant. Chaque refus reste au rapport, et le balayage ne s'arrête qu'une
        # fois le courrier remis (ou tous les successeurs refusés).
        # #1615/N2 : un refus qui porte sur l'ancien (relancé sous le verrou)
        # quitte la boucle — le répéter pour chaque successeur ne dit rien de
        # plus — et le courrier n'est pas expiré : sa boîte est de nouveau lue.
        forwarded = False
        ancien_relance = False
        for target in heirs:
            try:
                moved = forward(cfg, db, name, target, actor=actor, now=now, dry_run=dry_run)
            except RefusedOld as exc:
                # `old_relaunched` : le courrier sera lu par l'ancien, ce refus
                # n'est pas un échec du balayage (`cli._sweep_failed`).
                report["refused"].append({"old": name, "new": target, "reason": str(exc),
                                          "old_relaunched": True})
                ancien_relance = True
                break
            except Refused as exc:
                # successeur lui-même mort : on essaie le suivant ; si tous
                # refusent, le courrier suit la rétention ci-dessous plutôt
                # que d'attendre indéfiniment
                report["refused"].append({"old": name, "new": target, "reason": str(exc)})
                continue
            if dry_run:
                count = len(moved["messages"])
                action = {"old": name, "new": target, "count": count, "dry_run": True,
                          "messages": [int(m["id"]) for m in moved["messages"]]}
            else:
                count = len(moved["forwarded"])
                action = {"old": name, "new": target, "count": count,
                          "messages": [int(r["original_id"]) for r in moved["forwarded"]],
                          "senders": sorted({r.get("sender") for r in moved["forwarded"]
                                             if r.get("sender")})}
            if count:
                report["forwarded"].append(action)
                forwarded = True
            # rien à renvoyer (déjà remis entre-temps) : inutile d'essayer les autres
            break
        if forwarded or ancien_relance:
            continue
        due = cutoff is not None and group["oldest_ts"] is not None \
            and float(group["oldest_ts"]) <= cutoff
        if not due:
            continue
        expired = mail.expire_dead_letters(name, {"at": now, "days": days, "by": actor},
                                           cutoff, dry_run=dry_run)
        if not expired:
            continue                  # relancé, réservé ou déjà remis entre-temps
        action = {"recipient": name, "count": len(expired),
                  "oldest_ts": group["oldest_ts"], "unknown": bool(group["unknown"]),
                  "messages": [int(m["id"]) for m in expired]}
        if dry_run:
            report["expired"].append(dict(action, dry_run=True))
            continue
        report["expired"].append(action)
        _trace_expiry(cfg, db, name, row, expired, days, actor)
    return report
