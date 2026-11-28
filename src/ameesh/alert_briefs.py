"""Alertes adressées aux agents qui peuvent agir (L132, décision 0036).

`ameesh notify` pousse ses alertes à l'humain responsable. Trois d'entre elles
appellent un geste qu'un agent sait faire : `idle_capacity` (L118),
`stale_lot` et `plan_underused`. Elles partent aussi, par courrier `event`,
avec `info` pour capacité et sous-utilisation ordinaires (#7308) : lecture au
tour suivant, sans réveil supplémentaire,

* aux orchestrateurs (ceux de l'alerte, de l'équipe concernée quand elle est
  connue), qui répartissent le travail ;
* à l'auditeur interne ([AUDITEUR.md](../../docs/AUDITEUR.md)), qui les suit
  — sauf `stale_lot` (L713) et `plan_underused` (#3274) : ces courriers ne le
  réveillent plus, il lit les lots stagnants et les forfaits sous-utilisés à
  sa passe (`ameesh alerts --json`) ; si plus aucun orchestrateur n'est
  vivant, le forfait sous-utilisé lui revient en relais (#3036).

#705 : pour un lot en `qa`, `stale_lot` va au relecteur DÉSIGNÉ (le verdict
lui est dû) ; sans relecteur désigné, l'alerte part aux orchestrateurs du
projet du lot et demande d'en désigner un (`ameesh work reviewer <id>
<agent>` — le lot est déjà en qa). La désignation à l'entrée en qa se fait,
elle, par `ameesh work move <id> qa --reviewer <agent>`.

#3007 : `notify_undeliverable` ne demande pas un geste sur un lot — elle dit
qu'aucun notificateur VIVANT ne couvre l'humain qui a des décisions en
attente. Elle part aux orchestrateurs de l'alerte et à l'auditeur, avec le
repli (canal de secours ou retour du notificateur). Son brief n'est **pas**
envoyé par le notificateur (ce serait le disparu qui parle) : la passe du
runner hors du poste le porte (`notify_gap.brief_once`), et
`NOTIFIER_EXCLUDED` l'en écarte côté `ameesh notify`.

#5563 : `host_disk_soon_full` et `host_unsampled` vont au rôle de conception
(#7661 : l'agent dont la fiche du canon déclare `conception`/`design`, jamais
un nom) et au bureau (fil pour un bureau externe), avec filtre des
destinataires vivants. Le silence du relevé et le passage du disque au
critique sont urgents.

Chaque courrier dit l'action attendue, avec la commande exacte. Une alerte
part une fois à sa levée, puis une seule relance si elle dure encore
`REMIND_AFTER_S` (6 h) plus tard ; les SLO ne repartent qu'au palier suivant
ou après changement d'état (#2602). Pour une alerte de jauge
(`plan_underused`), c'est une fois par **fenêtre** : son début est arrondi à
la minute et `reason` n'entre pas dans la clé, pour qu'une perte projetée qui
oscille autour du seuil ne refasse pas briefer à chaque retour (#3236). Ce
module rend les textes ; l'envoi et le dédoublonnage sont dans
`notify.Notifier`.
"""

from __future__ import annotations

import re
import time

from . import sous_utilisation, vocab, work_lifecycle as lifecycle
from .roles import AUDITOR_ROLES

#: #8172 : le texte d'un véhicule de paquet validé, au détail de l'alerte SLO
#: et à son brief : le train ne prend jamais un porteur `integration/**`.
PACKET_VEHICLE_MERGE = ("packet vehicle: merged by the pilot (§12), the train "
                        "does not take it")

#: types adressés aux agents (en plus de l'humain, s'ils sont dans `notify.types`)
BRIEFED_TYPES = ("host_disk_soon_full", "host_unsampled", "staging_interrupted",
                 # #5727 : le pilote fait poser une copie du canon à jour
                 "staging_canon_stale",
                 # #7164 : le pilote fait poser le venv de l'exécuteur
                 "staging_executor_stale",
                 "host_idle_reserved", "baseline_oversized", "host_release_failed",
                 "host_release_overdue", "host_release_no_runs",
                 "host_release_canon_unreadable",
                 # #5932 : worker de l'adaptateur VM arrêté après une intention
                 # d'arrêt durable ; une requête en attente jeune reste normale
                 "host_release_adapter_stalled",
                 # #8459 : `sortir` a échoué, la liste synchrone du hub est
                 # peut-être vide ; l'adaptateur rejoue à chaque tick
                 "host_release_standby_exit_failed",
                 # #6014 : un train en pause depuis plus d'un jour ne
                 # redémarre pas seul ; le pilote du projet doit trancher
                 "train_paused",
                 # #7889 : un train figé hors phase terminale depuis plus
                 # d'un jour tient la réservation ; le pilote du projet
                 "train_stuck",
                 # #7759 : des candidats prêts laissés dehors par un créneau ;
                 # un seul composeur/pilote du projet
                 "packet_ready_waiting",
                 # #6014 : une coupure due sans release.projects déclaré sur
                 # le coupeur ; le pilote du projet doit déclarer ou déplacer
                 "release_project_undeclared",
                 # #5695 : l'équipe du projet de la source renouvelle son jeton
                 "fingerprints_source_failing", "idle_capacity", "stale_lot", "plan_underused", "estimate_bias",
                 "waiting_without_question", "module_deviation",
                 # #8196 : à l'assigné du suivi seul
                 "follow_up_unfollowed",
                 "critical_budget_pause", "idle_actionable_lot", "slo_breach",
                 # #7712 : une coordination de vague qui refuse tour après
                 # tour — l'agent critique ne tourne plus, ce n'est pas un
                 # plafond de budget ; l'équipe vérifie les bascules
                 "account_wave_coordination",
                 # #1466 : une pause qui dure est un plafond périmé, un compte
                 # retiré ou une configuration que l'exécuteur ne lit pas ; les
                 # orchestrateurs peuvent vérifier, l'auditeur suit
                 "budget_pause_long", "review_family_paused",
                 # #3969 : un compte invalide sur l'hôte — l'équipe le répare
                 "account_config_invalid",
                 # #3961 : une pause opérateur restée posée (maintenance
                 # oubliée) ; les orchestrateurs la lèvent ou la confirment
                 "operator_pause_long", "operator_pause_host",
                 # #3036 : le courrier en souffrance part aux orchestrateurs
                 # VIVANTS ; le brief est ainsi suivi et re-tenté comme les
                 # autres, au lieu d'une remise unique à un hôte perdu.
                 "mail_undeliverable",
                 # #3007 : aucun notificateur vivant ne couvre l'humain qui a
                 # des décisions en attente — les agents doivent le savoir
                 "notify_undeliverable",
                 # #3733 : un agent bridé une bonne part de l'heure — le
                 # pilote confie ses relectures dues à une autre famille
                 "rate_limited_chronic")
#: #3007 : types qu'un notificateur ne briefe jamais lui-même — leur brief
#: dépend de sa propre liveness, il est porté par `notify_gap.brief_once`
#: (ménage du runner) pour survivre à la chute du poste.
NOTIFIER_EXCLUDED = ("notify_undeliverable",)
#: une relance, si l'alerte dure encore 6 h après le premier courrier
REMIND_AFTER_S = 6 * 3600.0
#: #7307 : une pause d'allure de famille de relecture se lève puis se repose
#: 10 à 25 min plus tard sur le même compte et la même fenêtre ; c'est le même
#: épisode tant que l'absence continue reste sous ce seuil.
FAMILY_PAUSE_GAP_S = 45 * 60.0
#: #7572 : le statut d'un agent en pause budget clignote `blocked`/`idle` et
#: l'alerte peut manquer une passe ; une absence plus longue que ce seuil
#: ferme l'épisode (l'agent a repris). Une pause ultérieure est un nouvel
#: épisode, avec son propre brief.
BUDGET_PAUSE_GAP_S = 15 * 60.0
#: #3057 : un bail rendu depuis moins de ce délai (redémarrage d'exécuteur,
#: quelques secondes) n'est pas encore une perte : le brief attend, sans
#: escalade ni copie au fil ; trois passes du notificateur (30 s)
LEASE_GRACE_S = 90.0
#: #6642 : un SLO de niveau 1 ordinaire n'est briefé qu'après ce retard ;
#: 62 des 78 L1 du 09/11 étaient en retard de 0 à 4 min (auditeur #206650)
SLO_L1_GRACE_S = 300
#: rôles d'une fiche Agent du canon qui désignent l'auditeur ; le rôle se lit
#: sur la fiche (`roles:`), jamais sur le nom de la persona. Le jeton est dans
#: `roles.py` (#7792), comme tous les rôles de gouvernance.


def claims_closed_hosts(db) -> set:
    """#3036 : les hôtes dont les claims sont fermés.

    Un agent sur un hôte `draining`/`lost` (mise au repos, hôte perdu) ou sous
    une clôture de release (`host_release_fences`) ne peut pas prendre de
    nouveau bail, donc ne lira pas le courrier qu'on lui dépose. Les deux
    sources sont lues ici : le contrôle de cycle de vie (mode) et la porte
    calculée par `ameesh_host_gate_state` sur les relevés (`draining` couvre
    aussi une clôture de release). Une lecture en échec ne ferme rien : on ne
    devine pas qu'un hôte est mort."""
    from . import storage
    st = storage.of(db)
    names: set = set()
    try:
        names |= {str(row["host"]) for row in st.host_lifecycle.listing()
                  if row.get("mode") in ("draining", "lost")}
    except Exception:  # noqa: BLE001 - un contrôle illisible ne ferme pas un hôte
        pass
    try:
        names |= {str(row["host"]) for row in st.hosts.current(None)
                  if row.get("gate_state") == "draining"}
    except Exception:  # noqa: BLE001 - idem
        pass
    return names


def _unavailable(row: dict, closed: set) -> str:
    """#3036 : pourquoi un agent ne lira pas son courrier (`""` s'il le peut).

    Trois preuves : son hôte a les claims fermés ; il est `stopped`/`dead` (il
    ne prendra plus de tour) ; ou son bail n'est plus vivant (le runner qui le
    lisait est mort ou l'a rendu). L'état `queued` n'est pas une preuve en
    soi, seul le bail compte : un `queued` qui tient son bail lira, un
    `queued` sans bail vivant (hôte plein, `max_agents`, porte fermée) est
    écarté comme tout agent sans bail — rien ne dit quand il prendra un tour.
    Une ligne sans information de bail (appel hors base, test) n'est pas
    déclarée morte : on ne remplace pas une preuve absente par une
    supposition."""
    if row.get("retired_ts") is not None:
        return "retired"
    host = str(row.get("host") or "")
    if host and host in closed:
        return "host %s: claims closed" % host
    status = str(row.get("status") or "")
    if status in ("stopped", "dead"):
        return "status %s" % status
    if "lease_live" in row and not row.get("lease_live"):
        return "no live lease"
    return ""


def lease_grace(listing: list, closed: set, since: dict, now: float) -> tuple[list, set]:
    """#3057 : la grâce d'un bail tout juste rendu.

    Un exécuteur qui redémarre rend ses baux quelques secondes ; sans grâce,
    les briefs de cette fenêtre partent au palier suivant ou au fil du bureau,
    puis au destinataire à son retour (deux remises pour une absence de
    secondes). `since` (état du notificateur, nom → première passe vue sans
    bail) date l'absence. Rend le registre où un agent sans bail depuis moins
    de `LEASE_GRACE_S` compte vivant pour le routage, et l'ensemble de ces
    agents `held` : l'appelant ne leur remet rien encore. Seule l'absence de
    bail a une grâce : un hôte aux claims fermés ou un agent `stopped` est une
    perte prouvée. Une perte réelle n'est retardée que de la grâce."""
    out: list = []
    held: set = set()
    for row in listing:
        name = row["name"]
        if "lease_live" not in row or row.get("lease_live") \
                or _unavailable(dict(row, lease_live=True), closed):
            since.pop(name, None)
            out.append(row)
            continue
        first = float(since.setdefault(name, now))
        if now - first < LEASE_GRACE_S:
            held.add(name)
            out.append(dict(row, lease_live=True))
        else:
            out.append(row)
    names = {row["name"] for row in listing}
    for name in [n for n in since if n not in names]:
        del since[name]
    return out, held


def _dead_note(dead: dict) -> str:
    """#3036 : « aucun destinataire vivant : X, Y » en tête du brief.

    Le texte est le même pour tous les destinataires vivants du palier ; il
    leur dit qui manque, pour qu'ils couvrent le geste au lieu de croire que
    l'alerte est partie à tout le monde."""
    if not dead:
        return ""
    return "No live recipient: %s. " % ", ".join(
        "%s (%s)" % (name, why) for name, why in sorted(dead.items()))


def _escalation_note(dead: dict, from_level: int, to_level: int) -> str:
    """#3036 : le palier visé n'avait personne qui lise ; on le dit et on
    adresse le palier suivant."""
    return ("No live recipient: %s. Escalated from level %d to level %d. "
            % (", ".join("%s (%s)" % (name, why) for name, why in sorted(dead.items()))
               or "none designated", int(from_level), int(to_level)))


