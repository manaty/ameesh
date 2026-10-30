# SPDX-License-Identifier: AGPL-3.0-only
"""`agent_registry` : présence des agents et baux (R3, R5).

Un agent est épinglé à un hôte (v1 : les sessions des harnais sont des fichiers
locaux). Le bail est la seule autorisation de tourner :

* `claim()` est atomique : un `UPDATE ... WHERE` sous READ COMMITTED, donc deux
  exécuteurs concurrents ne peuvent pas gagner le même agent ;
* `renew()` et `end_turn()` sont *fencés* par `lease_epoch` : un exécuteur dont
  le bail a expiré ne peut ni le prolonger, ni écrire un résultat ;
* un bail expiré est reprenable par n'importe quel autre exécuteur de l'hôte
  (`lease_expires_at < now()`), donc la mort d'un exécuteur ne bloque rien.

Le SQL est dans le stockage (`storage.of(db).agents` / `.leases`, spec §10) ;
ce module garde les règles et les signatures publiques.
"""
from __future__ import annotations

import sys

from typing import Sequence

from . import persona_sessions, storage
from .config import NAME_RE
from .db import Db
from .storage.postgres import registry as _pg

#: profondeur maximale d'une lignée d'éphémères suivie (au-delà : gouverné)
_LINEAGE_MAX = _pg.LINEAGE_MAX

#: colonnes rendues pour un agent (pilote Postgres ; alias de compatibilité)
AGENT_COLUMNS = _pg.AGENT_COLUMNS

#: condition de réclamation liée au canon (§4.1) et appartenance au canon :
#: expressions SQL du pilote Postgres, gardées ici sous leur nom historique
#: (`registry.canon_claim_predicate_sql`, cité par les migrations 0008, 0022
#: et 0024) — voir `storage.postgres.registry`
canon_governed_sql = _pg.canon_governed_sql
canon_claim_predicate_sql = _pg.canon_claim_predicate_sql

#: modes d'agent (L37, décision 0030) : `execute` = mené par un exécuteur,
#: sous bail ; `externe` = session humaine, boîte seulement, non réveillable
MODES = ("execute", "externe")
#: raisons d'arrêt structurées (`stop_reason`, L37) ; NULL tant que l'agent tourne
STOP_REASONS = ("manuel", "bail_expire", "retire_du_canon", "externe", "erreur")


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("invalid agent name: %r" % (name,))
    return name


def _responsible_required(db: Db) -> bool:
    cfg = getattr(db, "cfg", None)
    return bool(cfg is not None and getattr(cfg, "responsible_required", False))


def upsert(
    db: Db,
    name: str,
    *,
    chantier: str | None = None,
    harness: str | None = None,
    host: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    status_text: str | None = None,
    model: str | None = None,
    budget_usd: float | None = None,
    mode: str | None = None,
) -> dict:
    """Crée l'agent ou met à jour ce qui est fourni (les autres champs restent).

    `mode` (L37, 0030) ne s'applique qu'à la CRÉATION : un hook sans bail crée
    un agent `externe`, mais n'écrase jamais le mode d'un agent existant
    (`set_mode` / `ameesh set <agent> mode=…` pour le changer)."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.upsert")
    _check_name(name)
    if mode is not None and mode not in MODES:
        raise ValueError("unknown agent mode: %r (%s)" % (mode, " | ".join(MODES)))
    return storage.of(db).agents.upsert(
        name, chantier=chantier, harness=harness, host=host, cwd=cwd,
        session_id=session_id, status=status, status_text=status_text, model=model,
        budget_usd=budget_usd, mode=mode)


def upsert_unleased(db: Db, name: str, *, chantier: str | None = None,
                    harness: str | None = None, host: str | None = None,
                    cwd: str | None = None, session_id: str | None = None) -> dict:
    """L46 : inscription par un hook SANS bail (session externe, shell qui a
    hérité de AGENT_MAIL_NAME).

    * agent inconnu : il naît `externe` (L37, 0030) avec ces champs ;
    * agent `execute` existant : SEUL `last_seen` avance — jamais l'hôte, le
      harnais, la session, le dossier ni le chantier (une session étrangère ne
      doit pas se faire reprendre par l'exécuteur, ni le déplacer d'hôte) ;
    * agent `externe` existant : hôte, harnais et chantier suivent la session
      humaine ; session et dossier ne sont écrits que s'il n'y en avait pas
      encore (L36).

    Une seule instruction : pas de course entre la lecture du mode et
    l'écriture."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.upsert_unleased")
    _check_name(name)
    return storage.of(db).agents.upsert_unleased(
        name, chantier=chantier, harness=harness, host=host, cwd=cwd,
        session_id=session_id)