def _split_live(out: dict, by_name: dict, closed: set,
                covered=(), *, respect_pause=False) -> tuple[dict, dict]:
    """#3036 : sépare les briefs des destinataires vivants de ceux qui ne
    liront pas. Un nom hors registre (les pseudo-destinataires `thread:…`)
    n'est pas filtré : il n'a pas de bail à contrôler. #3100 : un nom de
    `covered` (déjà remis) non plus : son courrier l'attend. Pour les SLO,
    `respect_pause` écarte une pause opérateur même déjà couverte : le
    courrier ne suffit pas à la lever (#6396). #6664 : il écarte de même une
    pause budget de jauge (la fenêtre courte du compte reste un destinataire,
    #3876) : le relecteur ne lira pas la relance, le palier suivant porte le
    geste."""
    from . import review_designation as rd

    live: dict = {}
    dead: dict = {}
    for name, text in out.items():
        row = by_name.get(name) or {}
        # #6396 : un brief SLO déjà déposé ne réveille pas une pause.
        # Le pilote doit prendre le relais même si le bail reste vivant.
        if respect_pause and row.get("operator_paused"):
            dead[name] = "operator paused"
            continue
        # #6664 : une pause budget de jauge ne prendra pas de tour ; la
        # relance doit passer au pilote, pas attendre dans sa boîte.
        if respect_pause and row and rd.is_budget_pause(row):
            dead[name] = "budget paused"
            continue
        if name.startswith("thread:") or name in covered:
            live[name] = text
            continue
        if not row:
            live[name] = text
            continue
        why = _unavailable(row, closed)
        if why:
            dead[name] = why
        else:
            live[name] = text
    return live, dead


def _external(by_name: dict, name) -> bool:
    """Un avatar `externe` (session humaine, 0030) ne lit pas sa boîte par
    l'exécuteur : sa copie va au fil lisible du lot, jamais un courrier de
    plus."""
    return (by_name.get(name) or {}).get("mode") == "externe"


def _desk_names(alert: dict, by_name: dict, avatar_names=(),
                desk_names=()) -> list[str]:
    """Les agents qui portent la route humaine d'une alerte, dans l'ordre.

    L'avatar déclaré au canon de l'humain visé d'abord, quand l'appelant l'a
    reconnu vivant (#7749 : un avatar sans destinataire vivant n'est pas
    passé ici — la route configurée prend sa place) ; à défaut, la route
    explicitement configurée pour cet humain (`desk_names`, lue au registre
    par l'appelant) ; à défaut, un rôle du `targets` qui nomme un agent
    (compatibilité des alertes écrites avant #7634). Aucun agent n'est deviné
    depuis son seul mode `externe` : la session d'un autre humain ne reçoit
    pas ce courrier (#7634, relecture cx1 B1)."""
    names = [str(n) for n in avatar_names or () if n]
    if names:
        return list(dict.fromkeys(names))
    names = [str(n) for n in desk_names or () if n in by_name]
    if names:
        return list(dict.fromkeys(names))
    known = {"assignee", "pilot", AUDITOR_ROLES[0], "avatar"}
    return [str(role) for role in alert.get("targets") or ()
            if role not in known and role in by_name]


def _fallback_avatar(by_name: dict, text: str, names=()) -> dict:
    """#3036 : quand plus aucun agent ne peut lire, la route humaine est
    l'avatar : sa boîte s'il est un agent normal, sinon le fil lisible du lot
    (la voie déjà employée pour un avatar externe). Sans avatar au registre,
    rien n'est inventé : l'appelant réessaiera au passage suivant."""
    out = {}
    for name in names or ():
        if name not in by_name:
            continue
        if _external(by_name, name):
            out["thread:%s" % name] = text
        else:
            out[name] = text
    return out


def auditors(listing: list, canons=None, *, explicit_only=False) -> list:
    """Les auditeurs du registre : ceux dont la fiche Agent du canon porte
    `roles: [auditeur]` (ou `auditor`, `role:`), jamais un nom de persona.

    Le rôle est lu sur la fiche, pas sur le titre : une persona renommée qui
    le déclare reste auditrice, une persona nommée `auditeur` qui ne le
    déclare pas ne l'est plus. Sans canon chargé, la projection du registre
    (`roles`, copiée de la carte par `canon sync`) est la seule déclaration
    disponible ; avec un canon, c'est lui qui fait foi. `explicit_only` est
    conservé pour les appelants qui exigeaient le rôle sans repli par le nom :
    c'est désormais la seule règle."""
    names = {row["name"] for row in listing}
    found = sous_utilisation.role_orchestrators(canons or [], AUDITOR_ROLES)
    if not canons:
        found |= {row["name"] for row in listing
                  if sous_utilisation.row_has_role(row, AUDITOR_ROLES)}
    return sorted(n for n in found if n in names)


def _projects(by_name: dict, name) -> set:
    """L243 : les projets d'un agent (`projects`, sinon son équipe)."""
    from . import projects as projects_mod
    return set(projects_mod.agent_projects(by_name.get(name) or {}))


def _workstream(row: dict) -> str:
    """`workstream` d'abord ; `chantier` reste lu tant que les lecteurs chauds
    ne l'ont pas remplacé (L148, stage 3 C1)."""
    return row.get("workstream") or row.get("chantier") or ""


def _of_teams(orchestras, by_name: dict, teams: set) -> list:
    """Les orchestrateurs d'un des projets (ou sans projet) ; sans projet
    connu, tous.

    `orchestras` vient de `sous_utilisation.alert_orchestrators` (#1114) :
    l'historique d'attribution ne confère pas le rôle, donc un agent sans
    projet n'est ici qu'un hub explicitement déclaré (rôle au canon ou
    `AMEESH_ALERT_ORCHESTRATORS`), jamais un assigneur récent."""
    if not teams:
        return list(orchestras)
    return [o for o in orchestras if not _projects(by_name, o)
            or _projects(by_name, o) & teams]


def _of_project(orchestras, by_name: dict, project, teams: set) -> list:
    """#705 : les orchestrateurs du PROJET du lot (`team` ou `workstream`),
    à défaut ceux de l'équipe de l'assigné (routage d'origine), à défaut tous.
    L243 : l'agent est membre de chacun de ses projets (`_projects`)."""
    project = (project or "").strip()
    if project:
        matched = [o for o in orchestras
                   if project in _projects(by_name, o)
                   or project == _workstream(by_name.get(o) or {})]
        if matched:
            return matched
    return _of_teams(orchestras, by_name, teams)


def project_pilots(project: str, listing: list, *, cfg=None, db=None,
                   canons=None) -> list:
    """#2935 : les pilotes (orchestrateurs) d'un projet, ordre stable.

    Un échec de CI sur une branche de base n'appartient à aucun lot : il doit
    partir au pilote du projet, pas à personne. Le rôle vient du canon
    (`orchestrateur`) ; `_of_project` le restreint au projet, avec le repli
    « tous les orchestrateurs » quand aucun ne le déclare — mieux vaut un
    pilote d'un autre projet qu'aucun destinataire pour une base rouge. Le
    choix d'un seul destinataire est laissé à l'appelant (`[0]` : ordre
    stable)."""
    orchestras = sous_utilisation.alert_orchestrators(
        cfg, db, listing, canons=canons)
    by_name = {row["name"]: row for row in listing}
    return sorted(_of_project(orchestras, by_name, project, set()))


def _reviewer_missing(alert: dict) -> str:
    """#705 : un lot en qa SANS relecteur désigné n'attend personne ; le texte
    demande à l'orchestrateur du projet d'en désigner un (ou de relire lui-même)."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("No reviewer designated: %s%s (%s) has been waiting for a verdict for %s "
            "with no designated reviewer, so the wait belongs to nobody. Expected action: "
            "designate the reviewer: ameesh work reviewer %d <agent>; or take the "
            "review yourself and give the verdict: ameesh work milestone %d verdict "
            "ok|blocked --sha <frozen head> --note \"…\"." % (
                v.label(lot), " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                sous_utilisation._duration(alert.get("value") or 0), lot, lot))


def _verdict_due(alert: dict) -> str:
    """#705 : le relecteur DÉSIGNÉ d'un lot en qa qui stagne : le verdict lui est dû."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("Verdict expected: %s%s (%s) has been waiting for its verdict for %s; you "
            "are its designated reviewer. Expected action: review it and give your verdict: "
            "ameesh work milestone %d verdict ok|blocked --sha <frozen head> --note \"…\"; "
            "if you cannot, say so on the %s (ameesh work note %d \"hold: <reason>\") so "
            "the project's orchestrator can hand the review over." % (
                v.label(lot), " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                sous_utilisation._duration(alert.get("value") or 0), lot, v.noun, lot))


def _reserved_label(alert: dict) -> str | None:
    """#6105 : comment nommer l'exécutant d'un lot réservé dans un brief.

    Un `system:…` déclaré nomme son responsable humain — c'est à lui que la
    relance s'adresse, pas à un système qui n'agit pas. Une déclaration
    retirée ne porte plus personne : le brief dit au pilote de lever la
    réservation au lieu de laisser le lot attendre en silence. Un agent
    `externe` ou un humain garde le texte #3447 (« external executor »).
    Rend None pour un lot non réservé."""
    external = (alert.get("external_executor") or "").strip() or None
    if not external:
        return None
    if alert.get("external_retired"):
        retired_by = (alert.get("external_retired_by") or "").strip()
        return ("%s (retired non-agent system%s; the pilot clears or reassigns)"
                % (external, ", retired by %s" % retired_by if retired_by else ""))
    responsible = (alert.get("external_responsible") or "").strip() or None
    if responsible:
        return ("%s (external executor, declared responsible %s)"
                % (external, responsible))
    return "%s (external executor)" % external


def _stale_move(alert: dict, lot: int, who: str | None) -> str:
    """L'action proposée pour un lot stagnant (#1773).

    Un lot en qa dont l'attente est la fusion (gel + verdict acquis) est un
    candidat de paquet/de file de fusion : relancer son assigné ou le
    réassigner ne fait pas avancer la fusion. Le brief le dit — sans retirer
    l'alerte (canon §4 : un lot sans mouvement se signale au pilote) ni les
    portes de clôture, qui restent offertes plus bas."""
    v = vocab.active()
    if alert.get("lot_state") in lifecycle.TO_VALIDATE and alert.get("waiting_for") == "fusion":
        return ("this %s is frozen with its verdict acquired: it is a candidate for its "
                "merge packet/queue — no author action, do not nudge its assignee or "
                "reassign it; the composer/pilot carries the merge and can declare the "
                "planned landing that stops the reminders: ameesh work landing %d "
                "--vehicle <vehicle lot> --until <YYYY-MM-DD>" % (v.noun, lot))
    if who:
        # #2016 : la commande porte le titulaire lu ici (`--if-holder`) : un
        # second agent parti du même brief est refusé si le lot a changé de
        # main entre-temps, au lieu de voler le lot au premier.
        return ("nudge %s: ameesh mail send %s \"…\" --lot %d, or reassign it: "
                "ameesh work assign %d <agent> --if-holder %s" % (who, who, lot, lot, who))
    external = (alert.get("external_executor") or "").strip() or None
    if external:
        # #3447 : réservé, pas sans assigné — le geste est celui de
        # l'exécutant, ou la levée préalable par le pilote.
        if alert.get("external_retired"):
            # #6105 : le système déclaré n'existe plus ; personne n'agit.
            return ("this %s is reserved to %s, a retired non-agent system: nobody "
                    "acts — the pilot clears the reservation and reassigns the lot: "
                    "ameesh work external-executor %d --clear"
                    % (v.noun, external, lot))
        responsible = (alert.get("external_responsible") or "").strip() or None
        if responsible:
            # #6105 : la relance va au responsable humain déclaré, nommé.
            return ("this %s is reserved to %s, a non-agent system; its declared "
                    "responsible %s acts (not an agent, never woken) — or tells the "
                    "pilot the reservation is without object, who clears it: ameesh "
                    "work external-executor %d --clear"
                    % (v.noun, external, responsible, lot))
        return ("this %s is reserved to %s (external executor, not an assignee): the "
                "executor acts; if the reservation is without object, the pilot clears "
                "it first: ameesh work external-executor %d --clear"
                % (v.noun, external, lot))
    # #1026 : un lot sans assigné se prend (`--claim`) : deux orchestrateurs
    # qui l'assignent en même temps ne peuvent plus se voler le lot.
    return "assign it: ameesh work assign %d <agent> --claim" % lot


def _one_orchestrator(orchestras, by_name: dict, lot) -> list:
    """Un seul orchestrateur pour un lot sans assigné (#1026).

    Parmi ceux dont l'exécuteur est vivant (`sous_utilisation._awake_runner`),
    un seul, choisi par le numéro du lot : stable d'une passe à l'autre
    (relance comprise) et réparti. Aucun exécuteur vivant : tous — ils sont
    arrêtés, le premier revenu agit, et la prise du lot est de toute façon
    atomique (`ameesh work assign --claim`)."""
    names = sorted(orchestras)
    if not names:
        return []
    awake = [n for n in names if sous_utilisation._awake_runner(by_name.get(n) or {})]
    if not awake:
        return names
    return [awake[int(lot or 0) % len(awake)]]


def _stale_lot(alert: dict) -> str:
    lot = int(alert["lot"])
    who = alert.get("agent")
    external = (alert.get("external_executor") or "").strip() or None
    title = alert.get("title")
    v = vocab.active()
    head = "Stale %s: #%d%s (%s) no activity for %s, %s%s." % (
        v.noun, lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
        sous_utilisation._duration(alert.get("value") or 0),
        "assigned to %s" % who if who
        else "reserved to %s" % _reserved_label(alert) if external
        else "unassigned",
        ", waiting for: %s" % alert["waiting_for"] if alert.get("waiting_for") else "")
    move = _stale_move(alert, lot, who)
    # L368: a dependency declared towards an open parent lot is a planned wait,
    # which already suppresses the alert and its 6 h reminder. The brief must
    # offer it, otherwise a stacked lot is triaged in vain every 6 h (#1642) ;
    # #1261 le disait déjà « the alert stops while the wait is planned ».
    hold = ("if the %s is parked on another open %s, declare the dependency that stops "
            "the reminders (the alert stops while the wait is planned): "
            "ameesh work dependencies %d --add <blocker>; if a package vehicle is to "
            "carry it, declare the landing: ameesh work landing %d --vehicle "
            "<vehicle lot> --until <YYYY-MM-DD>" % (v.noun, v.noun, lot, lot))
    # #1664 : une porte hors mesh (fenêtre opérateur, décision humaine sans
    # demande ouverte) n'est portée par aucun lot ; le pilote la consigne en
    # hold, qui éteint la relance comme une dépendance déclarée.
    park = ("if it is parked on a gate no open %s carries (operator window, human "
            "decision), the pilot records the hold that stops the reminders: "
            "ameesh work note %d \"hold: <reason>\" (the pilot lifts it with an "
            "\"unhold: <reason>\" note or a state change)" % (v.noun, lot))
    return ("%s Expected action: if it was merged outside a PR, close it: ameesh work merged %d "
            "--sha <merge commit>; if it was delivered outside the target (report, note, head "
            "reviewed on a branch, no merge), close it: ameesh work close %d --delivered "
            "--sha <reviewed head>; %s; %s; otherwise %s; if it is no longer needed: ameesh work "
            "close %d --abandoned (or --superseded-by <id>, or --done if it has no branch)."
            % (head, lot, lot, hold, park, move, lot))


def _plan_underused(alert: dict) -> str:
    v = vocab.active()
    idle = list(alert.get("idle_agents") or ())
    if idle:
        move = ("give work to the idle agents (%s), tied to a %s: ameesh mail send "
                "<agent> \"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign "
                "<lot> <agent>" % (", ".join(idle), v.noun))
    else:
        move = ("no %s agent is idle: move work onto this harness (ameesh "
                "work assign <lot> <agent>) or wake a stopped agent (ameesh resume "
                "<agent>)" % (alert.get("harness") or "of this account"))
    return ("Underused plan: %s. Expected action: %s; open %s without an agent: "
            "ameesh projects." % (alert.get("detail") or "?", move, v.plural))


def _estimate_bias(alert: dict) -> str:
    """L157b : à l'estimateur et aux orchestrateurs."""
    ratio = alert.get("value")
    sens = ("underestimates" if ratio is not None and float(ratio) > 1 else "overestimates")
    return ("Estimate bias: %s. %s %s %s. Expected action: start from "
            "the reference estimate (shown by ameesh work add and ameesh work show, "
            "table published by the auditor) and deviate from it only with a justification "
            "(--estimate-source), estimate broken down into phases (agent, CI, waiting for "
            "review, human)." % (alert.get("detail") or "?", alert.get("agent") or "?",
                                 sens, vocab.active().plural))


def _waiting_without_question(alert: dict) -> str:
    """L233: to the assignee and the orchestrators of its team."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("%s%s is in waiting_human but no decision request is open: the owner sees "
            "a %s waiting for them and has no question to answer. Expected action: ask the "
            "owner through the queue — ameesh decide ask --lot %d --question \"…\" "
            "--option a=\"…\" [--recommend a --why \"…\"] — or, if nothing is expected from "
            "him, move the %s back: ameesh work move %d <state>. A decision written in a "
            "mail (to an avatar or anyone) never counts." % (
                v.label(lot, capital=True), " « %s »" % title if title else "",
                v.noun, lot, v.noun, lot))


def _follow_up_unfollowed(alert: dict) -> str:
    """#8196 : à l'assigné du suivi seul (son humain une heure après)."""
    lot = int(alert["lot"])
    title = alert.get("title")
    return ("Your follow-up%s: %s. Write what you found in a note on #%d. Without a "
            "change within the hour, your responsible human is told." % (
                " « %s »" % title if title else "",
                alert.get("detail") or alert.get("reason") or "?", lot))


def _idle_actionable_lot(alert: dict) -> str:
    """#408 (règle 3) : à l'auditeur seul, qui relance (niveau 1)."""
    lot = int(alert["lot"])
    who = alert.get("agent") or "?"
    title = alert.get("title")
    v = vocab.active()
    if alert.get("runner_live"):
        nudge = ("agent-mail send %s \"…\" --lot %d (what is expected on the %s, or "
                 "ask it to record why it cannot advance: ameesh work note %d \"hold: "
                 "<reason>\")" % (who, lot, v.noun, lot))
    else:
        nudge = "ameesh resume %s (its runner is not live), then agent-mail send %s " \
                "\"…\" --lot %d" % (who, who, lot)
    return ("Idle with an actionable %s: %s holds #%d%s (%s), and %d wake(s) of its runner "
            "left the %s unchanged. Expected action, level 1 (reversible), not just naming "
            "it: nudge it now: %s; if it cannot take the %s, reassign it: ameesh work "
            "assign %d <agent>. Never twice within an hour for the same agent." % (
                v.noun, who, lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                int(alert.get("wakes") or 0), v.noun, nudge, v.noun, lot))


def _packet_ci_text(ci: dict) -> str:
    """#3487 : l'état de la CI du paquet, dans le geste SLO.

    Le lot porté n'attend plus sa propre CI ; le brief dit où en est celle du
    paquet (en cours, en file, pas encore démarrée, verte, rouge, annulée) pour
    que le palier visé sache s'il doit pousser le composeur ou traiter un
    rouge. Un état illisible ne fait pas taire le geste : il nomme le paquet.
    """
    state = str((ci or {}).get("state") or "unknown")
    run = "run %s" % ci["run_id"] if (ci or {}).get("run_id") else "its CI"
    # #8155 : la tête lue et les workflows rouges ou en cours sont nommés.
    sha = str((ci or {}).get("head_sha") or "")[:8]
    on = " on %s" % sha if sha else ""
    red = ", ".join((ci or {}).get("red_workflows") or ())
    running = ", ".join((ci or {}).get("running_workflows") or ())
    if state == "running":
        return " (%s%s is still running%s)" % (
            run, on, ": %s" % running if running else "")
    if state == "pending":
        return " (its full CI has not started yet%s)" % on
    if state == "uncertain":
        # #8155 : la tête actuelle de la branche n'a pas pu être lue ; des runs
        # existent sur d'autres têtes, aucune n'est présentée comme courante.
        seen = ", ".join(str(sha)[:8] for sha in (ci or {}).get("seen_heads") or ())
        recorded = (ci or {}).get("recorded_state")
        mine = ("the recorded head%s is %s" % (on, recorded) if recorded
                else "no run on the recorded head%s" % on)
        return (" (its CI state is unproven: %s, runs seen on %s, and the "
                "branch's current head could not be read; check the packet PR "
                "before acting)" % (mine, seen or "other heads"))
    if state == "green":
        return " (%s%s is green; the packet merge is pending)" % (run, on)
    if state == "red":
        return (" (%s%s is red%s: a unit must be fixed or removed before the packet "
                "merges; a red also present on the target is not the units' "
                "fault%s)" % (
                    run, on, " — %s" % red if red else "",
                    "; still running: %s" % running if running else ""))
    if state in ("cancelled", "superseded"):
        return " (%s was %s; a new run is expected)" % (run, state)
    return " (its CI is not readable here)"


#: #7205 : la fin du brief SLO d'un lot prêt qui attend son véhicule. Le 13/11,
#: cinq accusés `hold: …` du pilote ont écarté cinq lots validés de h-06.
READY_HOLD_HINT = ("; acknowledge with a plain note, never `hold: …` — a pilot's "
                   "hold is read by scripts/paquet-candidats.py as `pilot-hold` and "
                   "drops the lot from the next package (#7205)")

def _desk_label(alert: dict, avatar_names=()) -> str:
    """Le nom de la route humaine pour le texte d'escalade (#7634).

    L'avatar déclaré au canon quand il est connu ; sinon le rôle du `targets`
    qui nomme un agent (les alertes d'avant #7634 et les bancs nomment encore
    leur avatar « bureau »), sinon « avatar »."""
    for name in avatar_names or ():
        if name:
            return str(name)
    known = {"assignee", "pilot", AUDITOR_ROLES[0], "avatar"}
    for role in alert.get("targets") or ():
        if role not in known:
            return str(role)
    return "avatar"


def _with_desk(text: str, desk: str | None) -> str:
    """Le texte de l'escalade nomme la route humaine réellement visée.

    Seuls les libellés de route sont réécrits (`avatar or auditor:`,
    `avatar:`, « the avatar … ») : le remplacement porte sur le geste, jamais
    sur le titre du lot, où le mot peut être du contenu (#7634, relecture
    auditeur)."""
    if not desk:
        return text
    return re.sub(r"\bavatar\b", lambda _: str(desk), text)


def _slo_breach(alert: dict, *, level: int | None = None,
                desk: str | None = None) -> str:
    """#789 tranche 2 (canon §9) : une étape interne du lot a dépassé son
    délai ; le texte dit l'étape, le retard, et le geste du niveau atteint
    (1× l'assigné, 2× le pilote, 3× le bureau ou l'auditeur, bureau et
    auditeur pour un P1). #2395 : aucun dépassement n'est notifié à l'owner
    (décision owner 18/10 : « rien sur les SLO »).

    #3036 : `level` force le niveau du texte quand le palier atteint n'avait
    aucun destinataire vivant et que le brief part au palier suivant — le
    geste demandé doit être celui du palier qui reçoit, pas celui du palier
    muet.

    #2231 : quand l'attente est une fusion (`merge*`) ou un déblocage
    (`unblock`), le porteur n'est pas le blocage — le brief vise le pilote/le
    composeur et ne prescrit ni relance ni réassignation du porteur.

    #3487 : quand l'attente est un paquet ouvert (`packet`), le lot est porté
    par un paquet composé ; le geste nomme le paquet, sa branche et l'état de
    sa CI complète, et ne prescrit jamais de réassigner le porteur — il pousse
    le pilote/le composeur à voir le paquet jusqu'à sa fusion.

    #4572 : quand `waiting.candidate` nomme une cause côté auteur
    (`freeze-not-head`, `conflict`…), le composeur ne peut rien : le geste
    nomme l'auteur et ce qu'il doit faire (regeler, résoudre le conflit,
    obtenir l'avis de la remise). Le texte « le composeur le porte » reste
    réservé au lot réellement prêt (`ready`) ; une lecture incomplète renvoie
    à la liste calculée sans nommer personne au hasard.

    #2378 : de même pour une demande de décision ouverte et échue
    (`waiting.decision`) : le geste est de vérifier l'échéance avec le
    demandeur (renouveler ou retirer la demande) ; le décideur a le rappel de
    sa demande, l'alerte SLO ne le relance pas (canon §9, owner 18/10).

    #3288 : `blocked_freeze` est due par l'assigné (le débiteur de la
    correction), pas par le relecteur : ×1 l'assigné corrige et regel, ×2 le
    pilote réassigne, ×3 le bureau/l'auditeur réassigne à un agent libre de la
    famille de l'auteur.

    #3447 : un lot réservé à un exécutant externe n'est pas « unassigned » —
    le texte nomme l'exécutant (bureau, owner) et le fait que ce n'est pas une
    attribution. #6105 : pour un `system:…` déclaré, il nomme le responsable
    humain ; retiré, il dit que le pilote doit lever la réservation.
    #8163 : une réservation levée après une note de travail fait ne rend pas le
    lot « unassigned » : il est à clore, et le geste est sa clôture."""
    lot = int(alert["lot"])
    title = alert.get("title")
    level = int(alert.get("level") or 1) if level is None else int(level)
    step = alert.get("step_label") or alert.get("step") or "?"
    who = alert.get("agent")
    external = alert.get("external_executor")
    v = vocab.active()
    head = ("SLO breach, level %d: %s%s (%s), step `%s` late by %s (target %s)%s."
            % (level, v.label(lot), " \"%s\"" % title if title else "",
               alert.get("lot_state") or "?", step,
               sous_utilisation._duration(alert.get("value") or 0),
               sous_utilisation._duration(alert.get("threshold") or 0),
               (", owed by reviewer %s" % who if alert.get("step") == "green_verdict"
                else ", fix owed by %s" % who if alert.get("step") == "blocked_freeze"
                else ", assigned to %s" % who if who
                else ", reserved to %s" % _reserved_label(alert) if external
                else ", external work done, to close" if alert.get("external_done")
                else ", unassigned")))
    # #2016 : quand la commande est proposée, elle porte le titulaire lu
    # (`--if-holder`) : le pilote ou le bureau parti de ce brief est refusé si
    # le lot a changé de main entre-temps, au lieu d'écraser le premier geste.
    holder = " --if-holder %s" % who if who else ""
    waiting = alert.get("waiting") or {}
    what = waiting.get("what") if isinstance(waiting, dict) else None
    # #2511 : le lot cible le dépôt du canon ; un paquet ameesh ne le porte
    # pas, la fusion est celle du bureau. #7795 : le dépôt se lit à la
    # configuration et voyage dans l'alerte (`canon_repo`) ; sans lui, le
    # texte dit « le dépôt du canon », jamais un nom compilé.
    canon = bool(isinstance(waiting, dict) and waiting.get("canon"))
    canon_repo = str(alert.get("canon_repo") or "").strip()
    canon_where = ("the canon repository (%s)" % canon_repo if canon_repo
                   else "the canon repository")
    # #2378 : une demande ouverte et échue (sinon le lot serait en attente
    # prévue) ; les cibles sont passées au pilote à toute étape
    # (`exploitation._slo_targets`), le brief les suit. Sans demande,
    # l'attente `decision` garde le texte ordinaire.
    decision = waiting.get("decision") if what == "decision" else None
    if decision is None and (what == "decision"
                             or alert.get("step") != "verdict_merge"):
        # The targets move to the pilot on `verdict_merge` only
        # (`exploitation._slo_targets`): the brief follows them, so a text
        # addressed to the pilot never reaches the assignee.
        what = None
    # #4572 : la cause du refus d'admission au paquet, lue par la passe SLO
    # avec les codes de `paquet-candidats` (`exploitation._slo_breaches`).
    # Côté auteur (`freeze-not-head`, conflit…), le composeur ne peut rien :
    # le brief nomme l'auteur et son geste. « Le composeur le porte » reste
    # réservé au lot réellement prêt (`ready`) et pas encore composé.
    cause = waiting.get("candidate") if isinstance(waiting, dict) else None
    if what in ("merge", "merge-approval", "merge-outcome"):
        if canon:
            if level >= 3:
                move = ("avatar: merge it directly — its `canon/…` branch targets %s, "
                        "which an ameesh package does not carry; "
                        "auditor: see that the avatar does it, do not reassign or nudge the "
                        "holder" % canon_where)
            else:
                move = ("avatar: merge it directly — its `canon/…` branch targets %s, "
                        "outside the ameesh package; the "
                        "pilot/composer cannot carry it, do not wait for them" % canon_where)
        elif cause and cause.get("code") and cause.get("owner") == "author":
            author = who or alert.get("responsible") or "the author"
            code = str(cause["code"])
            detail = str(cause.get("detail") or "")
            seen = " — %s" % detail if detail else ""
            gesture = str(cause.get("gesture") or "")
            if level >= 3:
                move = ("avatar or auditor: the %s is frozen with its review verdict "
                        "acquired, but a package cannot carry it — cause `%s`%s; the "
                        "author %s still owes the fix (%s) — push the author and the "
                        "pilot, reassign the %s to a free agent of the same family if "
                        "the author cannot act; do not wait for the composer"
                        % (v.noun, code, seen, author, gesture, v.noun))
            elif level >= 2:
                move = ("pilot: the %s is frozen with its review verdict acquired, but a "
                        "package cannot carry it — cause `%s`%s; the author %s owes the fix "
                        "(%s) — push them; if they cannot act, hand the %s to a free agent "
                        "of the same family (ameesh work assign %d <agent>%s); the composer "
                        "cannot carry it before"
                        % (v.noun, code, seen, author, gesture, v.noun, lot, holder))
            else:
                move = ("author %s: the %s is frozen with its review verdict acquired, but "
                        "a package cannot carry it — cause `%s`%s; %s; the composer cannot "
                        "advance it until this is cleared"
                        % (author, v.noun, code, seen, gesture))
        elif cause and cause.get("unknown"):
            # Lecture incomplète sur cet hôte (dépôt absent, refs manquantes) :
            # on ne nomme ni l'auteur ni le composeur au hasard ; le brief
            # renvoie à la liste calculée, qui fait foi à la composition.
            if level >= 3:
                move = ("avatar or auditor: the %s is frozen with its review verdict "
                        "acquired; this host cannot read the candidate refusal — check "
                        "`scripts/paquet-candidats.py` before pushing the composer, the "
                        "%s may be waiting on its author (freeze-not-head, conflict)"
                        % (v.noun, v.noun))
            else:
                move = ("pilot: the %s is frozen with its review verdict acquired; this "
                        "host cannot read the candidate refusal — check "
                        "`scripts/paquet-candidats.py` before acting; the %s may be "
                        "waiting on its author (freeze-not-head, conflict), and the "
                        "composer can only carry a ready %s" % (v.noun, v.noun, v.noun))
        elif alert.get("packet_missed"):
            # #5860 : le créneau est passé sans le lot, alors que rien côté
            # auteur ne le retenait ; le pilote dit pourquoi le composeur l'a
            # laissé (note du lot) ou le met dans le paquet suivant.
            missed = alert["packet_missed"]
            slot = time.strftime("%H:%M", time.gmtime(float(missed.get("slot") or 0)))
            move = ("%s: the packet slot %sZ passed without the %s (%d slot(s) missed; "
                    "the clock runs from that slot) — %s say why the composer left it "
                    "out (ameesh work note %d \"packet: <reason>\", per "
                    "scripts/paquet-candidats.py) or include it in the next package; "
                    "do not reassign or nudge its holder"
                    % ("avatar or auditor" if level >= 3 else "pilot", slot, v.noun,
                       int(missed.get("count") or 0),
                       "push the pilot/composer to" if level >= 3 else "either", lot))
        elif alert.get("vehicle_exit_ts") is not None:
            # #4369 : retiré du paquet par sa recomposition ; le temps passé
            # dans le véhicule n'est pas compté, l'horloge part de la sortie.
            left = time.strftime("%H:%M", time.gmtime(float(alert["vehicle_exit_ts"])))
            move = ("%s: the %s left its packet at recomposition (%sZ; the clock runs "
                    "from there, not from the GO) — do not reassign or nudge its holder; "
                    "%s return it to build with the cause (ameesh work move %d build) "
                    "or include it in the next package"
                    % ("avatar or auditor" if level >= 3 else "pilot", v.noun, left,
                       "push the composer/pilot to" if level >= 3 else "either", lot))
        elif level >= 3:
            move = ("avatar or auditor: the %s waits for its merge vehicle/package — "
                    "the composer/pilot carries it, and reassigning or nudging its holder "
                    "does not advance it; push the pilot/composer to include it in the next "
                    "package" % v.noun)
        else:
            move = ("pilot: the %s is frozen with its review verdict acquired and waits "
                    "for its merge vehicle/package — the composer/pilot carries it; do not "
                    "reassign or nudge its holder, include it in the next package" % v.noun)
        if (not canon and not (cause and (cause.get("unknown")
                                          or (cause.get("code")
                                              and cause.get("owner") == "author")))):
            # #7205 : le lot est prêt et attend son véhicule. Un accusé
            # `hold: …` du pilote est une retenue que `paquet-candidats` lit
            # (`pilot-hold`) : le lot sortirait du paquet que ce brief demande.
            move += READY_HOLD_HINT
    elif decision is not None:
        decider = waiting.get("owner") or "its decider"
        due = waiting.get("due_ts")
        late = (" (deadline %s passed)" % time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(float(due)))
                if due is not None else "")
        # Canon §9 (owner 18/10) : rien sur les SLO pour l'owner — le rappel
        # du décideur est celui de la demande (`decision_overdue`), pas un
        # geste dicté par l'alerte SLO.
        if level >= 3:
            move = ("avatar or auditor: the %s is `waiting_human` on decision #%d for "
                    "%s%s — reassigning or nudging its holder does not advance it; push "
                    "the pilot to check the deadline with the requester, who renews or "
                    "withdraws the request (ameesh decide show %d)"
                    % (v.noun, decision, decider, late, decision))
        else:
            move = ("pilot: the %s is `waiting_human` on decision #%d for %s%s — do not "
                    "reassign its holder; check the deadline with the requester, who "
                    "renews or withdraws the request (ameesh decide show %d); the "
                    "decider gets the request's own reminder, not an SLO nudge"
                    % (v.noun, decision, decider, late, decision))
    elif what == "unblock":
        if level >= 3:
            move = ("avatar or auditor: the %s is `blocked` and the pilot owns the "
                    "unblock (or the recorded decision) — do not reassign its holder; "
                    "push the pilot to unblock it or to say why the %s waits"
                    % (v.noun, v.noun))
        else:
            move = ("pilot: the %s is `blocked` and waits for its unblock/decision — "
                    "do not reassign its holder; unblock it or record the decision "
                    "(ameesh work note %d \"hold: <reason>\")" % (v.noun, lot))
    elif what == "packet":
        # #3487 : le lot est porté par un paquet composé ; son attente est la CI
        # complète du paquet et sa fusion. Le geste nomme le paquet et sa CI,
        # jamais une réassignation du porteur (qui n'avancerait rien).
        vehicle = waiting.get("vehicle")
        packet = ("#%s" % vehicle if vehicle is not None
                  else (waiting.get("who") or "its packet"))
        branch = str(waiting.get("branch") or "")
        where = " (%s)" % branch if branch else ""
        ci = _packet_ci_text(waiting.get("ci") or {})
        if waiting.get("self"):
            # #6703 : le lot est lui-même le véhicule du paquet ; sa vraie
            # attente est sa propre CI complète, puis sa fusion. Aucun
            # composeur ne le portera et aucune dépendance n'est à déclarer.
            if level >= 3:
                move = ("avatar or auditor: the %s is itself the packet vehicle %s%s "
                        "and waits for its full CI%s — push the pilot to merge it once "
                        "green; no composer carries it and no dependency is to be "
                        "declared" % (v.noun, packet, where, ci))
            else:
                move = ("pilot: the %s is itself the packet vehicle %s%s and waits for "
                        "its full CI%s — do not look for a composer or declare a "
                        "dependency: merge it once its CI is green"
                        % (v.noun, packet, where, ci))
        elif level >= 3:
            move = ("avatar or auditor: the %s is carried by packet %s%s and waits for "
                    "the packet's full CI%s — do not reassign or nudge its holder; push "
                    "the pilot/composer to see the packet through (the %s merges with "
                    "it)" % (v.noun, packet, where, ci, v.noun))
        else:
            move = ("pilot: the %s is carried by packet %s%s and waits for the packet's "
                    "full CI%s — do not reassign or nudge its holder; the composer/pilot "
                    "carries the packet merge" % (v.noun, packet, where, ci))
        no_train = str(waiting.get("no_train") or "")
        vehicle_of = str(waiting.get("packet_vehicle") or "")
        if vehicle_of:
            # #8172 : le train exclut un véhicule `integration/**` par
            # conception ; le pilote du projet le fusionne (§12).
            move += "; %s — %s" % (
                PACKET_VEHICLE_MERGE,
                "push the pilot of %s to merge it once its full CI is green"
                % vehicle_of if level >= 3
                else "merge it yourself once its full CI is green on the target head")
        elif no_train:
            # #8159 : aucun train ni radar n'est déclaré pour le projet ; rien
            # ne composera le paquet ni ne démarrera sa CI à la place du pilote.
            move += ("; no train declared for %s: manual composition by the pilot "
                     "(§12) — %s" % (
                         no_train,
                         "push the pilot of %s to compose the packet on the target "
                         "head and dispatch its full CI" % no_train if level >= 3
                         else "no train will compose, start or merge it: integrate "
                         "the target head and dispatch the full CI yourself"))
    elif alert.get("step") == "green_verdict":
        # #3033 : l'avis est dû par le relecteur désigné, pas par le porteur ;
        # le geste est de rendre le verdict, puis de remplacer le relecteur.
        sha = str(alert.get("sha") or "")
        verdict = "ameesh work milestone %d verdict ok|blocked --sha %s" % (
            lot, sha or "<frozen sha>")
        if level >= 3:
            move = ("avatar or auditor: the pilot has not replaced reviewer %s — designate "
                    "another reviewer of an independent family now (ameesh work reviewer "
                    "%d <agent>, then ameesh work reviewer %d %s --remove)"
                    % (who or "?", lot, lot, who or "<reviewer>"))
        elif level >= 2:
            move = ("pilot: reviewer %s has not delivered the verdict — nudge them, or "
                    "designate another reviewer of an independent family (ameesh work "
                    "reviewer %d <agent>, then ameesh work reviewer %d %s --remove); do not "
                    "reassign the %s's holder" % (who or "?", lot, lot,
                                                  who or "<reviewer>", v.noun))
        else:
            move = ("reviewer %s: review the frozen head and render your verdict now (%s); "
                    "if you cannot, tell the pilot so another reviewer is designated"
                    % (who or "?", verdict))
    elif alert.get("step") == "blocked_freeze":
        # #3288 : un avis bloquant n'attend pas un relecteur mais la correction
        # de l'assigné ; le canon §9 escalade de l'assigné au pilote, puis au
        # bureau/à l'auditeur. #3487 : même horloge pour un lot revenu en build
        # après son GO, et le niveau 3 ne prescrit jamais de réassigner le
        # porteur qui porte le correctif (la réassignation perd son contexte).
        blocker = str(alert.get("blocked_by") or "")
        reason = ("the fix owed after returning to build" if alert.get("returned")
                  else "the blocking verdict of %s" % (blocker or "a reviewer"))
        if level >= 3:
            move = ("avatar or auditor: %s is still owed by %s, who carries the fix — do "
                    "not reassign the holder (a reassignment loses the context of the "
                    "fix); push the holder and the pilot to deliver the new freeze, and "
                    "ask the pilot to record a hold if it cannot advance (a pilot hold is "
                    "what suspends this clock)" % (reason, who or "the assignee"))
        elif level >= 2:
            move = ("pilot: %s — push %s; if the holder cannot deliver it, take the fix "
                    "yourself or hand it over to a free agent of the same family (ameesh "
                    "work assign %d <agent>%s, then agent-mail send <agent> \"…\" --lot "
                    "%d)" % (reason, who or "the assignee", lot, holder, lot))
        else:
            move = ("assignee: deliver %s and freeze the new head (`ameesh work "
                    "milestone %d frozen --sha <new head>`); if you moved the %s to "
                    "build, re-enter qa (`ameesh work move %d qa`); if you cannot, say "
                    "so (`ameesh work note %d \"hold: <reason>\"`) so the pilot hands "
                    "the fix over" % (reason, lot, v.noun, lot, lot))
    elif alert.get("external_done"):
        # #8163 : le travail est fait et la réservation levée : le lot est à
        # clore. Aucun rôle ne le réassigne comme s'il n'avait pas de preneur ;
        # le palier ne change que le destinataire du même geste.
        was = str(alert.get("external_was") or "").strip() or "the external executor"
        close = ("ameesh work close %d --delivered --sha <reviewed head> if it was "
                 "delivered outside the target, or ameesh work close %d --done "
                 "without a branch" % (lot, lot))
        if level >= 3:
            move = ("avatar or auditor: %s is to close — its external executor %s "
                    "reported the work done (verification/delivery note) and cleared "
                    "the reservation; close it now (%s); do not reassign it"
                    % (v.label(lot), was, close))
        elif level >= 2:
            move = ("pilot: %s is to close — its external executor %s reported the work "
                    "done (verification/delivery note) and cleared the reservation; "
                    "close it now (%s); do not reassign it"
                    % (v.label(lot), was, close))
        else:
            move = ("pilot: %s is to close — its external executor %s reported the work "
                    "done (verification/delivery note) and cleared the reservation; "
                    "close it now (%s); a reassignment would hand finished work to an "
                    "agent" % (v.label(lot), was, close))
    elif external:
        # #3447 : le lot est réservé, pas assigné — le geste n'est ni une
        # relance d'assigné ni une réassignation directe (refusée par
        # `work assign`) mais l'action de l'exécutant ou la levée préalable.
        # #6105 : pour un `system:…` déclaré, l'action nomme son responsable
        # humain ; retiré, elle revient au pilote, seul à pouvoir lever.
        responsible = (alert.get("external_responsible") or "").strip() or None
        if alert.get("external_retired"):
            move = ("pilot: %s is reserved to %s, a retired non-agent system — nobody "
                    "acts for it; clear the reservation and reassign the lot (ameesh "
                    "work external-executor %d --clear)"
                    % (v.label(lot), external, lot))
        elif responsible and level >= 3:
            move = ("avatar or auditor: %s is reserved to %s, a non-agent system whose "
                    "declared responsible is %s — this is not an assignment; the "
                    "responsible acts, or the pilot clears the reservation before any "
                    "reassignment (ameesh work external-executor %d --clear)"
                    % (v.label(lot), external, responsible, lot))
        elif responsible and level >= 2:
            move = ("pilot: %s is reserved to %s, a non-agent system (declared "
                    "responsible %s) — do not assign it; have the responsible act, or "
                    "clear the reservation before reassigning (ameesh work "
                    "external-executor %d --clear)"
                    % (v.label(lot), external, responsible, lot))
        elif responsible:
            move = ("%s (declared responsible for the non-agent system %s): %s is "
                    "reserved to that system; act on it, or tell the pilot the "
                    "reservation is without object (the pilot clears it with ameesh "
                    "work external-executor %d --clear)"
                    % (responsible, external, v.label(lot), lot))
        elif level >= 3:
            move = ("avatar or auditor: %s is reserved to %s (external executor, not "
                    "an assignee) — this is not an assignment; the executor acts, or the "
                    "pilot clears the reservation before any reassignment (ameesh work "
                    "external-executor %d --clear)" % (v.label(lot), external, lot))
        elif level >= 2:
            move = ("pilot: %s is reserved to %s (external executor) — do not assign "
                    "it as is; the executor acts, or clear the reservation before "
                    "reassigning (ameesh work external-executor %d --clear)"
                    % (v.label(lot), external, lot))
        else:
            move = ("%s (external executor): %s is reserved to you; act on it, or tell "
                    "the pilot it is not for you (the pilot clears the reservation with "
                    "ameesh work external-executor %d --clear)"
                    % (external, v.label(lot), lot))
    elif level >= 3:
        move = ("avatar or auditor: reassign it now to a free agent (ameesh work assign "
                "%d <agent>%s, then agent-mail send <agent> \"…\" --lot %d); do not wait "
                "for the pilot if it is itself late" % (lot, holder, lot))
    elif level >= 2:
        move = ("pilot: reassign it or unblock it (ameesh work assign %d <agent>%s, then "
                "agent-mail send <agent> \"…\" --lot %d)" % (lot, holder, lot))
    else:
        # #3274 : un `hold:` de l'assigné n'arrête que `sans_tour` (24 h,
        # `exploitation.ORPHAN_SELF_HOLD_S`) ; seule la retenue d'un pilote
        # suspend le SLO (#1664, `planned_waits.pilot_holds`). Le texte le dit,
        # sinon l'assigné croit l'horloge arrêtée et ne voit pas l'escalade.
        move = ("assignee: act on it, or record why it cannot advance (ameesh work note "
                "%d \"hold: <reason>\") — a silent %s is not a hold. Your own hold only "
                "postpones the `sans_tour` reminder for 24 h; it does not suspend this "
                "SLO clock — only a pilot's hold does" % (lot, v.noun))
    desk = desk or _desk_label(alert)
    if alert.get("critical"):
        move += ("; the %s is P1: the avatar handles it with the auditor (canon §9, "
                 "niveau 4), and no SLO delay is notified to the owner — nothing waits "
                 "for a human that an agent can do" % v.noun)
    return "%s Expected action: %s." % (head, _with_desk(move, desk))