def get(db: Db, name: str) -> dict | None:
    return storage.of(db).agents.get(name)


def live_harness(row: dict | None) -> str:
    """#1037 : le harnais de la session VIVANTE d'un agent.

    `session_harness` (0384) est le harnais d'ouverture de la session active,
    tenu égal à `session_origin` par déclencheur ; `harness`, lui, est
    DÉCLARÉ (canon, `agent-runner register`, `adopt`) et ne dit pas sous
    lequel la session tourne (L242b : harnais emprunté). Sans session ouverte
    — origine inconnue — le harnais déclaré reste le seul connu.

    C'est le harnais de la session courante. La porte §2 « autre famille de
    modèle » ne l'utilise plus telle quelle : #1053/#1777 relit les tours du
    gel (`review_lot._author_at_freeze`), qui seuls prouvent une famille
    historique ; ce lecteur reste l'API du registre pour l'état courant."""
    row = row or {}
    return str(row.get("session_harness") or row.get("harness") or "")


def presence(row: dict | None) -> tuple[float, str]:
    """Dernier signe de vie d'un agent, et sa source (#3337).

    `seen` : inscription au registre (hook, exécuteur, statut), lue dans
    `last_seen`. `wrote` : un message déposé par l'identité, lu dans
    `last_mail_ts` (le plus récent gagne). Une session `externe` écrit sans
    exécuteur, donc sans que `last_seen` avance : sans cette seconde source,
    `mail list` la présentait comme absente alors qu'elle venait d'écrire.

    Un nom d'expéditeur est déclaré, pas prouvé (un message non signé ne
    porte aucune autorité) : c'est un signe de vie, jamais une preuve."""
    row = row or {}
    seen = float(row.get("last_seen_ts") or row.get("last_seen") or 0.0)
    wrote = float(row.get("last_mail_ts") or row.get("last_mail") or 0.0)
    if wrote > seen:
        return wrote, "wrote"
    return seen, "seen"


def overview(db: Db) -> list[dict]:
    """Vue d'observabilité (`mesh list --json`).

    Colonnes du canon : `canon_governed` (l'agent relève du canon),
    `canon_status` / `canon_diagnostic` / `canon_checked_ts` (dernier état du
    canon de son hôte, NULL si aucun), `canon_claim_ok` (la condition de
    réclamation liée au canon est remplie).
    """
    return storage.of(db).agents.overview()


def runner_seen(db: Db, host: str, owner: str, names: Sequence[str] | None) -> None:
    """Preuve de vie du runner, indépendante du bail (#508)."""
    storage.of(db).agents.runner_seen(host, owner, names)