def _critical_budget_pause(alert: dict) -> str:
    """L242b (5) : aux orchestrateurs de l'équipe et aux auditeurs (urgent)."""
    return ("Critical agent paused on budget: %s. Expected action: check the accounts "
            "(ameesh accounts list) and the mesh caps (ameesh budget); free an account or "
            "raise a cap with the owner, or hand its urgent work to another agent."
            % (alert.get("detail") or "?"))


def _account_wave_coordination(alert: dict) -> str:
    """#7712 : aux orchestrateurs de l'équipe et aux auditeurs."""
    return ("Critical agent blocked by Claude account-wave coordination: %s. "
            "No budget cap is involved: its session origin and the last effective "
            "switch disagree, and the planner keeps refusing the wave. Expected "
            "action: read `account_switches` (planned/agent) for that persona and "
            "the wave journal; the turn retries on its own after one refusal, so "
            "check again next pass before acting, then free the effective account "
            "(`ameesh accounts`) or let the pending switch apply."
            % (alert.get("detail") or "?"))


def _account_config_invalid(alert: dict) -> str:
    """#3969 : aux orchestrateurs de l'équipe et aux auditeurs."""
    return ("No valid harness account on the host: %s. Expected action: apply the fix "
            "given for each account (chmod 700 of a profile directory, a login), check "
            "with ameesh doctor on that host; the agent resumes on its own at the next "
            "check, no `ameesh resume` needed." % (alert.get("detail") or "?"))


def _budget_pause_long(alert: dict) -> str:
    """#1466 : une pause budget qui dure n'est plus ordinaire. Aux
    orchestrateurs de l'équipe et aux auditeurs.

    #6031 : l'âge du relevé est dans le détail ; un relevé ancien est une
    cause possible de la pause (la fenêtre du fournisseur a pu changer sans
    qu'aucun tour ne publie de relevé neuf).

    #7175 : tous les comptes du harnais au seuil sur l'hôte, échéance
    connue — aucun geste, dit une fois.

    #7572 : l'échéance peut être une remise à zéro (fixe) ou un rattrapage
    du rythme (révisable) ; le détail le dit, avec le compte qui la porte.
    Un recul de l'échéance ne refait pas de courrier. Une remise à zéro
    réellement dépassée sans reprise, elle, est un nouvel épisode (#7175) :
    le brief suivant passe par le texte « check the caps ». Une attente de
    remise à zéro n'a pas de relance ; une pause avec geste attendu garde
    celle de 6 h (#1466).

    #8266 : une pause de famille et de compte est une seule alerte, qui liste
    ses agents. Tant qu'une décision owner datée la couvre (canon
    `auto_take`), c'est un rapport d'information : aucun réveil, aucune
    relance ; l'alerte reste lisible dans `ameesh alerts`. Un agent critique
    sans tour possible garde son propre `critical_budget_pause`, urgent."""
    detail = alert.get("detail") or "?"
    agents = list(alert.get("agents") or ())
    grouped = bool(alert.get("grouped")) and len(agents) > 1
    who = ("%d agents paused" % len(agents)) if grouped else "Agent paused"
    critical = ", ".join(alert.get("critical_agents") or ())
    critical_txt = (" The critical agent(s) %s keep their own urgent budget "
                    "alert." % critical) if critical else ""
    if alert.get("covered_by_decision"):
        coverage = alert.get("coverage") or {}
        until = coverage.get("until_ts")
        try:
            until_txt = (time.strftime("%d/%m %H:%MZ", time.gmtime(float(until)))
                         if until is not None else "?")
        except (TypeError, ValueError):
            until_txt = "?"
        return ("%s on budget for a long time: %s. Expected action: none — the "
                "canon `%s` exclusion of family %s is the dated owner decision "
                "that covers this pause (rule expires %s, %s; covered until %s). "
                "This is an information report: no wake and no reminder while it "
                "lasts; the pause stays live in `ameesh alerts`.%s"
                % (who, detail, coverage.get("rule") or "auto_take",
                   coverage.get("family") or "?", coverage.get("rule_expire") or "?",
                   coverage.get("timezone") or "?", until_txt, critical_txt))
    uncovered = ", ".join(alert.get("uncovered_agents") or ())
    uncovered_txt = (" Uncovered by any unexpired owner decision: %s." % uncovered
                     if uncovered else "")
    if alert.get("waits_reset"):
        return ("%s on budget until the announced time: %s. Expected action: "
                "none — every account of its harness on the host is at its "
                "threshold and the agent resumes on its own at the announced "
                "time; this is said once for the pause, with no reminder while it "
                "lasts and no new alert when a pace catch-up slides (read the live "
                "detail with `ameesh alerts`). If the announced window reset passes "
                "and the agent has not resumed, a new alert follows.%s%s"
                % (who, detail, uncovered_txt, critical_txt))
    return ("%s on budget for a long time: %s. Expected action: check the caps "
            "(ameesh budget), the accounts (ameesh accounts list) and the configuration "
            "the runner reads (ameesh doctor; a profile can diverge from the host's "
            "config.json); free an account, fix the profile, or have the runner restart, "
            "then `ameesh resume` the agent if its runner is not live. If the detail "
            "announces a resume time, its nature is stated there: a window reset is "
            "fixed, a pace catch-up may recede as the account is consumed. If the last "
            "gauge reading is old, that reading may be stale and hold the pause after "
            "its window reset — check it with `ameesh accounts list`. This is not "
            "repeated at every reading; it stays live in `ameesh alerts` and is "
            "reminded after six hours while it lasts.%s%s"
            % (who, detail, uncovered_txt, critical_txt))


def automatic_sleep(alert: dict) -> bool:
    """#4525 : la pause qui dure est le sommeil automatique du découpage."""
    from . import file_decoupage
    return bool(alert.get("sleep") or file_decoupage.is_sleep_reason(alert.get("reason")))