def claim(db: Db, name: str, owner: str, ttl_seconds: float, *,
          host: str | None = None, election_brief: str | None = None) -> dict | None:
    """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None.

    Le verrou de ligne est pris **d'abord** (`SELECT ... FOR UPDATE`), puis les
    conditions sont recontrôlées avec `clock_timestamp()` dans l'instruction qui
    écrit : un verrou attendu au-delà de l'échéance refuse la réclamation, même
    si le détenteur n'a fait qu'un `SELECT ... FOR UPDATE` sans modifier la ligne
    (EvalPlanQual ne réévalue pas un `WHERE` sans nouvelle version — grille
    codex3).

    L46 : un agent `externe` n'est jamais réclamé (ni par `claimable`, ni par
    `claim`), même avec une session et du courrier en attente.

    `claim` applique **les mêmes règles que `claimable`** — éphémère échu,
    responsable résolu quand il est requis, prédicat du canon — dans la même
    instruction : la course `claimable` → `claim` est fermée.

    L165 (EP1) : `host` est l'hôte qui réclame. Il ne sert qu'à un agent
    `releve: election` (l'hôte candidat, qui devient le détenteur) ; un agent
    `hote` n'est réclamable que sur l'hôte de sa ligne.

    L165 (EP3) : `election_brief` n'est appliqué que sur une relève CROISÉE
    (agent élu dont la ligne était chez un autre hôte). La même instruction
    oublie alors la session de l'ancien hôte, écrit le brief dans la ligne
    (résumé de la session neuve) et ouvre un premier tour court ; la ligne
    rendue porte `elected` et l'ancien détenteur (`previous_host`,
    `previous_owner`, `previous_epoch`, `previous_session`). Même hôte : brief
    ignoré, session gardée. Une relève croisée SANS brief est **refusée** (le
    CAS ne déplace pas la ligne) : sans lui, l'hôte changerait en gardant la
    session et le compte de l'ancien (le contrat médié force le brief à null).
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.claim")
    _check_name(name)
    return storage.of(db).leases.claim(
        name, owner, ttl_seconds, require_responsible=_responsible_required(db),
        host=host, election_brief=election_brief)


def lease_agent(db: Db, name: str, owner: str, epoch: int) -> tuple[bool, str, dict | None]:
    """Bail vivant et détenu par ce runner, AVEC la ligne lue au passage.

    #811 : le hook a besoin des projets de l'agent (mono/multi) pour son
    budget de remise (L61) ; la lecture du bail les ramène déjà (`state` porte
    `projects`, `team`, `chantier`), le troisième élément évite une seconde
    lecture du registre. `None` quand le backend ne les porte pas.
    """
    row = storage.of(db).leases.state(name)
    if row is None:
        return False, "agent %s not in the registry" % name, None
    if row.get("status") == "stopped":
        return False, "agent stopped", row
    if (row.get("lease_owner") or "") != owner:
        return False, "lease held by %s" % (row.get("lease_owner") or "nobody"), row
    if int(row.get("lease_epoch") or 0) != int(epoch):
        return (False, "epoch %s in the registry, %s in the session"
                % (row.get("lease_epoch"), epoch), row)
    if not row.get("live"):
        return False, "lease expired", row
    return True, "", row


def lease_matches(db: Db, name: str, owner: str, epoch: int) -> tuple[bool, str]:
    """Le bail est-il vivant et détenu par ce runner à cet epoch ?

    C'est ce qui lie une identité agent-mail à un bail (et non à un dossier) :
    une session dont le bail a expiré ou a changé de main ne peut plus
    consommer le courrier de l'agent.
    """
    ok, reason, _ = lease_agent(db, name, owner, epoch)
    return ok, reason


def renew(db: Db, name: str, owner: str, epoch: int, ttl_seconds: float) -> float | None:
    """Prolonge le bail. None si on ne le détient plus (fencing par epoch).

    Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()` dans
    l'écriture : un renouvellement qui a attendu le verrou au-delà de
    l'expiration est refusé (grille codex3). Le renouvellement ne raccourcit
    jamais l'échéance : l'écriture garde le plus tardif de l'échéance tenue et
    de la nouvelle (`GREATEST`, sous le verrou de ligne) — un renouvellement
    concurrent ne peut pas être écrasé par une lecture antérieure (#3115).
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.renew")
    return storage.of(db).leases.renew(name, owner, epoch, ttl_seconds)


def release(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Rend le bail (arrêt propre). L'epoch avance : tout ancien détenteur est caduc."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.release")
    return storage.of(db).leases.release(name, owner, epoch)


def turn_in_progress(db: Db, name: str) -> bool:
    """Un tour (ou une session attach) est-il vivant ? (verdict L8 B1)

    `current_prompt` ne suffit pas : un tour de courrier, d'événement ou de
    reprise ne le remplit pas. Le marqueur fiable est `status = 'running'` avec
    un bail vivant — `begin_turn` le pose pour tout tour, `attach` pour sa
    session, `end_turn` / `release` le retirent.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.turn_in_progress")
    return storage.of(db).leases.turn_in_progress(name)


def attach_claim(db: Db, name: str, owner: str, ttl_seconds: float) -> dict | None:
    """Prend le bail pour une session interactive (`ameesh attach`, C9).

    Contrairement à `claim`, un bail vivant mais **sans tour ni session en
    cours** peut être repris : c'est ce qui suspend la réclamation automatique
    (l'ancien exécuteur est fencé par l'epoch à son prochain renouvellement).
    Un tour vivant — courrier, événement, reprise ou attach déjà ouvert — n'est
    jamais interrompu (verdict L8 B1 : `status = 'running'` + bail vivant, et
    non `current_prompt`). Mêmes règles que `claimable` : éphémère échu,
    responsable résolu quand il est requis, prédicat du canon ; verrou pris
    d'abord et échéances recontrôlées avec `clock_timestamp()`.

    L46 : seule différence voulue avec `claimable` / `claim`, un agent
    `externe` PEUT être attaché — `attach` ouvre une session humaine
    interactive, ce qu'est précisément un agent externe. Le bail d'attach ne
    change pas son mode : rendu, l'agent reste hors de portée de l'exécuteur.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.attach_claim")
    _check_name(name)
    return storage.of(db).leases.attach_claim(
        name, owner, ttl_seconds, require_responsible=_responsible_required(db))


def mark_event_wake(db: Db, name: str) -> None:
    """Note l'instant du dernier réveil d'événements (regroupement C9).

    Cet état vit en base (et non dans l'exécuteur) : un redémarrage ne remet
    pas à zéro la fenêtre de regroupement.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.mark_event_wake")
    storage.of(db).agents.mark_event_wake(name)


def claimable(db: Db, host: str, names: Sequence[str] | None = None, *,
              require_responsible: bool | None = None) -> list[dict]:
    """Agents de cet hôte sans bail vivant (libres, expirés, ou morts).

    R14 (C3) : un éphémère échu n'est jamais réclamable ; et quand le
    responsable est requis (`AMEESH_REQUIRE_RESPONSIBLE`, vrai par défaut dès
    qu'un canon est configuré), un agent sans humain responsable résolu non
    plus. `canon sync` n'écrit `responsible` que résolu ; un éphémère
    l'hérite de son créateur.

    §4.1 (toujours, quel que soit le réglage) : un agent gouverné par le canon
    n'est réclamable que si le dernier état du canon de son hôte est `ok`
    (`canon_claim_predicate_sql`).

    L165 (EP1) : un agent `releve: election` est rendu sur chaque hôte
    candidat, avec l'hôte et le dossier du candidat qui le réclamerait ; sa
    ligne peut encore être chez un autre détenteur.
    """
    if names is not None:
        from .agent_names import resolve
        names = list(dict.fromkeys(resolve(n, db=db, operation="lease.claimable") for n in names))
    if require_responsible is None:
        cfg = getattr(db, "cfg", None)
        require_responsible = bool(cfg is not None and cfg.responsible_required)
    return storage.of(db).agents.claimable(host, names,
                                           require_responsible=require_responsible)


def reap(db: Db, host: str | None = None) -> list[dict]:
    """Marque morts les agents dont le bail a expiré en plein tour."""
    return storage.of(db).leases.reap(host)


def set_session(db: Db, name: str, session_id: str, account: str | None = None, *,
                harness: str | None = None, cwd: str | None = None) -> None:
    """Enregistre la session et, L39 (0030), le compte sous lequel elle tourne
    (`session_account` ; None : aucun compte déclaré pour le harnais) ; #408,
    le harnais et le dossier de son ouverture s'ils sont donnés."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_session")
    storage.of(db).agents.set_session(name, session_id, account, harness=harness, cwd=cwd)


def set_session_account(db: Db, name: str, account: str | None) -> bool:
    """Le compte de la session courante change sans changer de session : reprise
    portable sous un autre compte (L39). Faux sans session enregistrée."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_session_account")
    return storage.of(db).agents.set_session_account(name, account)


def clear_session(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Oublie la session (rotation, 0018), **fencé par un bail valide**.

    Renvoie False si le bail n'est plus le nôtre ou s'il a expiré : un
    remplaçant a pu réclamer l'agent et ouvrir sa propre session, qu'il ne faut
    pas effacer (verdicts codex3 L11 B3). Le verrou est pris d'abord et
    l'échéance est recontrôlée avec `clock_timestamp()` dans l'écriture, comme
    les autres opérations de bail.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.clear_session")
    return storage.of(db).leases.clear_session(name, owner, epoch)


def adopt_session_origin(db: Db, name: str, session_id: str, lot: str, owner: str,
                         epoch: int, *, harness: str | None = None,
                         cwd: str | None = None) -> bool:
    """#1900 B1 : note l'origine d'une session sans origine, **fencé**.

    Sous verrou de ligne et bail vivant (owner, epoch, échéance recontrôlée
    après l'attente), et seulement si la ligne porte encore `session_id` et
    `lot` : une session remplacée entre-temps (rotation, remplaçant, autre lot)
    n'est jamais réécrite. N'écrit que l'origine, le dossier et le harnais.
    Renvoie False si le bail n'est plus le nôtre, s'il a expiré pendant
    l'attente du verrou, ou si la session/le lot ont changé.
    """
    return storage.of(db).leases.adopt_session_origin(
        name, owner, epoch, session_id, lot, harness=harness, cwd=cwd)


def record_session_history(db: Db, name: str, session_id: str,
                           account: str | None = None) -> None:
    """Record an accepted session after runner or hook session filtering."""
    _record_history(lambda: persona_sessions.record_start(db, name, session_id, account=account))


def _record_history(action):
    """History never blocks the agent, including before its migration is installed."""
    try:
        return action()
    except Exception as exc:  # noqa: BLE001
        print("ameesh: session history could not be recorded: %s" % exc, file=sys.stderr)


def pause(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    """Met l'agent en pause sous un bail VIVANT détenu par ce worker (L31).

    Faux si le bail n'est plus le nôtre, s'il a expiré (un remplaçant a pu
    réclamer la ligne et lancer son tour), ou si un tour est en cours : une
    pause ne coupe jamais un tour (0028). Verrou pris d'abord, conditions
    recontrôlées dans l'écriture."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pause")
    return storage.of(db).leases.pause(name, owner, epoch, status_text)


def hold_note(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    """Dit pourquoi le prochain tour attend (L31b), statut inchangé, sous un
    bail vivant détenu par ce worker ; refusé hors `idle`/`queued`."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.hold_note")
    return storage.of(db).leases.hold_note(name, owner, epoch, status_text)


def release_hold(db: Db, name: str, owner: str, epoch: int, status_text: str,
                 restore: str = "") -> bool:
    """Lève l'attente ou la pause de pression (L31b) si son texte est toujours
    en place ; une pause `blocked` repasse `queued`/`idle`, le texte d'avant
    (`restore`) revient."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.release_hold")
    return storage.of(db).leases.release_hold(name, owner, epoch, status_text, restore)


def pending_spend_put(db: Db, name: str, start_index: int, turn: str | None = None,
                      model: str | None = None,
                      work_item_id: int | str | None = None,
                      turn_id: str | None = None) -> bool:
    """Pose (atomiquement) le marqueur comptable d'un tour : une seule ligne.

    `work_item_id` (#796) : le lot du tour, écrit avec le marqueur pour que la
    ligne du grand livre — y compris réparée par un autre worker — porte la
    même attribution. `turn_id` : l'identifiant du tour
    (`turn_resources.turn_id`), pour la même raison."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pending_spend_put")
    return storage.of(db).pending_spend.put(name, start_index, turn, model, work_item_id,
                                            turn_id)


def pending_spend_begin(db: Db, name: str, start_index: int, turn: str | None = None,
                        model: str | None = None,
                        work_item_id: int | str | None = None,
                        turn_id: str | None = None) -> dict | None:
    """Write and capture one marker atomically on PostgreSQL (#1469).

    The mediated backend keeps its fenced write; its operations are separate.
    `work_item_id`/`turn_id` (#796) partent avec le marqueur, comme
    `pending_spend_put`.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pending_spend_begin")
    api = storage.of(db).pending_spend
    if hasattr(api, "put_get"):
        return _pending_spend_row(api.put_get(name, start_index, turn, model,
                                              work_item_id=work_item_id,
                                              turn_id=turn_id))
    if not api.put(name, start_index, turn, model, work_item_id, turn_id):
        return None
    return pending_spend_get(db, name)


def pending_spend_set_model(db: Db, name: str, model: str | None) -> bool:
    """Remplace le modèle du marqueur par celui **annoncé par le flux** (L13 B4).

    L'annonce doit vivre en base dès qu'elle arrive : si l'écriture comptable
    échoue et qu'un **autre** worker répare, il doit facturer le modèle
    réellement utilisé, pas celui du lancement. Faux si aucun marqueur n'existe
    (rien n'a été mis à jour) : l'appelant le traite comme un échec.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pending_spend_set_model")
    return storage.of(db).pending_spend.set_model(name, model)


def pending_spend_set_resident_usage(db: Db, name: str, usage: dict | None,
                                     marker: dict | None = None) -> bool:
    """Pose (ou efface avec None) l'événement `resident.turn` du marqueur (L80a).

    L'état durable du tour incomplet : un worker de remplacement y retrouve la
    mesure et peut écrire l'événement puis la ligne du grand livre. `marker`
    porte l'identité exacte (#2166 : `created_at`, `spend_key`) et borne
    l'écriture au marqueur de ce tour, jamais celui d'un remplaçant."""
    return storage.of(db).pending_spend.set_resident_usage(
        name, usage,
        None if marker is None else marker.get("created_at"),
        None if marker is None else marker.get("spend_key"))


def pending_spend_get(db: Db, name: str) -> dict | None:
    """Le marqueur comptable en attente, ou None (aucun travail suspendu)."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pending_spend_get")
    return _pending_spend_row(storage.of(db).pending_spend.get(name))


def _pending_spend_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "agent": row["agent"],
        "start": int(row["start_index"]),
        "turn": row.get("turn") or "",
        "model": row.get("model"),
        # #2166 : l'identité brute du marqueur (texte UTC à la microseconde),
        # jamais un float8 relu ; `created_ts` reste l'epoch pour `started_at`.
        "created_at": row.get("created_at"),
        "created_ts": row.get("created_ts"),
        # #2166/#2905 : l'identité brute portée par la colonne ; elle borne
        # `clear_if`/`set_resident_usage`. None sur un marqueur d'avant 0471,
        # ou périmée après un retour arrière (§14) : `ledger_key` seul fait foi
        # pour le grand livre.
        "spend_key": row.get("spend_key"),
        # #2166 : la clé déjà écrite par ce tour — celle du marqueur, ou la
        # clé durable retrouvée dans sa ligne/réservation (marqueur d'avant
        # 0471, quelle que soit la précision de la connexion de réparation).
        # #2905 : un `ledger_key` explicitement NULL (clé indémontrable) ne
        # retombe jamais sur la colonne, qui peut être périmée ; le repli sur
        # `spend_key` ne sert qu'aux backends qui ne rendent pas `ledger_key`.
        "ledger_key": (row["ledger_key"] if "ledger_key" in row
                       else row.get("spend_key")),
        # #2166/B1 : plusieurs candidats incohérents, aucun tour identifiable :
        # la comptabilité de ce tour doit rester suspendue (fail-closed).
        "ledger_ambiguous": bool(row.get("ledger_ambiguous")),
        # #796 : le lot du tour (None si inconnu, ou marqueur d'avant 0423)
        "work_item_id": row.get("work_item_id"),
        # #796 : l'identifiant du tour, pour relier la ligne à ses ressources
        "turn_id": row.get("turn_id"),
        # L80a : mesure durable d'un tour résident non encore réglé
        "resident_usage": row.get("resident_usage"),
    }


def pending_spend_clear(db: Db, name: str, marker: dict | None = None) -> bool:
    """Efface le marqueur ; avec `marker` (#1469), seulement s'il est encore
    celui de ce tour — un worker en retard n'efface pas celui de son
    remplaçant. L'identité est exacte (#2166) : `created_at` en texte et
    `spend_key`, jamais un float8 relu à précision réduite."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.pending_spend_clear")
    api = storage.of(db).pending_spend
    if marker is None:
        return api.clear(name)
    created_at = marker.get("created_at")
    if not created_at:
        # pas d'identité exacte : on n'efface pas un marqueur qu'on ne peut pas
        # identifier (fail-closed), même si l'ancien float8 correspondait.
        return False
    spend_key = marker.get("spend_key")
    if hasattr(api, "clear_if"):
        return api.clear_if(name, created_at, spend_key)
    # backend médié : pas d'opération conditionnelle, le bail est fencé au
    # serveur ; on compare l'identité exacte rendue par `get`.
    row = api.get(name)
    if row is None or row.get("created_at") != created_at \
            or (row.get("spend_key") or None) != (spend_key or None):
        return False
    return api.clear(name)


def cwd_used(db: Db, cwd: str, exclude: str) -> bool:
    """Ce dossier de travail est-il déjà celui d'un autre agent ? (0018)"""
    from .agent_names import resolve as resolve_agent_name
    exclude = resolve_agent_name(exclude, db=db, operation="registry.cwd_used")
    return storage.of(db).agents.cwd_used(cwd, exclude)


def set_status(
    db: Db, name: str, status: str, status_text: str | None = None, error: str | None = None,
    *, stop_reason: str | None = None,
) -> None:
    """Pose le statut. `stop_reason` (L37, 0030) dit pourquoi un agent passe
    `stopped`/`dead` ; il est effacé pour tout autre statut (déclencheur de la
    migration 0030), donc dès que l'agent repart."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_status")
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        raise ValueError("unknown stop reason: %r (%s)"
                         % (stop_reason, " | ".join(STOP_REASONS)))
    storage.of(db).agents.set_status(name, status, status_text, error, stop_reason)


def set_status_text(db: Db, name: str, text: str) -> bool:
    """#2789 : le texte libre d'un agent (`agent-mail status`), jamais son
    statut — un tour en cours reste `running`. Faux si l'agent est inconnu,
    retenu ou arrêté (son texte est alors un marqueur, gardé)."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_status_text")
    return storage.of(db).agents.set_status_text(name, text)


def set_mode(db: Db, name: str, mode: str) -> bool:
    """Change le mode d'un agent (`ameesh set <agent> mode=…`, L37)."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_mode")
    if mode not in MODES:
        raise ValueError("unknown agent mode: %r (%s)" % (mode, " | ".join(MODES)))
    return storage.of(db).agents.set_mode(name, mode)


def wake_status(db: Db, cfg, name: str) -> tuple[bool, str, str]:
    """`wakeable`, plus une remarque non bloquante (L46) : `(ok, raison,
    remarque)`. La remarque dit qu'un agent réveillable n'est pas réclamable
    À L'INSTANT parce que le canon de son hôte n'est pas `ok`.

    #2200 : une pause opérateur n'est PAS réveillable — le geste humain dit
    « aucun nouveau tour » et `claim` la respecte déjà ; la disponibilité
    partagée (attribution, courrier, adoption, alertes) la nomme donc.
    #4610 : le sommeil automatique du découpage (#3256 §2.3) n'est pas ce
    geste : il est réveillable pour recevoir un lot, avec la remarque qui dit
    que l'attribution le lèvera à la passe suivante. `claim` reste fermé tant
    que la pause est là.

    #3460 : un agent retiré n'est pas réveillable non plus. Sans ce refus
    explicite, `canon_claim_ok` (qui inclut `retired_at IS NULL`) se lisait
    comme une attente temporaire de canon sync et `wakeable` répondait vrai."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.wake_status")
    row = storage.of(db).agents.wake_check(name)
    if row is None:
        return False, "%s is not in the registry (unknown agent)" % name, ""
    if row.get("retired_ts"):
        return False, ("%s is retired: restore it first (ameesh agent restore %s); "
                       "a retirement is not a temporary canon wait" % (name, name)), ""
    if (row.get("mode") or "execute") == "externe":
        return False, ("%s is an external agent (human session, cannot be woken)%s"
                       % (name, "" if row.get("responsible")
                          else " and has no human owner")), ""
    sleep_note = ""
    if row.get("operator_paused"):
        # #2200 : une pause opérateur est un geste humain explicite (« aucun
        # nouveau tour »). `claim` la respecte déjà en SQL ; la disponibilité
        # partagée doit dire la même chose, sinon un lot est confié à un agent
        # qui ne le prendra pas. `resume --operator` la lève nommément.
        reason = (row.get("operator_pause_reason") or "").strip()
        by = (row.get("operator_pause_by") or "").strip()
        # #4610 : le sommeil automatique « feuille de route épuisée » n'est pas
        # ce geste humain — il est levé par l'attribution elle-même. Un lot
        # peut donc lui être confié ; les autres contrôles (vivant,
        # responsable, placement, canon) continuent de juger l'agent. `claim`
        # reste fermé tant que la passe de découpage n'a pas levé la pause.
        from . import file_decoupage
        if not file_decoupage.is_sleep_reason(reason):
            # #3961 : le demandeur sait jusqu'à quand (code `operator_paused`)
            until = row.get("operator_pause_until_ts")
            if until:
                from .agent_pause import until_label
                until = " until %s" % until_label(until)
            return False, ("%s is under operator pause%s%s%s"
                           % (name, " by %s" % by if by else "",
                              until or " (no deadline)",
                              ": %s" % reason if reason else "")), ""
        sleep_note = ("%s is under the automatic roadmap sleep: the assignment "
                      "wakes it at the next pass" % name)
    if not row.get("alive"):
        return False, "%s is an expired ephemeral agent" % name, ""
    if cfg is None:
        cfg = getattr(db, "cfg", None)
    if cfg is not None and getattr(cfg, "responsible_required", False) \
            and not (row.get("responsible") or "").strip():
        return False, "%s has no human owner (required with a canon)" % name, ""
    if not row.get("placement_admitted"):
        if row.get("placement_ok") is True:
            # #1219 : nommer la divergence (profils évalué et courant) — le même
            # diagnostic que `ameesh list`/`canon check`, au lieu d'un texte
            # court qui cache la cause et promet un canon sync qui ne corrige
            # pas une inscription qui re-divergera.
            from .placement import recorded as _recorded  # import tardif : pas de cycle
            verdict = (_recorded(db, row.get("host")) or {}).get(name) or {}
            why = verdict.get("placement_diagnostic") or (
                "profile diverged since evaluation (next canon sync)")
        else:
            why = row.get("placement_diagnostic") or (
                "placement not evaluated" if row.get("placement_ok") is None
                else "placement refused")
        return False, ("%s is not admitted on its host %s (%s)"
                       % (name, row.get("host") or "?", why)), ""
    note = sleep_note
    if not row.get("canon_claim_ok"):
        claim_note = ("canon of host %s temporarily %s: %s will only be claimed at the "
                      "next valid sync" % (
                          row.get("host") or "?", row.get("canon_status") or "no state",
                          name))
        note = "; ".join(part for part in (note, claim_note) if part)
    return True, "", note


def wakeable(db: Db, cfg, name: str) -> tuple[bool, str]:
    """L'agent est-il réveillable par ameesh ? (règle 2 de 0030, L37)

    Rend `(vrai, "")` ou `(faux, raison)`. Réveillable : connu du registre, en
    mode `execute`, éphémère non échu, et ADMIS sur son hôte (placement admis
    pour son profil actuel, `placement_admitted_sql`). Quand le responsable
    est requis (`cfg.responsible_required`, vrai dès qu'un canon est
    configuré), il faut aussi un humain responsable. Un agent arrêté
    (`stopped`) reste réveillable au sens de cette règle : l'arrêt est un
    geste humain réversible, que l'appelant peut signaler. Un agent retiré
    (#3460) ne l'est pas : `restore` le rend au registre actif.

    L46 : l'ÉTAT du canon de l'hôte n'en fait plus partie — un canon
    momentanément invalide ne doit pas bloquer une attribution ; la
    réclamation, elle, reste fermée tant qu'il n'est pas `ok` (`claim`).
    `wake_status` rend en plus la remarque correspondante.

    #1021 : la tenue budget/rythme n'entre pas ici — elle se juge là où la
    garde de l'exécuteur la calcule (`account_turn.turn_hold`, sur la ligne du
    registre et les comptes de l'hôte) et `work.check_assignee` la refuse :
    un agent réveillable peut être en pause budget, et un lot ne doit pas lui
    être confié pour autant. `resume`, lui, reste permis (c'est le geste de
    reprise, la pause se lèvera d'elle-même).
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.wakeable")
    ok, why, _note = wake_status(db, cfg, name)
    return ok, why


def set_pending_prompt(db: Db, name: str, prompt: str | None) -> None:
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_pending_prompt")
    storage.of(db).agents.set_pending_prompt(name, prompt)


def set_marked_block(db: Db, name: str, owner: str, epoch: int, status_text: str,
                     error: str, error_prefix: str) -> str:
    """Blocage marqué fencé par le bail (L35) : "done" | "kept" | "lease"."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.set_marked_block")
    return storage.of(db).leases.set_marked_block(name, owner, epoch, status_text,
                                                  error, error_prefix)


def clear_marked_block(db: Db, name: str, owner: str, epoch: int, status_text: str,
                       error_prefix: str) -> str:
    """Levée du blocage marqué fencée par le bail (L35) : "done" | "kept" | "lease"."""
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.clear_marked_block")
    return storage.of(db).leases.clear_marked_block(name, owner, epoch, status_text,
                                                    error_prefix)


def take_pending_prompt(db: Db, name: str, owner: str, epoch: int) -> str | None:
    """Consomme la consigne en attente, atomiquement, et passe l'agent en `running`.

    Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()` dans
    l'écriture (grille codex3) : une consigne n'est pas consommée sous un bail
    expiré pendant l'attente du verrou.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.take_pending_prompt")
    return storage.of(db).leases.take_pending_prompt(name, owner, epoch)


def begin_turn(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.begin_turn")
    return storage.of(db).leases.begin_turn(name, owner, epoch, status_text)


def restore_prompt(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Remet en attente la consigne d'un tour qui n'a pas abouti.

    Si une consigne était déjà en attente, les deux sont conservées (l'ancienne
    d'abord) : un `coalesce` perderait la consigne du tour interrompu.
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.restore_prompt")
    return storage.of(db).leases.restore_prompt(name, owner, epoch)


def end_turn(
    db: Db,
    name: str,
    owner: str,
    epoch: int,
    *,
    status: str = "idle",
    status_text: str | None = None,
    error: str | None = None,
    cost_usd: float | None = None,
    turn_id: str | None = None,
) -> bool:
    """Clôt un tour *seulement si le bail est encore détenu* (fencing).

    ``turn_id`` nomme le tour local fermé ; il permet de refuser la clôture
    générique d'un tour sandbox par identité (#4881).
    """
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="registry.end_turn")
    return storage.of(db).leases.end_turn(name, owner, epoch, status=status,
                                          status_text=status_text, error=error,
                                          cost_usd=cost_usd, turn_id=turn_id)