def sleep_unbriefed(alert: dict) -> bool:
    """#6256 : le sommeil automatique n'est briefé à aucun agent.

    Quand la feuille de route est épuisée, 12 à 26 agents s'endorment ensemble
    et franchissent ensemble les seuils : un courrier par agent, par hôte et par
    destinataire, puis la relance de 6 h (264 courriers en 24 h, 08/11), pour un
    texte qui dit lui-même qu'aucun geste n'est attendu. L'alerte reste dans
    `ameesh alerts` ; une vraie pause opérateur garde son brief."""
    return alert.get("type") in ("operator_pause_long", "operator_pause_host") \
        and automatic_sleep(alert)


def slo_ordinary_l1(alert: dict) -> bool:
    """#6642 : un `slo_breach` de niveau 1 ni urgent ni P1 (critique)."""
    return alert.get("type") == "slo_breach" \
        and int(alert.get("level") or 0) == 1 \
        and not alert.get("urgent") and not alert.get("critical")


def slo_grace_unbriefed(alert: dict) -> bool:
    """#6642 (décision owner #207135 a) : un SLO de niveau 1 ordinaire en
    retard de moins de `SLO_L1_GRACE_S` n'est briefé à personne.

    L'alerte existe dès le retard, datée à l'heure exacte (`since`, `value`),
    et reste dans `ameesh alerts` : seul le courrier est différé. Urgent et P1
    gardent leur envoi immédiat (contre-examen #207148) ; le niveau 2 aussi."""
    if not slo_ordinary_l1(alert):
        return False
    try:
        return float(alert.get("value") or 0) < SLO_L1_GRACE_S
    except (TypeError, ValueError):
        return False


def slo_vehicle_wait(alert: dict, name: str | None = None) -> bool:
    """#7443 : un lot validé qui attend son véhicule de fusion n'appelle aucun geste.

    Au niveau 1 ordinaire, le composeur ou le paquet porte la fusion : le
    pilote n'a rien à corriger, rien à décider, et attend un tiers nommé. Le
    rappel part donc en information passive (lu au tour suivant), comme le
    faisait déjà #7308 pour le seul cas `packet` et pour `merge` sans
    responsable nommé.

    Gardent le réveil les cas où le destinataire a un geste : créneau de
    paquet manqué (`packet_missed`), cause côté auteur (`candidate`), fusion
    du canon portée par le bureau, lot lui-même véhicule du paquet (`self`),
    CI de paquet rouge (une unité est à corriger) et sortie du véhicule
    (`vehicle_exit_ts`, à remettre en build ou au paquet suivant). Les
    niveaux 2/3, un P1 et une urgence réveillent aussi : la relance du palier
    supérieur est un geste.
    """
    if not (slo_ordinary_l1(alert)
            and alert.get("lot_state") == "validated"
            and alert.get("step") == "verdict_merge"):
        return False
    waiting = alert.get("waiting") or {}
    # `merge-approval`/`merge-outcome` attendent un tiers nommé (approbateur,
    # responsable) : le pilote ne décide ni ne réconcilie à sa place.
    if waiting.get("what") not in ("merge", "merge-approval", "merge-outcome",
                                   "packet"):
        return False
    if (waiting.get("canon") or waiting.get("candidate")
            or waiting.get("packet_missed") or waiting.get("self")
            or alert.get("vehicle_exit_ts") is not None):
        return False
    # Le tiers nommé qui reçoit lui-même le rappel n'est plus un tiers : il a
    # le geste (fusion, approbation, réconciliation) et garde le réveil.
    who = waiting.get("who")
    if isinstance(who, str) and name is not None and who == name:
        return False
    ci = waiting.get("ci")
    if isinstance(ci, dict) and ci.get("state") == "red":
        return False
    return True


#: #8117 : alertes dont le geste revient à un opérateur (relivrer, relancer
#: un agent arrêté) : le courrier d'agent part en `info`, sans réveil, sauf
#: alerte critique ou urgente (P1).
OPERATOR_GESTURE_TYPES = ("mail_undeliverable",)


def quiet_modes(alert: dict, names, by_name: dict) -> dict:
    """#7443 : par destinataire, le mode de son courrier et la raison du choix.

    Rend `{nom: {"payload": {...}, "reason": "..."}}`. Le mode se lit dans le
    payload (`mail.passive_reason`) : `info`/`cc` sont passifs, son absence
    réveille. Le journal du notifieur nomme ainsi le mode et la raison
    (#7443), au lieu de laisser le lecteur deviner.

    #5044/#7308 : le sommeil automatique, les constats de capacité et un lot
    validé qui attend son véhicule de fusion n'attendent aucun geste. Une
    alerte SLO copiée à plusieurs coordinateurs n'en réveille qu'un, stable
    d'une passe à l'autre (`_one_orchestrator`) ; les autres la lisent en copie
    à leur tour suivant, et le palier suivant reprend si rien ne bouge. Un P1
    (`urgent`) réveille tout le monde, comme avant.

    #7009 : une pause critique ne réveille pas un destinataire lui-même en
    pause budget sur le même compte (11/11 03:32-04:23Z : auditeur,
    mesh-design et orchestrateur se réveillaient l'un l'autre pour une pause
    qu'aucun ne pouvait lever) ; il la lit en `info`, sans l'urgence.

    #8117 : une alerte de `OPERATOR_GESTURE_TYPES` non critique part en
    `info` à tous ses destinataires."""
    # Les textes routés portent aussi une escalade de secours (#3036).
    escalated = {name for name, body in names.items()
                 if "Escalated from level " in body} if isinstance(names, dict) else set()
    names = [name for name in names if not name.startswith("thread:")]
    kind = alert.get("type")
    if kind == "critical_budget_pause":
        account = (alert.get("episode") or ("",))[0]
        return {name: {"payload": {"info": True, "urgent": False},
                       "reason": "budget pause on the same account: no action due"}
                for name in names
                if account and _budget_paused_on(by_name.get(name), account)}
    if alert.get("urgent"):
        return {}
    if kind == "budget_pause_long" and alert.get("covered_by_decision"):
        # #8266 : la pause est couverte par une décision owner datée et non
        # échue ; le rapport reste lisible au tour suivant, sans réveil. Un
        # agent critique sans tour possible garde son `critical_budget_pause`
        # à part, urgent : la couverture ne l'éteint pas.
        return {name: {"payload": {"info": True},
                       "reason": "budget pause covered by a dated owner decision: "
                                 "no action due"}
                for name in names}
    if kind in OPERATOR_GESTURE_TYPES and not alert.get("critical"):
        # #8117 : le geste revient à un opérateur ; le pilote le lit au tour
        # suivant, sans réveil ni interruption (l'urgence du type reste celle
        # de la route humaine, `notify.URGENT_TYPES`).
        return {name: {"payload": {"info": True, "urgent": False},
                       "reason": "operator gesture: no wake unless critical"}
                for name in names}
    # #7308/#7443 : les constats sans geste immédiat restent lisibles au tour
    # suivant.
    if not alert.get("critical") and kind in ("idle_capacity", "plan_underused"):
        return {name: {"payload": {"info": True},
                       "reason": "status report: no action due"}
                for name in names if name not in escalated}
    vehicles = {name for name in names
                if name not in escalated and slo_vehicle_wait(alert, name)}
    if vehicles:
        return {name: {"payload": {"info": True},
                       "reason": "validated lot waiting for its merge vehicle: "
                                 "no action due"} for name in vehicles}
    if kind in ("operator_pause_long", "operator_pause_host") and automatic_sleep(alert):
        return {name: {"payload": {"info": True},
                       "reason": "automatic sleep: no action due"}
                for name in names}
    if kind != "slo_breach" or len(names) < 2:
        return {}
    holder = alert.get("agent")
    woken = holder if holder in names else \
        _one_orchestrator(names, by_name, alert.get("lot"))[0]
    return {name: {"payload": {"cc": woken},
                   "reason": "copy: %s wakes for this SLO" % woken}
            for name in names if name != woken}


def quiet_payloads(alert: dict, names, by_name: dict) -> dict:
    """#5044 : les payloads passifs par destinataire (compatibilité de l'API).

    `quiet_modes` porte en plus la raison journalisée (#7443)."""
    return {name: decision["payload"]
            for name, decision in quiet_modes(alert, names, by_name).items()}


def wake_reason(alert: dict, name: str, payload: dict | None = None) -> str:
    """#7443 : pourquoi ce destinataire est réveillé, pour le journal.

    Le mode `wake` n'a pas de payload passif ; la raison nomme le geste attendu
    (correction due, verdict de relecture, fusion, urgence)."""
    if (payload or {}).get("urgent") or alert.get("urgent"):
        return "urgent"
    if alert.get("type") == "slo_breach":
        step = alert.get("step")
        level = int(alert.get("level") or 1)
        action = {
            "blocked_freeze": "correction owed after a review",
            "green_verdict": "review verdict owed",
            "verdict_merge": "merge or unblock due",
            "intake_started": "lot to take",
            "build_quiet": "work due",
        }.get(step, "action due")
        return "%s (SLO level %d)" % (action, level)
    return "action expected"


def _budget_paused_on(row, account: str) -> bool:
    """#7009 : l'agent est en pause budget (statut `blocked`) sur ce compte."""
    from . import accounts as accounts_mod
    from . import budget as budget_mod
    if not row or row.get("status") != "blocked":
        return False
    text = row.get("status_text") or ""
    if not text.startswith(budget_mod.PAUSE_PREFIX):
        return False
    reason = text[len(budget_mod.PAUSE_PREFIX):]
    if reason.startswith(budget_mod.CRITICAL_MARK):
        reason = reason[len(budget_mod.CRITICAL_MARK):]
    nature = accounts_mod.pause_nature(reason).split("|")
    if nature[0] == "pause":
        return nature[1] == account
    return (row.get("session_account") or "") == account


def _operator_pause(alert: dict) -> str:
    """#3961 : une pause opérateur qui dure (un agent, ou la plupart des
    agents d'un hôte). Aux orchestrateurs de l'équipe et aux auditeurs.

    #4525 : le sommeil automatique du découpage (`SLEEP_REASON`) n'est pas une
    maintenance. L'alerte le reconnaît et dit qu'il est levé au premier lot
    démarrable, sans demander à l'owner de mettre fin à une maintenance.
    #4610 : la levée est aussi automatique sur un lot attribué ou un courrier
    du pilote ou d'un orchestrateur.
    """
    v = vocab.active()
    if automatic_sleep(alert):
        return ("Automatic sleep still in place: %s. This is the automatic sleep of an "
                "exhausted roadmap, not a maintenance: it is lifted by the first "
                "startable %s, an assigned actionable %s, or pilot/orchestrator "
                "mail, and at the latest when its deadline expires. No "
                "operator action is expected; the owner is not asked to end any "
                "maintenance." % (alert.get("detail") or "?", v.noun, v.noun))
    return ("Operator pause still in place: %s. Expected action: check with whoever set it "
            "(ameesh show <agent>) whether the maintenance is over; if so, lift it "
            "(ameesh resume <agent> --operator --reason <reason>), otherwise hand the "
            "agent's urgent %s to another agent." % (alert.get("detail") or "?", v.plural))


def _rate_limited_chronic(alert: dict) -> str:
    """#3733 : des épisodes de débit courts, effacés par un succès, couvrent
    une bonne part de l'heure. Aux orchestrateurs de l'équipe et aux
    auditeurs ; pas à l'agent, qui attend sa reprise."""
    agent = alert.get("agent") or "?"
    return ("Agent rate-limited for a large share of the last hour: %s. Expected action: "
            "the runner resumes it by itself, but its work does not move — hand its "
            "reviews due and urgent %s to an agent of another account or family "
            "(ameesh work pending-reviews %s; ameesh work reviewer <lot> <agent>); check "
            "its recent input volume (ameesh cost turns --agent %s) and rotate its "
            "session earlier if each turn rereads a large context."
            % (alert.get("detail") or "?", vocab.active().plural, agent, agent))


def _notify_undeliverable(alert: dict) -> str:
    """#3007 : aucun notificateur vivant ne couvre l'humain qui a des décisions
    en attente. Aux orchestrateurs et à l'auditeur : la poussée n'a pas eu
    lieu ; #3383 : le geste est la route de l'humain sur chaque candidat du
    rôle qui porte la minuterie `alerts`. Le brief est porté hors du poste (`notify_gap.brief_once`)."""
    return ("No living notifier covers the human with pending decisions: %s. Expected "
            "action (#3383): the alerts pass is a role timer, run by the lease holder; "
            "declare the human's notify route on every candidate of the role and run "
            "`ameesh role timers --install` there (`--check` names the gap); keep "
            "the pending requests visible in the %s threads meanwhile — no agent can "
            "push them without a channel. This alert resolves by itself when a notifier "
            "beats again for that human, or when nothing is pending."
            % (alert.get("detail") or "no notifier heartbeat", vocab.active().noun))


def _for_auditor(alert: dict) -> str:
    """`idle_capacity` à l'auditeur : le constat et l'action de l'alerte ; avec
    des lots démarrables ouverts sans agent, l'écart est le sien (L165b,
    niveau 1). #1608 : un lot qu'une attente planifiée retient n'est pas
    démarrable, on ne lui demande pas de l'assigner."""
    v = vocab.active()
    text = "Idle capacity: %s." % (alert.get("detail") or "?")
    waiting = (" %s are waiting with idle agents: if this still holds at your next "
               "pass, 30 min or more after this mail, fix it yourself (level 1, reversible): "
               "ameesh work assign <lot> <agent> (an idle agent of the %s's team, highest "
               "priority first), then agent-mail send <agent> \"…\" --lot <lot> to "
               "wake it; name it in your report." % (v.title_plural, v.noun))
    if alert.get("startable_lots"):
        text += waiting
    elif alert.get("external_lots"):
        # #3447 : ces lots ne s'assignent pas — l'alerte nomme l'exécutant
        # externe (bureau, owner) ; le pilote lève la réservation s'il la
        # juge sans objet.
        text += (" No startable %s: %s — the pilot declared them; their external executor "
                 "acts, do not assign an agent. If the reservation is without object, ask "
                 "the pilot to clear it (ameesh work external-executor <lot> --clear)."
                 % (v.noun, ", ".join("#%s (%s)" % (row.get("lot"), row.get("executor"))
                                      for row in alert.get("external_lots") or ())))
    elif alert.get("lots") and "startable_lots" not in alert:
        # Producteur d'avant #1608 : la liste `lots` n'était pas classée.
        text += waiting
    return text


def _target_names(role: str, alert: dict, by_name: dict, orchestras, teams: set,
                  auditor_names=(), avatar_names=()) -> list:
    """#3036 : les noms que porte un rôle d'escalade du §9, avant vivacité.

    `assignee` : l'agent du lot ; `pilot` : les orchestrateurs de l'équipe (le
    `targets` du moteur SLO dit le rôle, pas les noms) ; `avatar` : l'avatar
    de l'humain visé, lu au canon (#7634), sauf `externe` (0030 : il ne lit
    pas sa boîte, sa copie va au fil) ; `auditeur` : les auditeurs
    configurés. Un rôle inconnu qui nomme un agent du registre est cet agent
    (compatibilité des alertes antérieures à #7634 et des bancs). L'ordre est
    stable, sans doublon."""
    if role == "assignee":
        name = alert.get("agent")
        return ([name] if name in by_name and not _external(by_name, name) else [])
    if role == "pilot":
        return [name for name in _of_teams(orchestras, by_name, teams)
                if not _external(by_name, name)]
    if role == "avatar":
        return [name for name in dict.fromkeys(str(n) for n in avatar_names or ())
                if name in by_name and not _external(by_name, name)]
    if role == AUDITOR_ROLES[0]:
        return [name for name in auditor_names or ()
                if name in by_name and not _external(by_name, name)]
    if role in by_name:
        return [] if _external(by_name, role) else [str(role)]
    return []


def _slo_briefs(alert: dict, by_name: dict, orchestras, teams: set,
                auditor_names, closed: set, covered=(), avatar_names=()) -> dict:
    """#3036 : destinataires d'un `slo_breach`, filtrés par vivacité, avec
    passage au palier suivant quand le palier atteint n'a personne qui lise.

    Palier atteint : les `targets` du moteur SLO. Aucun vivant : on essaie le
    palier suivant de l'échelle §9 (assigné → pilote → bureau/auditeur), puis
    la route humaine (copie au fil du bureau). Le brief dit toujours qui
    manquait (« No live recipient: X (no live lease). ») et le texte du geste
    est celui du palier qui reçoit, pas celui du palier muet.

    #7749 : l'appelant met dans `avatar_names` la route humaine effective —
    avatar vivant, sinon route configurée de l'humain — et non un avatar en
    pause budget, arrêté ou sans bail. Sans route configurée, le fil de
    l'avatar mort reste la trace lisible ; l'auditeur, présent au palier des
    P1 et du niveau 3, porte le geste.

    #3057 (route voulue) : un lot sans assigné, ou assigné au bureau
    `externe` qui ne lit pas sa boîte, n'a personne au palier 1 ; il part au
    pilote dès le niveau 1, comme un assigné mort. Un palier 1 muet laisserait
    le lot sans lecteur jusqu'au double du délai, et le pilote est celui qui
    peut l'affecter (§9 : assigné → pilote). Le brief nomme le bureau externe
    parmi les absents au lieu de « none designated ».

    #3100 : `covered` (les destinataires déjà remis, `to` du registre des
    briefs) compte comme vivant : le courrier est dans leur boîte, lu au
    retour ; un bail qui cligne (redémarrage d'exécuteur, clôture de release)
    ne fait ni escalader ni nommer absent un palier déjà prévenu.
    #6396 : une pause opérateur fait exception, même après remise : ce
    courrier ne la lève pas ; le palier suivant doit porter le geste."""
    covered = set(covered or ())
    roles = set(alert.get("targets") or ("assignee",))
    desk = _desk_names(alert, by_name, avatar_names)
    live_desks, _ = _split_live({name: "" for name in desk}, by_name, closed,
                                respect_pause=True)
    label = next(iter(live_desks), "avatar")
    text = _slo_breach(alert, desk=label)
    raw: dict = {}
    for role in roles:
        for name in _target_names(role, alert, by_name, orchestras, teams,
                                  auditor_names, avatar_names):
            raw.setdefault(name, role)
    live, dead = _split_live({name: text for name in raw}, by_name, closed, covered,
                             respect_pause=True)
    if "assignee" in roles and alert.get("agent") \
            and _external(by_name, alert.get("agent")):
        dead.setdefault(str(alert.get("agent")),
                        "external avatar, reads the %s thread"
                        % vocab.active().noun)
    if live and len(live) > 1 and slo_ordinary_l1(alert):
        # #6642 : au niveau 1 ordinaire, le courrier va au seul débiteur —
        # le porteur s'il lit, sinon un pilote stable d'une passe à l'autre ;
        # les copies partent dès le niveau 2.
        # Un destinataire déjà remis (`covered`, #3100) le reste : un bail qui
        # cligne ne fait pas repartir le courrier vers un autre pilote.
        holder = alert.get("agent")
        names = sorted(live)
        served = [name for name in names if name in covered]
        if served:
            keep = served[0]
        elif holder in live:
            keep = holder
        else:
            picked = _one_orchestrator(names, by_name, alert.get("lot"))
            keep = picked[0] if len(picked) == 1 \
                else names[int(alert.get("lot") or 0) % len(names)]
        live = {keep: live[keep]}
    if live:
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    known = {"assignee", "pilot", AUDITOR_ROLES[0], "avatar"}
    current = (3 if roles & {"avatar", AUDITOR_ROLES[0]}
               or any(r not in known for r in roles)
               else 2 if "pilot" in roles else 1)
    for next_level in range(current + 1, 4):
        next_text = _slo_breach(alert, level=next_level, desk=label)
        if next_level == 2:
            names = _target_names("pilot", alert, by_name, orchestras, teams,
                                  auditor_names, avatar_names)
        else:
            names = (_target_names("avatar", alert, by_name, orchestras, teams,
                                   auditor_names, avatar_names)
                     + _target_names(AUDITOR_ROLES[0], alert, by_name, orchestras, teams,
                                     auditor_names, avatar_names))
            if not names:
                names = list(desk)
        candidates = {name: next_text for name in dict.fromkeys(names)}
        live, more = _split_live(candidates, by_name, closed, covered,
                                 respect_pause=True)
        dead.update(more)
        if live:
            note = _escalation_note(dead, current, next_level)
            return {name: note + body for name, body in live.items()}
        if next_level == 3:
            # Un avatar externe est la route humaine du dernier palier : sa
            # copie va au fil lisible, sans courrier de plus.
            for name in desk:
                if name in by_name and _external(by_name, name):
                    return {"thread:%s" % name:
                            _escalation_note(dead, current, 3) + next_text}
    # Plus aucun agent au registre ne peut lire : route humaine (fil de
    # l'avatar), en nommant les absents. Sans avatar, rien n'est inventé — le
    # passage suivant réessaiera (l'alerte reste dans `ameesh alerts`).
    fallback = _fallback_avatar(by_name, _slo_breach(alert, level=3, desk=label), desk)
    live, more = _split_live(fallback, by_name, closed, covered, respect_pause=True)
    dead.update(more)
    if live:
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    for name in desk:
        if name in by_name:
            return {"thread:%s" % name:
                    _dead_note(dead) + _slo_breach(alert, level=3,
                                                   desk=label)}
    return {}


def _mail_undeliverable_briefs(alert: dict, by_name: dict, auditor_names,
                               closed: set, dead_out: dict | None = None,
                               avatar_names=(), desk_names=()) -> dict:
    """#3036 : le courrier en souffrance part aux orchestrateurs VIVANTS de
    l'équipe ; à défaut aux auditeurs, puis au bureau, puis au fil lisible.

    Le destinataire mort (`alert["agent"]`) n'est jamais visé ; un
    orchestrateur sur un hôte perdu ne peut pas relivrer, il ne doit pas
    compter comme prévenu. `dead_out` reçoit les écartés (nom → raison) pour
    que le notificateur réessaie à la passe suivante, quand l'un revient."""
    from . import undeliverable
    text = undeliverable.brief_text(alert)
    names = [name for name in alert.get("orchestrators") or ()
             if name != alert.get("agent")]
    live, dead = _split_live({name: text for name in names}, by_name, closed)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    fallback = {name: text for name in auditor_names or () if name in by_name}
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    desk = _desk_names(alert, by_name, avatar_names, desk_names)
    fallback = _fallback_avatar(by_name, text, desk)
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if dead_out is not None:
        dead_out.update(dead)
    for name in desk:
        if name in by_name:
            return {"thread:%s" % name: _dead_note(dead) + text}
    return {}


def briefs(alert: dict, listing: list, lot_titles: dict | None = None,
           auditor_names=(), *, orchestrator_names=(),
           observation_names=(), closed_hosts=(), dead_out: dict | None = None,
           covered=(), avatar_names=(), desk_names=()) -> dict:
    """`{agent: texte}` pour une alerte de `BRIEFED_TYPES` (vide sinon).

    Orchestrateurs : `idle_capacity`, ceux de L118 ; `stale_lot`, ceux de
    l'équipe de l'assigné (sinon celle du lot, #917) — ou du projet du lot
    quand aucun relecteur n'est désigné (#705) —, ou **un seul** quand le lot
    n'a pas d'assigné
    (#1026 : sans équipe, le brief partait à tous et deux orchestrateurs
    traitaient le même lot) ; `plan_underused`, ceux des équipes des agents au
    repos ; `estimate_bias` (L157b), l'estimateur seul s'il est un agent du
    registre (#6426 : ni le pilote ni l'auditeur n'ont de geste, l'auditeur lit
    le biais à sa passe), sinon l'auditeur en repli. `module_deviation` :
    rôle explicite et projet, sans repli ; un constat de portée hôte
    (`project` absent, `dev` workspaces) n'a pas de fil de projet et part au
    responsable humain de son hôte ou agent (`notify.Router`), jamais en
    diffusion aux agents. `stale_lot` d'un lot en qa : le relecteur DÉSIGNÉ
    reçoit en outre le verdict qui lui est dû (#705).
    Auditeurs : pour tous les types sauf `stale_lot` (L713), `plan_underused`
    (#3274), `packet_ready_waiting` (#7759) et `estimate_bias` avec un
    estimateur au registre (#6426) — lus à
    la passe de l'auditeur, sans réveil ; sans aucun orchestrateur vivant,
    `plan_underused` se replie sur l'auditeur (#3036). Un agent à la fois
    orchestrateur et auditeur reçoit un seul texte.

    `observation_names` (#7661) : les agents dont la fiche déclare le rôle de
    conception, destinataires des alertes d'hôte (`host_disk_soon_full`,
    `host_unsampled`) ; vide, l'alerte retombe sur le bureau comme avant, sans
    nom d'agent deviné.

    #3036 : `closed_hosts` (les hôtes de `claims_closed_hosts`) et le champ
    `lease_live` des lignes du registre écartent les destinataires qui ne
    liront pas ; un `slo_breach` sans vivant au palier passe au palier suivant
    puis au fil du bureau, `mail_undeliverable`/`idle`/`underused` aux
    auditeurs puis au bureau. Le brief nomme les absents ("No live recipient:
    …") pour que les vivants couvrent le geste. `dead_out`, quand l'appelant
    le fournit, reçoit ces absents (nom → raison) : le notificateur garde la
    liste pour réévaluer à la passe suivante et remettre à qui est revenu.
    `covered` (#3100, `slo_breach`) : les destinataires déjà remis, comptés
    vivants pour le palier même si leur bail cligne à cette passe."""
    kind = alert.get("type")
    if kind not in BRIEFED_TYPES:
        return {}
    by_name = {row["name"]: row for row in listing}
    orchestras = list(alert.get("orchestrators") or ())
    closed = set(closed_hosts or ())
    desks = _desk_names(alert, by_name, avatar_names, desk_names)
    if kind in ("host_disk_soon_full", "host_unsampled"):
        # #5563 : opérateurs du mesh, sans diffusion aux pilotes produit.
        # Le bureau externe lit le fil, pas sa boîte. #7661 : l'opérateur est
        # l'agent dont la fiche déclare le rôle de conception, jamais un nom.
        text = alert["detail"]
        if kind == "host_unsampled":
            text += (" Check the host sampler: ameesh-releve-hote.timer (system) "
                     "or ameesh-host-publish.timer (user, publish@); verify that "
                     "a fresh reading appears in ameesh hosts.")
        out = {name: text for name in observation_names if name in by_name}
        out.update(_fallback_avatar(by_name, text, desks))
        live, dead = _split_live(out, by_name, closed)
        if dead_out is not None:
            dead_out.update(dead)
        if not live:
            for name in desks:
                if name in by_name:
                    live = {"thread:%s" % name: text}
                    break
        return {name: _dead_note(dead) + body for name, body in live.items()}
    if kind == "module_deviation":
        text = alert.get("action") or alert.get("detail") or "Module procedure deviation"
        project = alert.get("project")
        if not project:
            # Host-scoped finding: no project owns it, so no project thread
            # and no agent brief. `notify.Router` sends it to the responsible
            # human of its host or agent; never broadcast it to every agent.
            return {}
        out = {o: text for o in orchestras
               if o in orchestrator_names and o in by_name
               and _workstream(by_name[o]) == project}
        for name in auditor_names or ():
            if name in by_name and _workstream(by_name[name]) in ("", project):
                out.setdefault(name, text)
        live, dead = _split_live(out, by_name, closed)
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    elif kind == "mail_undeliverable":
        return _mail_undeliverable_briefs(alert, by_name, auditor_names, closed,
                                          dead_out=dead_out, desk_names=desks)
    elif kind == "follow_up_unfollowed":
        # #8196 : l'assigné seul, ni pilote ni auditeur ; un assigné externe
        # (session de conversation) lit le fil du lot, pas sa boîte. Sans
        # lecteur, rien : l'humain prend le relais (`human_due`).
        name = alert.get("agent")
        if name not in by_name:
            return {}
        text = _follow_up_unfollowed(alert)
        if _external(by_name, name):
            return {"thread:%s" % name: text}
        live, dead = _split_live({name: text}, by_name, closed)
        if dead_out is not None:
            dead_out.update(dead)
        return live
    elif kind == "idle_actionable_lot":
        out = {}
        audit_text = _idle_actionable_lot(alert)
    elif kind == "idle_capacity":
        out = sous_utilisation.orchestrator_briefs(alert, listing, lot_titles)
        audit_text = _for_auditor(alert)
    elif kind == "slo_breach":
        # #789 t2 (canon §9) : le niveau atteint règle les destinataires
        # (`targets` du moteur SLO) ; l'auditeur n'est briefé qu'au niveau 3,
        # sinon le niveau 1 doublerait `stale_lot` pour du bruit.
        # #3036 : le rôle ne suffit pas — un agent sans bail vivant ou sur un
        # hôte aux claims fermés ne lira pas ; le palier suivant prend le
        # relais, et le brief dit qui manquait.
        teams = _projects(by_name, alert.get("agent"))
        if (alert.get("waiting") or {}).get("no_train"):
            # #8159 : sans train déclaré, le pilote visé est celui du projet du
            # lot (il compose à la main, §12), pas celui des équipes du porteur.
            teams = {str(alert["waiting"]["no_train"])}
        if (alert.get("waiting") or {}).get("packet_vehicle"):
            # #8172 : un véhicule de paquet va au pilote de son projet.
            teams = {str(alert["waiting"]["packet_vehicle"])}
        # #3036 : le pilote se résout ici aussi quand l'alerte ne porte pas
        # `orchestrators` (niveau 1 d'une passe sans autre lot de niveau 2) ;
        # le notificateur fournit les rôles vivants du canon.
        return _slo_briefs(alert, by_name, orchestras or orchestrator_names,
                           teams, auditor_names, closed, covered,
                           avatar_names=desks)
    else:
        extra: dict[str, str] = {}
        no_reviewer = False
        if kind in ("staging_interrupted", "staging_canon_stale",
                    "staging_executor_stale"):
            text = alert["detail"]
            teams = set()
        elif kind == "stale_lot":
            waiting = alert.get("waiting") or {}
            reviewer = waiting.get("who") if waiting.get("what") == "verdict" else None
            # #705 : en qa, l'attente est celle du relecteur DÉSIGNÉ ; sans
            # désignation, elle n'est à personne et l'orchestrateur du projet
            # doit en désigner un.
            no_reviewer = bool(waiting.get("what") == "verdict" and not reviewer)
            text = _reviewer_missing(alert) if no_reviewer else _stale_lot(alert)
            teams = _projects(by_name, alert.get("agent"))
            if not teams:
                # #917 : sans assigné connu, l'équipe du lot — sinon un lot
                # ameesh réveillerait les orchestrateurs nexlink, et
                # inversement (une alerte reçue pousse à assigner, ce qui
                # entretient la liste des orchestrateurs par historique).
                teams = {(alert.get("lot_team") or "").strip()} - {""}
            # #2076 : plusieurs relecteurs désignés, chacun briefé pour son
            # verdict ; #917 garde l'équipe du lot quand l'assigné est inconnu.
            for name in str(reviewer or "").split(","):
                name = name.strip()
                if name in by_name:
                    extra[name] = _verdict_due(alert)
        elif kind == "estimate_bias":
            text = _estimate_bias(alert)
            # #6426 : l'estimateur est le seul acteur du biais (il part de la
            # table de référence et justifie ses écarts) ; le pilote et
            # l'auditeur recevaient le même courrier sans geste — le pilote l'a
            # écrit (mesh-design, 09/11 01:03 : « no action on these automated
            # estimate-bias alerts »), et l'auditeur lit les biais à sa passe
            # (`ameesh alerts --json`, docs/AUDITEUR.md §11). Un estimateur
            # hors registre garde le repli auditeur/bureau ci-dessous (#3502).
            orchestras = []
            teams = set()
        elif kind == "waiting_without_question":
            text = _waiting_without_question(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "review_family_paused":
            text = alert["detail"]
            teams = {row["project"] for row in alert["held_lots"] if row.get("project")}
            extra.update(_fallback_avatar(by_name, text, desks))
        elif kind == "critical_budget_pause":
            text = _critical_budget_pause(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "account_wave_coordination":
            text = _account_wave_coordination(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "account_config_invalid":
            text = _account_config_invalid(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "budget_pause_long":
            text = _budget_pause_long(alert)
            # #8266 : le groupe peut couvrir plusieurs projets ; ses agents
            # sont tous nommés. Une alerte par agent garde son équipe.
            members = alert.get("agents") or (alert.get("agent"),)
            teams = {p for name in members for p in _projects(by_name, name)}
        elif kind == "rate_limited_chronic":
            text = _rate_limited_chronic(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind in ("operator_pause_long", "operator_pause_host"):
            text = _operator_pause(alert)
            teams = ({p for n in alert.get("agents") or () for p in _projects(by_name, n)}
                     if kind == "operator_pause_host" else _projects(by_name, alert.get("agent")))
        elif kind in ("host_idle_reserved", "baseline_oversized",
                     "host_release_failed", "host_release_overdue",
                     "host_release_no_runs", "host_release_canon_unreadable",
                     "host_release_adapter_stalled",
                     "host_release_standby_exit_failed"):
            text = alert["detail"]
            teams = set()
        elif kind == "fingerprints_source_failing":
            text = alert["detail"]
            teams = {alert["project"]} if alert.get("project") else set()
        elif kind in ("train_paused", "train_stuck"):
            text = alert["detail"]
            teams = {alert["project"]} if alert.get("project") else set()
        elif kind == "release_project_undeclared":
            text = alert["detail"]
            teams = {alert["project"]} if alert.get("project") else set()
        elif kind == "packet_ready_waiting":
            text = alert["detail"]
            teams = {alert["project"]} if alert.get("project") else set()
        elif kind == "notify_undeliverable":
            # #3007 : aucun notificateur vivant ne couvre l'humain ; tous les
            # orchestrateurs de l'alerte et l'auditeur la reçoivent.
            text = _notify_undeliverable(alert)
            teams = set()
        else:
            text = _plan_underused(alert)
            teams = {p for n in alert.get("idle_agents") or ()
                     for p in _projects(by_name, n)}
        if no_reviewer:
            out = {o: text for o in _of_project(orchestras, by_name,
                                                alert.get("project"), teams)}
        else:
            out = {o: text for o in _of_teams(orchestras, by_name, teams)}
        if kind == "stale_lot" and not alert.get("agent"):
            # #1026 : un lot sans assigné n'a pas d'équipe connue, donc `teams`
            # est vide et `_of_teams` rend tous les orchestrateurs. Deux d'entre
            # eux ont traité le même brief en 60 s (course #894/#895) : un seul
            # destinataire.
            # B1 bureau (15/10) : le vivier est d'abord restreint au PROJET du
            # lot (#705 `_of_project` ; l'alerte porte `project`), sans quoi un
            # lot ameesh peut n'être briefé qu'à un orchestrateur d'un autre
            # projet, qui ne peut ni l'assigner ni le suivre — avant #1026 le
            # brief partait à tous, donc l'orchestrateur du projet le recevait.
            pool = _of_project(list(out), by_name, alert.get("project"), teams)
            chosen = _one_orchestrator(pool, by_name, alert.get("lot"))
            out = {name: out[name] for name in chosen}
        if kind == "packet_ready_waiting":
            # #7759 : un seul pilote du projet, stable d'une passe à l'autre
            pool = _of_project(list(out), by_name, alert.get("project"), teams)
            chosen = _one_orchestrator(pool, by_name, 0)
            out = {name: out[name] for name in chosen}
        if kind in ("estimate_bias", "waiting_without_question") \
                and alert.get("agent") in by_name:
            out.setdefault(alert["agent"], text)       # l'estimateur, l'assigné lui-même
        out.update(extra)
        audit_text = text
    # L713 : le lot stagnant ne réveille plus l'auditeur par courrier ; il le
    # lit à sa passe (`ameesh alerts --json`). Les orchestrateurs gardent
    # l'avis, et un auditeur qui est aussi orchestrateur de l'équipe le reçoit
    # par cette voie (un seul texte). #3274 : `plan_underused` aussi est lu à
    # la passe de l'auditeur — donner du travail aux agents au repos est le
    # geste des orchestrateurs, pas le sien ; s'il ne reste aucun orchestrateur
    # vivant, l'auditeur prend le relais plus bas (#3036). #7759 : les
    # candidats prêts laissés dehors sont le geste du composeur/pilote.
    if kind not in ("stale_lot", "plan_underused", "packet_ready_waiting"):
        for name in auditor_names or ():
            if kind == "estimate_bias" and alert.get("agent") in by_name:
                # #6426 : l'estimateur est briefé ; l'auditeur lit le biais à
                # sa passe (`ameesh alerts --json`, docs/AUDITEUR.md §11), sans
                # second courrier ni réveil. Un estimateur hors registre garde
                # le repli auditeur plus bas (#3502).
                continue
            out.setdefault(name, audit_text)
    if kind in ("critical_budget_pause", "budget_pause_long", "rate_limited_chronic",
                "account_config_invalid", "operator_pause_long",
                "account_wave_coordination"):
        # #8266 : tous les agents du groupe sont en pause, aucun ne lirait.
        for name in (alert.get("agents") or ()) if kind == "budget_pause_long" else ():
            out.pop(name, None)
        out.pop(alert.get("agent"), None)       # en pause : il ne le lirait pas
    if kind == "operator_pause_host":
        for name in alert.get("agents") or ():
            out.pop(name, None)
    # #3036 : un destinataire sans bail vivant (ou sur un hôte aux claims
    # fermés) ne lira pas ; il n'est pas compté prévenu. Le brief dit qui
    # manquait aux destinataires restants. Si plus aucun agent ne lit, la
    # route humaine est le bureau (boîte s'il est un agent normal, fil lisible
    # s'il est `externe`) ; `stale_lot` excepté, qui ne réveille pas
    # l'auditeur (L713) et attend le retour d'un orchestrateur.
    live, dead = _split_live(out, by_name, closed)
    if kind == "plan_underused" and not live:
        # #3274 : plus aucun orchestrateur vivant — l'auditeur prend le relais
        # (#3036), puis le bureau ou le fil par le repli commun. Avec un
        # orchestrateur vivant, le forfait sous-utilisé attend la passe de
        # l'auditeur, qui le lit sans être réveillé.
        relay = {name: audit_text for name in auditor_names or ()}
        live, more = _split_live(relay, by_name, closed)
        dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if kind == "stale_lot":
        return {}
    fallback = _fallback_avatar(by_name, audit_text, desks)
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if dead_out is not None:
        dead_out.update(dead)
    for name in desks:
        if name in by_name:
            return {"thread:%s" % name: _dead_note(dead) + audit_text}
    return {}


def reminder(text: str, raised_ts: float, now: float) -> str:
    """Le texte de la relance : le même, précédé de l'âge de l'alerte."""
    return "Reminder, alert still active for %s: %s" % (
        sous_utilisation._duration(max(0.0, now - raised_ts)), text)
